"""KDE Plasma 6 workspace + window client: KWin, over the session bus.

Workspaces are KWin's virtual desktops, from org.kde.KWin /VirtualDesktopManager. Its
`desktops` and `current` properties say what there is, its signals say when that changes, its
methods create, rename and remove desktops, and setting `current` switches to one.

D-Bus tells nothing about windows, so this client hands KWin a small script when it connects
(windows_script). The script calls our Windows method with every window it sees (app id, title,
desktops, screen, pid, whether it has the keyboard): at once, and again whenever a window opens,
closes, gets the keyboard, or changes its title, desktop or screen. Nothing can call into a KWin
script, so focusing, closing and moving a window go the other way: each is a script of its own,
run once, which says Done when it has acted and is then unloaded (action_script).

The daemon's client owns the well-known bus name io.github.kulaxa.sidelit, and its script keeps
reporting there. A one-shot command (`sidelit ws`, `sidelit open`: primary=False) gets a single
report, on its own unique bus name, so the two never take each other's reports.

KDE keeps every desktop until you remove it and has no way to reorder them, so "pin" and "move"
are not in caps: the sidebar offers no drag-to-reorder, and where the daemon would pin a fresh
workspace on COSMIC it adds a desktop here.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from sidelit import config, model  # noqa: E402

KWIN = "org.kde.KWin"
DESKTOPS_PATH, DESKTOPS_IFACE = "/VirtualDesktopManager", "org.kde.KWin.VirtualDesktopManager"
SCRIPTING_PATH, SCRIPTING_IFACE = "/Scripting", "org.kde.kwin.Scripting"
SCRIPT_IFACE = "org.kde.kwin.Script"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# what our scripts call; a test instance (SIDELIT_HOME) gets names of its own
_TEST = "" if not os.environ.get("SIDELIT_HOME") else \
    "t" + hashlib.sha1(os.environ["SIDELIT_HOME"].encode()).hexdigest()[:8]
BUS_NAME = "io.github.kulaxa.sidelit" + (f".{_TEST}" if _TEST else "")
OUR_PATH, OUR_IFACE = "/io/github/kulaxa/sidelit/KWin", "io.github.kulaxa.sidelit.KWin"
OUR_INTERFACE_XML = f"""<node><interface name="{OUR_IFACE}">
  <method name="Windows"><arg name="report" type="s" direction="in"/></method>
  <method name="Done"><arg name="script" type="s" direction="in"/></method>
</interface></node>"""
SCRIPT_PREFIX = "sidelit" + (f"-{_TEST}" if _TEST else "")

_clients = itertools.count(1)  # numbers each client's object path

CALL_TIMEOUT_MS = 3000
ONCE_WAIT_S = 2.0     # how long a one-shot command waits for its scripts to say Done
FIRST_REPORT_S = 2.0  # how long connecting waits for the script's first window report
STALE_S = 10.0        # a one-shot script that has not said Done by now never will


@dataclass
class Desktop(model.Workspace):  # a KWin virtual desktop: proxy and id are its KWin id
    pass


@dataclass
class Window(model.Toplevel):  # a window as the script reports it: identifier is KWin's internalId
    desktops: tuple = ()    # ids of the desktops it is on (none while it is on all of them)
    on_all: bool = False    # on every desktop
    output: str = ""        # the screen it is on, by connector name
    active: bool = False    # it has the keyboard
    focused_at: float = 0.0  # when it last got the keyboard

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str  # connector, e.g. "DP-1"


def _js(value) -> str:
    """A Python value as a JavaScript literal (JSON is one; ASCII keeps every engine happy)."""
    return json.dumps(value, ensure_ascii=True)


# Shared by every script: what a window looks like to us. Plasma 6 names (windowList,
# internalId, desktops, output); only normal windows and dialogs, as a task bar lists them, so
# panels, the desktop, OSDs, menus and our own sidebar stay out.
_JS_COMMON = """
var sidelit = {
    wanted: function (w) {
        return !!w && !w.deleted && (w.normalWindow || w.dialog);
    },
    info: function (w) {
        var desktops = [];
        for (var i = 0; w.desktops && i < w.desktops.length; i++) desktops.push(String(w.desktops[i].id));
        return {id: String(w.internalId), app: String(w.desktopFileName || w.resourceClass || ""),
                title: String(w.caption || ""), pid: w.pid || 0, desktops: desktops,
                all: !!w.onAllDesktops, output: w.output ? String(w.output.name) : "", active: !!w.active};
    },
    report: function (only, gone) {
        var list = only ? [only] : workspace.windowList(), out = [], screens = [];
        for (var i = 0; i < list.length; i++)
            if (list[i] !== gone && sidelit.wanted(list[i])) out.push(sidelit.info(list[i]));
        for (var j = 0; j < workspace.screens.length; j++) screens.push(String(workspace.screens[j].name));
        callDBus(SERVICE, OUR_PATH, OUR_IFACE, "Windows",
                 JSON.stringify({full: !only, windows: out, screens: screens}));
    },
    done: function () { callDBus(SERVICE, OUR_PATH, OUR_IFACE, "Done", NAME); },
    byId: function (id) {
        var list = workspace.windowList();
        for (var i = 0; i < list.length; i++) if (String(list[i].internalId) === id) return list[i];
        return null;
    }
};
"""

# the daemon's script: everything once, then every change as it happens (a title, which busy
# Claudes animate ten times a second, goes alone; a window opening or closing, all of them)
_JS_WATCH = """
function sidelitWatch(w) {
    var one = function () { if (sidelit.wanted(w)) sidelit.report(w); };
    w.captionChanged.connect(one);
    w.desktopsChanged.connect(one);
    w.outputChanged.connect(one);
    if (w.desktopFileNameChanged) w.desktopFileNameChanged.connect(one);
}
workspace.windowList().forEach(sidelitWatch);
workspace.windowAdded.connect(function (w) { sidelitWatch(w); sidelit.report(null); });
workspace.windowRemoved.connect(function (w) { sidelit.report(null, w); });
workspace.windowActivated.connect(function () { sidelit.report(null); });
workspace.screensChanged.connect(function () { sidelit.report(null); });
sidelit.report(null);
"""

# what each action does to window `w` (found by its id)
ACTIONS = {
    "focus": "workspace.activeWindow = w;",
    "close": "w.closeWindow();",
    "move": """
        var desktop = workspace.desktops.filter(function (d) { return String(d.id) === ARG.desktop; })[0];
        if (desktop) w.desktops = [desktop];
        var screen = workspace.screens.filter(function (s) { return String(s.name) === ARG.output; })[0];
        if (screen && w.output !== screen) workspace.sendClientToScreen(w, screen);""",
}


def _script(what: str, to: tuple[str, str], name: str, body: str, arg: dict | None = None) -> str:
    """A script called `name` that reports to `to` (bus name, object path)."""
    service, path = to
    head = (f"var SERVICE = {_js(service)}, OUR_PATH = {_js(path)}, OUR_IFACE = {_js(OUR_IFACE)}, "
            f"NAME = {_js(name)}, ARG = {_js(arg or {})};")
    return f"// Sidelit: {what} for {service}\n{head}\n{_JS_COMMON}\n{body}"


def windows_script(to: tuple[str, str], name: str, watch: bool) -> str:
    """The script that reports windows to `to`: keeps doing so with `watch`, else once."""
    return _script("windows", to, name, _JS_WATCH if watch else "sidelit.report(null);\nsidelit.done();\n")


def action_script(to: tuple[str, str], name: str, action: str, window_id: str, arg: dict | None = None) -> str:
    """A one-shot script: do `action` (see ACTIONS) to the window, then say Done."""
    # (Done whatever happens: it is what gets the script unloaded)
    body = (f"try {{\n    var w = sidelit.byId({_js(window_id)});\n    if (w) {{ {ACTIONS[action]} }}\n"
            f"}} catch (e) {{\n    print(\"Sidelit: \" + NAME + \": \" + e);\n}} finally {{\n    sidelit.done();\n}}\n")
    return _script(action, to, name, body, arg)


# ---- the "Switch to Desktop N" shortcuts -------------------------------------------------------
SHORTCUTS_FILE = "kglobalshortcutsrc"
# KWin's own bindings, for a desktop with no shortcuts file (nothing changed yet)
DEFAULT_SWITCH_KEYS = {1: "Ctrl+F1", 2: "Ctrl+F2", 3: "Ctrl+F3", 4: "Ctrl+F4"}
_SWITCH_RE = re.compile(r"Switch to Desktop (\d+)")


def switch_shortcuts(text: str | None) -> dict[int, str]:
    """Desktop number -> the key bound to "Switch to Desktop N", from kglobalshortcutsrc.

    Each [kwin] line reads `Switch to Desktop 1=<current>,<default>,<description>`, the current
    keys tab-separated ("none" when unbound). No file, or none of these in it: KWin's defaults."""
    if text is None:
        return dict(DEFAULT_SWITCH_KEYS)
    keys, section, found = {}, "", False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        name, sep, value = line.partition("=")
        match = _SWITCH_RE.fullmatch(name.strip())
        if section != "kwin" or not sep or match is None:
            continue
        found = True
        current = value.split(",", 1)[0].replace("\\t", "\t").split("\t")[0].strip()
        if current and current.lower() != "none":
            keys[int(match.group(1))] = current
    return keys if found else dict(DEFAULT_SWITCH_KEYS)


def chip_text(keys: str) -> str:
    """"Meta+Ctrl+1" -> "⊞ Ctrl 1": the shortcut chip's text, Super as ⊞ like on COSMIC."""
    parts = re.split(r"\+(?=.)", keys)
    return " ".join("⊞" if p in ("Meta", "Super") else p for p in parts)


class KWinClient:
    caps = frozenset(("create", "rename", "remove", "focus", "close_toplevel"))

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True,
                 bus: Gio.DBusConnection | None = None, script_dir: Path | None = None):
        """`bus` and `script_dir` are for the tests: a private bus, and where the scripts KWin
        reads are written (the runtime directory)."""
        self.on_change = on_change
        self.primary = primary
        self.workspaces: dict[str, Desktop] = {}   # by KWin desktop id
        self.toplevels: dict[str, Window] = {}     # by KWin internalId
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}   # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()       # (the daemon sets it; every desktop has one id here)
        self._pending: dict[str, float] = {}        # one-shot scripts not Done yet: name -> started
        self._seq = itertools.count(1)
        self._reported = False                     # a first window report has come in
        self._shortcuts: tuple = (None, 0.0, {})   # (mtime, checked at, keys)
        self._signals: list[int] = []
        self._registration = 0
        try:
            self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._kwin_owner = self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                          "org.freedesktop.DBus", "GetNameOwner", GLib.Variant("(s)", (KWIN,)))[0]
        except GLib.Error as exc:
            raise RuntimeError(f"KWin is not on the session bus ({exc.message}); is this a Plasma 6 session?") from None
        # where our scripts call us: a path of our own, should another client share the connection
        self.to = (BUS_NAME if primary else self.bus.get_unique_name(), f"{OUR_PATH}/{next(_clients)}")
        self.script_dir = script_dir or config.RUNTIME_DIR / "kwin"
        self._windows_script = f"{SCRIPT_PREFIX}-windows" if primary else self._once_name()
        self._owns_name = False
        self._watching = False  # our watching script is loaded (only ever the daemon's)
        try:
            self._connect()
        except GLib.Error as exc:
            self.close()
            raise RuntimeError(f"KWin: {exc.message}") from None
        except Exception:
            self.close()
            raise

    def _connect(self):
        if self.primary:
            self._own_name()
        self._registration = self.bus.register_object(
            self.to[1], Gio.DBusNodeInfo.new_for_xml(OUR_INTERFACE_XML).interfaces[0], self._on_call, None, None)
        self._signals.append(self.bus.signal_subscribe(
            KWIN, DESKTOPS_IFACE, None, DESKTOPS_PATH, None, Gio.DBusSignalFlags.NONE, self._on_desktops_signal))
        self._signals.append(self.bus.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", KWIN,
            Gio.DBusSignalFlags.NONE, self._on_kwin_owner))
        self.script_dir.mkdir(mode=0o700, parents=True, exist_ok=True)  # KWin runs what is in here
        self._read_desktops()
        if not self._start_windows_script():
            raise RuntimeError("KWin did not run Sidelit's window script (KDE Plasma 6 is needed)")
        # the screens and windows come with the script's first report: have them before anyone
        # looks, as COSMIC has its screens on connecting (or the daemon would see screens arrive)
        context, end = GLib.MainContext.default(), time.monotonic() + FIRST_REPORT_S
        while not self._reported and time.monotonic() < end:
            if not context.iteration(False):
                time.sleep(0.005)

    def _start_windows_script(self) -> bool:
        name = self._windows_script
        self._watching = self._start_script(name, windows_script(self.to, name, watch=self.primary),
                                            once=not self.primary) and self.primary
        return self._watching or name in self._pending

    # ---- D-Bus plumbing ---------------------------------------------------------------------
    def _call(self, dest: str, path: str, iface: str, method: str, args: GLib.Variant | None = None) -> tuple:
        reply = self.bus.call_sync(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE,
                                   CALL_TIMEOUT_MS, None)
        return reply.unpack() if reply is not None else ()

    def _kwin(self, path: str, iface: str, method: str, args: GLib.Variant | None = None) -> tuple:
        return self._call(KWIN, path, iface, method, args)

    def _own_name(self):
        flags = 4  # DBUS_NAME_FLAG_DO_NOT_QUEUE: have it now or not at all
        got = self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                         GLib.Variant("(su)", (BUS_NAME, flags)))[0]
        if got not in (1, 4):  # primary owner / already ours
            raise RuntimeError(f"another Sidelit daemon holds {BUS_NAME} on the session bus")
        self._owns_name = True

    def _on_call(self, bus, sender, path, iface, method, params, invocation):
        if sender != self._kwin_owner:  # only KWin (our scripts) may tell us anything
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "only KWin reports here")
            return
        invocation.return_value(None)
        try:
            text = params.unpack()[0]
            if method == "Windows":
                self._on_windows(json.loads(text))
            elif method == "Done":
                self._script_done(text)
        except (ValueError, TypeError, AttributeError) as exc:
            print(f"kwin: bad {method} from the script: {exc}", file=sys.stderr, flush=True)

    def _changed(self):
        if self.on_change:
            self.on_change()

    # ---- scripts ----------------------------------------------------------------------------
    def _once_name(self) -> str:
        return f"{SCRIPT_PREFIX}-{os.getpid()}-{next(self._seq)}"

    def _start_script(self, name: str, source: str, once: bool) -> bool:
        """Load `source` into KWin as script `name` and run it. A one-shot one is unloaded when
        it says Done (or when we close)."""
        path = self.script_dir / f"{name}.js"
        loaded = False
        try:
            path.write_text(source)
            if self._kwin(SCRIPTING_PATH, SCRIPTING_IFACE, "isScriptLoaded", GLib.Variant("(s)", (name,)))[0]:
                self._kwin(SCRIPTING_PATH, SCRIPTING_IFACE, "unloadScript", GLib.Variant("(s)", (name,)))
            number = self._kwin(SCRIPTING_PATH, SCRIPTING_IFACE, "loadScript",
                                GLib.Variant("(ss)", (str(path), name)))[0]
            loaded = number >= 0
            if loaded:
                if once:
                    self._pending[name] = time.monotonic()
                self._kwin(f"/Scripting/Script{number}", SCRIPT_IFACE, "run")
                return True
        except (GLib.Error, OSError) as exc:
            print(f"kwin: script {name}: {getattr(exc, 'message', exc)}", file=sys.stderr, flush=True)
        self._pending.pop(name, None)
        if loaded:
            self._unload(name)
        else:
            path.unlink(missing_ok=True)
        return False

    def _unload(self, name: str):
        try:
            self.bus.call(KWIN, SCRIPTING_PATH, SCRIPTING_IFACE, "unloadScript", GLib.Variant("(s)", (name,)),
                          None, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None, None, None)
        except GLib.Error:
            pass
        try:
            (self.script_dir / f"{name}.js").unlink()
        except OSError:
            pass

    def _script_done(self, name: str):
        if self._pending.pop(name, None) is not None:
            self._unload(name)

    def _act(self, action: str, tl: Window, arg: dict | None = None) -> bool:
        if tl.closed or not tl.identifier:
            return False
        now = time.monotonic()
        for stale in [n for n, started in self._pending.items() if now - started > STALE_S]:
            self._script_done(stale)  # never said Done (KWin dropped it?): unload it all the same
        name = self._once_name()
        return self._start_script(name, action_script(self.to, name, action, tl.identifier, arg), once=True)

    # ---- what KWin tells us -----------------------------------------------------------------
    def _read_desktops(self):
        try:
            props = self._kwin(DESKTOPS_PATH, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (DESKTOPS_IFACE,)))[0]
        except GLib.Error as exc:
            raise RuntimeError(f"KWin's virtual desktops are not on D-Bus ({exc.message})") from None
        current = str(props.get("current") or "")
        live: dict[str, Desktop] = {}
        for position, desktop_id, name in props.get("desktops") or []:
            ws = self.workspaces.get(desktop_id) or Desktop(proxy=desktop_id, id=desktop_id)
            ws.name, ws.coords, ws.state, ws.removed = name, (int(position) + 1,), int(desktop_id == current), False
            live[desktop_id] = ws
        for desktop_id, ws in self.workspaces.items():
            if desktop_id not in live:
                ws.removed = True
        self.workspaces = live

    def _on_desktops_signal(self, *_):
        try:
            self._read_desktops()
        except RuntimeError as exc:
            print(f"kwin: {exc}", file=sys.stderr, flush=True)
            return
        self._changed()

    def _on_kwin_owner(self, bus, sender, path, iface, signal, params):
        name, _old, new = params.unpack()
        if name != KWIN:
            return
        self._kwin_owner = new
        if not new:
            return  # KWin went; it reports again when it is back
        # a new KWin (it restarted): our script went with the old one
        try:
            self._read_desktops()
        except RuntimeError:
            return
        self._start_windows_script()
        self._changed()

    def _on_windows(self, report: dict):
        now = time.time()
        seen = set()
        for item in report.get("windows") or []:
            wid = str(item.get("id") or "")
            if not wid:
                continue
            seen.add(wid)
            tl = self.toplevels.get(wid)
            if tl is None:
                tl = self.toplevels[wid] = Window(proxy=wid, identifier=wid)
            tl.app_id = str(item.get("app") or "")
            if tl.app_id.endswith(".desktop"):
                tl.app_id = tl.app_id[: -len(".desktop")]
            tl.title = str(item.get("title") or "")
            tl.pid = int(item.get("pid") or 0)
            tl.desktops = tuple(str(d) for d in item.get("desktops") or ())
            tl.on_all = bool(item.get("all"))
            tl.output = str(item.get("output") or "")
            active = bool(item.get("active"))
            if active and not tl.active:
                tl.focused_at = now  # when it got the keyboard, as it happens
            tl.active = active
        if report.get("full"):
            for wid in [w for w in self.toplevels if w not in seen]:
                self._gone(self.toplevels.pop(wid), now)
            self.screens = [Screen(str(n)) for n in report.get("screens") or [] if n]
            self._reported = True
        self._changed()

    def _gone(self, tl: Window, now: float):
        tl.closed = True
        # clicking a window's close button focuses it a moment before it goes: keep that
        if tl.focused_at:
            self.closed_focus[tl.identifier] = tl.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    def fd(self) -> int:
        return -1  # D-Bus arrives on the GLib main loop: on_change wakes the daemon

    def pump(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def wait(self, seconds: float = 1.5):
        """Handle D-Bus for up to `seconds`, for one-shot commands with no main loop of their own:
        until the window report is in and every action has been done."""
        context = GLib.MainContext.default()
        end = time.monotonic() + seconds
        while time.monotonic() < end and (self._pending or not self._reported):
            if not context.iteration(False):
                time.sleep(0.01)
        self.pump()

    def roundtrip(self):
        self.wait(ONCE_WAIT_S)

    def settle(self, max_rounds: int = 6):
        self._read_desktops()

    def close(self):
        """Unload our scripts and let go of the bus: KWin keeps a script until it is unloaded."""
        for name in list(self._pending):
            self._unload(name)
        self._pending.clear()
        if self._watching:  # (never another daemon's: one that failed to start loaded nothing)
            self._watching = False
            self._unload(self._windows_script)
        for sub in self._signals:
            self.bus.signal_unsubscribe(sub)
        self._signals = []
        if self._registration:
            self.bus.unregister_object(self._registration)
            self._registration = 0
        if self._owns_name:
            self._owns_name = False
            try:
                self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReleaseName",
                           GLib.Variant("(s)", (BUS_NAME,)))
            except GLib.Error:
                pass
        try:
            self.bus.flush_sync(None)
        except GLib.Error:
            pass

    def list(self, all_groups: bool = False) -> list[Desktop]:
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
        """The chip on a tab: the key bound to "Switch to Desktop N" in KDE's shortcuts."""
        keys = self._switch_keys().get(ws.index)
        return chip_text(keys) if keys else ""

    def _switch_keys(self) -> dict[int, str]:
        mtime, checked, keys = self._shortcuts
        now = time.monotonic()
        if checked and now - checked < 2:
            return keys
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / SHORTCUTS_FILE
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = None
        if checked == 0.0 or stamp != mtime:
            try:
                keys = switch_shortcuts(path.read_text(errors="replace") if stamp is not None else None)
            except OSError:
                keys = switch_shortcuts(None)
        self._shortcuts = (stamp, now, keys)
        return keys

    # windows
    def workspace_of(self, tl: Window) -> Desktop | None:
        """The desktop a window is on (the first, if several); None while it is on all of them."""
        if tl.on_all:
            return None
        on = [self.workspaces[d] for d in tl.desktops if d in self.workspaces]
        return min(on, key=lambda w: w.coords) if on else None

    def copy_ids_of(self, tl: Window) -> frozenset:
        return frozenset() if tl.on_all else frozenset(d for d in tl.desktops if d in self.workspaces)

    def toplevels_on(self, ws: Desktop) -> list[Window]:
        return [t for t in self.toplevels.values() if not t.closed and not t.on_all and ws.id in t.desktops]

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
        self._act("focus", tl)

    def close_toplevel(self, tl: Window):
        self._act("close", tl)

    def move_toplevel(self, tl: Window, ws: Desktop, output: str | None = None) -> bool:
        """Put a window on a desktop, and on screen `output` if given and there."""
        if ws.removed:
            return False
        return self._act("move", tl, {"desktop": ws.id, "output": output or ""})

    # desktops
    def activate(self, ws: Desktop):
        try:
            self._kwin(DESKTOPS_PATH, PROPS_IFACE, "Set",
                       GLib.Variant("(ssv)", (DESKTOPS_IFACE, "current", GLib.Variant("s", ws.id))))
        except GLib.Error as exc:
            print(f"kwin: could not switch to {ws.name or ws.id}: {exc.message}", file=sys.stderr, flush=True)
            return
        for w in self.workspaces.values():  # (the signal confirms it in a moment)
            w.state = int(w is ws)

    def create(self, name: str) -> Desktop | None:
        """A new desktop at the end, called `name`."""
        before = set(self.workspaces)
        try:
            self._kwin(DESKTOPS_PATH, DESKTOPS_IFACE, "createDesktop", GLib.Variant("(us)", (len(before), name)))
        except GLib.Error as exc:
            print(f"kwin: could not add a desktop: {exc.message}", file=sys.stderr, flush=True)
            return None
        self._read_desktops()
        return next((w for i, w in self.workspaces.items() if i not in before), None)

    def rename(self, ws: Desktop, name: str):
        self._kwin(DESKTOPS_PATH, DESKTOPS_IFACE, "setDesktopName", GLib.Variant("(ss)", (ws.id, name)))
        self._read_desktops()

    def remove(self, ws: Desktop):
        try:
            self._kwin(DESKTOPS_PATH, DESKTOPS_IFACE, "removeDesktop", GLib.Variant("(s)", (ws.id,)))
        except GLib.Error as exc:
            print(f"kwin: could not remove {ws.name or ws.id}: {exc.message}", file=sys.stderr, flush=True)
            return
        self._read_desktops()
