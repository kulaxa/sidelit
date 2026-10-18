"""The two pictures of the desktop the engine compares.

`Obs` is what the compositor shows right now (rebuilt from scratch on every step).
`Layout` is what the desktop should look like: the one source of truth, saved as
~/.config/sidelit/layout.json. Only the engine changes a Layout.

A Layout is an ordered list of workspaces, each holding *slots*. A slot is one window
we keep: its adapter (terminal, browser, files, ...), the app, the screen it belongs
on and whatever the adapter can remember about it (a Claude session, a directory, a
URL). A live window is *bound* to at most one slot; a slot without a window is
*missing*.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

SCHEMA_VERSION = 1


# ---- observed ------------------------------------------------------------------------
@dataclass(frozen=True)
class Win:
    id: str                # the compositor's window identifier: unique for the whole session
    app: str
    title: str
    space: int | None      # index of the workspace it is on; None until the compositor says
    output: str | None     # connector of the one screen it is on (None: unknown or spanning two)
    pid: int = 0           # 0 where the compositor does not tell (COSMIC)
    focused: bool = False  # has the keyboard
    space_ids: frozenset = frozenset()  # ids of the copy it is on (COSMIC: one per screen); empty if unpinned or unknown


@dataclass(frozen=True)
class Ended:
    """A process seen ending: one we started (its exit status), or one the crash reporter caught."""
    t: float
    pid: int
    exe: str               # its executable's file name; "" when not known
    crashed: bool          # killed by a signal, not exited by itself


@dataclass(frozen=True)
class Space:
    index: int
    ids: frozenset         # every id a copy of this workspace carries (COSMIC: one per screen)
    pinned: bool
    active: bool
    name: str = ""
    copies_pinned: bool = True  # every screen's copy is pinned (COSMIC pins per screen)
    output: str = ""            # its screen, where each workspace is on one (Hyprland); "" where all show them


@dataclass
class Obs:
    spaces: list[Space]
    windows: dict[str, Win]
    outputs: frozenset
    # window id -> the Claude session it shows: {"session", "title", "status", "cwd"}
    claude: dict[str, dict] = field(default_factory=dict)
    # window id -> the browser window it is, from the extension: {"instance", "window", "tabs", "profile"}
    browser: dict[str, dict] = field(default_factory=dict)
    # extension instance connected now -> the ids of every window in its latest report
    browser_reported: dict[str, set] = field(default_factory=dict)
    # window id -> when it last got the keyboard, as the compositor said so (also for windows that
    # just closed: clicking a close button focuses the window a moment before it goes)
    focused_at: dict[str, float] = field(default_factory=dict)

    def space(self, index: int | None) -> Space | None:
        return next((s for s in self.spaces if s.index == index), None) if index is not None else None

    def windows_on(self, index: int) -> list[Win]:
        return [w for w in self.windows.values() if w.space == index]

    @property
    def trailing(self) -> Space | None:
        return max(self.spaces, key=lambda s: s.index) if self.spaces else None


# ---- desired ---------------------------------------------------------------------------
def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


@dataclass
class Slot:
    id: str
    kind: str                         # adapter name
    app: str                          # app id as last seen
    title: str = ""
    output: str = ""                  # screen it belongs on; "" = wherever
    state: dict = field(default_factory=dict)
    window: str = ""                  # last window bound to it (identifiers are unique per session)
    missed: int = 0                   # restores in a row it did not come back from
    created: float = 0.0
    updated: float = 0.0

    def to_json(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "app": self.app, "title": self.title}
        for key in ("output", "state", "window", "missed"):
            if getattr(self, key):
                d[key] = getattr(self, key)
        d["created"] = round(self.created, 1)
        d["updated"] = round(self.updated, 1)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Slot":
        return cls(id=str(d["id"]), kind=str(d.get("kind") or "generic"), app=str(d.get("app") or ""),
                   title=str(d.get("title") or ""), output=str(d.get("output") or ""),
                   state=dict(d.get("state") or {}), window=str(d.get("window") or ""),
                   missed=int(d.get("missed") or 0), created=float(d.get("created") or 0),
                   updated=float(d.get("updated") or 0))

    @property
    def session(self) -> str | None:
        return self.state.get("session") or None

    @property
    def claudes(self) -> list[dict]:
        """Every agent session its window shows, {"session", "claude_title", "cwd"} each, and
        "agent" for one that is not Claude Code's: the one `session` names, then state["more"],
        the others in the same window (split panes, tabs)."""
        if not self.session:
            return []
        first = {"session": self.session, "claude_title": self.state.get("claude_title") or "",
                 "cwd": self.state.get("cwd") or ""}
        if self.state.get("agent"):
            first["agent"] = self.state["agent"]
        return [first] + [m for m in self.state.get("more") or [] if isinstance(m, dict) and m.get("session")]


@dataclass
class Workspace:
    id: str
    ids: list[str] = field(default_factory=list)   # compositor ids this workspace is known by
    name: str = ""                                  # a name you gave it
    cwd: str = ""
    slots: list[Slot] = field(default_factory=list)
    created: float = 0.0
    # the other source's item (sources.py) this workspace was opened for: its tab is drawn from it
    source: str = ""                                # the source's key (its config file's name)
    item: str = ""                                  # the item's id in that source's feed

    def to_json(self) -> dict:
        d = {"id": self.id, "ids": list(self.ids)}
        if self.name:
            d["name"] = self.name
        if self.cwd:
            d["cwd"] = self.cwd
        if self.source:
            d["source"], d["item"] = self.source, self.item
        d["slots"] = [s.to_json() for s in self.slots]
        d["created"] = round(self.created, 1)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Workspace":
        return cls(id=str(d["id"]), ids=[str(i) for i in d.get("ids") or []], name=str(d.get("name") or ""),
                   cwd=str(d.get("cwd") or ""), slots=[Slot.from_json(s) for s in d.get("slots") or []],
                   created=float(d.get("created") or 0), source=str(d.get("source") or ""),
                   item=str(d.get("item") or ""))

    @property
    def claude(self) -> Slot | None:
        """The slot running (or last running) this workspace's Claude."""
        return next((s for s in self.slots if s.session), None)


@dataclass
class Layout:
    workspaces: list[Workspace] = field(default_factory=list)
    active: str = ""                                   # workspace you were on
    recent: list[dict] = field(default_factory=list)   # Claude sessions whose window you closed
    # workspaces that left the layout, newest first, to bring back whole:
    # {"id", "closed", "why", "workspace": Workspace.to_json()}
    history: list[dict] = field(default_factory=list)
    # [source, item] of every other source's item that has had a workspace, newest last: one
    # that opened by itself and was closed does not open again
    opened_items: list[list] = field(default_factory=list)

    RECENT_MAX = 25
    HISTORY_MAX = 100  # (a couple of weeks of closing a few a day; ~1 KB each)
    OPENED_ITEMS_MAX = 500

    def to_json(self) -> dict:
        d = {"version": SCHEMA_VERSION, "active": self.active,
             "workspaces": [w.to_json() for w in self.workspaces], "recent": self.recent,
             "history": self.history}
        if self.opened_items:
            d["opened_items"] = self.opened_items
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Layout":
        if not isinstance(d, dict) or not isinstance(d.get("workspaces"), list):
            raise ValueError("not a layout: no workspaces list")
        if int(d.get("version") or 0) > SCHEMA_VERSION:
            raise ValueError(f"layout version {d.get('version')} is newer than this build understands")
        lay = cls(workspaces=[Workspace.from_json(w) for w in d["workspaces"]], active=str(d.get("active") or ""),
                  recent=[r for r in d.get("recent") or [] if isinstance(r, dict)],
                  history=[h for h in d.get("history") or [] if isinstance(h, dict) and isinstance(h.get("workspace"), dict)],
                  opened_items=[[str(x[0]), str(x[1])] for x in d.get("opened_items") or []
                                if isinstance(x, list) and len(x) == 2])
        lay.validate()
        return lay

    def validate(self):
        seen = set()
        for w in self.workspaces:
            for x in [w.id] + [s.id for s in w.slots]:
                if x in seen:
                    raise ValueError(f"duplicate id {x!r} in layout")
                seen.add(x)

    # ---- lookups
    def ws(self, ws_id: str | None) -> Workspace | None:
        return next((w for w in self.workspaces if w.id == ws_id), None) if ws_id else None

    def find(self, slot_id: str | None) -> tuple[Workspace | None, Slot | None]:
        for w in self.workspaces:
            for s in w.slots:
                if s.id == slot_id:
                    return w, s
        return None, None

    def slots(self):
        for w in self.workspaces:
            for s in w.slots:
                yield w, s

    def owner_of_id(self, compositor_id: str) -> Workspace | None:
        return next((w for w in self.workspaces if compositor_id in w.ids), None)

    def for_item(self, source: str, item: str) -> Workspace | None:
        """The workspace opened for an other source's item, if it has one."""
        return next((w for w in self.workspaces if source and w.source == source and w.item == item), None)

    def had_item(self, source: str, item: str) -> bool:
        return [source, item] in self.opened_items

    def remember_item(self, source: str, item: str):
        if [source, item] not in self.opened_items:
            self.opened_items.append([source, item])
            del self.opened_items[:-self.OPENED_ITEMS_MAX]

    # ---- edits
    def remember_recent(self, slot: Slot, ws: Workspace | None, now: float, claudes: list[dict] | None = None):
        """Claudes whose window closed, or that ended in it, go to the top of the recent list:
        every one the slot's window showed, or only `claudes` of them."""
        for c in reversed(slot.claudes if claudes is None else claudes):
            title = c.get("claude_title") or (slot.title if c["session"] == slot.session else "")
            self.recent = [r for r in self.recent if r.get("session") != c["session"]]
            self.recent.insert(0, {"session": c["session"], "title": title,
                                   "cwd": c.get("cwd") or (ws.cwd if ws else ""),
                                   "workspace": (ws.name if ws else "") or "", "closed": round(now, 1),
                                   **({"agent": c["agent"]} if c.get("agent") else {})})
        del self.recent[self.RECENT_MAX:]

    def remember_workspace(self, ws: Workspace, now: float, why: str, closed_slots=()) -> dict | None:
        """Put a workspace leaving the layout into the history, with its slots and the ones
        closed on it shortly before (closing a workspace window by window empties it first).
        A workspace with nothing to bring back is not remembered."""
        slots, seen = [], set()
        for s in list(ws.slots) + list(closed_slots):
            key = s.session or (s.kind, s.app, s.title, s.state.get("cwd"), s.state.get("url"), s.state.get("path"))
            if key in seen:
                continue
            seen.add(key)
            d = s.to_json()
            for k in ("window", "missed"):
                d.pop(k, None)
            d.get("state", {}).pop("no_claude_since", None)
            slots.append(d)
        if not slots:
            return None
        snap = {"id": new_id("h"), "closed": round(now, 1), "why": why,
                "workspace": {"id": ws.id, "ids": [], "name": ws.name, "cwd": ws.cwd, "slots": slots,
                              "created": round(ws.created, 1)}}
        self.history.insert(0, snap)
        del self.history[self.HISTORY_MAX:]
        return snap

    def take_from_history(self, entry_id: str) -> dict | None:
        entry = next((h for h in self.history if h.get("id") == entry_id), None)
        if entry is not None:
            self.history.remove(entry)
        return entry

    def remove_slot(self, slot_id: str) -> Slot | None:
        for w in self.workspaces:
            for s in list(w.slots):
                if s.id == slot_id:
                    w.slots.remove(s)
                    return s
        return None
