"""An X server with an EWMH window manager in it, in miniature, for the X11 backend's tests.

It stands where x11.XConn stands (the same methods, by atom name) and keeps the root window's
and each window's properties as a window manager keeps them: it answers the client messages a
pager sends (switch desktop, number of desktops, put a window on a desktop, focus, close, move,
maximise) and queues a PropertyNotify for every property that changes on the root window or on
a window the client watches. The test's own hands open, retitle and close windows, and add and
remove desktops the way Cinnamon does from its own menus (in the middle, the ones after it
moving down with their windows and names).
"""
from __future__ import annotations

ROOT = 1
ALL = 0xFFFFFFFF


class FakeX:
    root = ROOT

    def __init__(self, names=("Desktop 1", "Desktop 2", "Desktop 3"), monitors=(("DP-1", 0, 0, 1920, 1080),),
                 wm=True):
        self.props: dict[int, dict] = {ROOT: {}}
        self.geom: dict[int, tuple] = {}
        self.watched: set[int] = set()
        self.queue: list[tuple] = []
        self.sent: list[tuple] = []
        self.time = 1000
        self.next_wid = 0x2600001
        self.mons = list(monitors)
        self.closed = False
        self.fixed = False  # a window manager that will not change the number of desktops (i3)
        self.clients: list[int] = []
        if wm:
            self._set(ROOT, "_NET_NUMBER_OF_DESKTOPS", [len(names)])
            self._set(ROOT, "_NET_DESKTOP_NAMES", "".join(n + "\0" for n in names))
            self._set(ROOT, "_NET_CURRENT_DESKTOP", [0])
            self._set(ROOT, "_NET_CLIENT_LIST", [])
            self._set(ROOT, "_NET_ACTIVE_WINDOW", [0])
        self.queue.clear()

    # ---- what x11.XConn does ----------------------------------------------------------------------
    def fileno(self) -> int:
        return -1

    def get(self, wid, prop):
        value = self.props.get(wid, {}).get(prop)
        return list(value) if isinstance(value, list) else value

    def watch(self, wid):
        if wid in self.props:
            self.watched.add(wid)

    def send(self, wid, kind, data):
        data = (list(data) + [0] * 5)[:5]
        self.sent.append((wid, kind, data))
        self._wm(wid, kind, data)

    def set_strings(self, prop, values, append=False):
        before = self.props[ROOT].get(prop, "") if append else ""
        self._set(ROOT, prop, before + "".join(v + "\0" for v in values))

    def geometry(self, wid):
        return self.geom.get(wid)

    def monitors(self):
        return list(self.mons)

    def events(self):
        out, self.queue = self.queue, []
        return out

    def sync(self):
        pass

    def close(self):
        self.closed = True

    # ---- the X server ---------------------------------------------------------------------------
    def _set(self, wid, prop, value):
        self.props.setdefault(wid, {})[prop] = value
        self.time += 1
        if wid == ROOT or wid in self.watched:
            self.queue.append(("prop", wid, prop, self.time))

    @property
    def count(self) -> int:
        return self.props[ROOT]["_NET_NUMBER_OF_DESKTOPS"][0]

    @property
    def current(self) -> int:
        return self.props[ROOT]["_NET_CURRENT_DESKTOP"][0]

    @property
    def names(self) -> list:
        return self.props[ROOT]["_NET_DESKTOP_NAMES"].split("\0")[:-1]

    def desktop_of(self, wid) -> int:
        return self.props[wid]["_NET_WM_DESKTOP"][0]

    def _set_names(self, names):
        self._set(ROOT, "_NET_DESKTOP_NAMES", "".join(n + "\0" for n in names))

    # ---- the window manager ---------------------------------------------------------------------
    def _wm(self, wid, kind, data):
        if kind == "_NET_CURRENT_DESKTOP" and wid == ROOT and 0 <= data[0] < self.count:
            self._set(ROOT, "_NET_CURRENT_DESKTOP", [data[0]])
        elif kind == "_NET_NUMBER_OF_DESKTOPS" and wid == ROOT and data[0] > 0 and not self.fixed:
            n = data[0]
            for w in self.clients:  # (xfwm4, Muffin: the windows of a desktop that goes go to the last one)
                d = self.desktop_of(w)
                if d != ALL and d >= n:
                    self._set(w, "_NET_WM_DESKTOP", [n - 1])
            if self.current >= n:
                self._set(ROOT, "_NET_CURRENT_DESKTOP", [n - 1])
            self._set(ROOT, "_NET_NUMBER_OF_DESKTOPS", [n])
        elif kind == "_NET_WM_DESKTOP" and wid in self.clients and (data[0] < self.count or data[0] == ALL):
            self._set(wid, "_NET_WM_DESKTOP", [data[0]])
        elif kind == "_NET_ACTIVE_WINDOW" and wid in self.clients:
            d = self.desktop_of(wid)
            if d != ALL and d != self.current:  # (xfwm4's "bring": the window comes to you)
                self._set(wid, "_NET_WM_DESKTOP", [self.current])
            self._set(ROOT, "_NET_ACTIVE_WINDOW", [wid])
        elif kind == "_NET_CLOSE_WINDOW" and wid in self.clients:
            self.close_window(wid)
        elif kind == "_NET_MOVERESIZE_WINDOW" and wid in self.geom:
            x, y, width, height = self.geom[wid]
            flags = data[0]
            self.geom[wid] = (data[1] if flags & 1 << 8 else x, data[2] if flags & 1 << 9 else y, width, height)
            self.queue.append(("configure", wid))
        elif kind == "_NET_WM_STATE" and wid in self.clients:
            state = list(self.props[wid].get("_NET_WM_STATE") or [])
            for atom in data[1:3]:
                if not atom:
                    continue
                if data[0] == 0 and atom in state:
                    state.remove(atom)
                elif data[0] == 1 and atom not in state:
                    state.append(atom)
            self._set(wid, "_NET_WM_STATE", state)

    # ---- the test's hands -----------------------------------------------------------------------
    def open(self, title, wm_class=("xterm", "XTerm"), desktop=0, pid=0, kind="_NET_WM_WINDOW_TYPE_NORMAL",
             gtk_app=None, geometry=(100, 100, 800, 600), state=()) -> int:
        wid = self.next_wid
        self.next_wid += 0x100000
        props = {"_NET_WM_NAME": title, "WM_CLASS": "".join(p + "\0" for p in wm_class),
                 "_NET_WM_DESKTOP": [desktop], "_NET_WM_STATE": list(state)}
        if pid:
            props["_NET_WM_PID"] = [pid]
        if kind:
            props["_NET_WM_WINDOW_TYPE"] = [kind]
        if gtk_app:
            props["_GTK_APPLICATION_ID"] = gtk_app
        self.props[wid] = props
        self.geom[wid] = geometry
        self.clients.append(wid)
        self._set(ROOT, "_NET_CLIENT_LIST", list(self.clients))
        return wid

    def retitle(self, wid, title):
        self._set(wid, "_NET_WM_NAME", title)

    def close_window(self, wid):
        self.clients.remove(wid)
        self.props.pop(wid, None)
        self.watched.discard(wid)
        self._set(ROOT, "_NET_CLIENT_LIST", list(self.clients))
        if self.props[ROOT]["_NET_ACTIVE_WINDOW"] == [wid]:
            self._set(ROOT, "_NET_ACTIVE_WINDOW", [0])

    def add_desktop(self, name=None):
        """Cinnamon's "add workspace": one more at the end."""
        names = self.names
        self._set(ROOT, "_NET_NUMBER_OF_DESKTOPS", [self.count + 1])
        if name is not None:
            self._set_names((names + [""] * self.count)[:self.count - 1] + [name])

    def remove_desktop(self, at, regenerate=None):
        """Cinnamon's "remove workspace" on desktop `at`: its windows go to the one before, the
        windows and names after it move down by one. `regenerate`: names made up by position
        instead ("Workspace N", as Muffin's for unnamed ones)."""
        for w in self.clients:
            d = self.desktop_of(w)
            if d != ALL and d >= at:
                self._set(w, "_NET_WM_DESKTOP", [max(d - 1, 0)])
        if self.current >= at and self.current > 0:
            self._set(ROOT, "_NET_CURRENT_DESKTOP", [self.current - 1])
        names = self.names
        names = names[:at] + names[at + 1:]
        if regenerate:
            names = [f"{regenerate} {i + 1}" for i in range(self.count - 1)]
        self._set_names(names)
        self._set(ROOT, "_NET_NUMBER_OF_DESKTOPS", [self.count - 1])
