"""COSMIC workspace + window client (ext-workspace-v1, cosmic-workspace-v2, toplevel info/management).

Runs on its own Wayland connection so it can live next to a GTK main loop.
"""
from __future__ import annotations

import os
import select
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from sidelit import model



from sidelit.wire import Display  # noqa: E402  (pure-Python Wayland client, no compiled deps)

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL_DIR = os.path.join(HERE, "protocols")  # bundled protocol XML
PROTOCOLS = [
    "/usr/share/wayland/wayland.xml",
    "ext-workspace-v1.xml",
    "cosmic-workspace-unstable-v1.xml",
    "cosmic-workspace-unstable-v2.xml",
    "ext-foreign-toplevel-list-v1.xml",
    "cosmic-toplevel-info-unstable-v1.xml",
    "cosmic-toplevel-management-unstable-v1.xml",
]


def protocol_files() -> list[str]:
    files = []
    for name in PROTOCOLS:
        path = name if name.startswith("/") else os.path.join(PROTOCOL_DIR, name)
        if not os.path.exists(path) and name.startswith("/"):
            path = os.path.join(PROTOCOL_DIR, os.path.basename(name))  # bundled copy of wayland.xml
        files.append(path)
    return files

STATE_ACTIVE = 1
STATE_URGENT = 2
STATE_HIDDEN = 4
COSMIC_PINNED = 1


@dataclass
class Workspace(model.Workspace):  # one output's copy of a workspace, with its COSMIC handles
    caps: int = 0
    cosmic: object = None
    cosmic_caps: int = 0
    cosmic_state: int = 0
    group: object = None
    own_state: int = 0         # this copy's own flags; state/cosmic_state are merged over copies
    own_cosmic_state: int = 0

    @property
    def active(self) -> bool:
        return bool(self.state & STATE_ACTIVE)

    @property
    def pinned(self) -> bool:
        return bool(self.cosmic_state & COSMIC_PINNED)


@dataclass
class Toplevel(model.Toplevel):  # a window, with where COSMIC says it is
    cosmic: object = None
    ws_coords: set = field(default_factory=set)
    outputs: set = field(default_factory=set)  # id(wl_output proxy) the window is shown on
    states: frozenset = frozenset()            # zcosmic_toplevel_handle_v1 state values
    focused_at: float = 0.0                    # when it last got the keyboard


# zcosmic_toplevel_handle_v1.state
TL_MAXIMIZED, TL_MINIMIZED, TL_ACTIVATED, TL_FULLSCREEN, TL_STICKY = range(5)


@dataclass
class Output:
    proxy: object
    name: str = ""       # connector, e.g. "HDMI-A-1" (wl_output v4)
    model: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


class WorkspaceClient:
    # everything COSMIC can do; a backend for another desktop advertises what it can
    caps = frozenset(("create", "rename", "remove", "pin", "move", "focus", "close_toplevel"))

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True):
        # `primary` (see backend.open_client): COSMIC clients are independent Wayland
        # connections, so it is accepted and ignored here.
        self.on_change = on_change
        self.display = Display(protocol_files())
        self.display.connect()
        self.registry = self.display.get_registry()
        self.registry.dispatcher["global"] = self._on_global
        self.registry.dispatcher["global_remove"] = self._on_global_remove
        self._output_globals: dict[int, int] = {}  # wl registry name -> id(proxy)
        self.outputs_changed = False  # set when a screen appears or disappears; the sidebar clears it
        self.manager = None
        self.cosmic_manager = None
        self.toplevel_list = None
        self.toplevel_info = None
        self.toplevel_manager = None
        self.toplevel_manager_version = 0
        self.seat = None
        self.output = None  # first output (any one works for move_to_ext_workspace in Global mode)
        self.outputs: dict[int, Output] = {}  # keyed by id(proxy)
        self.group_outputs: dict[int, set] = {}  # id(group proxy) -> {id(wl_output proxy)}
        self.toplevels: dict[int, Toplevel] = {}
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.groups: list = []
        self.group_caps = 0
        self.workspaces: dict[int, Workspace] = {}  # keyed by proxy id
        self.preferred_ids: set[str] = set()  # ids the sidebar knows; they win when copies of an index disagree
        self._pending: list[Workspace] = []
        self.display.roundtrip()
        if self.manager is None:
            raise RuntimeError("compositor does not expose ext_workspace_manager_v1")
        self.settle()
        g = getattr(self, "_toplevel_list_global", None)
        if g is not None:
            self.toplevel_list = self.registry.bind(g[0], "ext_foreign_toplevel_list_v1", g[1])
            self.toplevel_list.dispatcher["toplevel"] = self._on_toplevel
            self.toplevel_list.dispatcher["finished"] = lambda *a: None
            self.settle()

    def shortcut_label(self, workspace, total: int) -> str:
        """The key COSMIC binds to a workspace out of the box: Super and its number for the
        first nine, Super+0 for the last one, nothing for the rest."""
        n = workspace.index
        key = str(n) if n in range(1, 10) else "0" if n == total else ""
        return "⊞ " + key if key else ""

    def wait(self, seconds: float = 1.5) -> None:
        """Read and handle events for `seconds`: for one-shot commands, which have no main loop."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.display.flush()  # what we asked for goes out before we wait for the answers
            if select.select([self.fd()], [], [], min(left, 0.3))[0]:
                self.pump()  # (reads, handles, flushes)

    def settle(self, max_rounds: int = 6):
        """Roundtrip until the compositor stops sending us new events."""
        for _ in range(max_rounds):
            self._events = 0
            self.display.roundtrip()
            if self._events == 0:
                break

    def _touch(self):
        self._events = getattr(self, "_events", 0) + 1

    # ---- registry -------------------------------------------------------
    def _on_global(self, registry, name, interface, version):
        if interface == "ext_workspace_manager_v1":
            self.manager = registry.bind(name, "ext_workspace_manager_v1", 1)
            self.manager.dispatcher["workspace_group"] = self._on_group
            self.manager.dispatcher["workspace"] = self._on_workspace
            self.manager.dispatcher["done"] = self._on_done
            self.manager.dispatcher["finished"] = lambda *a: None
        elif interface == "zcosmic_workspace_manager_v2":
            self.cosmic_manager = registry.bind(name, "zcosmic_workspace_manager_v2", min(version, 2))
        elif interface == "ext_foreign_toplevel_list_v1":
            # bound later, once workspaces are known, so cosmic can reference them
            self._toplevel_list_global = (name, min(version, 1))
        elif interface == "zcosmic_toplevel_manager_v1":
            # v4 adds move_to_ext_workspace (cosmic-comp 1.0 advertises 4)
            self.toplevel_manager_version = min(version, 4)
            self.toplevel_manager = registry.bind(name, "zcosmic_toplevel_manager_v1", self.toplevel_manager_version)
            self.toplevel_manager.dispatcher["capabilities"] = lambda *a: None
        elif interface == "wl_output":
            proxy = registry.bind(name, "wl_output", min(version, 4))
            out = Output(proxy=proxy)
            self.outputs[id(proxy)] = out
            self._output_globals[name] = id(proxy)
            if getattr(self, "workspaces", None) is not None and self.workspaces:  # not the initial burst
                self.outputs_changed = True
                self._touch_toplevel()
            if self.output is None:
                self.output = proxy
            proxy.dispatcher["geometry"] = lambda p, x, y, pw, ph, sp, make, model, tr: (
                setattr(out, "x", x), setattr(out, "y", y), setattr(out, "model", model))
            proxy.dispatcher["mode"] = lambda p, flags, w, h, r: (
                (setattr(out, "width", w), setattr(out, "height", h)) if flags & 1 else None)
            proxy.dispatcher["name"] = lambda p, v: setattr(out, "name", v)
            for ev in ("done", "scale", "description"):
                proxy.dispatcher[ev] = lambda *a: None
        elif interface == "wl_seat" and self.seat is None:
            self.seat = registry.bind(name, "wl_seat", min(version, 5))
            self.seat.dispatcher["capabilities"] = lambda *a: None
            self.seat.dispatcher["name"] = lambda *a: None
        elif interface == "zcosmic_toplevel_info_v1" and version >= 3:
            self.toplevel_info = registry.bind(name, "zcosmic_toplevel_info_v1", 3)
            self.toplevel_info.dispatcher["toplevel"] = lambda *a: None  # legacy path, unused
            self.toplevel_info.dispatcher["done"] = lambda *a: None
            self.toplevel_info.dispatcher["finished"] = lambda *a: None

    def _on_global_remove(self, registry, name):
        oid = self._output_globals.pop(name, None)
        if oid is not None:
            # cosmic sends no output_leave for a screen that is unplugged: forget it everywhere,
            # or every window that was on it counts as being on two screens from now on
            for tl in self.toplevels.values():
                tl.outputs.discard(oid)
            for outs in self.group_outputs.values():
                outs.discard(oid)
            out = self.outputs.pop(oid, None)
            if out is not None and self.output is out.proxy:
                self.output = next((o.proxy for o in self.outputs.values()), None)
            self.outputs_changed = True
            self._touch_toplevel()

    def output_names(self) -> set[str]:
        return {o.name for o in self.outputs.values() if o.name}

    # ---- toplevels (windows) ---------------------------------------------
    def _on_toplevel(self, lst, proxy):
        tl = Toplevel(proxy=proxy)
        self.toplevels[id(proxy)] = tl
        proxy.dispatcher["title"] = lambda p, v: setattr(tl, "title", v)
        proxy.dispatcher["app_id"] = lambda p, v: setattr(tl, "app_id", v)
        proxy.dispatcher["identifier"] = lambda p, v: setattr(tl, "identifier", v)
        proxy.dispatcher["done"] = lambda p: self._touch_toplevel()
        proxy.dispatcher["closed"] = lambda p: self._on_toplevel_closed(tl)
        if self.toplevel_info is not None:
            c = self.toplevel_info.get_cosmic_toplevel(proxy)
            tl.cosmic = c
            for ev in ("title", "app_id", "workspace_enter", "workspace_leave", "geometry", "done", "closed"):
                c.dispatcher[ev] = lambda *a: None
            c.dispatcher["state"] = lambda c, v: self._tl_state(tl, frozenset(_decode_array(v)))
            c.dispatcher["output_enter"] = lambda c, o: (tl.outputs.add(id(o)), self._touch_toplevel())
            c.dispatcher["output_leave"] = lambda c, o: (tl.outputs.discard(id(o)), self._touch_toplevel())
            c.dispatcher["ext_workspace_enter"] = lambda c, w: self._tl_ws(tl, w, True)
            c.dispatcher["ext_workspace_leave"] = lambda c, w: self._tl_ws(tl, w, False)

    def _tl_state(self, tl: Toplevel, states: frozenset):
        tl.states = states
        if TL_ACTIVATED in states:
            tl.focused_at = time.time()  # when it got the keyboard, as it happens (not when we next look)
        self._touch_toplevel()

    def _tl_ws(self, tl: Toplevel, ws_proxy, enter: bool):
        ws = self.workspaces.get(id(ws_proxy))
        if ws is None:
            return
        # workspaces get their coordinates before toplevels are announced; keep a
        # reference to the Workspace so we can resolve coords lazily
        if enter:
            tl.ws_coords.add(id(ws_proxy))
        else:
            tl.ws_coords.discard(id(ws_proxy))
        self._touch_toplevel()

    def _on_toplevel_closed(self, tl: Toplevel):
        tl.closed = True
        # clicking the close button of a window focuses it a moment before it goes: keep that
        now = time.time()
        if tl.identifier and tl.focused_at:
            self.closed_focus[tl.identifier] = tl.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}
        self.toplevels.pop(id(tl.proxy), None)
        try:
            if tl.cosmic is not None:
                tl.cosmic.destroy()
            tl.proxy.destroy()
        except Exception:
            pass
        self._touch_toplevel()

    def _touch_toplevel(self):
        self._touch()
        if self.on_change:
            self.on_change()

    def focus(self, tl: Toplevel):
        """Give keyboard focus to a window (raises it on its workspace)."""
        if self.toplevel_manager is None or self.seat is None or tl.cosmic is None or tl.closed or tl.cosmic.destroyed:
            return
        self.toplevel_manager.activate(tl.cosmic, self.seat)
        self.display.flush()

    def close_toplevel(self, tl: Toplevel):
        """Ask the compositor to close a window (the app gets a normal close request)."""
        if self.toplevel_manager is None or tl.cosmic is None or tl.closed or tl.cosmic.destroyed:
            return  # already gone
        self.toplevel_manager.close(tl.cosmic)
        self.display.flush()

    @property
    def can_move_windows(self) -> bool:
        return self.toplevel_manager is not None and self.toplevel_manager_version >= 4 and self.output is not None

    def outputs_of(self, tl: Toplevel) -> list[Output]:
        """The screens a window is on (live ones only)."""
        return [self.outputs[o] for o in tl.outputs if o in self.outputs]

    def output_of(self, tl: Toplevel) -> Optional[Output]:
        for oid in tl.outputs:
            if oid in self.outputs:
                return self.outputs[oid]
        return None

    def output_named(self, name: str) -> Optional[Output]:
        return next((o for o in self.outputs.values() if o.name == name), None) if name else None

    def move_toplevel(self, tl: Toplevel, ws: Workspace, output: str | None = None) -> bool:
        """Put a window on a workspace, and on the output called `output` (connector name) if given
        and present, else on the output it is on now (zcosmic_toplevel_manager_v1 v4
        move_to_ext_workspace). The compositor reports the new placement on its next tick."""
        if not self.can_move_windows or tl.cosmic is None or tl.closed or tl.cosmic.destroyed or ws.removed:
            return False
        target = self.output_named(output) or self.output_of(tl)
        proxy = target.proxy if target is not None else self.output
        # cosmic-comp keeps one workspace group per output; the target output is the one whose
        # copy of the workspace we pass (the output argument alone does not pick it)
        handle = ws.proxy
        if target is not None:
            for copy in self.workspaces.values():
                if copy.coords == ws.coords and not copy.removed and copy.group is not None and id(proxy) in self.group_outputs.get(id(copy.group), set()):
                    handle = copy.proxy
                    break
        self.toplevel_manager.move_to_ext_workspace(tl.cosmic, handle, proxy)
        self.display.flush()
        return True

    def workspace_of(self, tl: Toplevel) -> Optional[Workspace]:
        """The (merged) workspace a window is on, if known yet."""
        for ws in self.list():
            keys = {id(w.proxy) for w in self.workspaces.values() if w.coords == ws.coords}
            if tl.ws_coords & keys:
                return ws
        return None

    def copy_ids_of(self, tl: Toplevel) -> frozenset:
        """The ids of the very copies a window is on (not every copy at their index): after a
        replug an index can hold copies of two different workspaces."""
        return frozenset(w.id for k, w in self.workspaces.items() if k in tl.ws_coords and w.id and not w.removed)

    def toplevels_on(self, ws: Workspace) -> list[Toplevel]:
        """Windows on the given workspace (any output's copy with the same coordinates)."""
        keys = {id(w.proxy) for w in self.workspaces.values() if w.coords == ws.coords}
        return [t for t in self.toplevels.values() if not t.closed and t.ws_coords & keys]

    # ---- groups ----------------------------------------------------------
    def _on_group(self, manager, group):
        self.groups.append(group)
        group.dispatcher["capabilities"] = lambda g, caps: setattr(self, "group_caps", caps)
        group.dispatcher["workspace_enter"] = self._on_ws_enter
        group.dispatcher["workspace_leave"] = lambda g, ws: None
        group.dispatcher["output_enter"] = lambda g, o: self.group_outputs.setdefault(id(g), set()).add(id(o))
        group.dispatcher["output_leave"] = lambda g, o: self.group_outputs.get(id(g), set()).discard(id(o))
        group.dispatcher["removed"] = lambda g: self.groups.remove(g) if g in self.groups else None

    def _on_ws_enter(self, group, ws_proxy):
        ws = self.workspaces.get(id(ws_proxy)) or self._find(ws_proxy)
        if ws:
            ws.group = group

    def _find(self, proxy):
        for ws in self.workspaces.values():
            if ws.proxy == proxy:
                return ws
        return None

    # ---- workspaces ------------------------------------------------------
    def _on_workspace(self, manager, proxy):
        ws = Workspace(proxy=proxy)
        self.workspaces[id(proxy)] = ws
        proxy.dispatcher["id"] = lambda p, v: setattr(ws, "id", v)
        proxy.dispatcher["name"] = lambda p, v: setattr(ws, "name", v)
        proxy.dispatcher["coordinates"] = lambda p, v: setattr(ws, "coords", _decode_array(v))
        proxy.dispatcher["state"] = lambda p, v: (setattr(ws, "own_state", int(v)), setattr(ws, "state", int(v)))
        proxy.dispatcher["capabilities"] = lambda p, v: setattr(ws, "caps", int(v))
        proxy.dispatcher["removed"] = lambda p: self._on_ws_removed(ws)
        if self.cosmic_manager is not None:
            cws = self.cosmic_manager.get_cosmic_workspace(proxy)
            ws.cosmic = cws
            cws.dispatcher["capabilities"] = lambda p, v: setattr(ws, "cosmic_caps", int(v))
            cws.dispatcher["tiling_state"] = lambda p, v: None
            cws.dispatcher["state"] = lambda p, v: (setattr(ws, "own_cosmic_state", int(v)),
                                                    setattr(ws, "cosmic_state", int(v)))

    def _on_ws_removed(self, ws: Workspace):
        ws.removed = True
        self.workspaces.pop(id(ws.proxy), None)
        try:
            if ws.cosmic is not None:
                ws.cosmic.destroy()
            ws.proxy.destroy()
        except Exception:
            pass

    def _on_done(self, manager):
        self._touch()
        if self.on_change:
            self.on_change()

    # ---- public API ------------------------------------------------------
    def fd(self) -> int:
        return self.display.get_fd()

    def pump(self):
        """Read + dispatch pending events (call when fd is readable)."""
        self.display.dispatch(block=True)
        self.display.flush()

    def roundtrip(self):
        self.display.roundtrip()

    def list(self, all_groups: bool = False) -> list[Workspace]:
        """One entry per workspace index.

        COSMIC reports one workspace group per output; in Global mode they mirror each other
        by index. Usually only one output's copy carries a pinned id, but each output keeps
        its own pinned list, so after a screen is re-plugged an index can have two id-bearing
        copies (the registry's on one screen, the returning screen's own on the other). The
        copies are merged per index: the representative is the copy whose id is in
        `preferred_ids` (the sidebar puts the registry's ids there), else any copy with an id,
        else the first announced; every copy's id is kept in `ids` so lookups by id keep
        working whichever copy represents the index."""
        ws = [w for w in self.workspaces.values() if not w.removed]
        if all_groups:
            return sorted(ws, key=lambda w: w.coords)
        copies: dict[tuple, list[Workspace]] = {}
        for w in ws:
            copies.setdefault(w.coords, []).append(w)
        merged = []
        for coords, group in copies.items():
            rep = next((c for c in group if c.id and c.id in self.preferred_ids), None) \
                or next((c for c in group if c.id), None) or group[0]
            rep.ids = frozenset(c.id for c in group if c.id)
            # flags merged from every copy's own (never accumulated: a copy unpinned since is not pinned)
            rep.state, rep.cosmic_state = 0, 0
            for c in group:
                rep.state |= c.own_state
                rep.cosmic_state |= c.own_cosmic_state
            rep.copies_pinned = all(c.own_cosmic_state & COSMIC_PINNED for c in group)
            merged.append(rep)
        return sorted(merged, key=lambda w: w.coords)

    def copies_summary(self) -> str:
        """`1: cb8061@eDP-1 2613d2@HDMI-A-1 | 2: 12031fa@eDP-1 -@HDMI-A-1 | ...` for the log."""
        outs = {id(o.proxy): o.name or "?" for o in self.outputs.values()}
        by_coords: dict[tuple, list[str]] = {}
        for w in sorted(self.workspaces.values(), key=lambda w: (w.coords, w.id)):
            if w.removed:
                continue
            names = [outs[o] for o in self.group_outputs.get(id(w.group), set()) if o in outs] if w.group is not None else []
            by_coords.setdefault(w.coords, []).append(f"{w.id or '-'}@{','.join(names) or '?'}")
        return " | ".join(f"{c[0] if c else '?'}: {' '.join(v)}" for c, v in by_coords.items())

    def close(self):
        try:
            self.display.disconnect()
        except Exception:
            pass

    def active(self) -> Optional[Workspace]:
        for w in self.list():
            if w.active:
                return w
        return None

    def resolve(self, target: str) -> Optional[Workspace]:
        """Find a workspace by name, then by 1-based index."""
        ws = self.by_name(target)
        if ws is None and target.isdigit():
            ws = next((w for w in self.list() if w.index == int(target)), None)
        return ws

    def by_name(self, name: str) -> Optional[Workspace]:
        for w in self.list():
            if w.name == name:
                return w
        return None

    def activate(self, ws: Workspace):
        ws.proxy.activate()
        self.manager.commit()
        self.display.roundtrip()

    def create(self, name: str) -> Optional[Workspace]:
        """Create a named workspace. Falls back to renaming the trailing empty one."""
        before = {id(w.proxy) for w in self.workspaces.values()}
        if self.groups and (self.group_caps & 1):
            self.groups[0].create_workspace(name)
            self.manager.commit()
            self.display.roundtrip()
            self.display.roundtrip()
            for w in self.list():
                if id(w.proxy) not in before:
                    if w.name != name and w.cosmic is not None:
                        self.rename(w, name)
                    return w
        # fallback: COSMIC keeps an empty trailing workspace
        ws = self.list()[-1] if self.list() else None
        if ws is not None:
            self.rename(ws, name)
        return ws

    def rename(self, ws: Workspace, name: str):
        if ws.cosmic is None:
            raise RuntimeError("cosmic workspace extension unavailable; cannot rename")
        ws.cosmic.rename(name)
        self.manager.commit()
        self.settle()

    def pin(self, ws: Workspace, pinned: bool = True):
        """Pin (or unpin) every screen's copy of this workspace: COSMIC pins per screen, and a
        workspace pinned on one screen only follows that screen when it is unplugged."""
        copies = [c for c in self.workspaces.values() if c.coords == ws.coords and not c.removed and c.cosmic is not None]
        todo = [c for c in copies if bool(c.own_cosmic_state & COSMIC_PINNED) != pinned] or \
            ([ws] if ws.cosmic is not None else [])
        if not todo:
            return
        for c in todo:
            (c.cosmic.pin if pinned else c.cosmic.unpin)()
        self.manager.commit()
        self.settle()

    def move(self, ws: Workspace, other: Workspace, before: bool = True):
        """Reorder: put `ws` right before/after `other` (coordinates are 1-D, so axis 0)."""
        if ws.cosmic is None or ws is other:
            return
        (ws.cosmic.move_before if before else ws.cosmic.move_after)(other.proxy, 0)
        self.manager.commit()
        self.settle()

    def remove(self, ws: Workspace):
        if ws.caps & 4:
            ws.proxy.remove()
            self.manager.commit()
            self.display.roundtrip()


def _decode_array(v) -> tuple:
    if isinstance(v, (bytes, bytearray)):
        n = len(v) // 4
        return tuple(struct.unpack("<%dI" % n, bytes(v[: n * 4])))
    try:
        return tuple(int(x) for x in v)
    except TypeError:
        return ()
