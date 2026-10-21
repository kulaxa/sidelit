"""Entry point: `python3 -m sidelit [daemon|sidebar|ws ...|open URL|<command>]`.

`daemon` runs the layout keeper and `sidebar` the GTK sidebar, both in the foreground (the
systemd user units run them; bin/sidelit starts those). `ws` exposes the
workspace client as a CLI, `open` opens a URL in the browser window on the current
workspace. Everything else is a command for the running daemon.
"""
import json
import os
import sys

USAGE = """usage: sidelit <command>

  daemon | sidebar         run one of the two processes in the foreground
  state                    what the daemon sees: tabs, windows, missing slots (JSON)
  layout                   the layout it keeps (JSON; the file is ~/.config/sidelit/layout.json)
  why [TEXT]               the journal: why windows were kept, moved, forgotten (TEXT filters)
  restore                  start what is missing from the layout now (runs by itself after a login)
  reapply                  put every window back on its workspace and screen
  reload                   re-read config.json
  show | hide | toggle     the sidebar (hidden, its strip is the windows'; the tray icon also does it)
  hooks install|uninstall|status   Sidelit's plugin in each coding agent (Claude Code, Codex, OpenCode), which
                                   reports each session's status, and in Terminator, which shows its tab or split
  chrome install|uninstall|status  the bridge to the Chrome extension (windows and their tabs)
  ws ...                   talk to the compositor directly (list, toplevels, activate N, ...)
  open [--app] URL         open a link on the workspace you are looking at
  moveover                 take over from an install under this project's old name (`start` does it once)
  version"""


def _scrub_claude_env():
    """Drop inherited Claude Code markers. Started from inside a Claude session, they
    would make every claude launched from our terminals think it is a nested child and
    stop saving transcripts. CLAUDE_CONFIG_DIR is yours, not a session's: it stays."""
    for name in [k for k in os.environ if k.startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR"]:
        del os.environ[name]
    os.environ.pop("AI_AGENT", None)


def _show(cmd: str) -> int:
    """show, hide, toggle: the running sidebar, over the session bus (tray.py). `show` also
    forgets that it was hidden, so a sidebar still starting comes up shown (`start` runs it)."""
    from sidelit import config, tray
    if cmd == "show":
        config.HIDDEN_MARKER.unlink(missing_ok=True)
    if tray.control(cmd.capitalize()):
        return 0
    if cmd != "show":
        print("sidelit: the sidebar is not running (sidelit start)", file=sys.stderr)
        return 1
    return 0


def _ctl(cmd: str, args: list[str]) -> int:
    from sidelit import ipc
    params = {}
    if cmd == "why":
        params = {"text": " ".join(args), "limit": 60}
    try:
        result = ipc.call(cmd, **params)
    except (ConnectionError, RuntimeError) as e:  # no daemon, or it said no
        hint = "; start it with: sidelit start" if isinstance(e, ConnectionError) else ""
        sys.exit(f"sidelit: {e}{hint}")
    if cmd == "why":
        print("\n".join(result) or "nothing in the journal matches")
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result is not None:
        print(result)
    return 0


def _chrome(args: list[str]) -> int:
    from sidelit import chrome
    sub = args[0] if args else "status"
    if sub == "install":
        done = chrome.install_host(chrome.host_command())
        if not done:
            print("no Chrome, Chromium, Brave or Edge profile found under ~/.config")
            return 1
        for path in done:
            print(f"registered the native host: {path}")
        print(f"now load the extension in each Chrome profile: chrome://extensions -> Developer mode -> "
              f"Load unpacked -> {chrome.extension_dir()}")
        for prof in chrome.restores_itself():
            print(f"! {prof}: turn off 'Continue where you left off' (chrome://settings/onStartup, pick "
                  f"'Open the New Tab page'), or its windows come back twice")
        return 0
    if sub == "uninstall":
        for path in chrome.uninstall_host():
            print(f"removed {path}")
        return 0
    if sub == "status":
        from sidelit import ipc
        try:
            connected = ipc.call("state").get("chrome") or []
        except ConnectionError as e:
            print(e)
            return 1
        if not connected:
            print("no Chrome profile is connected (is the extension loaded? `sidelit chrome install`)")
        for c in connected:
            print(f"{c['instance']}  {c.get('profile') or 'profile not found yet'}  {c['windows']} windows")
        for prof in chrome.restores_itself():
            print(f"! {prof} restores its own windows at startup: turn that off (chrome://settings/onStartup)")
        return 0
    print("usage: sidelit chrome install|uninstall|status", file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _scrub_claude_env()
    from sidelit.procs import extend_path
    extend_path()  # (an agent npm installed through nvm & co. is on PATH only in your shell)
    cmd = argv[0] if argv else "sidebar"
    if cmd == "ws":
        from sidelit.backend import cli as ws_cli
        return ws_cli(argv[1:])
    if cmd == "open":
        from sidelit.openurl import main as open_main
        return open_main(["open"] + argv[1:])
    if cmd == "daemon":
        from sidelit.daemon import main as daemon_main
        return daemon_main()
    if cmd in ("sidebar", "--foreground", "-F"):
        from sidelit import backend, gnome
        if backend.chosen() == "gnome" and not gnome.hosted():
            # GNOME Shell runs the sidebar itself (the extension); the unit takes 4 as "not here"
            print("sidelit sidebar: on GNOME, Sidelit's GNOME Shell extension runs the sidebar "
                  "while the daemon runs (sidelit start)", file=sys.stderr)
            return 4
        # the GTK 4 sidebar when gtk4-layer-shell is there (config "sidebar"), else GTK 3
        from sidelit import sidebar4
        if sidebar4.should_run():
            return sidebar4.main()
        from sidelit.sidebar import main as sidebar_main
        return sidebar_main()
    if cmd in ("--version", "version"):
        from sidelit import version
        print(version())
        return 0
    if cmd == "hooks":
        from sidelit.hooks import main as hooks_main
        return hooks_main(argv[1:])
    if cmd == "chrome":
        return _chrome(argv[1:])
    if cmd == "moveover":
        from sidelit.moveover import main as moveover_main
        return moveover_main(argv[1:])
    if cmd == "opencode-fork":  # (what a fork of an OpenCode session runs in its new terminal)
        from sidelit.opencode import fork_here
        return fork_here(argv[1] if len(argv) > 1 else "")
    if cmd in ("show", "hide", "toggle"):
        return _show(cmd)
    if cmd in ("state", "layout", "why", "restore", "reapply", "reload", "ping"):
        return _ctl(cmd, argv[1:])
    sys.exit(USAGE)


if __name__ == "__main__":
    sys.exit(main())
