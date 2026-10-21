"""The sidebar's icon in the panel's tray, and its name on the session bus.

The icon is a StatusNotifierItem (KDE's tray protocol, which COSMIC, KDE, Xfce, Cinnamon, MATE,
waybar and GNOME with the AppIndicator extension all show), its right-click menu a
com.canonical.dbusmenu. A click shows or hides the sidebar. While the sidebar is hidden and a
session needs you, the icon says so: Status NeedsAttention, and a dot on the mark, the icon
itself, for trays that read no Status (COSMIC's).

The name (`BUS_NAME`) is also how `sidelit show` and the launcher bring a hidden sidebar back
where no tray is: Show, Hide and Toggle on `CONTROL_PATH`. GLib and Gio only, so both sidebars
use it as it is."""
from __future__ import annotations

import hashlib
import sys

from gi.repository import Gio, GLib

from sidelit import config

BUS_NAME = "io.github.kulaxa.sidelit.Sidebar"
if config._HOME:  # (a test instance next to the real one: a name of its own, like its socket)
    BUS_NAME += ".i" + hashlib.sha1(config._HOME.encode()).hexdigest()[:8]
CONTROL_PATH = "/io/github/kulaxa/sidelit/Sidebar"
CONTROL_IFACE = "io.github.kulaxa.sidelit.Sidebar"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"
SIZES = (22, 32, 48)

INTROSPECTION = f"""<node>
 <interface name="{CONTROL_IFACE}">
  <method name="Show"/>
  <method name="Hide"/>
  <method name="Toggle"/>
  <property name="Hidden" type="b" access="read"/>
 </interface>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
 </interface>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{{sv}})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
   <arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
   <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 </interface>
</node>"""

# the mark with a dot in the yellow of a session that wants you, for the attention icon
DOT_SVG = b'<circle cx="38" cy="10" r="8" fill="#ffd60a" stroke="#2c2c30" stroke-width="2.5"/>'


def argb(rgba: bytes, has_alpha: bool = True) -> bytes:
    """GdkPixbuf's RGBA (or RGB) rows as the tray's ARGB32 in network byte order."""
    step = 4 if has_alpha else 3
    out = bytearray(len(rgba) // step * 4)
    out[0::4] = rgba[3::4] if has_alpha else b"\xff" * (len(rgba) // 3)
    out[1::4], out[2::4], out[3::4] = rgba[0::step], rgba[1::step], rgba[2::step]
    return bytes(out)


def pixmaps(svg: bytes) -> list[tuple[int, int, bytes]]:
    """The SVG drawn at the tray's usual sizes ([] without an SVG loader: the host's own icon)."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return []
    out = []
    for size in SIZES:
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.set_size(size, size)
            loader.write(svg)
            loader.close()
            pb = loader.get_pixbuf()
        except GLib.Error:
            return []
        stride, n = pb.get_rowstride(), pb.get_n_channels()
        data = pb.get_pixels()
        rows = b"".join(data[y * stride:y * stride + size * n] for y in range(size))  # (rows are padded)
        out.append((size, size, argb(rows, pb.get_has_alpha())))
    return out


def with_dot(svg: bytes) -> bytes:
    return svg.replace(b"</svg>", DOT_SVG + b"\n</svg>")


def menu_props(items: list) -> list[tuple[int, dict]]:
    """dbusmenu's (id, properties) of `items`: (label, action) pairs, None a separator. An
    item's id is its place in the list, plus one (0 is the root)."""
    out = []
    for i, item in enumerate(items, 1):
        if item is None:
            out.append((i, {"type": GLib.Variant("s", "separator")}))
        else:
            out.append((i, {"label": GLib.Variant("s", item[0]), "enabled": GLib.Variant("b", item[1] is not None)}))
    return out


ROOT_PROPS = {"children-display": GLib.Variant("s", "submenu")}


def menu_layout(items: list) -> tuple:
    """dbusmenu's layout of `items`: the root, its children one level deep."""
    return (0, ROOT_PROPS, [GLib.Variant("(ia{sv}av)", (i, props, [])) for i, props in menu_props(items)])


def control(method: str) -> bool:
    """Show, Hide or Toggle a running sidebar; False when none answers (not running)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(BUS_NAME, CONTROL_PATH, CONTROL_IFACE, method, None, None,
                      Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return True
    except GLib.Error:
        return False


class Tray:
    """The sidebar's bus name, its control and its tray icon. `menu()` gives the right-click
    menu's items, (label, action) or None for a line, asked for each time the menu opens;
    `toggle()` is a click on the icon; `show`, `hide` the control's. `bus`: the session bus
    (the tests' is a private one)."""

    def __init__(self, logo: bytes, menu, toggle, show, hide, bus: Gio.DBusConnection | None = None):
        self._menu, self._toggle, self._show, self._hide = menu, toggle, show, hide
        self._actions: list = []
        self._revision = 1
        self.hidden = False
        self.wanting = 0  # sessions that want you
        self.registered = False  # a tray shows the icon
        self.on_registered = None
        self._icon, self._attention_icon = pixmaps(logo), pixmaps(with_dot(logo))
        self._bus = bus
        self._ids: list[int] = []
        self._watch = 0
        if self._bus is None:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error as exc:
                print(f"sidebar: no session bus, so no tray icon: {exc.message}", file=sys.stderr, flush=True)
                return
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        for path, iface in ((CONTROL_PATH, node.interfaces[0]), (ITEM_PATH, node.interfaces[1]),
                            (MENU_PATH, node.interfaces[2])):
            self._ids.append(self._bus.register_object(path, iface, self._call, self._get, None))
        # (a sidebar that restarts takes the name over from the one before, if that one still has it)
        Gio.bus_own_name_on_connection(self._bus, BUS_NAME,
                                       Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
                                       None, None)
        self._watch = Gio.bus_watch_name_on_connection(self._bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       self._watcher_up, self._watcher_gone)

    # ---- what the sidebar tells it --------------------------------------------------------
    def set_hidden(self, hidden: bool):
        if hidden == self.hidden:
            return
        self.hidden = hidden
        self._status_changed()
        self._layout_changed()

    def set_wanting(self, n: int):
        if n == self.wanting:
            return
        before = self.status()
        self.wanting = n
        if self.status() != before:
            self._status_changed()
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")

    def status(self) -> str:
        return "NeedsAttention" if self.hidden and self.wanting else "Active"

    def tooltip(self) -> str:
        if not self.wanting:
            return "Click to show the sidebar" if self.hidden else "Click to hide the sidebar"
        return f"{self.wanting} session{'s' if self.wanting != 1 else ''} waiting for you"

    # ---- the watcher ----------------------------------------------------------------------
    def _watcher_up(self, bus, name, owner):
        def done(b, result):
            try:
                b.call_finish(result)
            except GLib.Error as exc:
                print(f"sidebar: the tray did not take the icon: {exc.message}", file=sys.stderr, flush=True)
                return
            self._set_registered(True)
        # (the unique name, not BUS_NAME: until that is ours a host could not reach it)
        bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (bus.get_unique_name(),)), None, Gio.DBusCallFlags.NONE, 5000, None, done)

    def _watcher_gone(self, bus, name):
        self._set_registered(False)

    def _set_registered(self, on: bool):
        if on != self.registered:
            self.registered = on
            if self.on_registered:
                self.on_registered(on)

    # ---- D-Bus ----------------------------------------------------------------------------
    def _emit(self, path: str, iface: str, name: str, args: GLib.Variant | None = None):
        if self._bus is None:
            return
        try:
            self._bus.emit_signal(None, path, iface, name, args)
        except GLib.Error:
            pass

    def _status_changed(self):
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewStatus", GLib.Variant("(s)", (self.status(),)))
        # (COSMIC's tray reads no Status: the icon itself takes the dot)
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")

    def _layout_changed(self):
        self._revision += 1
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self._revision, 0)))

    def _get(self, bus, sender, path, iface, prop):
        if iface == CONTROL_IFACE:
            return GLib.Variant("b", self.hidden) if prop == "Hidden" else None
        if iface == "com.canonical.dbusmenu":
            return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                    "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)
        values = {
            "Category": ("s", "ApplicationStatus"), "Id": ("s", "sidelit"), "Title": ("s", "Sidelit"),
            "Status": ("s", self.status()), "WindowId": ("i", 0),
            "IconName": ("s", "" if self._icon else "view-left-pane-symbolic"),
            "IconPixmap": ("a(iiay)", self._attention_icon if self.status() == "NeedsAttention" else self._icon),
            "AttentionIconName": ("s", ""), "AttentionIconPixmap": ("a(iiay)", self._attention_icon),
            "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []),
            "ToolTip": ("(sa(iiay)ss)", ("", [], "Sidelit", self.tooltip())),
            "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH),
        }
        if prop not in values:
            return None
        return GLib.Variant(*values[prop])

    def _call(self, bus, sender, path, iface, method, params, invocation):
        try:
            invocation.return_value(self._handle(iface, method, params))
        except Exception as exc:  # (a bug here must not take the sidebar down)
            print(f"sidebar: tray {method}: {exc}", file=sys.stderr, flush=True)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(exc))

    def _handle(self, iface: str, method: str, params: GLib.Variant) -> GLib.Variant | None:
        if iface == CONTROL_IFACE:
            GLib.idle_add(lambda: ({"Show": self._show, "Hide": self._hide, "Toggle": self._toggle}[method](), False)[1])
            return None
        if iface == "org.kde.StatusNotifierItem":
            if method in ("Activate", "SecondaryActivate"):
                GLib.idle_add(lambda: (self._toggle(), False)[1])
            return None
        # com.canonical.dbusmenu
        if method == "GetLayout":
            items = self._menu()
            self._actions = [None if item is None else item[1] for item in items]
            return GLib.Variant("(u(ia{sv}av))", (self._revision, menu_layout(items)))
        if method == "GetGroupProperties":
            ids = set(params.unpack()[0])
            found = [(0, ROOT_PROPS)] + menu_props(self._menu())
            return GLib.Variant("(a(ia{sv}))", ([(i, props) for i, props in found if not ids or i in ids],))
        if method == "GetProperty":
            raise ValueError("no such property")
        if method == "Event":
            item_id, event = params.unpack()[:2]
            if event == "clicked":
                self._run(item_id)
            return None
        if method == "EventGroup":
            for item_id, event, _data, _t in params.unpack()[0]:
                if event == "clicked":
                    self._run(item_id)
            return GLib.Variant("(ai)", ([],))
        if method == "AboutToShow":
            return GLib.Variant("(b)", (False,))
        if method == "AboutToShowGroup":
            return GLib.Variant("(aiai)", ([], []))
        return None

    def _run(self, item_id: int):
        if 0 < item_id <= len(self._actions) and self._actions[item_id - 1] is not None:
            action = self._actions[item_id - 1]
            GLib.idle_add(lambda: (action(), False)[1])
