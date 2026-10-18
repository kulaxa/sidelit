"""GNOME workspace + window client: Sidelit's GNOME Shell extension, over the session bus.

GNOME has no Wayland protocol that lets an outside app see or arrange other windows, so
Sidelit ships a GNOME Shell extension (gnome-extension/, sidelit@kulaxa.github.io). It
exports io.github.kulaxa.Sidelit.Shell at /io/github/kulaxa/Sidelit on GNOME Shell's own
bus connection (org.gnome.Shell):

- GetState: every workspace and window, as JSON; the Changed signal sends the same whenever
  something changes (a window whose title changed alone, with "full": false);
- methods that switch, add, pin, reorder, rename and remove workspaces, and focus, move and
  close windows.

The extension also runs the sidebar, for as long as the daemon's client holds the bus name
io.github.kulaxa.sidelit (the one KDE's client holds too).

GNOME drops a workspace once it is empty (dynamic workspaces, the default), as COSMIC does,
unless it is pinned: "pin" is in caps then, and the daemon keeps a workspace by pinning it.
With a fixed number of workspaces GNOME drops none and "pin" is not in caps: the daemon adds
a workspace where it would pin one, as on KDE.

Workspace ids are the extension's, and last while it runs (it keeps running while the screen
is locked). Turned off and on again, it gives its workspaces new ids; this client then gives
each one back the id it had, by its position and the windows on it, so the daemon sees
nothing change. Window ids are GNOME's own and stay the same.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from sidelit import model  # noqa: E402

SHELL = "org.gnome.Shell"
PATH, IFACE = "/io/github/kulaxa/Sidelit", "io.github.kulaxa.Sidelit.Shell"
UUID = "sidelit@kulaxa.github.io"
# the extension runs the sidebar while this name is held; a test instance (SIDELIT_HOME)
# holds one of its own, so it never gets a sidebar from the real extension
_TEST = "" if not os.environ.get("SIDELIT_HOME") else \
    "t" + hashlib.sha1(os.environ["SIDELIT_HOME"].encode()).hexdigest()[:8]
BUS_NAME = "io.github.kulaxa.sidelit" + (f".{_TEST}" if _TEST else "")
HOSTED_ENV = "SIDELIT_HOSTED"  # the extension sets it to "gnome" in the sidebar it runs

CALL_TIMEOUT_MS = 3000
RETRY_S = 2.0  # while the extension is not there: how often to look again
ENABLE_HINT = (f"turn on Sidelit's GNOME Shell extension: gnome-extensions enable {UUID} "
               "(after installing it, log out and back in once so GNOME sees it)")
_GONE_ERRORS = ("org.freedesktop.DBus.Error.UnknownMethod", "org.freedesktop.DBus.Error.UnknownObject",
                "org.freedesktop.DBus.Error.UnknownInterface", "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner")


@dataclass
class Space(model.Workspace):  # a GNOME workspace: proxy and id are the extension's id for it
    kept: bool = False  # pinned: GNOME keeps it while it is empty

    @property
    def pinned(self) -> bool:
        return self.kept


@dataclass
class Window(model.Toplevel):  # identifier: GNOME's own id for the window
    workspace: str | None = None  # the id of the workspace it is on; None while on all of them
    output: str = ""              # the screen it is on, by connector name
    active: bool = False          # it has the keyboard
    focused_at: float = 0.0       # when it last got the keyboard

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str  # connector, e.g. "DP-1"


# ---- the "Switch to workspace N" shortcuts -------------------------------------------------------
KEYS_SCHEMA = "org.gnome.desktop.wm.keybindings"
_KEY_NAMES = {"Page_Up": "PgUp", "Page_Down": "PgDn", "Return": "Enter", "Escape": "Esc"}
_MODIFIERS = {"super": "⊞", "control": "Ctrl", "primary": "Ctrl", "ctrl": "Ctrl", "alt": "Alt",
              "shift": "Shift", "meta": "Meta", "hyper": "Hyper"}


def chip_text(accel: str) -> str:
    """"<Super>Home" -> "⊞ Home", "<Control><Alt>1" -> "Ctrl Alt 1": a GTK accelerator as the
    shortcut chip on a tab shows it."""
    mods = re.findall(r"<([^>]+)>", accel)
    key = re.sub(r"<[^>]+>", "", accel).strip()
    if not key:
        return ""
    parts = [_MODIFIERS.get(m.lower(), m) for m in mods]
    key = _KEY_NAMES.get(key, key)
    parts.append(key[3:] if key.startswith("KP_") else key.replace("_", " "))
    return " ".join(parts)


def switch_shortcuts(read: Callable[[str], list] | None = None) -> dict:
    """Workspace number -> the first key bound to "Switch to workspace N", and "last" -> the
    one for the last workspace, from GNOME's settings. `read(key)` is for the tests."""
    if read is None:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup(KEYS_SCHEMA, True) is None:
            return {}
        settings = Gio.Settings.new(KEYS_SCHEMA)
        read = settings.get_strv
    keys: dict = {}
    for n in range(1, 13):
        bound = [k for k in read(f"switch-to-workspace-{n}") or [] if k]
        if bound:
            keys[n] = bound[0]
    last = [k for k in read("switch-to-workspace-last") or [] if k]
    if last:
        keys["last"] = last[0]
    return keys


def extension_running() -> bool:
    """Is Sidelit's extension running in this session's GNOME Shell? (for `sidelit start`)"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(SHELL, PATH, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (IFACE, "Version")), None, Gio.DBusCallFlags.NONE, 2000, None)
        return reply is not None
    except GLib.Error:
        return False


# ---- for the sidebar the extension runs -------------------------------------------------------------
def hosted() -> bool:
    """Is this the sidebar GNOME Shell runs (the extension started it)?"""
    return os.environ.get(HOSTED_ENV) == "gnome"


def _sidebar_call(method: str, args: GLib.Variant | None = None) -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(SHELL, PATH, IFACE, method, args, GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
        return bool(reply.unpack()[0])
    except GLib.Error as exc:
        print(f"sidebar: GNOME Shell: {method}: {exc.message}", file=sys.stderr, flush=True)
        return False


def place_sidebar(connector: str) -> bool:
    """Ask the extension to keep this sidebar on the screen `connector` ('' for the leftmost).
    The strip it keeps free is as wide as the sidebar's window."""
    return _sidebar_call("PlaceSidebar", GLib.Variant("(s)", (connector or "",)))


def focus_sidebar() -> bool:
    """Ask for the keyboard: GNOME gives a dock it only when asked, not on a click."""
    return _sidebar_call("FocusSidebar")


class GnomeClient:
    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True,
                 bus: Gio.DBusConnection | None = None, wait_s: float | None = None):
        """`wait_s`: how long to wait for the extension to be there; None waits for as long as
        it takes for the daemon (primary) and not at all for a one-shot command. `bus` is for
        the tests (a private bus)."""
        self.on_change = on_change
        self.primary = primary
        self.workspaces: dict[str, Space] = {}  # by our id (the extension's, or the one it had)
        self.toplevels: dict[str, Window] = {}  # by GNOME's window id
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()      # (the daemon sets it; every workspace has one id here)
        self.dynamic = True        # GNOME drops empty workspaces (unless pinned)
        self.can_pin = True        # the extension can keep them
        self.locked = False       # the screen is locked: the extension changes nothing
        self.online = False        # the extension is there
        self._run = ""             # which time the extension was turned on, as it says
        self._seq = 0              # the number of the newest report taken in
        self._alias: dict[str, str] = {}  # the extension's id -> ours, after it was turned off and on
        self._shortcuts: tuple = (0.0, {})
        self._signals: list[int] = []
        self._owns_name = False
        self._shell_owner = ""
        try:
            self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            raise RuntimeError(f"no session bus ({exc.message})") from None
        try:
            self._connect(wait_s if wait_s is not None else (None if primary else 0.0))
        except Exception:
            self.close()
            raise

    def _connect(self, wait_s: float | None):
        self._signals.append(self.bus.signal_subscribe(
            SHELL, IFACE, "Changed", PATH, None, Gio.DBusSignalFlags.NONE, self._on_changed_signal))
        self._signals.append(self.bus.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", SHELL,
            Gio.DBusSignalFlags.NONE, self._on_shell_owner))
        if self.primary:
            self._own_name()
        if self._fetch():
            return
        if wait_s is not None and wait_s <= 0:
            raise RuntimeError(f"Sidelit's GNOME Shell extension is not running; {ENABLE_HINT}")
        print(f"gnome: waiting for Sidelit's GNOME Shell extension; {ENABLE_HINT}", file=sys.stderr, flush=True)
        context = GLib.MainContext.default()
        end = None if wait_s is None else time.monotonic() + wait_s
        retry = time.monotonic() + RETRY_S
        while not self.online:
            if end is not None and time.monotonic() > end:
                raise RuntimeError(f"Sidelit's GNOME Shell extension did not appear; {ENABLE_HINT}")
            if not context.iteration(False):
                time.sleep(0.05)
            if not self.online and time.monotonic() > retry:
                retry = time.monotonic() + RETRY_S
                self._fetch()
        print("gnome: the extension is there", file=sys.stderr, flush=True)

    # ---- D-Bus plumbing ---------------------------------------------------------------------
    def _bus_call(self, dest: str, path: str, iface: str, method: str, args: GLib.Variant | None = None) -> tuple:
        reply = self.bus.call_sync(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE,
                                   CALL_TIMEOUT_MS, None)
        return reply.unpack() if reply is not None else ()

    def _call(self, method: str, args: GLib.Variant | None = None) -> tuple | None:
        """A method of the extension; None when it failed (it is said why)."""
        try:
            return self._bus_call(SHELL, PATH, IFACE, method, args)
        except GLib.Error as exc:
            if any(name in (exc.message or "") for name in _GONE_ERRORS):
                self.online = False
            print(f"gnome: {method}: {exc.message}", file=sys.stderr, flush=True)
            return None

    def _own_name(self):
        flags = 4  # DBUS_NAME_FLAG_DO_NOT_QUEUE: have it now or not at all
        got = self._bus_call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                             "RequestName", GLib.Variant("(su)", (BUS_NAME, flags)))[0]
        if got not in (1, 4):  # primary owner / already ours
            raise RuntimeError(f"another Sidelit daemon holds {BUS_NAME} on the session bus")
        self._owns_name = True

    def _changed(self):
        if self.on_change:
            self.on_change()

    # ---- what the extension tells us --------------------------------------------------------
    def _fetch(self) -> bool:
        """Everything, now. False while the extension is not there."""
        try:
            text = self._bus_call(SHELL, PATH, IFACE, "GetState")[0]
            report = json.loads(text)
        except GLib.Error:
            return False
        except (ValueError, TypeError, IndexError) as exc:
            print(f"gnome: bad state from the extension: {exc}", file=sys.stderr, flush=True)
            return False
        return self._apply(report)

    def _on_changed_signal(self, bus, sender, path, iface, signal, params):
        try:
            report = json.loads(params.unpack()[0])
        except (ValueError, TypeError, IndexError) as exc:
            print(f"gnome: bad report from the extension: {exc}", file=sys.stderr, flush=True)
            return
        if self._apply(report):
            self._changed()

    def _on_shell_owner(self, bus, sender, path, iface, signal, params):
        name, _old, new = params.unpack()
        if name != SHELL:
            return
        if not new:
            self.online = False  # GNOME Shell went (a logout, or X11's restart): keep what we had
            return
        if self._fetch():
            self._changed()

    def _apply(self, report: dict) -> bool:
        """Take in a report; whether anything was taken."""
        run = str(report.get("run") or "")
        if report.get("stopped"):
            if run == self._run:
                self.online = False  # turned off; its windows did not go, so neither do ours
            return False
        if run != self._run:
            if not report.get("full"):
                return self._fetch()  # a run we have not had in full yet: everything first
            if self._run:
                self._adopt_ids(report)
            self._run, self._seq = run, 0
        seq = report.get("seq")
        if isinstance(seq, int):
            # a signal sent just before an answer we already took in reaches us after it: old news
            if seq <= self._seq:
                return False
            self._seq = seq
        self.online = True
        now = time.time()
        if report.get("full"):
            self.dynamic = bool(report.get("dynamic", True))
            self.can_pin = bool(report.get("can_pin", True))
            self.locked = bool(report.get("locked"))
            self._workspaces_from(report.get("workspaces") or [])
            self.screens = [Screen(str(o["name"])) for o in report.get("outputs") or [] if o.get("name")]
        self._windows_from(report.get("windows") or [], bool(report.get("full")), now)
        return True

    def _ours(self, ext_id: str) -> str:
        return self._alias.get(ext_id, ext_id)

    def _theirs(self, our_id: str) -> str:
        return next((ext for ext, ours in self._alias.items() if ours == our_id), our_id)

    def _adopt_ids(self, report: dict):
        """The extension was turned off and on: same workspaces, new ids. Each new one gets the
        id of the one that was at its position with the same windows on it."""
        before = {ws.index: ws for ws in self.list()}
        windows_on: dict[str, set] = {}
        for tl in self.toplevels.values():
            if tl.workspace:
                windows_on.setdefault(tl.workspace, set()).add(tl.identifier)
        alias = {}
        for item in report.get("workspaces") or []:
            new_id = str(item.get("id") or "")
            old = before.get(int(item.get("index", -1)) + 1)
            now_on = {str(w.get("id")) for w in report.get("windows") or [] if w.get("workspace") == new_id}
            if new_id and old is not None and windows_on.get(old.id, set()) == now_on:
                alias[new_id] = old.id
        self._alias = alias
        print(f"gnome: the extension started again; {len(alias)} of {len(report.get('workspaces') or [])} "
              "workspaces keep their ids", file=sys.stderr, flush=True)

    def _workspaces_from(self, items: list):
        live: dict[str, Space] = {}
        for item in items:
            ws_id = self._ours(str(item.get("id") or ""))
            if not ws_id:
                continue
            ws = self.workspaces.get(ws_id) or Space(proxy=ws_id, id=ws_id)
            ws.name = str(item.get("name") or "")
            ws.coords = (int(item.get("index", 0)) + 1,)
            ws.state = int(bool(item.get("active")))
            ws.kept = bool(item.get("pinned"))
            ws.copies_pinned = ws.kept or not self.dynamic
            ws.removed = False
            live[ws_id] = ws
        for ws_id, ws in self.workspaces.items():
            if ws_id not in live:
                ws.removed = True
        self.workspaces = live

    def _windows_from(self, items: list, full: bool, now: float):
        seen = set()
        for item in items:
            wid = str(item.get("id") or "")
            if not wid:
                continue
            seen.add(wid)
            tl = self.toplevels.get(wid)
            if tl is None:
                tl = self.toplevels[wid] = Window(proxy=wid, identifier=wid)
            app = str(item.get("app") or "")
            if item.get("x11") and item.get("instance"):
                app = str(item["instance"])  # an X11 window's instance names it as the other desktops do
            tl.app_id = app
            tl.title = str(item.get("title") or "")
            tl.pid = int(item.get("pid") or 0)
            workspace = item.get("workspace")
            tl.workspace = self._ours(str(workspace)) if workspace and not item.get("all") else None
            tl.output = str(item.get("output") or "")
            active = bool(item.get("focused"))
            if active and not tl.active:
                tl.focused_at = now  # when it got the keyboard, as it happens
            tl.active = active
        if full:
            for wid in [w for w in self.toplevels if w not in seen]:
                self._gone(self.toplevels.pop(wid), now)

    def _gone(self, tl: Window, now: float):
        tl.closed = True
        # clicking a window's close button focuses it a moment before it goes: keep that
        if tl.focused_at:
            self.closed_focus[tl.identifier] = tl.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    @property
    def caps(self) -> frozenset:
        """What GNOME can do here. Workspaces are kept by pinning them while GNOME drops empty
        ones (dynamic workspaces); with a fixed number there is nothing to pin."""
        caps = {"create", "rename", "remove", "move", "focus", "close_toplevel"}
        if self.dynamic and self.can_pin:
            caps.add("pin")
        return frozenset(caps)

    def fd(self) -> int:
        return -1  # D-Bus arrives on the GLib main loop: on_change wakes the daemon

    def pump(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def wait(self, seconds: float = 1.5):
        """Handle D-Bus for a moment, for one-shot commands: the state came in on connecting
        and the actions are answered as they are made, so there is little to wait for."""
        context = GLib.MainContext.default()
        end = time.monotonic() + min(seconds, 0.2)
        while time.monotonic() < end:
            if not context.iteration(False):
                time.sleep(0.01)
        self.pump()

    def roundtrip(self):
        self.pump()

    def settle(self, max_rounds: int = 6):
        self._fetch()

    def close(self):
        for sub in self._signals:
            self.bus.signal_unsubscribe(sub)
        self._signals = []
        if self._owns_name:
            self._owns_name = False
            try:
                self._bus_call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "ReleaseName", GLib.Variant("(s)", (BUS_NAME,)))
            except GLib.Error:
                pass
        try:
            self.bus.flush_sync(None)
        except GLib.Error:
            pass

    def list(self, all_groups: bool = False) -> list[Space]:
        return sorted((w for w in self.workspaces.values() if not w.removed), key=lambda w: w.coords)

    def active(self) -> Space | None:
        return next((w for w in self.list() if w.active), None)

    def by_name(self, name: str) -> Space | None:
        return next((w for w in self.list() if w.name == name), None)

    def resolve(self, target: str) -> Space | None:
        """A workspace by name, else by its number (from 1)."""
        found = self.by_name(target)
        if found is None and target.isdigit():
            found = next((w for w in self.list() if w.index == int(target)), None)
        return found

    def shortcut_label(self, ws, total: int) -> str:
        """The chip on a tab: the key GNOME binds to "Switch to workspace N" (out of the box,
        Super+Home for the first and Super+End for the last)."""
        keys = self._switch_keys()
        accel = keys.get(ws.index) or (keys.get("last") if ws.index == total else None)
        return chip_text(accel) if accel else ""

    def _switch_keys(self) -> dict:
        checked, keys = self._shortcuts
        now = time.monotonic()
        if not checked or now - checked > 2:
            keys = switch_shortcuts()
            self._shortcuts = (now, keys)
        return keys

    # windows
    def workspace_of(self, tl: Window) -> Space | None:
        """The workspace a window is on; None while it is on all of them."""
        ws = self.workspaces.get(tl.workspace) if tl.workspace else None
        return ws if ws is not None and not ws.removed else None

    def copy_ids_of(self, tl: Window) -> frozenset:
        return frozenset({tl.workspace}) if self.workspace_of(tl) is not None else frozenset()

    def toplevels_on(self, ws: Space) -> list[Window]:
        return [t for t in self.toplevels.values() if not t.closed and t.workspace == ws.id]

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
            self._call("FocusWindow", GLib.Variant("(s)", (tl.identifier,)))

    def close_toplevel(self, tl: Window):
        if not tl.closed:
            self._call("CloseWindow", GLib.Variant("(s)", (tl.identifier,)))

    def move_toplevel(self, tl: Window, ws: Space, output: str | None = None) -> bool:
        """Put a window on a workspace, and on screen `output` if given and there."""
        if tl.closed or ws.removed:
            return False
        done = self._call("MoveWindow", GLib.Variant("(sss)", (tl.identifier, self._theirs(ws.id), output or "")))
        return bool(done and done[0])

    # workspaces
    def activate(self, ws: Space):
        done = self._call("ActivateWorkspace", GLib.Variant("(s)", (self._theirs(ws.id),)))
        if done and done[0]:
            for w in self.workspaces.values():  # (the next report confirms it)
                w.state = int(w is ws)

    def create(self, name: str) -> Space | None:
        """A new workspace at the end, kept (pinned where GNOME would drop it), called `name`."""
        done = self._call("AddWorkspace", GLib.Variant("(s)", (name,)))
        if not done or not done[0]:
            return None
        self._fetch()
        return self.workspaces.get(self._ours(done[0]))

    def rename(self, ws: Space, name: str):
        self._call("RenameWorkspace", GLib.Variant("(ss)", (self._theirs(ws.id), name)))
        self._fetch()

    def pin(self, ws: Space, pinned: bool = True):
        self._call("PinWorkspace", GLib.Variant("(sb)", (self._theirs(ws.id), pinned)))
        self._fetch()

    def move(self, ws: Space, other: Space, before: bool = True):
        """Reorder: put `ws` right before (or after) `other`."""
        if ws is other:
            return
        self._call("MoveWorkspace", GLib.Variant("(ssb)", (self._theirs(ws.id), self._theirs(other.id), before)))
        self._fetch()

    def remove(self, ws: Space):
        self._call("RemoveWorkspace", GLib.Variant("(s)", (self._theirs(ws.id),)))
        self._fetch()
