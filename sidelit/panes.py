"""Bringing one pane of a terminal window forward from outside, where the terminal offers a way.

The hook records what a terminal tells the shells in its panes (plugin/hooks/hook.py,
PANE_VARS). tmux, WezTerm and kitty (with remote control on) can be asked to show a pane.
Terminator cannot: its D-Bus interface lists and splits terminals but has no call that focuses
one, and the terminals Sidelit starts run without it (-u). There the window gets the keyboard
and you pick the pane yourself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

_PLAIN = re.compile(r"%?[0-9]{1,9}")


def focus_commands(pane: dict | None) -> tuple[list[list[str]], dict]:
    """(commands, extra environment) that bring this pane forward; no commands when its
    terminal offers no way (or is not installed here)."""
    pane = pane if isinstance(pane, dict) else {}
    tmux, wez, kitty = pane.get("TMUX_PANE"), pane.get("WEZTERM_PANE"), pane.get("KITTY_WINDOW_ID")
    if tmux and _PLAIN.fullmatch(tmux) and tmux.startswith("%") and shutil.which("tmux"):
        sock = (pane.get("TMUX") or "").split(",")[0]
        base = ["tmux"] + (["-S", sock] if sock.startswith("/") else [])
        return [base + ["select-window", "-t", tmux], base + ["select-pane", "-t", tmux]], {}
    if wez and _PLAIN.fullmatch(wez) and not wez.startswith("%") and shutil.which("wezterm"):
        sock = pane.get("WEZTERM_UNIX_SOCKET") or ""
        return [["wezterm", "cli", "activate-pane", "--pane-id", wez]], ({"WEZTERM_UNIX_SOCKET": sock} if sock else {})
    listen = pane.get("KITTY_LISTEN_ON") or ""
    if kitty and _PLAIN.fullmatch(kitty) and not kitty.startswith("%") and listen and shutil.which("kitty"):
        return [["kitty", "@", "--to", listen, "focus-window", "--match", f"id:{kitty}"]], {}
    return [], {}


def can_focus(pane: dict | None) -> bool:
    return bool(focus_commands(pane)[0])


def focus(pane: dict | None) -> bool:
    """Bring the pane forward, off the caller's thread; whether there was a way to."""
    commands, extra = focus_commands(pane)
    if not commands:
        return False

    def run():
        env = {**os.environ, **extra}
        for argv in commands:
            try:
                subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.SubprocessError):
                return
    threading.Thread(target=run, daemon=True).start()
    return True
