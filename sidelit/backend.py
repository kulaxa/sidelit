"""Which desktop Sidelit drives, and `sidelit ws`, which talks to it by hand.

A backend is a workspace client: it lists the desktop's workspaces and windows and switches,
creates, pins, moves, focuses and closes them (wsclient.WorkspaceClient is the one to follow;
model.py has the shapes the daemon and the sidebar rely on). Where a desktop cannot do
something at all it is left out of ``client.caps``, and the daemon and the sidebar do without
it: the control is hidden rather than offered as a button that fails.

- ``cosmic``: COSMIC, over its Wayland protocols (wsclient.py).
- ``kde``: KDE Plasma 6, over KWin's D-Bus interface and a KWin script (kwin.py).

The session's XDG_CURRENT_DESKTOP picks one (COSMIC unless it names KDE);
``SIDELIT_BACKEND=cosmic`` or ``kde`` overrides it.
"""
from __future__ import annotations

import os
import sys
from typing import Callable

BACKENDS = ("cosmic", "kde")


def chosen(env=None) -> str:
    """The backend for this session's desktop. Raises RuntimeError for an unknown override."""
    env = os.environ if env is None else env
    override = (env.get("SIDELIT_BACKEND") or "").strip().lower()
    if override:
        if override not in BACKENDS:
            raise RuntimeError(f"unknown SIDELIT_BACKEND={override!r} (use {' or '.join(BACKENDS)})")
        return override
    desktops = {part.strip().upper() for part in (env.get("XDG_CURRENT_DESKTOP") or "").split(":")}
    return "kde" if "KDE" in desktops else "cosmic"


def open_client(on_change: Callable[[], None] | None = None, primary: bool = True):
    """A connected workspace client for this session, or RuntimeError saying why there is none.

    ``on_change`` is called whenever the desktop changes. ``primary`` is for the long-running
    daemon; a one-shot command passes False, for a backend that holds something only one
    process may (on KDE, the bus name the window reports go to).
    """
    if chosen() == "kde":
        from sidelit.kwin import KWinClient
        return KWinClient(on_change, primary=primary)
    from sidelit.wsclient import WorkspaceClient
    return WorkspaceClient(on_change, primary=primary)


WS_USAGE = """usage: sidelit ws [COMMAND]
  list                       workspaces (* you are on it, P pinned), the default
  toplevels                  every window and the workspace it is on
  activate N                 switch to workspace N (a number from 1, or a name)
  new NAME                   add a workspace
  rename N NAME
  pin N | unpin N            (COSMIC)
  move A before|after B      put workspace A next to B (COSMIC)
  movewin TITLE N [OUTPUT]   put the windows whose title has TITLE on workspace N"""

# how many arguments each command takes, at least
_ARGS = {"list": 0, "toplevels": 0, "activate": 1, "new": 1, "rename": 2, "pin": 1, "unpin": 1,
         "move": 3, "movewin": 2}


def cli(args: list[str]) -> int:
    """`sidelit ws ...`: what the backend sees of the desktop, and its actions, by hand."""
    cmd, rest = (args[0], args[1:]) if args else ("list", [])
    if len(rest) < _ARGS.get(cmd, len(rest) + 1) or (cmd == "move" and rest[1] not in ("before", "after")):
        print(WS_USAGE, file=sys.stderr)
        return 2
    try:  # (a desktop we cannot drive is an answer, not a crash: no traceback)
        client = open_client(primary=False)
    except RuntimeError as exc:
        print(f"sidelit ws: {exc}", file=sys.stderr)
        return 1
    try:
        return _ws_command(client, cmd, rest)
    except LookupError as missing:
        print(f"sidelit ws: no workspace {missing.args[0]!r}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _ws_command(c, cmd: str, rest: list[str]) -> int:
    needs = {"pin": "pin", "unpin": "pin", "move": "move"}.get(cmd)
    if needs and needs not in c.caps:
        print(f"sidelit ws {cmd}: this desktop cannot do that (it can: {', '.join(sorted(c.caps))})",
              file=sys.stderr)
        return 1

    def find(target: str):
        ws = c.resolve(target)
        if ws is None:
            raise LookupError(target)
        return ws

    if cmd == "list":
        c.wait(0.5)  # (COSMIC names its screens a moment after connecting)
        print(f"backend {type(c).__name__}, caps {','.join(sorted(c.caps))}")
        for ws in c.list():
            also = sorted(ws.ids - {ws.id})
            print(("*" if ws.active else " ") + ("P" if ws.pinned else " ")
                  + f" idx={ws.index:<2} id={ws.id!r:<40} name={ws.name!r}"
                  + (f"  also {','.join(also)}" if also else ""))
        copies = getattr(c, "copies_summary", None)
        if copies is not None:
            print("copies:", copies())
        return 0
    if cmd in ("toplevels", "movewin"):
        c.wait(1.5)  # which window is on which workspace comes a moment after connecting
    if cmd == "toplevels":
        placed = set()
        for ws in c.list():
            for tl in c.toplevels_on(ws):
                placed.add(id(tl))
                _print_window(c, str(ws.index), tl)
        for tl in c.toplevels.values():
            if not tl.closed and id(tl) not in placed:
                _print_window(c, "-", tl)  # on every workspace, or not on one yet
        return 0
    if cmd == "activate":
        c.activate(find(rest[0]))
    elif cmd == "new":
        ws = c.create(rest[0])
        print(f"created {ws.name!r}, workspace {ws.index}" if ws is not None else "not created")
    elif cmd == "rename":
        c.rename(find(rest[0]), rest[1])
    elif cmd in ("pin", "unpin"):
        c.pin(find(rest[0]), cmd == "pin")
    elif cmd == "move":
        c.move(find(rest[0]), find(rest[2]), before=rest[1] == "before")
    elif cmd == "movewin":
        ws, needle, output = find(rest[1]), rest[0].lower(), (rest[2] if len(rest) > 2 else None)
        hits = [tl for tl in c.toplevels.values() if not tl.closed and needle in (tl.title or "").lower()]
        if not hits:
            print(f"sidelit ws: no window has {rest[0]!r} in its title", file=sys.stderr)
            return 1
        for tl in hits:
            asked = c.move_toplevel(tl, ws, output)
            print(f"{tl.title!r} -> workspace {ws.index}{' on ' + output if output else ''}: "
                  + ("asked" if asked else "this desktop cannot move it"))
    c.roundtrip()  # the last request reaches the desktop before we hang up
    return 0


def _print_window(c, where: str, tl):
    screen = c.output_of(tl)
    print(f"ws {where}: app={tl.app_id!r:<20} out={screen.name if screen else '?':<9} title={tl.title!r}")
