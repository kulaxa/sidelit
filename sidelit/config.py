"""~/.config/sidelit/config.json and where everything else lives."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# SIDELIT_HOME puts config, state and the socket in one directory of its own:
# a second, test instance next to the real one (with SIDELIT_DRY_RUN=1 it only
# says what it would do)
_HOME = os.environ.get("SIDELIT_HOME")
if _HOME:
    CONFIG_DIR = STATE_DIR = RUNTIME_DIR = Path(_HOME)
else:
    # (set but empty counts as unset: a relative "sidelit" would be the package's own folder)
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sidelit"
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sidelit"
    RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "sidelit"
DRY_RUN = os.environ.get("SIDELIT_DRY_RUN") == "1"
CONFIG = CONFIG_DIR / "config.json"
LAYOUT = CONFIG_DIR / "layout.json"
REGISTRY = CONFIG_DIR / "registry.json"          # 0.3.x; migrated into layout.json once
JOURNAL = STATE_DIR / "journal.jsonl"
SOCKET = RUNTIME_DIR / "daemon.sock"
if _HOME:  # (a unix socket path has to stay under 108 bytes)
    import hashlib
    SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"sidelit-{hashlib.sha1(_HOME.encode()).hexdigest()[:8]}.sock"
RESTORED_MARKER = RUNTIME_DIR / "restored"       # which compositor had its windows brought back (session.py)
HIDDEN_MARKER = RUNTIME_DIR / "hidden"           # the sidebar is hidden (tray.py): a restart keeps it so, a login not

# The terminals Sidelit knows how to start, best first: config.json's "terminal" and
# "terminal_cmd" default to the first one installed (Terminator's when none is). Each is started
# in the workspace's directory and, where it takes one, given it as an option too. ({command} is
# one shell command line: a terminal that runs a program rather than a line gets it through sh.)
#   executable     the directory                      what to run there
TERMINALS = [
    ("terminator", "-u --working-directory={cwd}", "-e {command}"),
    ("konsole", "--workdir {cwd}", "-e {command}"),
    ("kgx", "--working-directory={cwd}", "-e {command}"),
    ("kitty", "--directory={cwd}", "sh -c {command}"),             # Hyprland's own
    ("foot", "--working-directory={cwd}", "sh -c {command}"),      # sway's
    ("alacritty", "--working-directory {cwd}", "-e sh -c {command}"),  # niri's
    ("gnome-terminal", "--working-directory={cwd}", "-- sh -c {command}"),  # Linux Mint's (Cinnamon's)
    ("xfce4-terminal", "--working-directory={cwd}", "-x sh -c {command}"),  # Xfce's
    ("mate-terminal", "--working-directory={cwd}", "-x sh -c {command}"),   # MATE's
    ("xterm", "", "-e {command}"),
]


def _default_terminal() -> tuple[str, str]:
    """("terminal", "terminal_cmd") for the first of TERMINALS that is installed."""
    exe, where, run = next((t for t in TERMINALS if shutil.which(t[0])), TERMINALS[0])
    plain = f"{exe} {where}".strip()
    return plain, f"{plain} {run}"


DEFAULT_TERMINAL = _default_terminal()

# What '+' offers for a new workspace: a name, the folder it opens in ("cwd", else your home),
# and what to start there, in order. The tab follows the first coding agent's step. A card's
# "Save as template…" writes one from what that workspace has open.
#   {"type": "claude"}                   a terminal with `claude` in it
#   {"type": "codex"}                    a terminal with `codex` in it
#   {"type": "opencode"}                 a terminal with `opencode` in it
#   {"type": "terminal"}                 a terminal; with "command": …, running that
#   {"type": "browser", "urls": […]}     a browser window of its own, with those tabs
#   {"type": "webapp", "url": …}         a web app window (no tabs)
#   {"type": "app", "app": …}            an app, by its desktop entry's id
#   {"type": "app", "exec": …}           any other program, as a command line
# An "exec" line may use {cwd}, the workspace's directory, and {browser}, the default browser
# set to open a window of its own.
DEFAULT_TEMPLATES = [
    {"name": "Claude Code", "steps": [{"type": "claude"}]},
    {"name": "Empty terminal", "steps": [{"type": "terminal"}]},
]

DEFAULT_CONFIG = {
    "width": 280,
    "terminal": DEFAULT_TERMINAL[0],      # a shell in {cwd}
    "terminal_cmd": DEFAULT_TERMINAL[1],  # {command} run in {cwd}
    "monitor": "leftmost",  # leftmost | rightmost | largest | <model substring, e.g. "Acer">
    "side": "left",         # the screen edge the sidebar docks at: left | right
    # auto: the GTK 4 sidebar when gtk4-layer-shell is installed, else the GTK 3 one; gtk3 | gtk4
    "sidebar": "auto",
    # the sidebar's look (themes.py): default | halloween | the name of a CSS file in
    # ~/.config/sidelit/themes/
    "theme": "default",
    "templates": DEFAULT_TEMPLATES,
    "default_template": "Claude Code",
    # right-click → "Open <name> here": one-click web app windows on a workspace, e.g.
    # [{"name": "Discord", "url": "https://discord.com/app"}]
    "web_apps": [],
    "restore_on_login": True,   # after a login: resume remembered sessions and bring windows back
    "restore_windows": True,    # False = only resume the sessions
    "relaunch": True,           # start again windows that went without you closing them (crashes)
    "restore_delay": 6,         # seconds after start before the restore begins
    "ignore_apps": [],          # app ids never kept in the layout
    # before a session's tab turns yellow, a model reads the end of its turn: a session that only
    # waits (CI, a deploy, a background job) stays orange, "waiting on CI pipeline". Each agent's
    # turns are read through that agent: Claude Code's by check_model, Codex's by
    # check_model_codex, OpenCode's by check_model_opencode (as provider/model; "": the model
    # your Codex or OpenCode is set to)
    "check_turns": True,
    "check_model": "claude-sonnet-5-5",
    "check_model_codex": "",
    "check_model_opencode": "",
}


def load() -> dict:
    try:
        user = json.loads(CONFIG.read_text())
    except Exception:
        user = {}
    cfg = {**DEFAULT_CONFIG, **(user if isinstance(user, dict) else {})}
    if not CONFIG.exists():
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass
    return cfg


def put(key: str, value) -> bool:
    """One setting changed in config.json, the rest left as written. False when the file is not
    JSON (half edited, say): it is not overwritten."""
    try:
        user = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(user, dict):
        return False
    user[key] = value
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(user, indent=2))
        tmp.replace(CONFIG)
    except OSError:
        return False
    return True


def templates(cfg: dict) -> list[dict]:
    """config.json's templates, or the defaults when it has none (or not a list of them)."""
    own = cfg.get("templates")
    if isinstance(own, list) and own:
        return own
    return DEFAULT_TEMPLATES


def remove_template(cfg: dict, name: str) -> bool:
    """The template of that name off config.json's list; the last one stays (with none, '+'
    would offer the defaults again)."""
    tpls = [t for t in templates(cfg) if t.get("name") != name]
    return bool(tpls) and put("templates", tpls)


def move_template(cfg: dict, name: str) -> bool:
    """The template of that name one place up the list ('+' lists them in this order)."""
    tpls = list(templates(cfg))
    at = next((i for i, t in enumerate(tpls) if t.get("name") == name), None)
    if not at:  # (not there, or first already)
        return False
    tpls[at - 1], tpls[at] = tpls[at], tpls[at - 1]
    return put("templates", tpls)


def replace_template(cfg: dict, old: str, template: dict) -> bool:
    """`template` in place of the one named `old` (else last), not under a name another has."""
    tpls = [dict(t) for t in templates(cfg)]
    at = next((i for i, t in enumerate(tpls) if t.get("name") == old), None) if old else None
    if any(t.get("name") == template["name"] for i, t in enumerate(tpls) if i != at):
        return False
    if at is None:
        tpls.append(template)
    else:
        tpls[at] = template
    return put("templates", tpls)


def clean_template(data, agent_names: set) -> dict:
    """A template as the editor sends it, checked and tidied: a name, the folder it opens in
    (under home as ~; none: your home folder), and at least one step of a kind '+'
    can start (DEFAULT_TEMPLATES says which) with its one setting. ValueError says what is
    wrong, in the editor's words."""
    if not isinstance(data, dict):
        raise ValueError("not a template")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("a template needs a name")
    tpl: dict = {"name": name}
    cwd = os.path.expanduser(str(data.get("cwd") or "").strip())
    cwd = cwd.rstrip(os.sep) or (os.sep if cwd else "")
    if cwd:
        home = str(Path.home())
        tpl["cwd"] = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + os.sep) else cwd
    steps = []
    for step in data.get("steps") if isinstance(data.get("steps"), list) else []:
        if not isinstance(step, dict):
            raise ValueError("not a step")
        kind = str(step.get("type") or "app")
        if kind in agent_names:
            steps.append({"type": kind})
        elif kind == "terminal":
            command = str(step.get("command") or "").strip()
            steps.append({"type": "terminal", **({"command": command} if command else {})})
        elif kind == "browser":
            urls = [str(u).strip() for u in (step.get("urls") or []) if str(u).strip()]
            bad = next((u for u in urls if not u.startswith(("http://", "https://"))), None)
            if bad is not None:
                raise ValueError(f"not a web address: {bad}")
            steps.append({"type": "browser", "urls": urls})
        elif kind == "webapp":
            url = str(step.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("a web app needs its address (https://…)")
            steps.append({"type": "webapp", "url": url})
        elif kind == "app":
            app, run = str(step.get("app") or "").strip(), str(step.get("exec") or "").strip()
            if app:
                steps.append({"type": "app", "app": app})
            elif run:
                steps.append({"type": "app", "exec": run})
            else:
                raise ValueError("an app step needs an app, or a command line")
        else:
            raise ValueError(f"no such kind of step: {kind}")
    if not steps:
        raise ValueError("a template opens something: add an app")
    tpl["steps"] = steps
    return tpl
