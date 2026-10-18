"""The desktop as every backend describes it to the rest of Sidelit.

The daemon, the sidebar and the restore never see a compositor's own objects. Each backend
(backend.py picks one: wsclient.py on COSMIC, kwin.py on KDE Plasma, and so on) hands out these two
shapes, extended with whatever it keeps for itself: workspaces with a stable id, a name, a
position and whether you are on it; windows with a title, an app id, an identifier that
stays the same while the window lives, and the process behind it where the desktop says.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Workspace:
    proxy: object = None   # the backend's own handle for it
    id: str = ""           # the backend's id for it; "" while it has none (an unpinned COSMIC one)
    name: str = ""         # as the desktop shows it
    coords: tuple = ()     # (position,) counting from 1
    state: int = 0         # the backend's flags for it; any of them set means you are on it
    removed: bool = False  # the desktop dropped it
    # every id carried by a copy of this workspace: COSMIC keeps one workspace set per output
    # and each output's copy of an index can be pinned with an id of its own
    ids: frozenset = frozenset()
    copies_pinned: bool = True  # every screen's copy is pinned (COSMIC pins per screen)

    @property
    def active(self) -> bool:
        return self.state != 0

    @property
    def all_ids(self) -> frozenset:
        return self.ids | ({self.id} if self.id else frozenset())

    def has_id(self, ws_id: str | None) -> bool:
        """Is `ws_id` this workspace, under any of its copies' ids?"""
        return bool(ws_id) and ws_id in self.all_ids

    @property
    def pinned(self) -> bool:
        """Does it outlive its last window? Where the desktop never drops a workspace by itself
        (KDE), always."""
        return True

    @property
    def index(self) -> int:
        return self.coords[0] if self.coords else -1


@dataclasses.dataclass
class Toplevel:
    proxy: object = None   # the backend's own handle for the window
    title: str = ""        # the window's title as it is now
    app_id: str = ""       # Wayland app id (on X11, the window class)
    identifier: str = ""   # stays the same while the window lives, never given to another
    pid: int = 0           # the process behind it; 0 where the desktop does not say (COSMIC)
    closed: bool = False   # gone, and about to be dropped from the backend's list

    @property
    def focused(self) -> bool:
        return 2 in getattr(self, "states", ())
