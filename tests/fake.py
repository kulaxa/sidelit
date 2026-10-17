"""A desktop in memory: the compositor's side of the engine, for tests.

It behaves like cosmic-comp where it matters:
- workspaces are shared by index across screens, but every screen keeps its own *copy* of
  each index, pinned (with an id of its own) or not;
- one trailing empty workspace, which becomes a real one when pinned; empty workspaces with
  no pinned copy disappear and the ones after them shift down;
- unplugging a screen appends that screen's copies (pinned ones, and ones with windows) as
  new workspaces on the remaining screen, with their windows; plugging it back in takes
  whatever of those is still there back to their old places, and gives every other index a
  fresh unpinned copy on the returning screen;
- window identifiers are never reused.
"""
from __future__ import annotations

import dataclasses
import itertools

from sidelit.core.model import Obs, Space, Win

_ids = itertools.count(1)


def _new_id() -> str:
    return f"ws{next(_ids)}"


class FakeSpace:
    """One workspace index: a copy per screen, {"id": str | None, "pinned": bool}."""

    def __init__(self, outputs, pinned_on=(), origin=None):
        self.copies = {o: {"id": _new_id() if o in pinned_on else None, "pinned": o in pinned_on} for o in outputs}
        self.origin = origin  # (screen, index) this was appended from when that screen went

    @property
    def ids(self) -> frozenset:
        return frozenset(c["id"] for c in self.copies.values() if c["id"])

    @property
    def pinned(self) -> bool:
        return any(c["pinned"] for c in self.copies.values())

    @property
    def all_pinned(self) -> bool:
        return all(c["pinned"] for c in self.copies.values())

    def pin(self):
        for c in self.copies.values():
            c["pinned"] = True
            c["id"] = c["id"] or _new_id()

    def unpin(self):
        for c in self.copies.values():
            c["pinned"] = False
            c["id"] = None

    # tests read spaces like the old dicts
    def __getitem__(self, key):
        return {"ids": self.ids, "pinned": self.pinned}[key]

    def update(self, pinned=None, ids=None):
        if pinned is False:
            self.unpin()
        elif pinned:
            self.pin()


class FakeDesktop:
    def __init__(self, spaces: int = 3, outputs=("eDP-1", "HDMI-A-1"), pinned_on=("eDP-1",)):
        self.outputs = set(outputs)
        self.order = list(outputs)
        # cosmic pins a workspace on one screen: the other screens' copies start unpinned
        self.spaces = [FakeSpace(self.order, pinned_on=pinned_on) for _ in range(spaces)]
        self.spaces.append(FakeSpace(self.order))  # trailing empty
        self.active = 1
        self.windows: dict[str, dict] = {}
        self.claude: dict[str, dict] = {}
        self.browser: dict[str, dict] = {}   # window id -> what the Chrome extension says about it
        self.extensions: set[str] = set()     # extension instances connected (they report their windows)
        self.reports: dict | None = None      # instance -> window ids, while a test holds a report back
        self.launched: list[dict] = []
        self.moves: list[tuple] = []
        self.activations: list[int] = []
        self.pins: list[int] = []
        self.moved_spaces: list[tuple] = []   # (index, before) workspace reorders asked for
        self.refuse_moves = False
        self.spawn = False   # launches open their window (terminals), like the real desktop
        self._last_obs = None
        self.focused_at: dict[str, float] = {}   # what the compositor says about focus as it happens

    # ---- things that happen on the desktop
    def open(self, app: str, title: str, space: int, output: str | None = "eDP-1", claude: dict | None = None) -> str:
        wid = f"win{next(_ids)}"
        self.windows[wid] = {"app": app, "title": title, "space": space, "output": output}
        if claude:
            self.claude[wid] = claude
        self._trailing()
        return wid

    def close(self, wid: str, by_you: bool = True):
        """You close it: the window you close has the keyboard (the engine's last look at it
        shows it focused). crash() is a window going on its own."""
        if by_you and self._last_obs is not None and wid in self._last_obs.windows:
            self._last_obs.windows[wid] = dataclasses.replace(self._last_obs.windows[wid], focused=True)
        self.windows.pop(wid, None)
        self.browser.pop(wid, None)
        self.claude.pop(wid, None)
        self._gc()

    def crash(self, wid: str):
        self.close(wid, by_you=False)

    def user_move(self, wid: str, space: int, output: str | None = None):
        self.windows[wid]["space"] = space
        if output:
            self.windows[wid]["output"] = output
        self._trailing()

    def index_of(self, compositor_id: str) -> int:
        return next(i + 1 for i, s in enumerate(self.spaces) if compositor_id in s.ids)

    def unplug(self, out: str):
        """The screen goes (cable out, lid shut, the monitor asleep)."""
        self.outputs.discard(out)
        self.order.remove(out)
        rest = self.order[0]
        appended = []
        for i, s in enumerate(self.spaces, start=1):
            copy = s.copies.pop(out, None)
            wins = [w for w in self.windows.values() if w["space"] == i and w["output"] == out]
            if copy is None or not (copy["pinned"] or wins):
                continue
            extra = FakeSpace([])
            extra.copies = {rest: copy}
            extra.origin = (out, i)
            appended.append((extra, wins))
        for extra, wins in appended:
            self.spaces.insert(len(self.spaces) - 1, extra)
            idx = len(self.spaces) - 1
            for w in wins:
                w.update(space=idx, output=rest)
        self._gc()

    def replug(self, out: str):
        self.outputs.add(out)
        self.order.append(out)
        back = [(i, s) for i, s in enumerate(self.spaces, start=1) if s.origin and s.origin[0] == out]
        homes = {}
        for i, s in back:
            home = min(s.origin[1], len(self.spaces) - len(back))
            homes[i] = (home, next(iter(s.copies.values())))
        for s in self.spaces:
            s.copies[out] = {"id": None, "pinned": False}
        for i, (home, copy) in homes.items():
            for w in self.windows.values():
                if w["space"] == i:
                    w.update(space=home, output=out)
        for i in sorted(homes, reverse=True):
            self.spaces.pop(i - 1)
            for w in self.windows.values():
                if w["space"] > i:
                    w["space"] -= 1
        for home, copy in homes.values():
            self.spaces[home - 1].copies[out] = copy
        self._gc()

    def _trailing(self):
        """COSMIC keeps exactly one empty workspace at the end."""
        last = self.spaces[-1]
        if last.pinned or any(w["space"] == len(self.spaces) for w in self.windows.values()):
            self.spaces.append(FakeSpace(self.order))

    def _gc(self):
        """Workspaces with no pinned copy and no windows (other than the trailing one and the
        active one) go away, and the ones after them shift down."""
        i = 1
        while i < len(self.spaces):
            s = self.spaces[i - 1]
            if not s.pinned and i != self.active and not any(w["space"] == i for w in self.windows.values()):
                self.spaces.pop(i - 1)
                for w in self.windows.values():
                    if w["space"] > i:
                        w["space"] -= 1
                if self.active > i:
                    self.active -= 1
                continue
            i += 1
        self._trailing()

    def _copy_ids(self, w: dict) -> frozenset:
        """The id of the copy a window is on: its screen's copy of its workspace."""
        if not 1 <= w["space"] <= len(self.spaces):
            return frozenset()
        copy = self.spaces[w["space"] - 1].copies.get(w["output"]) or {}
        return frozenset([copy["id"]]) if copy.get("id") else frozenset()

    def drop_copy(self, index: int, out: str):
        """One screen's list hears before the other's that workspace `index` went: its later
        copies (and their windows) shift down a place while the other screen's stay put, so
        for a while each index holds copies of two different workspaces."""
        copies = [s.copies.get(out) for s in self.spaces]
        copies.pop(index - 1)
        copies.append({"id": None, "pinned": False})
        for s, c in zip(self.spaces, copies):
            s.copies[out] = c
        for w in self.windows.values():
            if w["output"] == out and w["space"] > index:
                w["space"] -= 1
        self._gc()

    def reshuffle(self, index: int, to: int):
        """COSMIC puts workspace `index` (both screens' copies, and its windows) at `to` by itself."""
        FakeDesktop.move_space(self, index, to)
        self.moved_spaces.pop()

    def _space(self, i: int) -> Space:
        s = self.spaces[i - 1]
        return Space(index=i, ids=s.ids, pinned=s.pinned, active=(i == self.active), copies_pinned=s.all_pinned)

    # ---- the observation
    def obs(self) -> Obs:
        spaces = [self._space(i + 1) for i in range(len(self.spaces))]
        wins = {wid: Win(id=wid, app=w["app"], title=w["title"], space=w["space"], output=w["output"],
                         space_ids=self._copy_ids(w)) for wid, w in self.windows.items()}
        browser = {w: b for w, b in self.browser.items() if w in self.windows}
        reported = self.reports if self.reports is not None else \
            {inst: {b["window"] for b in browser.values() if b.get("instance") == inst} for inst in self.extensions}
        self._last_obs = Obs(spaces=spaces, windows=wins, outputs=frozenset(self.outputs), claude=dict(self.claude),
                             browser=browser, browser_reported=reported, focused_at=dict(self.focused_at))
        return self._last_obs

    # ---- the actuator
    def move(self, win_id, space, output):
        self.moves.append((win_id, space, output))
        if self.refuse_moves or win_id not in self.windows or not 1 <= space <= len(self.spaces):
            return True
        self.windows[win_id]["space"] = space
        if output and output in self.outputs:
            self.windows[win_id]["output"] = output
        self._gc()
        return True

    def move_space(self, index, before):
        """Put workspace `index` right before workspace `before` (COSMIC moves every screen's copy)."""
        self.moved_spaces.append((index, before))
        n = len(self.spaces)
        if not (1 <= index <= n and 1 <= before <= n) or index == before:
            return False
        order = list(range(1, n + 1))
        order.remove(index)
        order.insert(order.index(before) if before != n + 1 else len(order), index)
        new_of = {old: new for new, old in enumerate(order, start=1)}
        self.spaces = [self.spaces[old - 1] for old in order]
        for w in self.windows.values():
            w["space"] = new_of.get(w["space"], w["space"])
        self.active = new_of.get(self.active, self.active)
        self._trailing()
        return True

    def pin_new_space(self):
        idx = len(self.spaces)
        self.spaces[-1].pin()
        self._trailing()
        return self._space(idx)

    def pin(self, space):
        self.pins.append(space)
        self.spaces[space - 1].pin()
        self._trailing()
        return self._space(space)

    def unpin(self, space):
        if 1 <= space <= len(self.spaces):
            self.spaces[space - 1].unpin()
            self._gc()

    def activate(self, space):
        self.activations.append(space)
        self.active = space
        self._gc()  # the empty workspace you left goes

    def launch(self, spec):
        self.launched.append(spec)
        if self.spawn and spec.get("type") == "terminal":
            # a real terminal opens its window where you are; a resumed Claude shows its session
            cmd = spec.get("command") or ""
            sess = cmd.split("--resume ", 1)[1].split()[0] if "--resume " in cmd else None
            self.open("terminator", "✳ resumed", self.active, claude={"session": sess, "title": "resumed",
                                                                       "status": "idle", "cwd": spec.get("cwd")}
                      if sess else None)
        return 1000 + len(self.launched)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def run(engine, desk: FakeDesktop, clock: Clock, seconds: float, step: float = 0.5):
    """Advance time, stepping the engine like the daemon does."""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t = round(clock.t + step, 3)
        engine.step(desk.obs(), clock.t)
