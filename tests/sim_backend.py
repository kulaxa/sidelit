"""The engine driving a real backend client against a fake compositor, the way the daemon does:
for what only shows when the two work together (a login, a screen coming back, a rename done
outside Sidelit). The actuator below does what daemon.py's does, without GLib, the sidebar or
the agents; time is the test's (`advance`), the client's own short waits are real."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core.engine import Engine  # noqa: E402
from sidelit.core.model import Layout, Obs, Space, Win  # noqa: E402


class Act:
    """daemon.py's actuator, on a backend client."""

    def __init__(self, client, on_launch=None):
        self.ws = client
        self.on_launch = on_launch  # (the test opens the window a launch would)
        self.launched: list[dict] = []
        self._tl = {}

    def observe(self, engine) -> Obs:
        self.ws.preferred_ids = {i for lw in engine.layout.workspaces for i in lw.ids}
        windows, self._tl = {}, {}
        for tl in list(self.ws.toplevels.values()):
            if tl.closed:
                continue
            here = self.ws.workspace_of(tl)
            outs = self.ws.outputs_of(tl)
            windows[tl.identifier] = Win(id=tl.identifier, app=tl.app_id, title=tl.title,
                                         space=here.index if here is not None else None,
                                         output=outs[0].name if len(outs) == 1 else None, pid=tl.pid,
                                         focused=tl.focused, space_ids=self.ws.copy_ids_of(tl))
            self._tl[tl.identifier] = tl
        return Obs(spaces=[self._as_space(w) for w in self.ws.list()], windows=windows,
                   outputs=frozenset(self.ws.output_names()), blind=bool(getattr(self.ws, "blind", False)))

    def _space(self, index):
        return next((w for w in self.ws.list() if w.index == index), None)

    @staticmethod
    def _as_space(w) -> Space:
        return Space(index=w.index, ids=frozenset(w.all_ids), pinned=w.pinned, active=w.active, name=w.name or "",
                     copies_pinned=w.copies_pinned, output=getattr(w, "monitor", "") or "")

    def move(self, win_id, space, output):
        tl, ws = self._tl.get(win_id), self._space(space)
        return tl is not None and ws is not None and bool(self.ws.move_toplevel(tl, ws, output))

    def move_space(self, index, before):
        a, b = self._space(index), self._space(before)
        if a is None or b is None or a.coords == b.coords or "move" not in self.ws.caps:
            return False
        if getattr(a, "monitor", "") != getattr(b, "monitor", ""):
            return False
        self.ws.move(a, b, before=True)
        return True

    def pin_new_space(self, want=()):
        ws = self.ws.create("Workspace", want=tuple(want))  # (spare_workspace False: Hyprland, niri, sway)
        return self._as_space(ws) if ws is not None and ws.all_ids else None

    def pin(self, index):
        ws = self._space(index)
        if ws is None:
            return None
        if not (ws.pinned and ws.copies_pinned):
            self.ws.pin(ws, True)
            self.ws.settle()
            ws = next((w for w in self.ws.list() if w.coords == ws.coords), ws)
        return self._as_space(ws) if ws.all_ids else None

    def unpin(self, index):
        ws = self._space(index)
        if ws is not None:
            self.ws.pin(ws, False)

    def activate(self, index):
        ws = self._space(index)
        if ws is not None:
            self.ws.activate(ws)

    def launch(self, spec):
        self.launched.append(spec)
        if self.on_launch is not None:
            self.on_launch(spec)
        return 0


class Sim:
    def __init__(self, client, layout: Layout | None = None, on_launch=None):
        self.client = client
        self.act = Act(client, on_launch)
        self.t = 1000.0
        self.log: list[tuple] = []
        self.layout = layout or Layout()
        self.engine = Engine(self.layout, self.act, ctx=ad.Ctx(resume_command=lambda s, cwd, agent=None: f"resume {s}"),
                             journal=lambda kind, **f: self.log.append((kind, f)), now=self.t)

    def step(self):
        time.sleep(0.005)
        self.client.pump()
        self.engine.step(self.act.observe(self.engine), self.t)

    def advance(self, secs: float, dt: float = 0.5):
        end = self.t + secs
        while self.t < end:
            self.t += dt
            self.step()

    def journal(self, kind: str) -> list[dict]:
        return [f for k, f in self.log if k == kind]
