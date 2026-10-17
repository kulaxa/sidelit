"""`sidelit open URL`: open a link on the workspace you are looking at.

Browsers put a URL into whichever of their windows was focused last. With one browser
window per workspace that is usually a window on some *other* workspace, so a link
clicked in a terminal seems to open in a random place (or nowhere visible at all).

This looks at the active workspace: if a browser window is on it, that window is
focused first so the browser adds the tab there; otherwise a new browser window is
opened, which the compositor places on the active workspace. Wire it up as
Terminator's `custom_url_handler`, as `$BROWSER` (Claude Code honours it), and as the
desktop's default browser, which is how links from desktop apps (Discord, Slack, mail)
get here too: `xdg-settings set default-web-browser sidelit-open.desktop`.
Being the default browser means xdg-settings and xdg-open now name *us*, so neither is
followed blindly below.

One click can reach us more than once (a terminal and the app in it both handling it,
xdg-open and $BROWSER both pointing here): the same URL again within DEDUPE_S is dropped.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from sidelit.moveover import OLD_BINS

# app_id as cosmic reports it (lowercased) -> executable
BROWSERS = {
    "google-chrome": "google-chrome",
    "com.google.chrome": "google-chrome",
    "google-chrome-beta": "google-chrome-beta",
    "google-chrome-unstable": "google-chrome-unstable",
    "chromium": "chromium",
    "chromium-browser": "chromium-browser",
    "org.chromium.chromium": "chromium",
    "brave-browser": "brave-browser",
    "microsoft-edge": "microsoft-edge",
    "vivaldi-stable": "vivaldi",
    "firefox": "firefox",
    "org.mozilla.firefox": "firefox",
    "firefox-esr": "firefox-esr",
    "zen": "zen-browser",
}
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "sidelit", "config.json")
FOCUS_GRACE = 0.15  # let the browser see the activation before the URL reaches it
DEDUPE_S = 2.0      # the same URL again within this is the same click
LAST_URL = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "sidelit", "last-url.json")

# Made the desktop's default browser (so links from Discord, Slack and friends come here),
# we are what xdg-settings and xdg-open name: following either would call us right back. A
# default browser still set to the old name's handler is us too, and gone: never followed.
SELF_HANDLERS = {"sidelit-open", "sidelit", *OLD_BINS}
# tried in order when the default browser is us, or is not set at all
KNOWN_BROWSERS = ("google-chrome", "chromium", "chromium-browser", "brave-browser",
                  "microsoft-edge", "vivaldi", "firefox")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _is_self(exe: str) -> bool:
    argv = shlex.split(exe or "")
    return bool(argv) and os.path.basename(argv[0]).lower().removesuffix(".desktop") in SELF_HANDLERS


def _installed_browser() -> str:
    """First browser we can find on PATH, for when nothing usable is configured."""
    return next((exe for exe in KNOWN_BROWSERS if shutil.which(exe)), "google-chrome")


def seen_just_now(url: str, now: float | None = None, path: str = LAST_URL) -> bool:
    """True if this URL was already opened within DEDUPE_S (and remember it otherwise).
    Two handlers started by one click run at the same moment: the file lock orders them."""
    now = time.time() if now is None else now
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                last = json.loads(f.read() or "{}")
            except ValueError:
                last = {}
            if last.get("url") == url and 0 <= now - float(last.get("t", 0)) < DEDUPE_S:
                return True
            f.seek(0)
            f.truncate()
            json.dump({"url": url, "t": now}, f)
            return False
    except OSError:
        return False  # never lose a click over this


def _config_browser() -> str | None:
    """`browser` in config.json: executable used when no browser window is on the workspace."""
    try:
        with open(CONFIG_PATH) as handle:
            value = json.load(handle).get("browser")
        return value or None
    except (OSError, ValueError, AttributeError):
        return None


def default_browser() -> str:
    """Executable of the desktop's default browser (config.json `browser` wins)."""
    configured = _config_browser()
    if configured and not _is_self(configured):
        return configured
    try:  # without BROWSER, or xdg-settings answers with $BROWSER, which is us
        env = {k: v for k, v in os.environ.items() if k != "BROWSER"}
        desktop = subprocess.run(["xdg-settings", "get", "default-web-browser"], env=env,
                                 capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        desktop = ""
    stem = desktop[:-len(".desktop")] if desktop.endswith(".desktop") else desktop
    if not stem or _is_self(stem):
        return _installed_browser()
    return BROWSERS.get(stem.lower(), stem)


def as_url(arg: str) -> str:
    """What the caller meant. As the desktop's url handler we are handed more than typed
    addresses: `mailto:`, `about:blank`, and local files (xdg-settings hands us text/html
    along with http), none of which want an https:// glued in front of them."""
    if os.path.exists(arg):
        return "file://" + os.path.abspath(arg)
    if SCHEME_RE.match(arg):
        return arg
    return "https://" + arg


def open_app_window(url: str, profile: str | None = None):
    """A browser *app* window (no tabs, no address bar) for `url`, in the default browser
    (config "browser" wins). This is how a web app gets a window of its own on a
    workspace; the compositor puts it on the active one.
    `profile` is a Chrome profile directory ("Profile 1") when a remembered window came
    from one; new windows use the browser's default."""
    argv = shlex.split(default_browser())
    if profile:
        argv.append(f"--profile-directory={profile}")
    _spawn([*argv, "--new-window", f"--app={url}"])


def _spawn(cmd: list[str]):
    env = {k: v for k, v in os.environ.items() if k != "BROWSER"}  # never loop back into ourselves
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _fallback(url: str, why: str):
    """Open the URL wherever the browser wants it. Not via xdg-open: when we are the
    desktop's default handler that comes straight back to us."""
    exe = default_browser()
    print(f"sidelit open: {why}; falling back to {exe}", file=sys.stderr)
    _spawn([*shlex.split(exe), url])


def _caller() -> str:
    """Who ran us: the parent process chain, for the log."""
    names, pid = [], os.getppid()
    for _ in range(4):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            break
        names.append(stat[stat.index("(") + 1: stat.rindex(")")])
        pid = int(stat[stat.rindex(")") + 2:].split()[1])
        if pid <= 1:
            break
    return " < ".join(names) or "?"


def _log(msg: str):
    path = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                        "sidelit", "open.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 512_000:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def open_url(url: str, verbose: bool = False, app: bool = False) -> int:
    dup = seen_just_now(url)
    _log(f"{'ignored (same click) ' if dup else ''}{url[:200]}  from {_caller()}")
    if dup:
        if verbose:
            print(f"already opened {url} within {DEDUPE_S:g} s: the same click, ignored")
        return 0
    if app:
        if verbose:
            print(f"app window: {default_browser()} --new-window --app={url}")
        open_app_window(url)
        return 0
    try:
        from sidelit.backend import open_client
        client = open_client(primary=False)  # a one-shot: the daemon is the primary client
    except Exception as exc:  # no compositor we can drive, protocol missing
        _fallback(url, f"no workspace info ({exc})")
        return 0

    # which window is on which workspace comes a moment after connecting (COSMIC sends it on
    # its next refresh tick, KWin once our script has run): give it up to a second
    deadline = time.time() + 1.0
    while time.time() < deadline and all(client.workspace_of(t) is None for t in client.toplevels.values()):
        client.wait(min(0.1, deadline - time.time()))

    active = client.active()
    if active is None:
        client.close()
        _fallback(url, "no active workspace")
        return 0

    here = [t for t in client.toplevels_on(active) if t.app_id.lower() in BROWSERS]
    if here:
        window = here[0]
        exe = BROWSERS[window.app_id.lower()]
        if verbose:
            print(f"workspace {active.index}: focusing {window.app_id} {window.title!r}, then {exe} {url}")
        client.focus(window)
        client.roundtrip()
        client.close()
        time.sleep(FOCUS_GRACE)
        _spawn([exe, url])
    else:
        exe = default_browser()
        if verbose:
            print(f"workspace {active.index}: no browser window here, {exe} --new-window {url}")
        client.close()
        _spawn([*shlex.split(exe), "--new-window", url])
    return 0


def main(argv) -> int:
    flags = [a for a in argv[1:] if a in ("-v", "--app")]
    args = [a for a in argv[1:] if a not in flags]
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: sidelit open [-v] [--app] URL   (--app: a browser app window, no tabs)",
              file=sys.stderr)
        return 2
    return open_url(as_url(args[0]), verbose="-v" in flags, app="--app" in flags)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
