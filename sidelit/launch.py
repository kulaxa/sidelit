"""Starting things: the daemon's side of an adapter's launch spec.

  {"type": "terminal", "cwd": dir, "command": shell command or None}
  {"type": "app", "app": app id, "paths": [dirs or files]}     via its desktop entry
  {"type": "webapp", "url": url, "profile": "Profile 1" or None} a browser --app window
  {"type": "exec", "argv": [...], "cwd": dir}                   a template step

Everything started gets an environment without Claude Code's session markers: a claude
started in a terminal that inherited CLAUDE_CODE_CHILD_SESSION takes itself for a nested
child and stops saving its transcript.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE") and k not in ("AI_AGENT", "BROWSER", "SIDELIT_BACKEND")}


def _scoped(argv: list[str]) -> list[str]:
    """Run it in a systemd scope of its own, like the desktop launches apps. Started from the
    daemon's service it would otherwise belong to that service: restarting or stopping the
    daemon would kill every window it ever reopened."""
    if not shutil.which("systemd-run") or not os.path.isdir(f"/run/user/{os.getuid()}/systemd"):
        return argv
    name = re.sub(r"[^A-Za-z0-9_.]", "_", os.path.basename(argv[0]))[:40] or "app"
    unit = f"app-sidelit-{name}-{secrets.token_hex(4)}.scope"
    return ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--slice=app.slice", f"--unit={unit}", "--",
            *argv]


def spawn(argv: list[str], cwd: str | None = None) -> int | None:
    if not argv:
        return None
    where = cwd if cwd and os.path.isdir(cwd) else str(Path.home())
    try:
        # (systemd-run --scope execs the command: the pid is the app's)
        proc = subprocess.Popen(_scoped(argv), cwd=where, env=clean_env(), start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"launch: {argv[0]}: {e}", file=sys.stderr, flush=True)
        return None
    _running[proc.pid] = (proc, os.path.basename(argv[0]))
    return proc.pid


# pid -> (the process, its executable's name): what we started and has not ended yet
_running: dict[int, tuple] = {}


def reap() -> list[tuple[int, str, int]]:
    """(pid, executable, exit code) of everything started here that ended since the last call;
    the code is negative for a signal. Unreaped, each would stay behind as a zombie."""
    out = []
    for pid, (proc, exe) in list(_running.items()):
        code = proc.poll()
        if code is not None:
            del _running[pid]
            out.append((pid, exe, code))
    return out


def terminal_argv(cfg: dict, cwd: str, command: str | None) -> list[str]:
    template = cfg["terminal_cmd"] if command else cfg["terminal"]
    return [part.format(cwd=cwd, command=command or "") for part in shlex.split(template)]


def desktop_app(app_id: str):
    """Gio.DesktopAppInfo for a Wayland app id: `<id>.desktop`, else by StartupWMClass or
    executable name."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    if not app_id:
        return None
    for cand in (f"{app_id}.desktop", f"{app_id.lower()}.desktop"):
        try:
            info = Gio.DesktopAppInfo.new(cand)
        except Exception:
            info = None
        if info is not None:
            return info
    low = app_id.lower()
    for info in Gio.AppInfo.get_all():
        try:
            wm = (info.get_startup_wm_class() or "").lower()
            exe = os.path.basename(info.get_executable() or "").lower()
        except Exception:
            continue
        if low in (wm, exe) or low.rsplit(".", 1)[-1] == exe:
            return info
    return None


def launch_app(app_id: str, paths: list[str] | None = None) -> int | None:
    """Start an app from its desktop entry. Returns the pid, or 0 when it started but the
    pid is not known (D-Bus activated apps), or None when it could not be started."""
    info = desktop_app(app_id)
    if info is None:
        print(f"launch: no desktop entry for {app_id!r}", file=sys.stderr, flush=True)
        return None
    exe = info.get_executable()
    if exe and not info.get_boolean("DBusActivatable"):
        # run the Exec line ourselves so we know the pid and control the environment
        argv = _exec_argv(info, paths or [])
        if argv:
            return spawn(argv, paths[0] if paths and os.path.isdir(paths[0]) else None)
    try:
        uris = [Path(p).as_uri() for p in (paths or []) if os.path.exists(p)]
        ok = info.launch_uris(uris, None) if uris else info.launch([], None)
        return 0 if ok else None
    except Exception as e:
        print(f"launch: {app_id!r} failed: {e}", file=sys.stderr, flush=True)
        return None


def _exec_argv(info, paths: list[str]) -> list[str]:
    """The desktop entry's Exec line with its field codes filled in."""
    line = info.get_string("Exec") or ""
    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    out = []
    for part in parts:
        if part in ("%f", "%u"):
            out += [paths[0] if part == "%f" else Path(paths[0]).as_uri()] if paths else []
        elif part in ("%F", "%U"):
            out += [p if part == "%F" else Path(p).as_uri() for p in paths]
        elif part == "%i":
            icon = info.get_string("Icon")
            out += ["--icon", icon] if icon else []
        elif part == "%c":
            out.append(info.get_name() or "")
        elif part in ("%k", "@@", "@@u", "@@f"):  # (@@…: flatpak's own markers around the file args)
            continue
        else:
            out.append(part.replace("%%", "%"))
    return out


def webapp_argv(url: str, profile: str | None = None) -> list[str]:
    from sidelit.openurl import default_browser
    argv = shlex.split(default_browser())
    if profile:
        argv.append(f"--profile-directory={profile}")
    return [*argv, "--new-window", f"--app={url}"]


def browser_new_window() -> str:
    """`{browser}` in template exec lines: the default browser, opening a new window."""
    from sidelit.openurl import default_browser
    return shlex.join([*shlex.split(default_browser()), "--new-window"])


def run(spec: dict, cfg: dict) -> int | None:
    kind = spec.get("type")
    if kind == "terminal":
        cwd = spec.get("cwd") or str(Path.home())
        return spawn(terminal_argv(cfg, cwd, spec.get("command")), cwd)
    if kind == "app":
        return launch_app(spec.get("app") or "", spec.get("paths") or [])
    if kind == "webapp":
        return spawn(webapp_argv(spec["url"], spec.get("profile")))
    if kind == "exec":
        return spawn(list(spec.get("argv") or []), spec.get("cwd"))
    print(f"launch: unknown spec {spec!r}", file=sys.stderr, flush=True)
    return None
