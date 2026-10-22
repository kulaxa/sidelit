"""The engine: keeps the Layout true to what you do, and the desktop true to the Layout.

Every step it gets a fresh observation of the desktop and does two things:

Recorder: what changed since the last step, and was it you?
    The desktop is either *calm* or *disturbed*. Disturbances are the things that move
    windows without you asking: the daemon starting, a login restore, a screen plugged in
    or out, waking from suspend, many windows closing at once. While calm, a change is
    your intent and the layout follows it (you moved a window: its slot moves; you closed
    one: its slot is deleted once the close has stood for DELETE_GRACE_S). While
    disturbed, a change is drift and the layout stays as it is.

Reconciler: where does the desktop differ from the layout?
    A window bound to a slot that is not on its slot's workspace (or screen) is moved
    there. It runs in every state, calm or not: a change you made is already in the
    layout by the time the reconciler looks, and what it moves it moves because the
    layout says so (a sidebar command, or drift being undone). Moves it makes are
    remembered as expectations, so the recorder does not mistake them for you.

Relauncher: the layout is what the desktop should be, so a window that went without you
    closing it (an app crashed, the compositor restarted, something killed it) is started
    again once the desktop is calm, quietly and one at a time, backing off if it keeps dying.
    A window you closed is forgotten instead; a tray app's window you closed stays closed.
    An app that ended cleanly did not crash: where the crash reporter, the exit status of what
    we started, or a browser's own record of how it last quit says so, its windows count as
    closed, not crashed.

Restores (after a login or a compositor restart, or when you ask) start everything missing
the same way, one at a time, each launch waiting for its window. Nothing is ever closed
that you did not close.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Protocol

from sidelit.core import adapters as ad
from sidelit.core.model import Layout, Obs, Slot, Space, Win, Workspace, new_id

DELETE_GRACE_S = 20.0     # a closed window's slot is deleted once the close has stood this long
MASS_GRACE_S = 120.0      # ...this long when it closed along with many others (a logout, probably)
MASS_WINDOW_S = 3.0       # "many": MASS_MIN windows of two or more apps within this many seconds
MASS_MIN = 3
APP_EXIT_S = 0.6          # every window of an app gone within this: the app quit or crashed
STARTUP_S = 10.0
OUTPUTS_S = 30.0
OUTPUT_SETTLE_S = 8.0     # windows are only moved once the set of screens has stood this long
SPACES_SETTLE_S = 2.0     # ...and the workspaces (their indices and ids) this long: a close or a replug
                          # reaches each screen's list on its own, and for a moment they disagree
MIXED_HOLD_S = 10.0       # ...this long while an index holds copies of two kept workspaces
ALIAS_SETTLE_S = 10.0     # ids are learnt (and leftovers dropped) once the workspaces stood still this long
ORDER_KEEP_S = 120.0      # this long after a screen change, a workspace order you did not set is put back
ORDER_TRIES = 8           # reorders per disturbance before leaving the compositor's order be
PIN_RETRY_S = 30.0
RESUME_S = 30.0
ADOPT_AFTER_S = 1.5       # a new window is kept once it has been up this long (titles settle)
EXPECT_S = 4.0            # how long a move we made may take to show up
MOVE_TRIES = 4            # per window per disturbance: then leave it where the compositor keeps it
LAUNCH_WAIT_S = 15.0      # a launched slot's window has this long to appear
TEMPLATE_WAIT_S = 12.0
LEAVE_WAIT_S = 10.0       # a workspace you closed and are on: left once its windows are gone, if they go this soon
RESTORE_APP_GRACE_S = 45.0
RESTORE_MAX_S = 300.0
MAX_MISSED = 3            # a slot that failed to come back this many restores in a row is dropped
NO_REGISTER_S = 15.0
EMPTY_DROP_S = 5.0        # a kept workspace you emptied is let go this long after its last slot went
MAX_IDS = 8
CLOSE_FOCUS_S = 5.0       # a window that had the keyboard this recently and goes: you closed it
BROWSER_SAYS_S = 3.0      # a browser window that went without it: the extension has this long to say it closed it
BROWSER_QUIT_S = 5.0      # browser windows that went this soon before its extension did: the browser quit, not you
BROWSER_WENT = "the browser quit or restarted"
BROWSER_GONE_S = 15.0    # after the browser went, this long for it to bring its windows back itself (Relaunch)
RELAUNCH_AFTER_S = 5.0     # a window gone (not closed by you) this long, while calm, is started again
RELAUNCH_BACKOFF_S = (0.0, 30.0, 120.0)   # wait before the 1st, 2nd, 3rd relaunch of the same window...
RELAUNCH_WINDOW_S = 600.0  # ...counted over this long; then it is left missing
EXIT_SAYS_S = 3.0         # a window gone without the keyboard: this long for how its app ended to be on record
QUIT_GAP_S = 1.0          # a browser's windows going this close after one another: it was quit with them open
CLOSED_KEEP_S = 900.0     # windows you closed on a workspace are part of its history entry this long


def _same_app(app: str, exe: str | None, ended: str) -> bool:
    """Is `ended` (an executable's file name) the app `app`, whose desktop entry runs `exe`?
    com.system76.CosmicSettings is cosmic-settings, org.gnome.Nautilus nautilus."""
    def norm(x):
        return re.sub(r"[^a-z0-9]", "", x.lower())
    e = norm(ended)
    return bool(e) and e in (norm(app), norm(app.rsplit(".", 1)[-1]), norm(exe or ""))


class Actuator(Protocol):
    def move(self, win_id: str, space: int, output: str | None) -> bool: ...
    def pin_new_space(self, want: tuple = ()) -> Space | None: ...  # want: the ids it had (the desktop may bring one back)
    def pin(self, space: int) -> Space | None: ...
    def unpin(self, space: int) -> None: ...
    def activate(self, space: int) -> None: ...
    def launch(self, spec: dict) -> int | None: ...   # pid; 0 = started, pid unknown; None = failed


@dataclass
class Launch:
    """A window we are expecting because we started something."""
    t: float
    ws_id: str
    slot_id: str | None          # None: whatever appears becomes a new slot on ws_id
    deadline: float
    accepts: Callable[[Win], bool]
    pid: int | None = None
    what: str = ""
    done: bool = False
    restore: bool = False
    holds: Callable[[Win], bool] | None = None   # a window it may still turn into: kept for it meanwhile
    state: dict = field(default_factory=dict)    # the new slot starts with this (e.g. the command it ran)


@dataclass
class Restore:
    started: float
    reason: str
    delay: float
    scope: set | None = None      # only these workspaces (reopening one), None = everything
    claudes_only: bool = False    # config restore_windows = false
    back_to: str = ""
    planned: bool = False
    queue: list = field(default_factory=list)          # slot ids, started one at a time
    apps: dict = field(default_factory=dict)           # app id -> slot ids, started once
    apps_started: set = field(default_factory=set)
    apps_launched: set = field(default_factory=set)
    current: Launch | None = None
    log: list = field(default_factory=list)
    finished: bool = False
    planned_slots: list = field(default_factory=list)  # every slot it set out to bring back (progress)
    finished_t: float = 0.0
    slots: set | None = None      # only these slots (relaunching crashed windows)
    quiet: bool = False           # no disturbance, no workspace switching: the desktop is in use


@dataclass
class _Close:
    t: float
    win: str
    app: str
    slot: str
    browser: str = ""   # the extension instance the window was paired with


class Engine:
    def __init__(self, layout: Layout, actuator: Actuator, ctx: ad.Ctx | None = None,
                 journal: Callable[..., None] | None = None, now: float = 0.0, startup_s: float = STARTUP_S):
        self.layout = layout
        self.act = actuator
        self.ctx = ctx or ad.Ctx()
        self.ctx.on_session_ended = self._session_ended
        self._journal = journal or (lambda kind, **f: None)
        self.bind: dict[str, str] = {}               # window id -> slot id
        self.prev: Obs | None = None
        self.obs: Obs | None = None
        self.disturbed_until = 0.0
        self.disturb_reason = ""
        self.disturb_started = now
        self._was_disturbed = False
        self.first_seen: dict[str, float] = {}
        self._initial: set[str] | None = None        # windows already up when we started
        self._placed: dict[str, tuple] = {}           # window id -> (workspace key, output) last seen
        self.pending_delete: dict[str, float] = {}   # slot id -> when the deletion is committed
        self._closes: list[_Close] = []
        self.expect: dict[str, tuple] = {}            # window id -> (space index, output, deadline)
        self.move_tries: dict[str, int] = {}
        self.launches: list[Launch] = []
        self.restore: Restore | None = None
        self.no_register: dict[int, float] = {}
        self.primary: dict[str, Space] = {}           # layout workspace id -> the live workspace
        self.owner: dict[int, str] = {}               # live workspace index -> layout workspace id
        self.extras: list[tuple[Space, str]] = []     # further copies of a layout workspace
        self.dirty = False                            # layout changed: save it
        self.changed = False                          # anything the sidebar shows changed
        self._pinned_this_step = False
        self._unpinned: dict[int, float] = {}
        self._gave_up: set[str] = set()
        self._no_primary_since: dict[str, float] = {}
        self._empty_since: dict[str, float] = {}
        self.launched_pid: dict[str, int] = {}       # window id -> pid of what we started for it
        self._all_return = False                      # 'put back': every unbound window may be matched anywhere
        self.absorb_until = 0.0                       # until then, take bound windows as they are (after migrating)
        self._absorbed: set[str] = set()
        self.outputs_changed_at = float("-inf")
        self._pin_tried: dict[int, float] = {}
        self._closed_on: dict[str, list[tuple[float, Slot]]] = {}  # workspace id -> slots you closed there
        self.relaunch = True                          # start again what went without you closing it
        self._missing_since: dict[str, float] = {}
        self._relaunched: dict[str, list[float]] = {}  # slot id -> when we relaunched it
        self._closed_by_you: set[str] = set()          # tray apps' windows you closed: not reopened
        self._gave_up_relaunch: set[str] = set()
        self._last_focus: dict[str, float] = {}         # window id -> last time it had the keyboard
        self._paired: dict[str, tuple] = {}             # window id -> (extension instance, its window id)
        self._asking_browser: dict[str, tuple] = {}     # slot id -> (instance, its window id, when it went)
        self._asking_exit: dict[str, tuple] = {}        # slot id -> (app, pids, when it went, with its app's others)
        self._browser_quits: dict[str, dict] = {}       # extension instance gone -> {"t", "closes": {slot: when}}
        self._browser_closed_it: set[str] = set()       # slots whose window the browser closed while it ran on
        self._leaving: tuple | None = None              # (index of the workspace you closed while on it, until)
        self._space_map: tuple | None = None
        self.spaces_changed_at = float("-inf")
        self._mixed = False                              # an index holds copies of two kept workspaces
        self._retired: dict[str, float] = {}             # ids of workspaces you closed: never learnt again
        self._closing: dict[int, tuple[int, float]] = {}  # index you closed -> (workspaces then, until when)
        self._order_tries = 0
        self._order_wait = 0.0
        self.obs_time = now
        if startup_s:
            self.disturb(startup_s, "startup", now)
        self._forget_system_windows(now)

    # ---- small helpers ------------------------------------------------------------------
    def journal(self, kind: str, **fields):
        self._journal(kind, **fields)

    def say(self, msg: str):
        print(msg, file=sys.stderr, flush=True)

    def calm(self, now: float) -> bool:
        return now >= self.disturbed_until

    def disturb(self, seconds: float, reason: str, now: float):
        if self.calm(now):
            self.disturb_started = now
            self.move_tries.clear()
            self._gave_up.clear()
            self.journal("disturbed", reason=reason, seconds=seconds)
        until = now + seconds
        if until > self.disturbed_until:
            self.disturbed_until = until
            self.disturb_reason = reason
        self._was_disturbed = True
        self.changed = True

    def slot_window(self, slot_id: str) -> str | None:
        return next((w for w, s in self.bind.items() if s == slot_id), None)

    @property
    def bound_slots(self) -> set[str]:
        return set(self.bind.values())

    def _key(self, w: Win) -> tuple:
        owner = self.owner.get(w.space) if w.space is not None else None
        return ("ws", owner) if owner else ("space", w.space)

    def _mark(self):
        self.dirty = True
        self.changed = True

    # ---- binding --------------------------------------------------------------------------
    def _bind(self, win: Win, slot: Slot, now: float, why: str):
        old = self.slot_window(slot.id)
        if old and old != win.id:
            self.bind.pop(old, None)
        if slot.state.get("more") and slot.window != win.id:
            # a new window for a slot whose window showed several Claudes: it is the first
            # one's; the others keep places of their own, to come back in the next restore
            lw, _ = self.layout.find(slot.id)
            if lw is not None:
                self._split_claudes(lw, slot, now)
        self.bind[win.id] = slot.id
        slot.window = win.id
        slot.missed = 0
        slot.updated = now
        self.pending_delete.pop(slot.id, None)
        self._missing_since.pop(slot.id, None)
        self._closed_by_you.discard(slot.id)
        self._browser_closed_it.discard(slot.id)
        self._gave_up_relaunch.discard(slot.id)
        self._placed.pop(win.id, None)  # its placement is the slot's now: no edge from before
        adapter = ad.adapter_of(slot)
        adapter.capture(slot, win, self.ctx, self.calm(now), now)
        self._dedupe_session(slot)
        self._mark()
        self.journal("bound", slot=slot.id, window=win.id, what=adapter.describe(slot), why=why)

    def _dedupe_session(self, slot: Slot):
        """One Claude session, one slot: a missing slot for a session that is now showing
        elsewhere is superseded (else a restore would resume it twice)."""
        sessions = {c["session"] for c in slot.claudes}
        if not sessions:
            return
        bound = self.bound_slots
        for ws, other in list(self.layout.slots()):
            if other is slot or other.id in bound or not any(c["session"] in sessions for c in other.claudes):
                continue
            left = [c for c in other.claudes if c["session"] not in sessions]
            if not left:
                self.layout.remove_slot(other.id)
                self.pending_delete.pop(other.id, None)
                self.journal("slot_deleted", slot=other.id, why="same Claude session now in another window",
                             what=ad.adapter_of(other).describe(other))
                continue
            # it also kept Claudes that are not open anywhere: it stays for them, the first of
            # them now its own
            first, more = left[0], left[1:]
            other.state.update(session=first["session"], claude_title=first.get("claude_title") or "")
            if first.get("agent"):
                other.state["agent"] = first["agent"]
            else:
                other.state.pop("agent", None)
            if first.get("cwd"):
                other.state["cwd"] = first["cwd"]
            if more:
                other.state["more"] = more
            else:
                other.state.pop("more", None)
            self._mark()

    def _new_slot(self, win: Win, lw: Workspace, adapter: ad.Adapter, now: float, why: str) -> Slot:
        slot = Slot(id=new_id("s"), kind=adapter.name, app=win.app, title=win.title[:200],
                    output=win.output or "", created=now, updated=now)
        lw.slots.append(slot)
        self.journal("slot_created", slot=slot.id, ws=lw.id, why=why)
        self._bind(win, slot, now, why)
        return slot

    def _delete_slot(self, slot_id: str, why: str, now: float):
        lw, slot = self.layout.find(slot_id)
        if slot is None:
            return
        self.layout.remove_slot(slot_id)
        self.layout.remember_recent(slot, lw, now)
        if lw is not None and why == "closed":
            kept = [(t, s) for t, s in self._closed_on.get(lw.id, []) if now - t < CLOSED_KEEP_S]
            self._closed_on[lw.id] = kept + [(now, slot)]
        self.pending_delete.pop(slot_id, None)
        for w in [w for w, s in self.bind.items() if s == slot_id]:
            self.bind.pop(w, None)
        self._mark()
        self.journal("slot_deleted", slot=slot_id, ws=lw.id if lw else None,
                     what=ad.adapter_of(slot).describe(slot), why=why)

    def _forget_system_windows(self, now: float):
        """Windows of the desktop itself (adapters.IGNORED_APPS) kept by a Sidelit from before it
        knew them, such as Hyprland's update pop-up: not kept, nor started again at a login."""
        for _, slot in list(self.layout.slots()):
            if slot.app and slot.app in ad.IGNORED_APPS:
                self._delete_slot(slot.id, "a window of the desktop itself", now)

    def _session_ended(self, slot: Slot, ended: list[dict] | None = None):
        """Claudes that ended in a window that stays (all of its, or `ended`): to the recent list."""
        lw, _ = self.layout.find(slot.id)
        self.layout.remember_recent(slot, lw, self.obs_time, ended)
        for c in slot.claudes if ended is None else ended:
            self.journal("claude_ended", slot=slot.id, session=c["session"])

    # ---- the step --------------------------------------------------------------------------
    def step(self, obs: Obs, now: float):
        self.obs_time = now
        self.obs = obs
        self.ctx.claude = obs.claude
        self.ctx.cwd = obs.cwd
        self.ctx.browser = obs.browser
        self._pinned_this_step = False
        prev = self.prev
        if prev is not None and prev.outputs != obs.outputs:
            self.outputs_changed_at = now
            self.journal("screens", frm=sorted(prev.outputs), to=sorted(obs.outputs))
            self.disturb(OUTPUTS_S, f"screens {sorted(prev.outputs)} -> {sorted(obs.outputs)}", now)
        if obs.blind:
            # no screen on: nothing is where it seems, so the layout stays and nothing is started
            # again (it would open on no screen) until one is back
            self.disturb(OUTPUTS_S, "no screen on", now)
        if prev is not None:
            for inst in prev.browser_reported.keys() - obs.browser_reported.keys():
                self._browser_went(inst, now)
        for wid, w in obs.windows.items():
            self.first_seen.setdefault(wid, now)
            if w.focused:
                self._last_focus[wid] = now
        for wid, t in obs.focused_at.items():
            if t > self._last_focus.get(wid, float("-inf")):
                self._last_focus[wid] = t
        for wid, b in obs.browser.items():
            if b.get("instance") and b.get("window") is not None:
                self._paired[wid] = (b["instance"], b["window"])
        if self._initial is None:
            self._initial = set(obs.windows)
        space_map = tuple((s.index, s.ids) for s in obs.spaces)
        if space_map != self._space_map:
            self._space_map = space_map
            self.spaces_changed_at = now
        owner_of = {i: lw.id for lw in self.layout.workspaces for i in lw.ids}
        self._mixed = any(len({owner_of[i] for i in s.ids if i in owner_of}) > 1 for s in obs.spaces)
        self._resolve_spaces(obs)
        calm = self.calm(now)

        self._closed(prev, obs, now, calm)
        self._browser_says(obs, now)
        self._how_it_ended(now)
        self._browser_quit_settled(now)
        self._bursts(now)
        self._commit_deletes(now)
        self._rebind_by_identifier(obs, now)
        self._placements(obs, now, calm)
        self._claim_launches(obs, now)
        self._bind_matches(obs, now, calm)
        self._capture(obs, now, calm)
        if now < self.absorb_until:
            self._absorb(obs, now)

        calm = self.calm(now)  # a burst may have just disturbed us
        if calm:
            if self._was_disturbed:
                self._was_disturbed = False
                self._pair_leftovers(obs, now)
                self._all_return = False
                self.journal("calm")
                self.changed = True
            self._register_spaces(obs, now)
            self._pin_copies(now)
            self._adopt(obs, now)
            self._learn_aliases(now)  # (after adopting: a window goes by whose copy it was on before)
            self._drop_emptied(obs, now)
            self._record_workspaces(obs, now)

        self._leave_closed(obs, now)
        self._reconcile(obs, now)
        if self.calm(now):
            self._relaunch_missing(obs, now)
        self._restore_step(obs, now)
        self.launches = [l for l in self.launches if not l.done and now < l.deadline + 1]
        self.prev = obs

    # ---- spaces ---------------------------------------------------------------------------
    def _resolve_spaces(self, obs: Obs):
        """Which live workspace is each layout workspace. A live one can carry the ids of
        several copies (COSMIC keeps a copy per screen, and an unplugged screen's copies are
        appended as extra workspaces); the lowest index wins, the rest are extras whose
        windows go home."""
        primary, owner, extras = {}, {}, []
        taken: set[int] = set()
        for lw in self.layout.workspaces:
            ids = set(lw.ids)
            cands = sorted((s for s in obs.spaces if s.ids & ids), key=lambda s: s.index)
            free = [s for s in cands if s.index not in taken]
            if not free:
                continue
            primary[lw.id] = free[0]
            owner[free[0].index] = lw.id
            taken.add(free[0].index)
            for s in free[1:]:
                extras.append((s, lw.id))
        self.primary, self.owner, self.extras = primary, owner, extras

    def _learn_aliases(self, now: float):
        """While calm, every id a layout workspace's live copy carries names it too: after the
        screen that brought an id is unplugged, that id may be the only one left.

        Only once screens and workspaces have stood still: while a close or a replug reaches each
        screen's list on its own, one index briefly holds copies of two different workspaces, and
        an id learnt then names the wrong one for good. For the same reason, an id that another
        workspace with a live copy of its own also carries is a leftover of such a mix-up, and
        that workspace lets go of it. A workspace you closed is never learnt back."""
        if not self._screens_settled(now) or now - self.spaces_changed_at < ALIAS_SETTLE_S:
            return
        for lw_id, space in self.primary.items():
            lw = self.layout.ws(lw_id)
            for other in self.layout.workspaces:
                stale = [i for i in other.ids if i in space.ids]
                if other is lw or not stale or other.id not in self.primary:
                    continue
                other.ids = [i for i in other.ids if i not in stale]
                self.dirty = True
                self.journal("ids_dropped", ws=other.id, ids=stale, kept_by=lw.id,
                             why=f"workspace {space.index} is the other one's")
            if any(self.layout.owner_of_id(i) not in (None, lw) for i in space.ids):
                continue  # also claimed by a workspace with no copy of its own: not ours to name
            new = [i for i in sorted(space.ids) if i not in lw.ids and self.layout.owner_of_id(i) is None
                   and i not in self._retired]
            if new:
                lw.ids = (lw.ids + new)
                if len(lw.ids) > MAX_IDS:
                    lw.ids = lw.ids[:1] + lw.ids[-(MAX_IDS - 1):]
                self.dirty = True

    def _ensure_space(self, lw: Workspace, now: float) -> Space | None:
        """A live workspace for `lw`, pinning a fresh one if it has none (one per step)."""
        if lw.id in self.primary:
            return self.primary[lw.id]
        if self._pinned_this_step:
            return None
        self._pinned_this_step = True
        space = self.act.pin_new_space(want=tuple(lw.ids))
        if space is None or not space.ids:
            self.say(f"engine: could not get a workspace for {lw.name or lw.id}")
            return None
        # (newest last, as ids are learnt: a desktop that brings a workspace back under an id it
        # had goes by the last one)
        lw.ids = [i for i in lw.ids if i not in space.ids] + \
                 [i for i in space.ids if self.layout.owner_of_id(i) in (None, lw)]
        self.primary[lw.id] = space
        self.owner[space.index] = lw.id
        self._mark()
        self.journal("space_pinned", ws=lw.id, index=space.index, ids=sorted(space.ids))
        return space

    # ---- recorder: closes ----------------------------------------------------------------
    def _closed(self, prev: Obs | None, obs: Obs, now: float, calm: bool):
        if prev is None:
            return
        for wid in prev.windows.keys() - obs.windows.keys():
            self.first_seen.pop(wid, None)
            focused_at = self._last_focus.pop(wid, float("-inf"))
            paired = self._paired.pop(wid, None)
            pids = {p for p in (self.launched_pid.pop(wid, None), prev.windows[wid].pid) if p}
            self.expect.pop(wid, None)
            self._placed.pop(wid, None)
            self.move_tries.pop(wid, None)
            sid = self.bind.pop(wid, None)
            if sid is None:
                continue
            lw, slot = self.layout.find(sid)
            if slot is None:
                continue
            self.changed = True
            self._missing_since[sid] = now
            app = prev.windows[wid].app
            self._closes.append(_Close(now, wid, app, sid, paired[0] if paired else ""))
            if paired and paired[0] in self._browser_quits:
                self._browser_quits[paired[0]]["closes"].setdefault(sid, now)
            adapter = ad.adapter_of(slot)
            # (a browser quitting says nothing about other apps' windows: they are judged as ever)
            if not calm and not (self.disturb_reason == BROWSER_WENT and
                                 not (paired and paired[0] in self._browser_quits)):
                if paired:
                    self.journal("unbound", slot=sid, what=adapter.describe(slot),
                                 why=f"closed while {self.disturb_reason}")
                else:
                    # nothing says you closed it, but one that ended cleanly is not reopened now
                    self._asking_exit[sid] = (app, pids, now, True)
                continue
            if adapter.keep_on_close:
                self._closed_by_you.add(sid)
                self.journal("unbound", slot=sid, what=adapter.describe(slot), why="closed; kept, the app lives on in the tray")
                continue
            if not any(w.app == app for w in obs.windows.values()) and \
                    sum(1 for c in self._closes if c.app == app and now - c.t <= APP_EXIT_S) >= 2:
                # the app's last windows went together: it quit or crashed, nobody closed them
                for c in self._closes:
                    if c.app == app and now - c.t <= APP_EXIT_S:
                        self.pending_delete.pop(c.slot, None)
                        if not paired and c.slot != sid:
                            asked = self._asking_exit.get(c.slot, (app, set(), c.t))
                            self._asking_exit[c.slot] = (*asked[:3], True)
                if paired:  # its extension going says how (_browser_went)
                    self.journal("unbound", slot=sid, what=adapter.describe(slot), why=f"{app} quit or crashed")
                else:
                    self._asking_exit[sid] = (app, pids, now, True)
                continue
            last = prev.windows[wid]
            if not last.focused and now - focused_at > CLOSE_FOCUS_S:
                if paired and paired[0] in obs.browser_reported:
                    # the browser is still running, so it did not crash: its extension says in a
                    # moment whether the browser closed the window itself (only you make it do that)
                    self._asking_browser[sid] = (*paired, now)
                    continue
                # you close the window you are working in; one that went while you were elsewhere
                # crashed or was killed (a terminal is a process of its own: it goes alone), unless
                # its app ended cleanly (closed from the overview, or while the sidebar had the keyboard)
                self._asking_exit[sid] = (app, pids, now, False)
                continue
            self.pending_delete[sid] = now + DELETE_GRACE_S
            self.journal("closed", slot=sid, what=adapter.describe(slot),
                         why=f"closed by you; forgotten in {int(DELETE_GRACE_S)} s")

    def _browser_went(self, inst: str, now: float):
        """The browser's extension is gone: the browser quit, crashed or restarts (its Relaunch
        button). Closing its windows one by one, it hands each the keyboard as the one before
        goes, so they looked closed by you: they were not. A restarting browser brings them back
        itself, on the workspace you are on: they are recognised by their tabs and sent home
        before anything is started again."""
        quit = self._browser_quits.setdefault(inst, {"t": now, "closes": {}})
        for c in self._closes:
            if c.browser == inst and now - c.t <= BROWSER_QUIT_S:
                quit["closes"].setdefault(c.slot, c.t)
            if c.browser == inst and now - c.t <= BROWSER_QUIT_S and c.slot in self.pending_delete:
                del self.pending_delete[c.slot]
                _, slot = self.layout.find(c.slot)
                if slot is not None:
                    self.journal("unbound", slot=c.slot, what=ad.adapter_of(slot).describe(slot),
                                 why="the browser quit or restarted, it did not close it for you: it comes back")
        self.disturb(BROWSER_GONE_S, BROWSER_WENT, now)

    def _taken_as_crash(self, sid: str, what: str):
        self.journal("unbound", slot=sid, what=what,
                     why="went while it did not have the keyboard: taken as a crash, it comes back")

    def _browser_says(self, obs: Obs, now: float):
        """A browser window that went without the keyboard (closed from the overview, say, or while
        the sidebar had it). The browser never drops a window by itself, so a report from its
        extension without the window means you closed it; no such report in time (the browser
        went too, or still lists it) and it is taken as a crash."""
        bound = self.bound_slots
        for sid, (inst, ext, t) in list(self._asking_browser.items()):
            _, slot = self.layout.find(sid)
            if slot is None or sid in bound:
                del self._asking_browser[sid]
                continue
            reported = obs.browser_reported.get(inst)
            if reported is not None and ext not in reported:
                del self._asking_browser[sid]
                self._browser_closed_it.add(sid)
                self.pending_delete[sid] = now + DELETE_GRACE_S
                self.journal("closed", slot=sid, what=ad.adapter_of(slot).describe(slot),
                             why=f"closed by you (the browser closed it and is still running); "
                                 f"forgotten in {int(DELETE_GRACE_S)} s")
            elif inst in self._browser_quits:
                del self._asking_browser[sid]  # the browser went: how it ended decides (_browser_quit_settled)
            elif reported is None or now - t > BROWSER_SAYS_S:
                del self._asking_browser[sid]
                self._taken_as_crash(sid, ad.adapter_of(slot).describe(slot))

    def _how_it_ended(self, now: float):
        """A window that went without the keyboard, or with the rest of its app: did it crash, or
        did its app end cleanly? A clean end is you closing it (from the overview, from the panel,
        while the sidebar had the keyboard) or quitting the app: it is not started again."""
        bound = self.bound_slots
        for sid, (app, pids, t, together) in list(self._asking_exit.items()):
            _, slot = self.layout.find(sid)
            if slot is None or sid in bound:
                del self._asking_exit[sid]
                continue
            if now - t < EXIT_SAYS_S:
                continue
            del self._asking_exit[sid]
            what = ad.adapter_of(slot).describe(slot)
            how, by = self._ended(app, pids, t, together)
            if how == "crashed":
                self.journal("unbound", slot=sid, what=what, why=f"{by} crashed: it comes back")
            elif how is None:
                if together:
                    self.journal("unbound", slot=sid, what=what, why=f"{app} quit or crashed")
                else:
                    self._taken_as_crash(sid, what)
            elif together:
                self._closed_by_you.add(sid)
                self.journal("unbound", slot=sid, what=what,
                             why=f"{app} quit ({by}): kept, but not reopened before the next login")
            else:
                self.pending_delete[sid] = max(now, t + DELETE_GRACE_S)
                self.journal("closed", slot=sid, what=what,
                             why=f"closed by you ({by}); forgotten in {int(max(0, t + DELETE_GRACE_S - now))} s")

    def _ended(self, app: str, pids: set, t: float, together: bool) -> tuple[str | None, str]:
        """How `app` ended around `t`: ("crashed" | "clean", how we know), or (None, "") when
        nothing can tell."""
        if self.ctx.endings is None:
            return None, ""
        ended, watched = self.ctx.endings(t - 2 * EXIT_SAYS_S)
        exe = self.ctx.app_exe(app) if self.ctx.app_exe else None
        its = [e for e in ended if e.pid in pids or (e.exe and _same_app(app, exe, e.exe))]
        # a crash the reporter could not put a name to, just as the window went, is taken as its;
        # all of an app's windows going with anything crashing (Xwayland, say) is that crash
        crashed = [e for e in its if e.crashed] + \
                  [e for e in ended if e.crashed and (together or not e.exe) and e not in its]
        if crashed:
            return "crashed", crashed[0].exe or app
        if its:
            return "clean", "it exited by itself"
        if watched:
            return "clean", "it ended without crashing"
        return None, ""

    def _browser_quit_settled(self, now: float):
        """The browser went and did not bring its windows back (a Relaunch does, within
        BROWSER_GONE_S): how it ended says what its windows are. Quit cleanly (closing its last
        window quits it), the ones you closed one by one stay closed, and the ones it closed as
        it quit are kept but not reopened. Crashed, or no telling: they come back."""
        bound = self.bound_slots
        for inst, quit in list(self._browser_quits.items()):
            if now - quit["t"] < BROWSER_GONE_S:
                continue
            del self._browser_quits[inst]
            how = self.ctx.browser_exit(inst) if self.ctx.browser_exit else None
            gone = sorted((t, sid) for sid, t in quit["closes"].items()
                          if sid not in bound and self.layout.find(sid)[1] is not None)
            # the ones it said it closed while it still ran were yours whatever came after
            unsure = [(t, sid) for t, sid in gone if sid not in self._browser_closed_it]
            for t, sid in gone:
                slot = self.layout.find(sid)[1]
                what = ad.adapter_of(slot).describe(slot)
                self._asking_browser.pop(sid, None)
                if how is None:
                    self.journal("unbound", slot=sid, what=what,
                                 why="the browser went without a word on how it ended: taken as a crash, it comes back")
                elif how != "normal":
                    self.journal("unbound", slot=sid, what=what,
                                 why="the browser did not quit cleanly (it crashed, or is running again): it comes back")
                elif sid not in self._browser_closed_it and \
                        any(other != sid and abs(u - t) <= QUIT_GAP_S for u, other in unsure):
                    self._closed_by_you.add(sid)
                    self.journal("unbound", slot=sid, what=what,
                                 why="the browser was quit with it open: kept, but not reopened before the next login")
                else:
                    self.pending_delete[sid] = max(now, t + DELETE_GRACE_S)
                    self.journal("closed", slot=sid, what=what,
                                 why="closed by you (the browser then quit cleanly, it did not crash)")

    def _bursts(self, now: float):
        self._closes = [c for c in self._closes if now - c.t <= 10.0]
        recent = [c for c in self._closes if now - c.t <= MASS_WINDOW_S]
        if len(recent) >= MASS_MIN and len({c.app for c in recent}) >= 2:
            held = [c.slot for c in recent if c.slot in self.pending_delete and
                    self.pending_delete[c.slot] < now + MASS_GRACE_S - 1]
            for sid in held:
                # a logout closes apps one by one; if we are still alive in two minutes it was you
                self.pending_delete[sid] = now + MASS_GRACE_S
            if held:
                self.journal("mass_close", slots=held, why=f"{len(recent)} windows closed within "
                             f"{int(MASS_WINDOW_S)} s: held for {int(MASS_GRACE_S)} s in case the session is ending")
            self.disturb(10.0, "many windows closing", now)

    def _commit_deletes(self, now: float):
        for sid, when in list(self.pending_delete.items()):
            if now >= when:
                self._delete_slot(sid, "closed", now)

    def drop_pending(self):
        """Shutting down: a close that has not stood long enough is not remembered."""
        if self.pending_delete:
            self.journal("shutdown", kept=list(self.pending_delete),
                         why="closes not yet committed are kept (the session may be ending)")
        self.pending_delete.clear()

    # ---- recorder: binding ----------------------------------------------------------------
    def _rebind_by_identifier(self, obs: Obs, now: float):
        """Window identifiers are unique for the whole compositor session, so a slot that
        remembers one gets it back after a daemon restart without any guessing."""
        bound = self.bound_slots
        by_window = {s.window: s for _, s in self.layout.slots() if s.window and s.id not in bound}
        for wid, win in obs.windows.items():
            if wid in self.bind:
                continue
            slot = by_window.get(wid)
            if slot is not None:
                self._bind(win, slot, now, "same window as before")

    def _claim_launches(self, obs: Obs, now: float):
        for launch in sorted(self.launches, key=lambda l: l.t):
            if launch.done or now > launch.deadline:
                continue
            cands = [w for w in obs.windows.values() if w.id not in self.bind and
                     self.first_seen.get(w.id, now) >= launch.t - 0.5 and w.title and launch.accepts(w)]
            if not cands:
                continue
            by_pid = [w for w in cands if launch.pid and w.pid == launch.pid]
            win = (by_pid or sorted(cands, key=lambda w: self.first_seen.get(w.id, now)))[0]
            launch.done = True
            if launch.pid:
                self.launched_pid[win.id] = launch.pid
            lw = self.layout.ws(launch.ws_id)
            if lw is None:
                continue
            if launch.slot_id:
                _, slot = self.layout.find(launch.slot_id)
                if slot is not None:
                    self._bind(win, slot, now, f"started by us ({launch.what})")
                    continue
            adapter = ad.adapter_for(win, self.ctx) or ad.BY_NAME["generic"]
            slot = self._new_slot(win, lw, adapter, now, f"started by us ({launch.what or win.app})")
            if launch.state:
                slot.state.update(launch.state)

    def _candidates_for_matching(self, obs: Obs, calm: bool) -> list[Win]:
        out = []
        for w in obs.windows.values():
            if w.id in self.bind or not w.title or w.space is None:
                continue
            if any(not l.done and l.accepts(w) and self.first_seen.get(w.id, 0) >= l.t - 0.5 for l in self.launches) \
                    or self._held(w):
                continue  # a launch is waiting for this one
            out.append(w)
        return out

    def _held(self, win: Win) -> bool:
        """A window a waiting launch may still turn into: the blank window a browser opens as
        it starts, which the extension fills with the tabs of the window being reopened."""
        return any(not l.done and l.holds is not None and l.holds(win)
                   and self.first_seen.get(win.id, 0) >= l.t - 0.5 for l in self.launches)

    def _returning(self, wid: str) -> bool:
        """May this window be matched to a slot on another workspace and moved there? Only
        windows that appeared during the current disturbance: a window that was already up
        and unbound before (a scratch window) is not somebody coming back."""
        if self._all_return:
            return True
        seen = self.first_seen.get(wid, 0.0)
        if self._initial is not None and wid in self._initial:
            return self.restore is not None and not self.restore.finished and self.restore.scope is None \
                and not self.restore.quiet
        return seen >= self.disturb_started

    def _bind_matches(self, obs: Obs, now: float, calm: bool):
        """Confident matches between unbound windows and missing slots. While calm only on the
        window's own workspace (a window you just opened is never moved away); while disturbed
        anywhere, for windows coming back."""
        bound = self.bound_slots
        missing = [(lw, s) for lw, s in self.layout.slots() if s.id not in bound]
        if not missing:
            return
        for win in self._candidates_for_matching(obs, calm):
            here = self.owner.get(win.space)
            anywhere = not calm and self._returning(win.id)
            scored = []
            for lw, slot in missing:
                if slot.id in self.bound_slots:
                    continue
                adapter = ad.adapter_of(slot)
                # a tray app's window coming back goes home, whenever it comes back
                if not anywhere and lw.id != here and not adapter.keep_on_close:
                    continue
                if not adapter.claims(win, self.ctx) and win.app != slot.app:
                    continue
                score = adapter.score(win, slot, self.ctx)
                if score >= 2.0:
                    scored.append((score, lw.id == here, slot.updated, slot))
            if not scored:
                continue
            scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            if len(scored) > 1 and scored[0][:2] == scored[1][:2] and scored[0][0] < 3.0:
                continue  # two equally good homes: wait for more to go on (a title change)
            self._bind(win, scored[0][3], now, f"recognised (score {scored[0][0]:.1f})")

    def _pair_leftovers(self, obs: Obs, now: float):
        """The disturbance is over: pair the windows that came back without a confident match
        with the missing slots of the same app, best title match first. A window with nothing
        in common with any slot is only paired when every missing slot of its app is on one
        workspace (then there is only one place it can belong)."""
        bound = self.bound_slots
        wins = [w for w in obs.windows.values() if w.id not in self.bind and w.title and w.space is not None
                and self._returning(w.id)]
        slots = [(lw, s) for lw, s in self.layout.slots() if s.id not in bound]
        for app in sorted({w.app for w in wins}):
            aw = [w for w in wins if w.app == app]
            asl = [(lw, s) for lw, s in slots if s.app == app]
            if not asl:
                continue
            pairs = []
            for w in aw:
                for lw, s in asl:
                    score = ad.adapter_of(s).score(w, s, self.ctx)
                    pairs.append((score, self.owner.get(w.space) == lw.id, s.updated, w, s))
            pairs.sort(key=lambda p: (p[0], p[1], p[2]), reverse=True)
            one_home = len({lw.id for lw, _ in asl}) == 1
            used_w, used_s = set(), set()
            for score, _, _, w, s in pairs:
                if w.id in used_w or s.id in used_s:
                    continue
                if score <= 0 and (not one_home or not ad.adapter_of(s).pairable(s)):
                    continue
                used_w.add(w.id)
                used_s.add(s.id)
                self._bind(w, s, now, f"paired after {self.disturb_reason or 'the disturbance'} (score {score:.1f})")

    def _capture(self, obs: Obs, now: float, calm: bool):
        for wid, sid in list(self.bind.items()):
            win = obs.windows.get(wid)
            if win is None:
                continue
            lw, slot = self.layout.find(sid)
            if slot is None:
                self.bind.pop(wid, None)
                continue
            before = slot.session
            if ad.adapter_of(slot).capture(slot, win, self.ctx, calm, now):
                slot.updated = now
                self._mark()
                if slot.session and slot.session != before:
                    self._dedupe_session(slot)
            if slot.session and slot.state.get("cwd") and lw.claude is slot and lw.cwd != slot.state["cwd"]:
                lw.cwd = slot.state["cwd"]
                self._mark()

    # ---- recorder: placement ---------------------------------------------------------------
    def _placements(self, obs: Obs, now: float, calm: bool):
        """Edges in where bound windows are: ours (expected), yours (calm) or drift."""
        for wid, win in obs.windows.items():
            if win.space is None:
                continue
            key = self._key(win)
            last = self._placed.get(wid)
            out = win.output or (last[1] if last else None)
            self._placed[wid] = (key, out, win.space_ids)
            sid = self.bind.get(wid)
            if last is None or sid is None:
                continue
            ws_moved = key != last[0]
            if ws_moved and win.space_ids & last[2]:
                ws_moved = False  # still on the same copy: the workspaces around it changed, it did not move
            out_moved = bool(win.output) and bool(last[1]) and win.output != last[1]
            if not (ws_moved or out_moved):
                continue
            exp = self.expect.get(wid)
            if exp is not None and now <= exp[2]:
                if win.space == exp[0] and (not exp[1] or win.output == exp[1]):
                    self.expect.pop(wid, None)
                continue  # our move, or on its way
            if not calm:
                continue  # drift: the reconciler puts it back
            lw, slot = self.layout.find(sid)
            if slot is None:
                continue
            adapter = ad.adapter_of(slot)
            if ws_moved:
                target = self.owner.get(win.space)
                if target is None and not self._is_extra(win.space):
                    # moved onto a workspace of its own (COSMIC's empty one at the end): keep that one too
                    fresh = self.register_space(win.space, now, why="you moved a window there")
                    target = fresh.id if fresh else None
                if target is None:
                    self._delete_slot(sid, "moved by you to a workspace that is not kept", now)
                    continue
                if target != lw.id:
                    lw.slots.remove(slot)
                    self.layout.ws(target).slots.append(slot)
                    self.journal("moved", slot=sid, what=adapter.describe(slot), to=target, why="moved by you")
                    self._mark()
            if out_moved and slot.output != win.output:
                self.journal("moved", slot=sid, what=adapter.describe(slot), to=win.output, why="moved to another screen by you")
                slot.output = win.output
                self._mark()

    def _absorb(self, obs: Obs, now: float):
        """The layout we just migrated was a snapshot: where the bound windows are now is
        where they belong. A window on a workspace we do not keep is let go. (Each window
        once its workspace is known: the compositor reports that a moment after connecting.)"""
        for wid, sid in list(self.bind.items()):
            win = obs.windows.get(wid)
            if wid in self._absorbed or win is None or win.space is None:
                continue
            self._absorbed.add(wid)
            lw, slot = self.layout.find(sid)
            if slot is None:
                continue
            owner = self.owner.get(win.space)
            what = ad.adapter_of(slot).describe(slot)
            if owner is None:
                self._delete_slot(sid, "migrated: on a workspace that is not kept", now)
                continue
            if owner != lw.id:
                lw.slots.remove(slot)
                self.layout.ws(owner).slots.append(slot)
                self.journal("absorbed", slot=sid, what=what, to=owner, why="migrated: kept where it is now")
                self._mark()
            if win.output and slot.output != win.output:
                slot.output = win.output
                self._mark()

    def closed_empty(self, space: Space, now: float) -> bool:
        """A workspace you closed whose windows have all gone: the desktop drops it in a moment
        (COSMIC takes seconds), and until then it is not one to show. Not once a window is on it
        again, or it was kept again, or it is still there a while later (it stays, then).
        By index, as unpinned it has no ids (COSMIC); once the count of workspaces changes,
        it has gone or the others moved, and the index is no longer its."""
        if self.obs is None:
            return False
        count = len(self.obs.spaces)
        self._closing = {i: (n, t) for i, (n, t) in self._closing.items() if t > now and n == count}
        return space.index in self._closing and space.index not in self.owner and not self.obs.windows_on(space.index)

    # ---- recorder: new windows and workspaces (calm only) -----------------------------------
    def _is_extra(self, index: int | None) -> bool:
        """A further copy of a kept workspace (what COSMIC appends when a screen is unplugged)."""
        return any(sp.index == index for sp, _ in self.extras)

    def _register_spaces(self, obs: Obs, now: float):
        """A workspace you work on is kept: pinned and added to the layout. One with a Claude
        at once, any other once a window we keep has been up on it for a moment. Never an
        extra copy of a kept workspace: its windows are on their way home."""
        if not self._spaces_settled(now):
            return
        for space in obs.spaces:
            if space.index in self.owner or self.no_register.get(space.index, 0) > now or self._is_extra(space.index):
                continue
            wins = obs.windows_on(space.index)
            claude = next((obs.claude[w.id] for w in wins if w.id in obs.claude), None)
            if claude is not None:
                self.register_space(space.index, now, cwd=claude.get("cwd") or "", why="a Claude runs there")
                continue
            if any(w.title and now - self.first_seen.get(w.id, now) >= ADOPT_AFTER_S and ad.adapter_for(w, self.ctx)
                   and not any(not l.done and l.accepts(w) for l in self.launches) and not self._held(w) for w in wins):
                self.register_space(space.index, now, why="you have windows open there")

    def _pin_copies(self, now: float):
        """Every screen's copy of a kept workspace is pinned, and its id learnt (while calm).
        COSMIC pins per screen: with only one screen's copy pinned, unplugging that screen
        makes its copy -- appended to the other screen -- the only one carrying our ids, and
        the workspace would follow it there, windows and all."""
        if not self._screens_settled(now):
            return
        for lw_id, space in list(self.primary.items()):
            if space.copies_pinned or now - self._pin_tried.get(space.index, float("-inf")) < PIN_RETRY_S:
                continue
            self._pin_tried[space.index] = now
            fresh = self.act.pin(space.index)
            self.journal("pinned_copies", ws=lw_id, index=space.index,
                         ids=sorted(fresh.ids) if fresh is not None else None,
                         why="every screen's copy is pinned, so the workspace survives either screen going")

    def _screens_settled(self, now: float) -> bool:
        return now - self.outputs_changed_at >= OUTPUT_SETTLE_S

    def _spaces_settled(self, now: float) -> bool:
        """Can windows be placed by which workspace is at which index? Not while a close or a
        replug is still reaching each screen's list: for a moment an index holds copies of two
        different workspaces (given a while longer, in case one screen is slow to hear)."""
        quiet = now - self.spaces_changed_at
        return quiet >= SPACES_SETTLE_S and (not self._mixed or quiet >= MIXED_HOLD_S)

    def _drop_emptied(self, obs: Obs, now: float):
        """A kept workspace you emptied goes, the way COSMIC drops an empty one. Not one you
        named, and not one whose windows are only missing (an app crashed, a screen went):
        those come back."""
        for lw in list(self.layout.workspaces):
            space = self.primary.get(lw.id)
            empty = not lw.slots and not lw.name and (space is None or not obs.windows_on(space.index))
            if not empty:
                self._empty_since.pop(lw.id, None)
                continue
            since = self._empty_since.setdefault(lw.id, now)
            if now - since >= EMPTY_DROP_S:
                self._empty_since.pop(lw.id, None)
                self.remove_workspace(lw.id, now, "you closed everything on it")

    def register_space(self, index: int, now: float, cwd: str = "", name: str = "", why: str = "") -> Workspace | None:
        if index in self.owner:
            return self.layout.ws(self.owner[index])
        space = self.obs.space(index) if self.obs else None
        if space is None:
            return None
        if not space.pinned or not space.ids:
            space = self.act.pin(index)
            if space is None or not space.ids:
                self.say(f"engine: could not pin workspace {index}")
                return None
        lw = Workspace(id=new_id("w"), ids=sorted(i for i in space.ids if self.layout.owner_of_id(i) is None),
                       name=name, cwd=cwd, created=now)
        if not lw.ids:
            return None
        # keep the layout in the compositor's order
        after = [self.layout.ws(self.owner[i]) for i in sorted(self.owner) if i < index]
        pos = self.layout.workspaces.index(after[-1]) + 1 if after else 0
        self.layout.workspaces.insert(pos, lw)
        self.primary[lw.id] = space
        self.owner[space.index] = lw.id
        self._mark()
        self.journal("ws_created", ws=lw.id, index=index, why=why)
        return lw

    def _copy_owner(self, win: Win) -> str | None:
        """The kept workspace whose copy the window is on. Usually the one at its index, but not
        always: COSMIC slots a returning screen's copies back by position, so if the order changed
        while the screen was away, one index holds copies of two different workspaces."""
        return next((lw.id for i in sorted(win.space_ids) for lw in [self.layout.owner_of_id(i)] if lw), None)

    def _adopt(self, obs: Obs, now: float):
        if not self._spaces_settled(now):
            return
        for win in obs.windows.values():
            if win.id in self.bind or not win.title or win.space is None:
                continue
            # a window that was on its copy before the workspaces changed belongs to that copy's
            # workspace; one you opened since, to the one you saw at its index
            was_there = self.first_seen.get(win.id, now) < self.spaces_changed_at
            lw_id = (self._copy_owner(win) if was_there else None) or self.owner.get(win.space)
            if lw_id is None or now - self.first_seen.get(win.id, now) < ADOPT_AFTER_S:
                continue
            if any(not l.done and l.accepts(win) for l in self.launches) or self._held(win):
                continue
            adapter = ad.adapter_for(win, self.ctx)
            if adapter is None:
                continue
            self._new_slot(win, self.layout.ws(lw_id), adapter, now, "opened by you")

    def _record_workspaces(self, obs: Obs, now: float):
        active = next((s for s in obs.spaces if s.active), None)
        if active is not None and active.index in self.owner:
            ws_id = self.owner[active.index]
            if self.layout.active != ws_id and (self.restore is None or self.restore.finished):
                self.layout.active = ws_id
                self._mark()
        # the order you put workspaces in (bound ones; closed ones keep their place). Not soon after
        # a screen change: the compositor reshuffles them then by itself, and _restore_order puts
        # them back instead
        if now - self.outputs_changed_at < ORDER_KEEP_S or not self._spaces_settled(now):
            return
        live = [lw for lw in self.layout.workspaces if lw.id in self.primary]
        wanted = sorted(live, key=lambda lw: self.primary[lw.id].index)
        if [w.id for w in live] != [w.id for w in wanted]:
            slots = [i for i, lw in enumerate(self.layout.workspaces) if lw.id in self.primary]
            for i, lw in zip(slots, wanted):
                self.layout.workspaces[i] = lw
            self._mark()

    # ---- reconciler ------------------------------------------------------------------------
    def _reconcile(self, obs: Obs, now: float):
        if not self._screens_settled(now):
            # a screen just came or went: the compositor is still shuffling workspaces between
            # screens, and a screen that went for a moment (the monitor asleep on the lock
            # screen, a wobbly cable) puts everything back itself when it returns
            return
        if not self._spaces_settled(now):
            # a workspace just went or came: each screen's list hears of it on its own, and until
            # both have, an index can hold copies of two different workspaces
            return
        if self._restore_order(now):
            return  # windows go home once the workspaces stand where they belong
        needs_space: list[Workspace] = []
        bound = self.bound_slots
        for wid, sid in list(self.bind.items()):
            win = obs.windows.get(wid)
            if win is None or win.space is None:
                continue
            lw, slot = self.layout.find(sid)
            if slot is None:
                continue
            target = self.primary.get(lw.id)
            if target is None:
                # (a workspace can vanish for a moment while screens change: give it a few seconds)
                since = self._no_primary_since.setdefault(lw.id, now)
                if now - since >= 5.0 and lw not in needs_space:
                    needs_space.append(lw)
                continue
            self._no_primary_since.pop(lw.id, None)
            want_out = slot.output if slot.output and slot.output in obs.outputs else None
            if target.output:
                want_out = self._home_screen(lw, target, obs, bound)
            wrong_ws = win.space != target.index
            wrong_out = bool(want_out) and bool(win.output) and win.output != want_out
            if not (wrong_ws or wrong_out):
                self.move_tries.pop(wid, None)
                continue
            exp = self.expect.get(wid)
            if exp is not None and now <= exp[2]:
                continue
            tries = self.move_tries.get(wid, 0)
            if tries >= MOVE_TRIES:
                if wid not in self._gave_up:
                    self._gave_up.add(wid)
                    self.journal("gave_up", slot=sid, what=ad.adapter_of(slot).describe(slot),
                                 why=f"the compositor kept it on workspace {win.space} after {tries} moves")
                continue
            self.move_tries[wid] = tries + 1
            self.expect[wid] = (target.index, want_out, now + EXPECT_S)
            ok = self.act.move(wid, target.index, want_out)
            self.journal("move", slot=sid, what=ad.adapter_of(slot).describe(slot),
                         frm=f"{win.space}@{win.output}", to=f"{target.index}@{want_out or win.output}",
                         why=self.disturb_reason if not self.calm(now) else "layout", ok=ok)
        for lw in needs_space:
            self._ensure_space(lw, now)
        # extra copies of a kept workspace (a screen's workspaces appended after it was unplugged):
        # once their windows have gone home they are unpinned, and COSMIC drops them when empty
        for space, lw_id in self.extras:
            if not space.pinned or obs.windows_on(space.index) or space.active:
                continue
            if now - self._unpinned.get(space.index, 0) < 30:
                continue
            self._unpinned[space.index] = now
            self.act.unpin(space.index)
            self.journal("unpinned", index=space.index, ws=lw_id, why="leftover copy of a kept workspace")

    def _home_screen(self, lw: Workspace, space: Space, obs: Obs, bound: set) -> str | None:
        """The screen a workspace belongs on, where each workspace is on one screen (Hyprland,
        niri, sway): the one most of its open windows were kept on (or, with none open, after a
        login, most of its slots), of those plugged in. Its windows go there together. A window
        not open says nothing once others are: the recorder follows only open ones when you move
        a workspace. On a tie, where it is now: moving it to each window's screen in turn would
        never end."""
        slots = [s for s in lw.slots if s.id in bound] or lw.slots
        votes = Counter(s.output for s in slots if s.output and s.output in obs.outputs)
        if not votes:
            return None
        most = max(votes.values())
        tops = sorted(o for o, n in votes.items() if n == most)
        if space.output in tops:
            return space.output
        return tops[0] if len(tops) == 1 else None

    def _restore_order(self, now: float) -> bool:
        """Soon after a screen change, put the workspaces back in the layout's order: COSMIC keeps
        a list per screen, and an unplug or replug can leave them in another order than the one
        you set. One move at a time; True while one is on its way."""
        move_space = getattr(self.act, "move_space", None)
        if move_space is None or now - self.outputs_changed_at >= ORDER_KEEP_S:
            self._order_tries = 0
            return False
        if now < self._order_wait:
            return True
        live = [lw_id for lw_id, _ in sorted(self.primary.items(), key=lambda kv: kv[1].index)]
        wanted = [lw.id for lw in self.layout.workspaces if lw.id in self.primary]
        if live == wanted or self._order_tries >= ORDER_TRIES:
            return False
        # where each workspace is on one screen (Hyprland, niri), each screen's own order: a
        # workspace is never moved to another screen to stand in line
        pair = None
        for screen in sorted({self.primary[i].output for i in live}):
            here_live = [i for i in live if self.primary[i].output == screen]
            here_wanted = [i for i in wanted if self.primary[i].output == screen]
            k = next((n for n, (a, b) in enumerate(zip(here_live, here_wanted)) if a != b), None)
            if k is not None:
                pair = (here_wanted[k], here_live[k])
                break
        if pair is None:
            return False
        src, dst = self.primary[pair[0]].index, self.primary[pair[1]].index
        self._order_tries += 1
        self._order_wait = now + 1.0  # (the move changes the workspaces: SPACES_SETTLE_S holds us longer)
        ok = move_space(src, dst)
        self.journal("reordered", ws=pair[0], frm=src, to=dst, why=self.disturb_reason or "screens changed", ok=ok)
        if not ok:
            self._order_tries = ORDER_TRIES  # this desktop cannot reorder: leave it be
            return False
        if self._order_tries >= ORDER_TRIES:
            self.journal("order_gave_up", why=f"the compositor kept its own order after {ORDER_TRIES} moves")
        return True

    # ---- relauncher ------------------------------------------------------------------------
    def _relaunch_missing(self, obs: Obs, now: float):
        """Start again what went without you closing it (see the module docstring)."""
        if not self.relaunch or (self.restore is not None and not self.restore.finished):
            return
        if not self._screens_settled(now) or now < self.absorb_until:
            return
        bound = self.bound_slots
        due = []
        for lw, slot in self.layout.slots():
            if slot.id in bound or slot.id in self.pending_delete or slot.id in self._closed_by_you \
                    or slot.id in self._asking_browser or slot.id in self._asking_exit \
                    or any(slot.id in q["closes"] for q in self._browser_quits.values()):
                continue
            since = self._missing_since.setdefault(slot.id, now)
            if now - since < RELAUNCH_AFTER_S or slot.id in self._gave_up_relaunch:
                continue
            adapter = ad.adapter_of(slot)
            if adapter.mode(slot, self.ctx) == "none" or adapter.launch(slot, self.ctx) is None:
                continue  # nothing to start it with (a terminal in an unknown directory)
            if adapter.mode(slot, self.ctx) == "app" and any(w.app == slot.app for w in obs.windows.values()):
                continue  # the app is running: it brings its own windows back (or keeps them in the tray)
            tries = [t for t in self._relaunched.get(slot.id, []) if now - t < RELAUNCH_WINDOW_S]
            self._relaunched[slot.id] = tries
            if len(tries) >= len(RELAUNCH_BACKOFF_S):
                self._gave_up_relaunch.add(slot.id)
                self.journal("relaunch_gave_up", slot=slot.id, what=adapter.describe(slot),
                             why=f"it went again after {len(tries)} relaunches in {int(RELAUNCH_WINDOW_S / 60)} minutes;"
                                 " left missing (reopen it from the sidebar)")
                continue
            if tries and now - tries[-1] < RELAUNCH_BACKOFF_S[len(tries)]:
                continue
            due.append(slot)
        if not due:
            return
        for slot in due:
            self._relaunched.setdefault(slot.id, []).append(now)
        self.journal("relaunch", slots=[s.id for s in due], what=[ad.adapter_of(s).describe(s) for s in due],
                     why="gone without you closing them (a crash, a restart, killed)")
        self.start_restore(now, "relaunch", delay=0, slots={s.id for s in due}, quiet=True)

    # ---- restore ------------------------------------------------------------------------
    def start_restore(self, now: float, reason: str, delay: float = 6.0, scope: set | None = None,
                      claudes_only: bool = False, slots: set | None = None, quiet: bool = False):
        if self.restore is not None and not self.restore.finished:
            return False
        self.restore = Restore(started=now, reason=reason, delay=delay, scope=scope, back_to=self.layout.active,
                               claudes_only=claudes_only, slots=slots, quiet=quiet)
        if scope is None and not quiet:
            self.disturb(delay + 15.0, "restoring", now)
        self.journal("restore_started", reason=reason, scope=sorted(scope) if scope else None)
        self.changed = True
        return True

    def _split_claudes(self, lw: Workspace, slot: Slot, now: float) -> list[Slot]:
        """A window that showed several Claudes (split panes, tabs) comes back as a terminal
        for each: the slot keeps the first, and each of the others gets a slot of its own
        beside it. One open somewhere already is not resumed twice."""
        live = {c["session"] for _, s in self.layout.slots() if s.id in self.bound_slots for c in s.claudes}
        added = []
        at = lw.slots.index(slot) + 1
        for c in slot.state.pop("more", []):
            if not isinstance(c, dict) or not c.get("session") or c["session"] in live:
                continue
            new = Slot(id=new_id("s"), kind=slot.kind, app=slot.app, title=c.get("claude_title") or "",
                       output=slot.output, state={k: v for k, v in c.items() if v}, created=now, updated=now)
            lw.slots.insert(at + len(added), new)
            added.append(new)
        self._mark()
        self.journal("slots_split", slot=slot.id, into=[s.id for s in added],
                     why="its window showed several Claudes: each comes back in a terminal of its own")
        return added

    def _restore_log(self, msg: str):
        r = self.restore
        r.log.append(msg)
        self.say("restore: " + msg)
        self.changed = True

    def _restore_step(self, obs: Obs, now: float):
        r = self.restore
        if r is None or r.finished:
            return
        if r.scope is None and not r.quiet:
            self.disturb(10.0, "restoring", now)
        t = now - r.started
        if t < r.delay:
            return
        bound = self.bound_slots
        if not r.planned:
            r.planned = True
            for lw, slot in list(self.layout.slots()):
                if slot.state.get("more") and slot.id not in bound and (r.scope is None or lw.id in r.scope) \
                        and (r.slots is None or slot.id in r.slots):
                    added = self._split_claudes(lw, slot, now)
                    if r.slots is not None:
                        r.slots |= {s.id for s in added}
            for lw, slot in self.layout.slots():
                if slot.id in bound or (r.scope is not None and lw.id not in r.scope):
                    continue
                if r.slots is not None and slot.id not in r.slots:
                    continue
                if r.claudes_only and not slot.session:
                    continue
                adapter = ad.adapter_of(slot)
                mode = adapter.mode(slot, self.ctx)
                if mode == "slot":
                    r.queue.append(slot.id)
                elif mode == "app":
                    r.apps.setdefault(slot.app, []).append(slot.id)
            r.planned_slots = list(r.queue) + [sid for sids in r.apps.values() for sid in sids]
            order = {s.id: i for i, (_, s) in enumerate(self.layout.slots())}
            r.queue.sort(key=lambda sid: (ad.adapter_of(self.layout.find(sid)[1]).launch_after, order[sid]))
            self.journal("restore_plan", slots=list(r.queue), apps=sorted(r.apps))

        # apps that restore their own windows: started once, if not already running
        for app, sids in r.apps.items():
            if app in r.apps_started:
                continue
            lw, slot = next(((lw, s) for lw, s in (self.layout.find(i) for i in sids) if s is not None), (None, None))
            if slot is None:
                r.apps_started.add(app)
                continue
            adapter = ad.adapter_of(slot)
            if t < r.delay + adapter.launch_after:
                continue
            r.apps_started.add(app)
            if any(w.app == app for w in obs.windows.values()):
                continue  # running already, bringing its windows back itself
            spec = adapter.launch(slot, self.ctx)
            if spec is None:
                continue
            space = self._ensure_space(lw, now)
            if space is not None and not r.quiet:
                self.act.activate(space.index)
            pid = self.act.launch(spec)  # None: failed; 0: started, pid unknown
            if pid is not None:
                r.apps_launched.add(app)
            self._restore_log(f"started {app.rsplit('.', 1)[-1]}" + ("" if pid is not None else " (failed)"))
            self.journal("launch", app=app, spec=spec, ok=pid is not None)

        # one slot at a time, each waiting for its window
        cur = r.current
        if cur is not None and not cur.done and cur.slot_id in bound:
            cur.done = True  # it is back (recognised by what it shows before the launch claimed it)
        if cur is not None and not cur.done and now < cur.deadline:
            return
        if cur is not None and not cur.done:
            self._restore_log(f"no window for {cur.what}")
            cur.done = True
            if r.reason == "relaunch" and cur.slot_id:
                # started, and nothing came on our screens: it went somewhere we do not see (a
                # browser on another display opens it there). Again would only make copies.
                self._gave_up_relaunch.add(cur.slot_id)
                self.journal("relaunch_gave_up", slot=cur.slot_id, what=cur.what,
                             why="relaunched, and no window came; left missing (reopen it from the sidebar)")
        r.current = None
        while r.queue:
            sid = r.queue[0]
            lw, slot = self.layout.find(sid)
            if slot is None or sid in bound:
                r.queue.pop(0)
                continue
            adapter = ad.adapter_of(slot)
            if t < r.delay + adapter.launch_after:
                break
            spec = adapter.launch(slot, self.ctx)
            if spec is None:
                r.queue.pop(0)
                continue
            space = self._ensure_space(lw, now)
            if space is None:
                return  # a workspace is being made; next step
            r.queue.pop(0)
            if not r.quiet:  # (quietly, it opens where you are and is moved home)
                self.act.activate(space.index)
            pid = self.act.launch(spec)
            what = adapter.describe(slot)
            self.journal("launch", slot=sid, what=what, spec=spec, ok=pid is not None)
            if pid is None:
                self._restore_log(f"could not start {what}")
                continue
            launch = Launch(t=now, ws_id=lw.id, slot_id=sid, deadline=now + LAUNCH_WAIT_S,
                            accepts=lambda w, a=adapter, s=slot: a.accepts_launch(s, w, self.ctx), pid=pid,
                            what=what, restore=True,
                            holds=lambda w, a=adapter, s=slot: a.may_become_launch(s, w, self.ctx))
            self.launches.append(launch)
            r.current = launch
            self._restore_log(f"started {what}")
            return

        if t >= RESTORE_MAX_S:
            self._finish_restore(obs, now)
            return
        if r.queue or len(r.apps_started) < len(r.apps):
            return  # launches still due
        if not any(sid not in bound for sids in r.apps.values() for sid in sids):
            self._finish_restore(obs, now)  # every app window is back
            return
        # an app we started that has no window yet (a slow login) keeps the restore open
        waiting = [a for a in r.apps_launched if not any(w.app == a for w in obs.windows.values())]
        if t >= r.delay + RESTORE_APP_GRACE_S and not waiting:
            self._finish_restore(obs, now)

    def _finish_restore(self, obs: Obs, now: float):
        r = self.restore
        r.finished = True
        r.finished_t = now
        bound = self.bound_slots
        for lw, slot in list(self.layout.slots()):
            if r.scope is not None and lw.id not in r.scope:
                continue
            if r.slots is not None:
                continue  # a relaunch: its own backoff decides, not the login count
            if slot.id in bound:
                continue
            slot.missed += 1
            self._mark()
            if slot.missed >= MAX_MISSED:
                self._delete_slot(slot.id, f"did not come back {slot.missed} restores in a row", now)
        missing = sum(1 for lw, s in self.layout.slots() if s.id not in self.bound_slots
                      and (r.scope is None or lw.id in r.scope) and (r.slots is None or s.id in r.slots))
        if missing:
            self._restore_log(f"{missing} window{'s' if missing != 1 else ''} did not come back")
        target = self.primary.get(r.back_to) if r.back_to else None
        if target is not None and r.scope is None and not r.quiet:
            self.act.activate(target.index)
        if r.scope is None and not r.quiet:
            self.disturbed_until = min(self.disturbed_until, now + 3.0)
        self.journal("restore_finished", log=r.log)
        self.changed = True

    # ---- commands (from the sidebar and the CLI) --------------------------------------------
    def expect_window(self, ws_id: str, now: float, accepts: Callable[[Win], bool], pid: int | None = None,
                      what: str = "", slot_id: str | None = None, wait: float = TEMPLATE_WAIT_S,
                      state: dict | None = None):
        """Something we started for this workspace: the window it opens becomes a slot there
        (and is moved there if it opened elsewhere), starting with `state`."""
        self.launches.append(Launch(t=now, ws_id=ws_id, slot_id=slot_id, deadline=now + wait,
                                    accepts=accepts, pid=pid, what=what, state=dict(state or {})))

    def create_workspace(self, now: float, name: str = "", cwd: str = "", after: str | None = None,
                         source: str = "", item: str = "") -> Workspace | None:
        """A new kept workspace; `source` and `item` bind it to an other source's item."""
        lw = Workspace(id=new_id("w"), name=name, cwd=cwd, created=now, source=source, item=item)
        space = None
        self.layout.workspaces.append(lw)
        self._pinned_this_step = False
        space = self._ensure_space(lw, now)
        if space is None:
            self.layout.workspaces.remove(lw)
            return None
        if after:
            ref = self.layout.ws(after)
            if ref is not None:
                self.layout.workspaces.remove(lw)
                self.layout.workspaces.insert(self.layout.workspaces.index(ref) + 1, lw)
        if source:
            self.layout.remember_item(source, item)
        self.journal("ws_created", ws=lw.id, index=space.index,
                     why=f"opened for {source} item {item}" if source else "new workspace")
        self._mark()
        return lw

    def leave_when_empty(self, index: int | None, now: float):
        """You closed the workspace you are on: once its windows are gone, go to the one below it
        (after the last one, COSMIC's empty workspace at the end), so COSMIC drops the emptied one
        right away instead of keeping it until you switch. An app still asking about unsaved work
        keeps you there."""
        space = self.obs.space(index) if self.obs else None
        if space is not None and space.active:
            self._leaving = (space.index, now + LEAVE_WAIT_S)

    def _leave_closed(self, obs: Obs, now: float):
        if self._leaving is None:
            return
        # by index: unpinned, it has no ids any more, and COSMIC keeps the one you are on in place
        index, until = self._leaving
        space = obs.space(index)
        if space is None or not space.active or now > until:
            self._leaving = None  # gone already, you went elsewhere, or a window would not close
            return
        if obs.windows_on(space.index):
            return
        self._leaving = None
        target = obs.space(index + 1) or obs.space(index - 1)
        if space.output:
            # each workspace is on one screen (Hyprland): the nearest on the same screen, or the
            # emptied one stays shown there, and so stays
            near = sorted((s for s in obs.spaces if s.output == space.output and s.index != index),
                          key=lambda s: (abs(s.index - index), s.index < index))
            target = near[0] if near else target
        if target is not None:
            self.act.activate(target.index)
            self.journal("left_closed", index=space.index, to=target.index)

    def remove_workspace(self, ws_id: str, now: float, why: str) -> list[str]:
        """Drop a workspace from the layout. Returns the windows that were on it (the caller
        closes them for 'close', leaves them for 'forget')."""
        lw = self.layout.ws(ws_id)
        if lw is None:
            return []
        space = self.primary.get(ws_id)
        closed = [s for t, s in self._closed_on.pop(ws_id, []) if now - t < CLOSED_KEEP_S]
        entry = self.layout.remember_workspace(lw, now, why, closed_slots=reversed(closed))
        if entry is not None:
            self.journal("history_added", ws=ws_id, entry=entry["id"], slots=len(entry["workspace"]["slots"]), why=why)
        wins = []
        for slot in list(lw.slots):
            wid = self.slot_window(slot.id)
            if wid:
                wins.append(wid)
                self.bind.pop(wid, None)
            self.layout.remember_recent(slot, lw, now)
            self.pending_delete.pop(slot.id, None)
        if space is not None:
            wins += [w.id for w in (self.obs.windows_on(space.index) if self.obs else []) if w.id not in wins]
            self.no_register[space.index] = now + NO_REGISTER_S
            self._closing[space.index] = (len(self.obs.spaces) if self.obs else 0, now + NO_REGISTER_S)
            self.act.unpin(space.index)
        for i in lw.ids:
            self._retired[i] = now
        self.layout.workspaces.remove(lw)
        if self.layout.active == ws_id:
            self.layout.active = ""
        self.primary.pop(ws_id, None)
        if space is not None:
            self.owner.pop(space.index, None)
        self._mark()
        self.journal("ws_removed", ws=ws_id, why=why, windows=len(wins))
        return wins

    def restore_from_history(self, entry_id: str, now: float) -> Workspace:
        """A closed workspace back on a fresh workspace, its windows started one at a time."""
        if self.restore is not None and not self.restore.finished:
            raise RuntimeError("a restore is already running; try again in a moment")
        entry = next((h for h in self.layout.history if h.get("id") == entry_id), None)
        if entry is None:
            raise KeyError(f"no history entry {entry_id!r}")
        snap = Workspace.from_json(entry["workspace"])
        live = {c["session"] for _, s in self.layout.slots() for c in s.claudes}
        lw = self.create_workspace(now, name=snap.name, cwd=snap.cwd)
        if lw is None:
            raise RuntimeError("could not create a pinned workspace")
        self.layout.take_from_history(entry_id)
        for old in snap.slots:
            if old.session and old.session in live:
                continue  # that Claude is open somewhere already
            state = dict(old.state)
            if state.get("more"):
                state["more"] = [m for m in state["more"] if isinstance(m, dict) and m.get("session") not in live]
            lw.slots.append(Slot(id=new_id("s"), kind=old.kind, app=old.app, title=old.title, output=old.output,
                                 state=state, created=now, updated=now))
        sessions = {c["session"] for s in lw.slots for c in s.claudes}
        self.layout.recent = [r for r in self.layout.recent if r.get("session") not in sessions]
        self._mark()
        self.journal("history_restored", entry=entry_id, ws=lw.id, slots=len(lw.slots))
        self.start_restore(now, "from history", delay=0, scope={lw.id})
        return lw

    def rename(self, ws_id: str, name: str):
        lw = self.layout.ws(ws_id)
        if lw is not None and lw.name != name:
            lw.name = name
            self._mark()

    def reorder(self, ws_id: str, other_id: str, before: bool):
        lw, other = self.layout.ws(ws_id), self.layout.ws(other_id)
        if lw is None or other is None or lw is other:
            return
        self.layout.workspaces.remove(lw)
        i = self.layout.workspaces.index(other)
        self.layout.workspaces.insert(i if before else i + 1, lw)
        self._mark()

    def forget_slot(self, slot_id: str, now: float):
        self._delete_slot(slot_id, "forgotten by you", now)

    def reapply(self, now: float):
        """'Put windows back': treat the desktop as disturbed for a while, so everything bound
        goes home and windows that match missing slots are recognised."""
        self.disturb(15.0, "put back on request", now)
        self._all_return = True  # windows already up count as coming back
        self.move_tries.clear()
        self._gave_up.clear()
        self.expect.clear()

    def suspend_resumed(self, now: float):
        self.disturb(RESUME_S, "woke from suspend", now)
