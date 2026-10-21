"""The tray icon (tray.py) and hiding the sidebar: the icon's pixels and menu, and the icon against
a fake StatusNotifierWatcher and a fake tray on a private session bus, the way COSMIC's or KDE's
panel talks to it. The bus tests need PyGObject and dbus-daemon (skipped without them); none of
them ever touches your own session bus."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from gi.repository import Gio, GLib
    from sidelit import sidebar_common, tray
except (ImportError, ValueError):  # no GLib bindings
    tray = None

BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir={dir}</listen>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>"""

WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
 <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>"""


@unittest.skipIf(tray is None, "needs PyGObject")
class PiecesTest(unittest.TestCase):
    def test_pixels_go_from_rgba_to_argb(self):
        self.assertEqual(tray.argb(bytes([1, 2, 3, 4, 5, 6, 7, 8])), bytes([4, 1, 2, 3, 8, 5, 6, 7]))
        self.assertEqual(tray.argb(bytes([1, 2, 3]), has_alpha=False), bytes([255, 1, 2, 3]))

    def test_the_mark_is_drawn_at_the_trays_sizes(self):
        icons = tray.pixmaps(sidebar_common.LOGO_SVG)
        if not icons:
            self.skipTest("no SVG loader for GdkPixbuf")
        self.assertEqual([(w, h) for w, h, _ in icons], [(s, s) for s in tray.SIZES])
        for w, h, data in icons:
            self.assertEqual(len(data), w * h * 4)
        self.assertNotEqual(tray.pixmaps(tray.with_dot(sidebar_common.LOGO_SVG))[0][2], icons[0][2])

    def test_menu_items_lines_and_greyed_ones(self):
        props = tray.menu_props([("Show", print), None, ("Nothing", None)])
        self.assertEqual([i for i, _ in props], [1, 2, 3])
        self.assertEqual(props[0][1]["label"].unpack(), "Show")
        self.assertTrue(props[0][1]["enabled"].unpack())
        self.assertEqual(props[1][1]["type"].unpack(), "separator")
        self.assertFalse(props[2][1]["enabled"].unpack())


@unittest.skipIf(tray is None, "needs PyGObject")
class HidingTest(unittest.TestCase):
    """SidebarLogic's side of it, with the window and the tray as mocks."""

    def logic(self, marker: Path):
        logic = sidebar_common.SidebarLogic()
        logic._hidden = False
        logic._set_shown, logic._show_view, logic.tray = mock.Mock(), mock.Mock(), mock.Mock()
        logic.tray.registered = True
        return logic

    def test_hiding_and_showing_again_and_a_restart_in_between(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "hidden")
            with mock.patch.object(sidebar_common.config, "HIDDEN_MARKER", marker):
                logic = self.logic(marker)
                logic.hide_sidebar()
                logic._set_shown.assert_called_once_with(False)
                logic.tray.set_hidden.assert_called_once_with(True)
                self.assertTrue(marker.exists())  # (a restart now starts hidden)
                self.assertEqual(logic.tray_menu()[0][0], "Show sidebar")
                logic.hide_sidebar()
                logic._set_shown.assert_called_once()  # (hidden already)
                logic.toggle_hidden()
                logic._set_shown.assert_called_with(True)
                self.assertFalse(marker.exists())
                self.assertEqual(logic.tray_menu()[0][0], "Hide sidebar")

    def test_the_trays_settings_show_the_sidebar_on_them(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sidebar_common.config, "HIDDEN_MARKER", Path(tmp, "hidden")):
            logic = self.logic(Path(tmp, "hidden"))
            logic._hidden = True
            dict(i for i in logic.tray_menu() if i)["Settings"]()
            logic._show_view.assert_called_once_with("settings")
            logic._set_shown.assert_called_once_with(True)

    def test_the_button_says_what_brings_it_back(self):
        logic = self.logic(Path("/nonexistent"))
        self.assertIn("icon in the panel", logic.hide_tooltip())
        logic.tray.registered = False  # (no tray here: GNOME without the AppIndicator extension)
        self.assertIn("app menu", logic.hide_tooltip())


@unittest.skipIf(tray is None or shutil.which("dbus-daemon") is None, "needs GLib and dbus-daemon")
class AgainstAFakeTray(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        conf = Path(cls.tmp.name, "bus.conf")
        conf.write_text(BUS_CONF.format(dir=cls.tmp.name))
        cls.bus_daemon = subprocess.Popen(["dbus-daemon", "--config-file", str(conf), "--nofork", "--print-address=1"],
                                          stdout=subprocess.PIPE, text=True)
        cls.address = cls.bus_daemon.stdout.readline().strip()

    @classmethod
    def tearDownClass(cls):
        cls.bus_daemon.terminate()
        cls.bus_daemon.wait(5)
        cls.bus_daemon.stdout.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.host = self.connect()  # the panel's side: the watcher, and the tray that draws icons
        self.registered = []
        node = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML)
        self.watcher_id = self.host.register_object("/StatusNotifierWatcher", node.interfaces[0], self._watcher_call)
        self.done = []
        self.hidden = False
        self.tray = tray.Tray(sidebar_common.LOGO_SVG, self.menu, self.toggle, lambda: self.set(False),
                              lambda: self.set(True), bus=self.connect())

    def tearDown(self):
        self.tray._bus.close_sync(None)
        self.host.close_sync(None)

    # ---- the sidebar's side -----------------------------------------------------------------
    def menu(self):
        return [("Show sidebar" if self.hidden else "Hide sidebar", self.toggle), None,
                ("Put everything back", lambda: self.done.append("put back"))]

    def toggle(self):
        self.set(not self.hidden)

    def set(self, hidden):
        self.hidden = hidden
        self.tray.set_hidden(hidden)

    # ---- the panel's side -------------------------------------------------------------------
    def _watcher_call(self, bus, sender, path, iface, method, params, invocation):
        self.registered.append((sender, params.unpack()[0]))
        invocation.return_value(None)

    @classmethod
    def connect(cls):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        return Gio.DBusConnection.new_for_address_sync(cls.address, flags, None, None)

    def spin(self, until, timeout=5.0, sure=True):
        end = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while not until() and time.monotonic() < end:
            context.iteration(False) or time.sleep(0.005)
        if sure:
            self.assertTrue(until(), "timed out")

    def call(self, path, iface, method, args=None, dest=None):
        """An async call from the panel, the main loop spun meanwhile (both ends are in this thread)."""
        out = []
        self.host.call(dest or self.tray._bus.get_unique_name(), path, iface, method, args, None,
                       Gio.DBusCallFlags.NONE, 5000, None, lambda b, r: out.append(b.call_finish(r)))
        self.spin(lambda: out)
        return out[0].unpack()

    def prop(self, name):
        return self.call(tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", name)))[0]

    def own_watcher(self):
        got = []
        owner = Gio.bus_own_name_on_connection(self.host, tray.WATCHER, Gio.BusNameOwnerFlags.NONE,
                                               lambda *_: got.append(1), None)
        self.spin(lambda: got)
        return owner

    # ---- the tests --------------------------------------------------------------------------
    def test_it_registers_when_the_tray_comes_and_says_when_it_goes(self):
        self.spin(lambda: False, timeout=0.2, sure=False)
        self.assertFalse(self.tray.registered)  # (no tray yet: the button says the app menu)
        owner = self.own_watcher()
        self.spin(lambda: self.tray.registered)
        self.assertEqual(self.registered, [(self.tray._bus.get_unique_name(), self.tray._bus.get_unique_name())])
        Gio.bus_unown_name(owner)  # (the panel restarts: the icon is no one's until it is back)
        self.spin(lambda: not self.tray.registered)

    def test_the_icon_and_a_click(self):
        self.assertEqual(self.prop("Id"), "sidelit")
        self.assertEqual(self.prop("Menu"), tray.MENU_PATH)
        if tray.pixmaps(sidebar_common.LOGO_SVG):
            self.assertEqual([w for w, _h, _d in self.prop("IconPixmap")], list(tray.SIZES))
        self.call(tray.ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (0, 0)))
        self.spin(lambda: self.hidden)
        self.assertEqual(self.prop("Status"), "Active")
        self.tray.set_wanting(2)  # (hidden, and two sessions want you: the icon says so)
        self.assertEqual(self.prop("Status"), "NeedsAttention")
        if tray.pixmaps(sidebar_common.LOGO_SVG):  # (and the icon itself, for COSMIC's tray)
            self.assertEqual(self.prop("IconPixmap"), self.prop("AttentionIconPixmap"))
        self.assertIn("2 sessions", self.prop("ToolTip")[3])
        self.call(tray.ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (0, 0)))
        self.spin(lambda: not self.hidden)
        self.assertEqual(self.prop("Status"), "Active")  # (shown: you see them yourself)

    def test_the_menu(self):
        revision, (root, _props, children) = self.call(tray.MENU_PATH, "com.canonical.dbusmenu", "GetLayout",
                                                       GLib.Variant("(iias)", (0, -1, [])))
        self.assertEqual(root, 0)
        self.assertEqual([c[1].get("label") or c[1].get("type") for c in children],
                         ["Hide sidebar", "separator", "Put everything back"])
        self.call(tray.MENU_PATH, "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (3, "clicked", GLib.Variant("s", ""), 0)))
        self.spin(lambda: self.done)
        self.assertEqual(self.done, ["put back"])
        groups = self.call(tray.MENU_PATH, "com.canonical.dbusmenu", "GetGroupProperties",
                           GLib.Variant("(aias)", ([1], [])))[0]
        self.assertEqual(groups, [(1, {"label": "Hide sidebar", "enabled": True})])
        self.call(tray.MENU_PATH, "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("s", ""), 0)))
        self.spin(lambda: self.hidden)
        later, (_r, _p, children) = self.call(tray.MENU_PATH, "com.canonical.dbusmenu", "GetLayout",
                                              GLib.Variant("(iias)", (0, -1, [])))
        self.assertGreater(later, revision)  # (the host was told the layout changed)
        self.assertEqual(children[0][1]["label"], "Show sidebar")

    def test_sidelit_show_reaches_it_by_its_name(self):
        self.spin(lambda: self.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                                    GLib.Variant("(s)", (tray.BUS_NAME,)), dest="org.freedesktop.DBus")[0])
        self.call(tray.CONTROL_PATH, tray.CONTROL_IFACE, "Hide", dest=tray.BUS_NAME)
        self.spin(lambda: self.hidden)
        self.call(tray.CONTROL_PATH, tray.CONTROL_IFACE, "Show", dest=tray.BUS_NAME)
        self.spin(lambda: not self.hidden)


if __name__ == "__main__":
    unittest.main()
