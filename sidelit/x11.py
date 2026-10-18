"""X11 workspace + window client: any window manager that keeps the EWMH hints (Cinnamon, as on
Linux Mint, Xfce, MATE, Openbox and most others), through python-xlib.

Workspaces are the window manager's desktops: how many there are (_NET_NUMBER_OF_DESKTOPS),
their names (_NET_DESKTOP_NAMES) and which one you are on (_NET_CURRENT_DESKTOP), all on the
root window. Windows are the ones in _NET_CLIENT_LIST, each with its title (_NET_WM_NAME), app
(the GTK app id where GTK sets one, else WM_CLASS), process (_NET_WM_PID) and desktop
(_NET_WM_DESKTOP). Every change is a PropertyNotify, on the root window or on the window.

What Sidelit does goes to the window manager as a pager's client messages to the root window:
switch desktop, add or drop desktops at the end (a new number of desktops), put a window on a
desktop, focus it, close it, move it to another screen (_NET_MOVERESIZE_WINDOW). Renaming sets
_NET_DESKTOP_NAMES, which the window manager follows.

X11 desktops have no ids, only positions. This client gives each one an id of its own and
keeps them, with the names, in Sidelit's state folder (x11-desktops.json): the window manager
brings the same desktops back at the next login, and they get the same ids. When desktops come
or go, the names and the windows say which one it was (a removed desktop shifts the ones after
it, and their windows, down by one); a change Sidelit made itself, it knows.

EWMH only adds and drops desktops at the end and cannot reorder them, so "pin" and "move" are
not in caps: desktops stay until removed, as on KDE. To remove one in the middle, the windows
and names after it move down by one and the last desktop goes.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sidelit import config, model

ALL_DESKTOPS = 0xFFFFFFFF       # _NET_WM_DESKTOP of a window on every desktop
PAGER = 2                       # source indication: a pager, whose requests window managers follow
STATIC_GRAVITY = 10             # _NET_MOVERESIZE_WINDOW's x and y are the window's own, not its frame's
IDS_FILE = "x11-desktops.json"  # in config.STATE_DIR
SESSION_PROP = "_SIDELIT_SESSION"  # on the root window: this X server's token (session_token)
WM_WAIT_S = 30.0                # how long the daemon waits for a window manager at login
ACT_WAIT_S = 1.0                # how long adding or removing a desktop waits for the window manager
HINT_S = 5.0                    # how long our own add or remove explains the next change of desktops
WANTED = {"_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG"}  # as a task bar lists them
# the window properties we keep up with, and what each one is read for
WINDOW_PROPS = ("_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_GTK_APPLICATION_ID", "_NET_WM_PID",
                "_NET_WM_DESKTOP", "_NET_WM_WINDOW_TYPE")
ROOT_PROPS = ("_NET_NUMBER_OF_DESKTOPS", "_NET_DESKTOP_NAMES", "_NET_CURRENT_DESKTOP",
              "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW")


@dataclass
class Desktop(model.Workspace):  # proxy: its position, from 0; id: ours (see the top)
    pass


@dataclass
class Window(model.Toplevel):  # proxy: the X window id; identifier: this X server's token and the id
    desktop: int | None = None  # the position of its desktop, from 0; None while on all of them
    wanted: bool = True         # a normal window or a dialog, not a panel, a menu or the desktop
    wm_class: tuple = ("", "")  # (instance, class)
    gtk_app: str = ""           # the GTK app id, where GTK sets one
    name: str = ""              # _NET_WM_NAME (UTF-8)
    legacy_name: str = ""       # WM_NAME, for a window without _NET_WM_NAME
    active: bool = False        # it has the keyboard
    focused_at: float = 0.0     # when it last got the keyboard
    geometry: tuple | None = None  # (x, y, width, height) on the root window, read when needed

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str     # RandR's monitor name: the connector, e.g. "DP-1"
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def holds(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


def app_id(gtk_app: str, instance: str, cls: str) -> str:
    """A window's app id as on Wayland: GTK's app id where it sets one; else WM_CLASS, its
    instance where that is the class in other letters ("google-chrome", "Google-chrome"), the
    class where they differ (Firefox is "Navigator", "firefox")."""
    if gtk_app:
        return gtk_app
    if instance and (not cls or instance.lower() == cls.lower()):
        return instance
    return cls or instance


def _generated(name: str, position: int) -> bool:
    """A name the window manager could have made up for the desktop at `position` ("Workspace 3"
    third: Muffin names unnamed desktops by where they are), so no clue to which desktop it is."""
    return not name or re.search(rf"(^|\D){position + 1}$", name) is not None


def realign(ids: list, known: list, names: list, count: int, windows: list, hint: tuple | None = None,
            new_id: Callable[[], str] = lambda: secrets.token_hex(6)) -> list:
    """The ids of the `count` desktops there are now, from the `ids` they had when they had the
    names `known`. `names` are their names now; `windows` is (id, position) for each window: the
    desktop it was on then and where it is now. `hint` is ("add" | "remove", position): our own
    change, which explains it.

    One desktop more or fewer: the one whose going (or coming) explains the most names and
    windows (see _score). A tie (nothing to go on) is the end, where window managers add and
    drop them. More than one: at the end."""
    n = len(ids)
    if count == n:
        return list(ids)
    if abs(count - n) == 1:
        kind = "remove" if count < n else "add"
        if hint is not None and hint[0] == kind and 0 <= hint[1] <= (n - 1 if kind == "remove" else n):
            at = hint[1]
        else:
            at = max(range(n if kind == "remove" else n + 1),
                     key=lambda k: (*_score(ids, known, names, windows, kind, k), k))
        if kind == "remove":
            return ids[:at] + ids[at + 1:]
        return ids[:at] + [new_id()] + ids[at:]
    if count < n:
        return ids[:count]
    return ids + [new_id() for _ in range(count - n)]


def _score(ids, known, names, windows, kind, k) -> tuple:
    """How well desktop `k` going (or a new one at `k`) explains what is there now: the names
    that are where they would be, and the windows that are where the window manager would have
    put them (the windows of a desktop that goes go to the one before it, as xfwm4, Muffin and
    Marco do); then, to tell apart what that leaves alike, the windows still on their own
    desktop."""
    after = ids[:k] + ids[k + 1:] if kind == "remove" else ids[:k] + [None] + ids[k:]
    was = {d: i for i, d in enumerate(ids)}
    names_fit = 0
    for position, name in enumerate(names):
        d = after[position] if position < len(after) else None
        old = was.get(d)
        if old is not None and old < len(known) and name == known[old] and not _generated(name, position):
            names_fit += 1
    moved_fit = stayed = 0
    for d, position in windows:
        old = was.get(d)
        if old is None:
            continue
        if kind == "add":
            expected = old if old < k else old + 1
        else:
            expected = old if old < k else old - 1 if old > k else max(k - 1, 0)
        moved_fit += position == expected
        stayed += 0 <= position < len(after) and after[position] == d
    return names_fit + moved_fit, stayed


# ---- the "Switch to workspace N" shortcuts -------------------------------------------------------
XFCE_SHORTCUTS = "xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml"
CINNAMON_KEYS = ("org.cinnamon.desktop.keybindings.wm", True)  # (schema, keys are lists)
MATE_KEYS = ("org.mate.Marco.global-keybindings", False)


def xfce_switch_keys(text: str | None) -> dict:
    """Desktop number -> the key xfwm4 binds to "workspace_N_key", from xfconf's file: the
    custom set where there is one, else the defaults."""
    if not text:
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    wm = next((p for p in root.findall("property") if p.get("name") == "xfwm4"), None)
    if wm is None:
        return {}
    sets = {p.get("name"): p for p in wm.findall("property")}
    chosen = sets.get("custom") if sets.get("custom") is not None and len(sets["custom"]) else sets.get("default")
    keys = {}
    for p in chosen if chosen is not None else []:
        match = re.fullmatch(r"workspace_(\d+)_key", p.get("value") or "")
        if match and p.get("name"):
            keys.setdefault(int(match.group(1)), p.get("name"))
    return keys


def settings_switch_keys(schema: str, lists: bool, read: Callable[[str], object] | None = None) -> dict:
    """Desktop number -> the key in a GSettings schema's "switch-to-workspace-N" (Cinnamon's,
    MATE's). `read(key)` is for the tests."""
    if read is None:
        try:
            from gi.repository import Gio
        except ImportError:
            return {}
        source = Gio.SettingsSchemaSource.get_default()
        found = source.lookup(schema, True) if source is not None else None
        if found is None:
            return {}
        settings = Gio.Settings.new(schema)
        names = set(found.list_keys())
        read = lambda key: ((settings.get_strv(key) if lists else settings.get_string(key))  # noqa: E731
                            if key in names else None)
    keys = {}
    for n in range(1, 13):
        value = read(f"switch-to-workspace-{n}")
        first = next((v for v in value if v), "") if isinstance(value, (list, tuple)) else (value or "")
        if first and first != "disabled":
            keys[n] = first
    return keys


def switch_shortcuts(desktop: str | None = None) -> dict:
    """The keys this session's desktop binds to "switch to workspace N"."""
    desktops = {part.strip().upper() for part in (desktop if desktop is not None else
                                                  os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":")}
    if "XFCE" in desktops:  # yours, else the system's defaults
        home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        dirs = [home] + [Path(d) for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
        for d in dirs:
            try:
                keys = xfce_switch_keys((d / XFCE_SHORTCUTS).read_text(errors="replace"))
            except OSError:
                continue
            if keys:
                return keys
        return {}
    if "X-CINNAMON" in desktops:
        return settings_switch_keys(*CINNAMON_KEYS)
    if "MATE" in desktops:
        return settings_switch_keys(*MATE_KEYS)
    return {}


# ---- the X server ----------------------------------------------------------------------------------
class XConn:
    """The X server, through python-xlib: only what the client needs, by atom name, so that the
    tests can put a fake in its place (tests/fake_x11.py).

    Properties come back decoded: numbers (CARDINAL, WINDOW) as a list of ints, atoms as a list
    of names, text as a str (several strings are separated by NUL). Events come back as tuples:
    ("prop", window, property name, server time), ("configure", window), ("screens",)."""

    def __init__(self, display_name: str | None = None):
        try:
            from Xlib import X, display, error
            from Xlib.protocol import event
        except ImportError:
            raise RuntimeError("python-xlib is not installed (python3-xlib on Debian, Ubuntu, Mint and "
                               "Fedora, python-xlib on Arch)") from None
        self._X, self._error, self._event = X, error, event
        try:
            self.d = display.Display(display_name)
        except (error.DisplayError, error.ConnectionClosedError) as exc:
            raise RuntimeError(f"no X server to connect to ({exc})") from None
        self.d.set_error_handler(self._on_error)
        self._root = self.d.screen().root
        self.root = self._root.id
        self._atoms: dict[str, int] = {}
        self._names: dict[int, str] = {}
        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        self._randr = False
        try:
            if self.d.has_extension("RANDR") and hasattr(self._root, "xrandr_get_monitors"):
                from Xlib.ext import randr
                self._root.xrandr_select_input(randr.RRScreenChangeNotifyMask | randr.RROutputChangeNotifyMask
                                               | randr.RRCrtcChangeNotifyMask)
                self._randr = True
        except error.XError:
            pass

    def _on_error(self, err, request=None):
        if isinstance(err, (self._error.BadWindow, self._error.BadDrawable)):
            return  # a window that went while we spoke to it
        print(f"x11: {err}", file=sys.stderr, flush=True)

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.d.intern_atom(name)
            self._names[self._atoms[name]] = name
        return self._atoms[name]

    def atom_name(self, atom: int) -> str:
        if atom not in self._names:
            try:
                self._names[atom] = self.d.get_atom_name(atom)
            except self._error.XError:
                return ""
        return self._names[atom]

    def _window(self, wid: int):
        return self._root if wid == self.root else self.d.create_resource_object("window", wid)

    def fileno(self) -> int:
        return self.d.fileno()

    def get(self, wid: int, prop: str):
        try:
            reply = self._window(wid).get_full_property(self.atom(prop), self._X.AnyPropertyType)
        except self._error.XError:
            return None
        if reply is None:
            return None
        kind = self.atom_name(reply.property_type)
        if reply.format == 8:
            raw = reply.value if isinstance(reply.value, bytes) else str(reply.value).encode("latin-1")
            return raw.decode("utf-8" if kind == "UTF8_STRING" else "latin-1", errors="replace")
        values = [int(v) for v in reply.value]
        return [self.atom_name(v) for v in values] if kind == "ATOM" else values

    def watch(self, wid: int):
        try:
            self._window(wid).change_attributes(event_mask=self._X.PropertyChangeMask | self._X.StructureNotifyMask)
        except self._error.XError:
            pass

    def send(self, wid: int, kind: str, data: list):
        """A client message about window `wid` (the root for the desktops' own), to the root
        window, where the window manager takes it. Atom names in `data` go as atoms."""
        values = [self.atom(v) if isinstance(v, str) else int(v) & 0xFFFFFFFF for v in data]
        message = self._event.ClientMessage(window=self._window(wid), client_type=self.atom(kind),
                                            data=(32, (values + [0] * 5)[:5]))
        mask = self._X.SubstructureRedirectMask | self._X.SubstructureNotifyMask
        self._root.send_event(message, event_mask=mask)
        self.d.flush()

    def set_strings(self, prop: str, values: list, append: bool = False):
        """A list of strings on the root window, as UTF8_STRING (each one ends with NUL);
        `append`: after what is there (the X server appends one client's at a time)."""
        data = b"".join(v.encode("utf-8") + b"\0" for v in values)
        mode = self._X.PropModeAppend if append else self._X.PropModeReplace
        self._root.change_property(self.atom(prop), self.atom("UTF8_STRING"), 8, data, mode)
        self.d.flush()

    def geometry(self, wid: int) -> tuple | None:
        try:
            window = self._window(wid)
            size = window.get_geometry()
            at = self._root.translate_coords(window, 0, 0)
            return (int(at.x), int(at.y), int(size.width), int(size.height))
        except self._error.XError:
            return None

    def monitors(self) -> list:
        """(name, x, y, width, height) for each screen, from RandR 1.5; none without it."""
        if not self._randr:
            return []
        try:
            reply = self._root.xrandr_get_monitors(is_active=True)
        except self._error.XError:
            return []
        return [(self.atom_name(m.name), m.x, m.y, m.width_in_pixels, m.height_in_pixels) for m in reply.monitors]

    def events(self) -> list:
        out = []
        X = self._X
        while self.d.pending_events():
            ev = self.d.next_event()
            if ev.type == X.PropertyNotify:
                out.append(("prop", ev.window.id, self.atom_name(ev.atom), int(ev.time)))
            elif ev.type == X.ConfigureNotify:
                out.append(("configure", ev.window.id))
            elif type(ev).__name__ in ("ScreenChangeNotify", "CrtcChangeNotify", "OutputChangeNotify"):
                out.append(("screens",))
        return out

    def sync(self):
        self.d.sync()

    def close(self):
        try:
            self.d.close()
        except Exception:  # noqa: BLE001 (already gone)
            pass


def _strings(value) -> list:
    """A NUL-separated text property as its strings."""
    if not isinstance(value, str) or not value:
        return []
    parts = value.split("\0")
    return parts[:-1] if value.endswith("\0") else parts


def _number(value, default=None):
    return value[0] if isinstance(value, list) and value else default


def session_token(conn) -> str:
    """This X server's own token, kept on its root window (_SIDELIT_SESSION). Window ids start
    over with every X server, so a window's identifier is the token and its id: a window after
    the next login is never taken for one from before. The first client to look sets it; two at
    once both append theirs, and the first one there counts for both."""
    token = next(iter(_strings(conn.get(conn.root, SESSION_PROP))), "")
    if not token:
        conn.set_strings(SESSION_PROP, [secrets.token_hex(4)], append=True)
        conn.sync()
        token = next(iter(_strings(conn.get(conn.root, SESSION_PROP))), "") or secrets.token_hex(4)
    return token


class X11Client:
    caps = frozenset(("create", "rename", "remove", "focus", "close_toplevel"))

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True,
                 conn=None, ids_path: Path | None = None, wait_s: float | None = None):
        """`primary`: the daemon's, which keeps the desktops' ids (x11-desktops.json) and waits
        for a window manager at login; a one-shot command only reads them. `conn` and
        `ids_path` are for the tests."""
        self.on_change = on_change
        self.primary = primary
        self.conn = conn if conn is not None else XConn()
        self.ids_path = ids_path or config.STATE_DIR / IDS_FILE
        self.workspaces: dict[str, Desktop] = {}  # by our id
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()      # (the daemon sets it; every desktop has one id here)
        self._windows: dict[int, Window] = {}     # every window the window manager lists, by X id
        self._count = 0
        self._names: list[str] = []
        self._current = 0
        self._active = 0
        self._ids: list[str] = []          # ours, by position, as they were last worked out
        self._known: list[str] = []        # the desktops' names then
        self._was_on: dict[int, str] = {}  # window -> the id of its desktop then
        self._hint: tuple | None = None    # ("add" | "remove", position, until): our own change
        self._time = 0                     # the X server's time, as its last event said
        self._token = ""                   # this X server's (session_token), in every window's identifier
        self._shortcuts: tuple = (0.0, {})
        try:
            self._load_ids()
            self._wait_for_wm(wait_s if wait_s is not None else (WM_WAIT_S if primary else 0.0))
            self._token = session_token(self.conn)
            self._read_root(ROOT_PROPS)
            self._read_screens()
            self._sync()
        except Exception:
            self.conn.close()
            raise

    # ---- the window manager ------------------------------------------------------------------
    def _wait_for_wm(self, seconds: float):
        """A window manager that keeps the desktops' hints, else RuntimeError (after waiting
        `seconds` for one: at login the daemon can be up before it)."""
        end = time.monotonic() + seconds
        while self.conn.get(self.conn.root, "_NET_NUMBER_OF_DESKTOPS") is None:
            if time.monotonic() >= end:
                raise RuntimeError("the window manager does not say how many desktops there are "
                                   "(_NET_NUMBER_OF_DESKTOPS): Sidelit needs one that keeps the EWMH hints, "
                                   "as Cinnamon, Xfce, MATE and Openbox do")
            time.sleep(0.5)
            self.conn.events()

    def _changed(self):
        if self.on_change:
            self.on_change()

    # ---- the ids of the desktops -------------------------------------------------------------
    def _load_ids(self):
        try:
            saved = json.loads(self.ids_path.read_text())
            ids, names = saved.get("ids") or [], saved.get("names") or []
            if isinstance(ids, list) and all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids):
                self._ids = ids
                self._known = [str(n) for n in names][:len(ids)] if isinstance(names, list) else []
        except (OSError, ValueError, AttributeError):
            pass

    def _save_ids(self):
        if not self.primary:
            return  # (the daemon keeps them; a one-shot command next to it only reads them)
        try:
            self.ids_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ids_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ids": self._ids, "names": self._known}) + "\n")
            tmp.replace(self.ids_path)
        except OSError as exc:
            print(f"x11: could not keep the desktops' ids: {exc}", file=sys.stderr, flush=True)

    def _sync(self):
        """Give the desktops their ids (see the top), and the daemon its view of them."""
        names = (self._names + [""] * self._count)[:self._count]
        hint = self._hint if self._hint is not None and time.monotonic() < self._hint[2] else None
        windows = [(self._was_on[wid], w.desktop) for wid, w in self._windows.items()
                   if wid in self._was_on and w.desktop is not None]
        known = (self._known + [""] * len(self._ids))[:len(self._ids)]
        ids = realign(self._ids, known, names, self._count, windows, hint)
        if len(ids) != len(self._ids):
            self._hint = None  # (it explained this change, or this was not ours)
        save = ids != self._ids or names != self._known
        self._ids, self._known = ids, names
        self._was_on = {wid: ids[w.desktop] for wid, w in self._windows.items()
                        if w.desktop is not None and w.desktop < len(ids)}
        live = {}
        for position, ws_id in enumerate(ids):
            ws = self.workspaces.get(ws_id) or Desktop(proxy=position, id=ws_id)
            ws.proxy, ws.name, ws.coords = position, names[position], (position + 1,)
            ws.state, ws.removed = int(position == self._current), False
            live[ws_id] = ws
        for ws_id, ws in self.workspaces.items():
            if ws_id not in live:
                ws.removed = True
        self.workspaces = live
        if save:
            self._save_ids()

    # ---- what the X server tells us ----------------------------------------------------------
    def _read_root(self, props):
        get = self.conn.get
        root = self.conn.root
        if "_NET_NUMBER_OF_DESKTOPS" in props:
            self._count = max(0, _number(get(root, "_NET_NUMBER_OF_DESKTOPS"), 0))
        if "_NET_DESKTOP_NAMES" in props:
            self._names = _strings(get(root, "_NET_DESKTOP_NAMES"))
        if "_NET_CURRENT_DESKTOP" in props:
            self._current = _number(get(root, "_NET_CURRENT_DESKTOP"), 0)
        if "_NET_CLIENT_LIST" in props:
            self._read_clients(get(root, "_NET_CLIENT_LIST") or [])
        if "_NET_ACTIVE_WINDOW" in props or "_NET_CLIENT_LIST" in props:
            if "_NET_ACTIVE_WINDOW" in props:
                self._active = _number(get(root, "_NET_ACTIVE_WINDOW"), 0)
            now = time.time()
            for wid, w in self._windows.items():
                active = wid == self._active
                if active and not w.active:
                    w.focused_at = now  # when it got the keyboard, as it happens
                w.active = active

    def _read_clients(self, wids: list):
        now = time.time()
        live = set(wids)
        for wid in wids:
            if wid not in self._windows:
                w = Window(proxy=wid, identifier=f"{self._token}-0x{wid:x}")
                self.conn.watch(wid)  # (before reading: a change in between is not missed)
                self._read_window(w, WINDOW_PROPS)
                self._windows[wid] = w
        for wid in [wid for wid in self._windows if wid not in live]:
            self._gone(self._windows.pop(wid), now)

    def _read_window(self, w: Window, props):
        get, wid = self.conn.get, w.proxy
        if "_NET_WM_NAME" in props:
            w.name = get(wid, "_NET_WM_NAME") or ""
        if "WM_NAME" in props:
            w.legacy_name = get(wid, "WM_NAME") or ""
        if "WM_CLASS" in props:
            w.wm_class = tuple((_strings(get(wid, "WM_CLASS")) + ["", ""])[:2])
        if "_GTK_APPLICATION_ID" in props:
            w.gtk_app = get(wid, "_GTK_APPLICATION_ID") or ""
        if "_NET_WM_PID" in props:
            w.pid = _number(get(wid, "_NET_WM_PID"), 0)
        if "_NET_WM_DESKTOP" in props:
            desktop = _number(get(wid, "_NET_WM_DESKTOP"))
            w.desktop = None if desktop is None or desktop == ALL_DESKTOPS else desktop
        if "_NET_WM_WINDOW_TYPE" in props:
            kinds = [k for k in get(wid, "_NET_WM_WINDOW_TYPE") or [] if k.startswith("_NET_WM_WINDOW_TYPE_")]
            w.wanted = not kinds or kinds[0] in WANTED  # (the first one it knows counts)
        w.title = (w.name or w.legacy_name).rstrip("\0")
        w.app_id = app_id(w.gtk_app, *w.wm_class)

    def _read_screens(self):
        self.screens = [Screen(name, x, y, width, height) for name, x, y, width, height in self.conn.monitors() if name]

    def _gone(self, w: Window, now: float):
        w.closed = True
        # clicking a window's close button focuses it a moment before it goes: keep that
        if w.focused_at:
            self.closed_focus[w.identifier] = w.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}

    def _take(self, events: list) -> bool:
        """Take in the X server's events; whether anything we keep changed."""
        root_props, window_props, screens = set(), {}, False
        for ev in events:
            if ev[0] == "prop":
                _, wid, prop, stamp = ev
                self._time = max(self._time, stamp)
                if wid == self.conn.root:
                    if prop in ROOT_PROPS:
                        root_props.add(prop)
                elif prop in WINDOW_PROPS and wid in self._windows:
                    window_props.setdefault(wid, set()).add(prop)
            elif ev[0] == "configure":
                w = self._windows.get(ev[1])
                if w is not None:
                    w.geometry = None  # (read again when needed)
            elif ev[0] == "screens":
                screens = True
        if root_props:
            self._read_root(root_props)
        for wid, props in window_props.items():
            if wid in self._windows:
                self._read_window(self._windows[wid], props)
        if screens:
            self._read_screens()
        return bool(root_props or window_props or screens)

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    def fd(self) -> int:
        return self.conn.fileno()

    def pump(self):
        """Take in what the X server said. Reading a property can bring more events along, which
        python-xlib queues where the daemon's watch on the connection never sees them: so again
        until there are none (and list() looks once more)."""
        changed = False
        for _ in range(5):
            events = self.conn.events()
            if not events:
                break
            changed = self._take(events) or changed
        if changed:
            self._changed()

    def wait(self, seconds: float = 1.5):
        """For one-shot commands: everything was read on connecting, so only a moment."""
        end = time.monotonic() + min(seconds, 0.2)
        while time.monotonic() < end:
            self.conn.sync()
            self._take(self.conn.events())
            time.sleep(0.02)

    def roundtrip(self):
        self.conn.sync()
        self._take(self.conn.events())

    def settle(self, max_rounds: int = 6):
        self.roundtrip()
        self._sync()

    def close(self):
        self.conn.close()

    @property
    def toplevels(self) -> dict[str, Window]:
        """The windows a task bar would list, by identifier."""
        return {w.identifier: w for w in self._windows.values() if w.wanted and not w.closed}

    def list(self, all_groups: bool = False) -> list[Desktop]:
        self.pump()  # (events left in python-xlib's queue: see pump)
        self._sync()
        return sorted((w for w in self.workspaces.values() if not w.removed), key=lambda w: w.coords)

    def active(self) -> Desktop | None:
        return next((w for w in self.list() if w.active), None)

    def by_name(self, name: str) -> Desktop | None:
        return next((w for w in self.list() if w.name == name), None)

    def resolve(self, target: str) -> Desktop | None:
        """A desktop by name, else by its number (from 1)."""
        found = self.by_name(target)
        if found is None and target.isdigit():
            found = next((w for w in self.list() if w.index == int(target)), None)
        return found

    def shortcut_label(self, ws, total: int) -> str:
        """The chip on a tab: the key the desktop binds to "switch to workspace N" (Xfce's
        Ctrl+F1… out of the box; Cinnamon and MATE bind none until you do)."""
        checked, keys = self._shortcuts
        now = time.monotonic()
        if not checked or now - checked > 2:
            keys = switch_shortcuts()
            self._shortcuts = (now, keys)
        accel = keys.get(ws.index)
        if not accel:
            return ""
        from sidelit.gnome import chip_text  # (GTK's accelerators, as GNOME's are)
        return chip_text(accel)

    # windows
    def _desktop_at(self, position: int | None) -> Desktop | None:
        if position is None or not 0 <= position < len(self._ids):
            return None
        ws = self.workspaces.get(self._ids[position])
        return ws if ws is not None and not ws.removed else None

    def workspace_of(self, tl: Window) -> Desktop | None:
        """The desktop a window is on; None while it is on all of them."""
        return self._desktop_at(tl.desktop)

    def copy_ids_of(self, tl: Window) -> frozenset:
        ws = self.workspace_of(tl)
        return frozenset({ws.id}) if ws is not None else frozenset()

    def toplevels_on(self, ws: Desktop) -> list[Window]:
        return [t for t in self.toplevels.values() if t.desktop is not None and t.desktop == ws.proxy]

    def output_names(self) -> set[str]:
        return {s.name for s in self.screens}

    def outputs_of(self, tl: Window) -> list[Screen]:
        """The screen the middle of a window is on (none while it is off every screen)."""
        if len(self.screens) < 2:
            return list(self.screens)
        if tl.geometry is None:
            tl.geometry = self.conn.geometry(tl.proxy)
        if tl.geometry is None:
            return []
        x, y, width, height = tl.geometry
        return [s for s in self.screens if s.holds(x + width // 2, y + height // 2)][:1]

    def output_of(self, tl: Window) -> Screen | None:
        return next(iter(self.outputs_of(tl)), None)

    @property
    def can_move_windows(self) -> bool:
        return True

    def focus(self, tl: Window):
        """Give a window the keyboard, on its own desktop: switch there first, as xfwm4 would
        rather bring the window over to yours (its "activate_action" is "bring" out of the box)."""
        if tl.closed:
            return
        ws = self.workspace_of(tl)
        if ws is not None and not ws.active:
            self.activate(ws)
        self.conn.send(tl.proxy, "_NET_ACTIVE_WINDOW", [PAGER, self._time, 0])

    def close_toplevel(self, tl: Window):
        if not tl.closed:
            self.conn.send(tl.proxy, "_NET_CLOSE_WINDOW", [self._time, PAGER])

    def move_toplevel(self, tl: Window, ws: Desktop, output: str | None = None) -> bool:
        """Put a window on a desktop, and on screen `output` if given and there."""
        if tl.closed or ws.removed:
            return False
        if tl.desktop != ws.proxy:
            self.conn.send(tl.proxy, "_NET_WM_DESKTOP", [ws.proxy, PAGER])
            tl.desktop = ws.proxy  # (the window manager confirms it in a moment)
        target = next((s for s in self.screens if s.name == output), None) if output else None
        if target is not None and self.output_of(tl) not in (None, target):
            self._to_screen(tl, target)
        return True

    def _maximised(self, tl: Window) -> list:
        states = self.conn.get(tl.proxy, "_NET_WM_STATE") or []
        return [s for s in ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ") if s in states]

    def _to_screen(self, tl: Window, target: Screen):
        """Move a window to another screen, at the same place on it as far as it fits. A
        maximised one is unmaximised, moved and maximised again there, each step once the
        window manager has done the one before (xfwm4 maximises it where it was otherwise)."""
        here = self.output_of(tl)
        if tl.geometry is None or here is None:
            return
        maxed = self._maximised(tl)
        if maxed:
            self.conn.send(tl.proxy, "_NET_WM_STATE", [0, *(maxed + [0])[:2], PAGER])  # (0: take away)
            self._await(lambda: not self._maximised(tl))
            tl.geometry = self.conn.geometry(tl.proxy) or tl.geometry  # (back to its size before)
        x, y, width, height = tl.geometry
        x = target.x + max(0, min(x - here.x, target.width - width))
        y = target.y + max(0, min(y - here.y, target.height - height))
        flags = STATIC_GRAVITY | 1 << 8 | 1 << 9 | PAGER << 12  # x and y given, from a pager
        self.conn.send(tl.proxy, "_NET_MOVERESIZE_WINDOW", [flags, x, y, 0, 0])
        tl.geometry = None
        if maxed:
            def there() -> bool:
                tl.geometry = None  # (read it again each time)
                return self.output_of(tl) == target
            self._await(there)
            self.conn.send(tl.proxy, "_NET_WM_STATE", [1, *(maxed + [0])[:2], PAGER])  # (1: add)

    # desktops
    def activate(self, ws: Desktop):
        self.conn.send(self.conn.root, "_NET_CURRENT_DESKTOP", [ws.proxy, self._time])
        self._current = ws.proxy  # (the window manager confirms it in a moment)
        for w in self.workspaces.values():
            w.state = int(w is ws)

    def _await(self, done: Callable[[], bool]) -> bool:
        """Wait for the window manager to have done something we asked (ACT_WAIT_S at most)."""
        end = time.monotonic() + ACT_WAIT_S
        while True:
            self.conn.sync()
            if self._take(self.conn.events()):
                self._changed()
            if done() or time.monotonic() >= end:
                return done()
            time.sleep(0.02)

    def _set_names(self, names: list):
        self.conn.set_strings("_NET_DESKTOP_NAMES", names)
        self._names = list(names)

    def create(self, name: str) -> Desktop | None:
        """A new desktop at the end, called `name`."""
        self._sync()
        before = self._count
        self._hint = ("add", before, time.monotonic() + HINT_S)
        self.conn.send(self.conn.root, "_NET_NUMBER_OF_DESKTOPS", [before + 1])
        if not self._await(lambda: self._count == before + 1):
            self._hint = None
            print(f"x11: the window manager did not add a desktop ({self._count} of them)", file=sys.stderr, flush=True)
            return None
        self._sync()
        names = (self._names + [""] * self._count)[:self._count]
        names[before] = name
        self._set_names(names)
        self._sync()
        return self._desktop_at(before)

    def rename(self, ws: Desktop, name: str):
        self._sync()
        if ws.removed or ws.proxy >= self._count:
            return
        names = (self._names + [""] * self._count)[:self._count]
        names[ws.proxy] = name
        self._set_names(names)
        self._sync()

    def remove(self, ws: Desktop):
        """Remove a desktop. EWMH drops only the last one, so the last one goes first (nothing
        changes if the window manager will not), then for one in the middle the windows and
        names after it move down by one. Its own windows (the daemon removes only empty ones)
        go to the desktop before it."""
        self._sync()
        at, count, current = ws.proxy, self._count, self._current
        if ws.removed or count < 2 or not 0 <= at < count:
            return
        was_on = {w.proxy: w.desktop for w in self._windows.values() if w.desktop is not None}
        names = (self._names + [""] * count)[:count]
        self._hint = ("remove", at, time.monotonic() + HINT_S)
        self.conn.send(self.conn.root, "_NET_NUMBER_OF_DESKTOPS", [count - 1])
        if not self._await(lambda: self._count == count - 1):
            self._hint = None
            print(f"x11: the window manager did not remove a desktop ({self._count} of them)", file=sys.stderr, flush=True)
            return
        if at < count - 1:
            for wid, desktop in was_on.items():
                w = self._windows.get(wid)
                if w is not None and desktop >= at:
                    w.desktop = max(desktop - 1, 0)  # (the window manager confirms it in a moment)
                    self.conn.send(wid, "_NET_WM_DESKTOP", [w.desktop, PAGER])
            self._set_names(names[:at] + names[at + 1:])
            if current > at:  # you stay where you were
                self.conn.send(self.conn.root, "_NET_CURRENT_DESKTOP", [current - 1, self._time])
                self._current = current - 1
        self._sync()
