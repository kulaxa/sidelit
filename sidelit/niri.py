"""niri workspace + window client, over niri's IPC socket ($NIRI_SOCKET).

niri takes JSON requests on its socket, a line each, and answers each with a line: `"Workspaces"`
gets `{"Ok": {"Workspaces": [...]}}`, `{"Action": {"FocusWindow": {"id": 7}}}` gets
`{"Ok": "Handled"}`, a mistake `{"Err": "..."}`. A connection that asks for `"EventStream"` gets
the whole state first and then every change, a line each (WorkspacesChanged,
WindowOpenedOrChanged, WindowFocusChanged...): the daemon watches that one (fd, pump) and keeps
the state from it, so it asks for nothing while the desktop changes.

niri's workspaces are per screen and dynamic: each screen keeps an empty workspace at the end,
and drops an empty one you leave unless it has a name. A name is what keeps one, so "pin" names
a workspace (sidelit-1, sidelit-2, ...) and unpinning takes the name off again, only a name
Sidelit gave: workspaces you named yourself, or that your config names, stay as they are.
Sidelit's id for a workspace is its name, written as Hyprland's named ones are
("name:sidelit-1"), which lasts across logins: the daemon asks for a kept workspace back under
its name (create, want), on the empty workspace at the end of the screen you are on, so niri
keeps no spare for it (spare_workspace). An unnamed workspace has no id, as an unpinned one on
COSMIC. A workspace renamed (by you, in niri) keeps its old names among its ids while niri
runs, so Sidelit still knows it. Tabs go screen by screen, left to right, each screen's in its
order; "move" reorders them, onto another screen too.

niri numbers windows from 1 again each time it starts, so a window's identifier is a token for
this run of niri and its number: a window after a login is never taken for one from before.
"""
from __future__ import annotations

import errno
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sidelit import model, procs

REQUEST_TIMEOUT_S = 2.0
SYNC_S = 1.0          # how long an action's result may take to show
PREFIX = "sidelit-"   # the names Sidelit gives
OURS = re.compile(r"sidelit-(\d+)", re.IGNORECASE)
MAX_NAMES = 4         # the names a workspace had that it is still known by (renamed outside Sidelit)


def ws_ref(name: str) -> str:
    """Sidelit's id for the workspace niri calls `name` ("" for an unnamed one)."""
    return f"name:{name}" if name else ""


def name_of(ref: str) -> str:
    return ref[5:] if ref.startswith("name:") else ""


def _bringable(ref: str) -> bool:
    """Is `ref` a name this client can give (not an id another desktop gave)?"""
    name = name_of(str(ref))
    return bool(name) and name == name.strip() and "\n" not in name


@dataclass
class Space(model.Workspace):  # a niri workspace: proxy is niri's id for it
    monitor: str = ""   # the screen it is on
    idx: int = 0        # its place on that screen, from 1 (what Mod+1… go to)
    shown: bool = False  # its screen shows it

    @property
    def pinned(self) -> bool:
        return bool(self.name)  # (a named workspace is never dropped)


@dataclass
class Window(model.Toplevel):  # identifier: niri's window id
    workspace: int | None = None  # niri's id of its workspace
    output: str = ""
    active: bool = False
    focused_at: float = 0.0

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str  # connector, e.g. "DP-1"


def socket_path(env=None) -> str:
    """niri's socket: $NIRI_SOCKET, else (a unit started before niri handed it over, or with a
    niri that has gone) the one of the niri running here, on our WAYLAND_DISPLAY first. Its
    name is niri.<wayland display>.<pid>.sock in the runtime directory."""
    env = os.environ if env is None else env
    given = (env.get("NIRI_SOCKET") or "").strip()
    if given and os.path.exists(given):
        return given
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    found = []
    try:
        entries = list(runtime.iterdir())
    except OSError:
        entries = []
    for p in entries:
        m = re.fullmatch(r"niri\.(.+)\.(\d+)\.sock", p.name)
        if not m or not procs.alive(int(m.group(2))):
            continue
        try:
            stamp = p.stat().st_mtime
        except OSError:
            continue
        found.append((m.group(1) == env.get("WAYLAND_DISPLAY"), stamp, str(p)))
    if not found:
        raise RuntimeError(f"niri's socket is not there (NIRI_SOCKET={given}); is this a niri session?" if given
                           else "no niri runs in this session (NIRI_SOCKET is not set)")
    if given:
        print(f"niri: no socket {given} any more; taking the niri running now", file=sys.stderr, flush=True)
    return max(found)[2]


def _ref(ws: Space) -> dict:
    return {"Id": ws.proxy}


class NiriClient:
    caps = frozenset(("create", "rename", "remove", "pin", "move", "focus", "close_toplevel"))
    spare_workspace = False  # the empty workspace at the end is the one a kept workspace comes back on

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True, env=None):
        """`env` is for the tests: where to find niri."""
        self.on_change = on_change
        self.primary = primary
        self.path = socket_path(env)
        m = re.fullmatch(r"niri\..+\.(\d+)\.sock", os.path.basename(self.path))
        self.token = procs.run_token(int(m.group(1)) if m else 0, self.path)  # (this run of niri)
        self.connections = 0  # event streams opened: the daemon watches the newest one
        self.workspaces: dict[int, Space] = {}  # by niri's id
        self.toplevels: dict[str, Window] = {}  # by identifier (the token and niri's window id)
        self._had: dict[int, list[str]] = {}    # niri's id -> the names it had while niri runs, the last one now
        self._letting_go: set[int] = set()      # workspaces whose name Sidelit is taking off
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()      # (the daemon sets it: every id its layout knows)
        self._ws: dict[int, dict] = {}    # niri's workspaces, as it said them
        self._win: dict[int, dict] = {}   # niri's windows, as it said them
        self._order: list[str] = []       # the screens, left to right
        self._events: socket.socket | None = None
        self._buf = b""
        if self._ask("Version") is None:
            raise RuntimeError(f"niri does not answer on {self.path}")
        if primary:
            self._listen()
        if not self.refresh():
            self.close()
            raise RuntimeError("niri did not say what is on its screens")

    # ---- the socket -------------------------------------------------------------------------
    def _request(self, request) -> dict | None:
        """niri's reply to one request; None when it could not be asked."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        s.settimeout(REQUEST_TIMEOUT_S)
        try:
            s.connect(self.path)
            s.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                data = s.recv(65536)
                if not data:
                    break
                buf += data
            reply = json.loads(buf.split(b"\n", 1)[0].decode(errors="replace"))
            return reply if isinstance(reply, dict) else None
        except (OSError, ValueError) as exc:
            print(f"niri: {json.dumps(request)[:60]}: {exc}", file=sys.stderr, flush=True)
            return None
        finally:
            s.close()

    def _ask(self, what: str):
        """What niri says to `"Workspaces"`, `"Windows"`, `"Outputs"`, `"Version"`; None on failing."""
        reply = self._request(what)
        ok = (reply or {}).get("Ok")
        return ok.get(what) if isinstance(ok, dict) else None

    def _act(self, action: str, **fields) -> bool:
        reply = self._request({"Action": {action: fields}})
        if not reply or "Ok" not in reply:
            print(f"niri: {action}: {(reply or {}).get('Err', 'no answer')}", file=sys.stderr, flush=True)
            return False
        return True

    def _listen(self):
        """The event stream, on a connection of its own."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        s.settimeout(REQUEST_TIMEOUT_S)
        try:
            s.connect(self.path)
            s.sendall(b'"EventStream"\n')
            buf = b""
            while b"\n" not in buf:
                data = s.recv(65536)
                if not data:
                    raise OSError("niri closed the event stream")
                buf += data
        except OSError as exc:
            s.close()
            raise RuntimeError(f"niri's event stream: {exc}") from None
        _, _, self._buf = buf.partition(b"\n")  # (the reply; the state follows it)
        s.setblocking(False)
        self._events = s
        self.connections += 1

    # ---- what niri says ---------------------------------------------------------------------
    def refresh(self) -> bool:
        """Ask for everything again; False when niri did not answer."""
        outputs, spaces, windows = self._ask("Outputs"), self._ask("Workspaces"), self._ask("Windows")
        if not isinstance(spaces, list) or not isinstance(windows, list):
            return False
        if isinstance(outputs, dict):
            self._outputs(outputs)
        self._ws = {int(w["id"]): w for w in spaces if isinstance(w, dict) and "id" in w}
        self._win = {int(w["id"]): w for w in windows if isinstance(w, dict) and "id" in w}
        self._build()
        return True

    def _outputs(self, outputs: dict):
        """The screens in use, left to right (then top to bottom)."""
        placed = []
        for name, out in outputs.items():
            logical = (out or {}).get("logical") if isinstance(out, dict) else None
            if logical:
                placed.append((int(logical.get("x") or 0), int(logical.get("y") or 0), str(name)))
        self._order = [name for _, _, name in sorted(placed)]

    def _build(self):
        """Sidelit's shapes from what niri said."""
        now = time.time()
        rank = {name: i for i, name in enumerate(self._order)}
        live: dict[int, Space] = {}
        for wid, item in self._ws.items():
            ws = self.workspaces.get(wid) or Space(proxy=wid)
            ws.name = str(item.get("name") or "")
            ws.id = ws_ref(ws.name)
            had = self._had.setdefault(wid, [])
            if not ws.name and wid in self._letting_go:  # (Sidelit let it go: no name to know it by)
                self._letting_go.discard(wid)
                had.clear()
            if ws.name and (not had or had[-1] != ws.name):
                had[:] = ([n for n in had if n.lower() != ws.name.lower()] + [ws.name])[-MAX_NAMES:]
            ws.monitor = str(item.get("output") or "")
            ws.idx = int(item.get("idx") or 0)
            ws.shown = bool(item.get("is_active"))
            ws.state = int(bool(item.get("is_focused")))
            ws.copies_pinned = ws.pinned
            ws.removed = False
            live[wid] = ws
        for position, ws in enumerate(sorted(live.values(), key=lambda w: (rank.get(w.monitor, len(rank)),
                                                                          w.monitor, w.idx)), start=1):
            ws.coords = (position,)
        for wid, ws in self.workspaces.items():
            if wid not in live:
                ws.removed = True
        self.workspaces = live
        # a workspace renamed outside Sidelit is still known by the names it had, unless another
        # one has such a name now
        self._had = {wid: had for wid, had in self._had.items() if wid in live}
        self._letting_go &= set(live)
        named = {ws.name.lower() for ws in live.values() if ws.name}
        for wid, ws in live.items():
            ws.ids = frozenset(ws_ref(n) for n in self._had.get(wid, ()) if n.lower() not in named)
        screens = {ws.monitor for ws in live.values() if ws.monitor}
        self.screens = [Screen(n) for n in self._order if n in screens] + \
            [Screen(n) for n in sorted(screens - set(self._order))]

        seen = set()
        for nid, item in self._win.items():
            key = f"{self.token}-{nid}"
            seen.add(key)
            tl = self.toplevels.get(key)
            if tl is None:
                tl = self.toplevels[key] = Window(proxy=nid, identifier=key)
            tl.title = str(item.get("title") or "")
            tl.app_id = str(item.get("app_id") or "")
            tl.pid = int(item.get("pid") or 0)
            number = item.get("workspace_id")
            tl.workspace = int(number) if number is not None and int(number) in live else None
            tl.output = live[tl.workspace].monitor if tl.workspace is not None else ""
            active = bool(item.get("is_focused"))
            if active and not tl.active:
                tl.focused_at = now  # when it got the keyboard, as it happens
            tl.active = active
        for key in [k for k in self.toplevels if k not in seen]:
            tl = self.toplevels.pop(key)
            tl.closed = True
            # clicking a window's close button focuses it a moment before it goes: keep that
            if tl.focused_at:
                self.closed_focus[tl.identifier] = tl.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}

    def _event(self, event: dict) -> bool:
        """Take in one event; True when the screens are to be asked for again."""
        name, data = next(iter(event.items()), (None, None))
        if not isinstance(data, dict):
            return False
        if name == "WorkspacesChanged":
            before = {w.get("output") for w in self._ws.values()}
            self._ws = {int(w["id"]): w for w in data.get("workspaces") or [] if isinstance(w, dict) and "id" in w}
            return {w.get("output") for w in self._ws.values()} != before  # (a screen came or went)
        if name == "WorkspaceActivated":
            wid = int(data.get("id") or 0)
            output = (self._ws.get(wid) or {}).get("output")
            for w in self._ws.values():
                if w.get("output") == output:
                    w["is_active"] = int(w["id"]) == wid
                if data.get("focused"):
                    w["is_focused"] = int(w["id"]) == wid
        elif name == "WindowsChanged":
            self._win = {int(w["id"]): w for w in data.get("windows") or [] if isinstance(w, dict) and "id" in w}
        elif name == "WindowOpenedOrChanged":
            window = data.get("window")
            if isinstance(window, dict) and "id" in window:
                if window.get("is_focused"):
                    for w in self._win.values():
                        w["is_focused"] = False
                self._win[int(window["id"])] = window
        elif name == "WindowClosed":
            self._win.pop(int(data.get("id") or 0), None)
        elif name == "WindowFocusChanged":
            focused = data.get("id")
            for w in self._win.values():
                w["is_focused"] = focused is not None and int(w["id"]) == int(focused)
        return False

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    def fd(self) -> int:
        return self._events.fileno() if self._events is not None else -1

    def pump(self):
        """Take in the events that have come. ConnectionError once niri has gone. (niri drops an
        event stream that falls too far behind: one dropped while niri still answers is opened
        again, and the daemon goes on.)"""
        if self._events is None:
            return
        gone = False
        while True:
            try:
                data = self._events.recv(65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    break
                gone = True
                break
            if not data:
                gone = True
                break
            self._buf += data
        outputs = False
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                event = json.loads(line.decode(errors="replace"))
            except ValueError:
                continue
            if isinstance(event, dict):
                outputs = self._event(event) or outputs
        if gone:
            self._events.close()
            self._events, self._buf = None, b""
            try:
                self._listen()
            except RuntimeError:
                raise ConnectionError("niri closed its event stream (it quit or restarted)") from None
            print("niri: the event stream went; opened again", file=sys.stderr, flush=True)
            self.refresh()
        elif outputs:
            got = self._ask("Outputs")
            if isinstance(got, dict):
                self._outputs(got)
        self._build()
        if self.on_change:
            self.on_change()

    def _sync(self, done: Callable[[], bool]):
        """Wait for an action's result to show (on the event stream, or asking again)."""
        end = time.monotonic() + SYNC_S
        while True:
            if self._events is not None:
                self.pump()
            else:
                self.refresh()
            if done() or time.monotonic() > end:
                return
            time.sleep(0.02)

    def wait(self, seconds: float = 1.5):
        """For one-shot commands: everything was read on connecting, so only a short pause, then
        everything is read again (for one that waits for a change)."""
        time.sleep(min(max(seconds, 0.0), 0.1))
        if self._events is not None:
            self.pump()
        else:
            self.refresh()

    def roundtrip(self):
        self.wait(0)

    def settle(self, max_rounds: int = 6):
        self.wait(0)

    def close(self):
        if self._events is not None:
            self._events.close()
            self._events = None

    def list(self, all_groups: bool = False) -> list[Space]:
        return sorted((w for w in self.workspaces.values() if not w.removed), key=lambda w: w.coords)

    def active(self) -> Space | None:
        return next((w for w in self.list() if w.active), None)

    def by_name(self, name: str) -> Space | None:
        return next((w for w in self.list() if w.name and w.name.lower() == name.lower()), None)

    def resolve(self, target: str) -> Space | None:
        """A workspace by name, else by its place in the sidebar (from 1)."""
        found = self.by_name(target)
        if found is None and target.isdigit():
            found = next((w for w in self.list() if w.index == int(target)), None)
        return found

    def focused_screen(self) -> str:
        active = self.active()
        return active.monitor if active is not None else (self._order[0] if self._order else "")

    def shortcut_label(self, ws, total: int) -> str:
        """The chip on a tab: niri's config goes to a workspace of the screen you are on by its
        place there, Mod+1 to Mod+9 (Super, out of the box)."""
        return f"⊞ {ws.idx}" if 1 <= ws.idx <= 9 and ws.monitor == self.focused_screen() else ""

    # windows
    def workspace_of(self, tl: Window) -> Space | None:
        ws = self.workspaces.get(tl.workspace) if tl.workspace is not None else None
        return ws if ws is not None and not ws.removed else None

    def copy_ids_of(self, tl: Window) -> frozenset:
        ws = self.workspace_of(tl)
        return ws.all_ids if ws is not None else frozenset()

    def toplevels_on(self, ws: Space) -> list[Window]:
        return [t for t in self.toplevels.values() if not t.closed and t.workspace == ws.proxy]

    def output_names(self) -> set[str]:
        return {s.name for s in self.screens}

    def outputs_of(self, tl: Window) -> list[Screen]:
        return [s for s in self.screens if s.name == tl.output]

    def output_of(self, tl: Window) -> Screen | None:
        return next(iter(self.outputs_of(tl)), None)

    @property
    def can_move_windows(self) -> bool:
        return True

    def focus(self, tl: Window):
        if not tl.closed:
            self._act("FocusWindow", id=tl.proxy)

    def close_toplevel(self, tl: Window):
        if not tl.closed:
            self._act("CloseWindow", id=tl.proxy)

    def move_toplevel(self, tl: Window, ws: Space, output: str | None = None) -> bool:
        """Put a window on a workspace, staying where you are. With `output`, that workspace goes
        to that screen, windows and all: a workspace is on one screen in niri."""
        if tl.closed or ws.removed:
            return False
        ok = True
        if tl.workspace != ws.proxy:
            ok = self._act("MoveWindowToWorkspace", window_id=tl.proxy, reference=_ref(ws), focus=False)
        if ok and output and output != ws.monitor and output in self.output_names():
            ok = self._act("MoveWorkspaceToMonitor", output=output, reference=_ref(ws))
        return ok

    # workspaces
    def activate(self, ws: Space):
        if ws.removed or ws.active:
            return  # (going to the one you are on would go back to the last one, with back-and-forth on)
        if self._act("FocusWorkspace", reference=_ref(ws)):
            for w in self.workspaces.values():  # (the event confirms it in a moment)
                w.state = int(w is ws)

    def _next_name(self, ws: Space | None = None) -> str:
        """A name of Sidelit's for a workspace: one it had before, while nothing else has it (it
        was renamed or unnamed outside Sidelit), else the one after every one in use or known
        to the daemon's layout."""
        names = [n for had in self._had.values() for n in had] + [name_of(r) for r in self.preferred_ids]
        taken = {w.name.lower() for w in self.workspaces.values() if w.name}
        before = [n for n in self._had.get(ws.proxy, ()) if OURS.fullmatch(n) and n.lower() not in taken] if ws else []
        if before:
            return before[-1]
        numbers = {int(m.group(1)) for n in names + list(taken) if (m := OURS.fullmatch(n))}
        return f"{PREFIX}{max(numbers, default=0) + 1}"

    def _name(self, ws: Space, name: str) -> bool:
        if not self._act("SetWorkspaceName", name=name, workspace=_ref(ws)):
            return False
        self._sync(lambda: ws.name.lower() == name.lower() or ws.removed)
        return ws.name.lower() == name.lower()

    def create(self, name: str = "", want: tuple = ()) -> Space | None:
        """A kept workspace: the empty one at the end of the screen you are on, named. With
        `want`, the name a kept workspace had (Sidelit's or yours), when no workspace has it
        now; else the next of Sidelit's. `name` is the daemon's, and stays Sidelit's own."""
        screen = self.focused_screen()
        spare = [w for w in self.list() if w.monitor == screen and not w.name and not self.toplevels_on(w)]
        if not spare:
            print(f"niri: no empty workspace on {screen or 'any screen'} to keep", file=sys.stderr, flush=True)
            return None
        ws = max(spare, key=lambda w: w.idx)
        taken = {w.name.lower() for w in self.workspaces.values() if w.name}  # (niri's names go in any case)
        # (the layout learns a workspace's ids as it goes: the last one is the name it had last)
        wanted = next((name_of(str(r)) for r in reversed(want) if _bringable(r) and name_of(str(r)).lower() not in taken),
                      None)
        return ws if self._name(ws, wanted or self._next_name()) else None

    def pin(self, ws: Space, pinned: bool = True):
        """Keep a workspace (name it), or let it go: only a name Sidelit gave is taken off, and
        then the names it had are forgotten."""
        if pinned and not ws.name:
            self._name(ws, self._next_name(ws))
        elif not pinned and OURS.fullmatch(ws.name or ""):
            if self._act("UnsetWorkspaceName", reference=_ref(ws)):
                self._letting_go.add(ws.proxy)
                self._sync(lambda: not ws.name or ws.removed)

    def rename(self, ws: Space, name: str):
        """niri's name for a workspace, which is also Sidelit's id for it."""
        name = " ".join(name.split())
        if name:
            self._name(ws, name)

    def remove(self, ws: Space):
        """niri drops a workspace once it is empty, not shown and has no name."""
        self.pin(ws, False)

    def move(self, ws: Space, other: Space, before: bool = True):
        """Put `ws` right before (or after) `other`, onto `other`'s screen if need be."""
        if ws is other or ws.removed or other.removed:
            return
        if ws.monitor != other.monitor:
            if not self._act("MoveWorkspaceToMonitor", output=other.monitor, reference=_ref(ws)):
                return
            self._sync(lambda: ws.monitor == other.monitor)
            if ws.monitor != other.monitor:
                return  # (niri did not take it there: no place on the other screen to work out)
        # niri takes the workspace out, then puts it in at the place given (from 1)
        place = other.idx - (1 if ws.idx < other.idx else 0) + (0 if before else 1)
        if place != ws.idx:
            self._act("MoveWorkspaceToIndex", index=place, reference=_ref(ws))
            self._sync(lambda: ws.idx == place)
