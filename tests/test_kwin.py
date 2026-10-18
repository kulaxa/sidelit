"""The KDE backend: which desktop picks it, KDE's shortcuts on the chips, and the client against a
fake KWin (fake_kwin.py) on a private session bus. The bus tests need PyGObject and dbus-daemon
(skipped without them, as on CI's slim image); none of them ever touches your own session bus."""
import json
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

from sidelit import backend  # noqa: E402

try:
    from sidelit import kwin
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # no GLib bindings
    kwin = None

try:
    from sidelit import daemon
except (ImportError, ValueError):
    daemon = None

FAKE = os.path.join(ROOT, "tests", "fake_kwin.py")
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir={dir}</listen>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>"""


class WhichBackend(unittest.TestCase):
    def test_the_session_names_the_desktop(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE"}), "kde")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "plasma:KDE"}), "kde")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC"}), "cosmic")
        # anything else gets COSMIC's client, which says plainly that it cannot connect
        # (GNOME and Hyprland have their own: test_gnome.py, test_hyprland.py)
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "Wayfire"}), "cosmic")
        self.assertEqual(backend.chosen({}), "cosmic")

    def test_sidelit_backend_overrides_it(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "SIDELIT_BACKEND": "kde"}), "kde")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "SIDELIT_BACKEND": " Cosmic "}), "cosmic")
        with self.assertRaises(RuntimeError):
            backend.chosen({"SIDELIT_BACKEND": "wayfire"})

    def test_ws_usage(self):
        with mock.patch.object(backend, "open_client") as opened, mock.patch("sys.stderr"):
            self.assertEqual(backend.cli(["move", "1", "beside", "2"]), 2)
            self.assertEqual(backend.cli(["rename", "1"]), 2)
            self.assertEqual(backend.cli(["frobnicate"]), 2)
        opened.assert_not_called()


@unittest.skipIf(kwin is None, "needs GLib")
class SwitchShortcuts(unittest.TestCase):
    # what KWin 6.8 writes into ~/.config/kglobalshortcutsrc
    KWIN_68 = "\n".join([
        "[ActivityManager]", "switch-to-activity-1=none,none,Switch to activity",
        "[kwin]",
        "Switch to Desktop 1=Ctrl+F1\\tMeta+F1,Ctrl+F1\\tMeta+F1,Switch to Desktop 1",
        "Switch to Desktop 2=Ctrl+F2\\tMeta+F2,Ctrl+F2\\tMeta+F2,Switch to Desktop 2",
        "Switch to Desktop 3=Meta+3,Ctrl+F3\\tMeta+F3,Switch to Desktop 3",
        "Switch to Desktop 4=none,Ctrl+F4\\tMeta+F4,Switch to Desktop 4",
        "Switch to Desktop 5=none,none,Switch to Desktop 5",
        "Switch to Next Desktop=none,,Switch to Next Desktop",
        "[plasmashell]", "Switch to Desktop 6=Meta+6,none,not KWin's",
    ])

    def test_the_first_key_bound_now(self):
        keys = kwin.switch_shortcuts(self.KWIN_68)
        self.assertEqual(keys, {1: "Ctrl+F1", 2: "Ctrl+F2", 3: "Meta+3"})  # 4 and 5 unbound, 6 not KWin's

    def test_no_file_means_kwins_defaults(self):
        self.assertEqual(kwin.switch_shortcuts(None)[1], "Ctrl+F1")
        self.assertEqual(kwin.switch_shortcuts("[kwin]\nWindow Close=Alt+F4,Alt+F4,Close Window\n")[2], "Ctrl+F2")

    def test_chip_text(self):
        self.assertEqual(kwin.chip_text("Ctrl+F1"), "Ctrl F1")
        self.assertEqual(kwin.chip_text("Meta+Ctrl+1"), "⊞ Ctrl 1")
        self.assertEqual(kwin.chip_text("Ctrl++"), "Ctrl +")

    def test_the_chip_follows_the_file(self):
        client = object.__new__(kwin.KWinClient)
        client._shortcuts = (None, 0.0, {})
        with tempfile.TemporaryDirectory() as conf, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": conf}):
            ws = kwin.Desktop(id="a", coords=(3,))
            Path(conf, "kglobalshortcutsrc").write_text(self.KWIN_68)
            self.assertEqual(client.shortcut_label(ws, 4), "⊞ 3")
            self.assertEqual(client.shortcut_label(kwin.Desktop(id="b", coords=(4,)), 4), "")


# A KWin `workspace` as the scripts see it, in Node: windows with Qt-style signals, callDBus
# recording each call. Prints the calls (and what the actions did) as JSON at the end.
MOCK_KWIN_JS = r"""
function signal() { var s = []; return {connect: function (f) { s.push(f); }, fire: function (a) { s.forEach(function (f) { f(a); }); }}; }
var d1 = {id: "d1"}, d2 = {id: "d2"}, out0 = {name: "Virtual-0"}, out1 = {name: "Virtual-1"};
function win(id, props) {
  var w = {internalId: {toString: function () { return id; }}, desktopFileName: "foot", resourceClass: "foot",
           caption: id, pid: 7, desktops: [d1], onAllDesktops: false, output: out0, active: false,
           normalWindow: true, dialog: false, deleted: false, closed: false,
           captionChanged: signal(), desktopsChanged: signal(), outputChanged: signal(), desktopFileNameChanged: signal(),
           closeWindow: function () { this.closed = true; }};
  for (var k in props) w[k] = props[k];
  return w;
}
var wins = [win("{a}", {active: true}), win("{panel}", {normalWindow: false}), win("{dlg}", {normalWindow: false, dialog: true, desktops: [], onAllDesktops: true})];
if (process.argv[3] === "throw") wins[0].closeWindow = function () { throw new TypeError("not now"); };
var calls = [];
global.print = function () {};
global.workspace = {windowList: function () { return wins.slice(); }, screens: [out0, out1], desktops: [d1, d2],
  windowAdded: signal(), windowRemoved: signal(), windowActivated: signal(), screensChanged: signal(),
  activeWindow: null, sendClientToScreen: function (w, s) { w.output = s; }};
global.callDBus = function () { calls.push(Array.prototype.slice.call(arguments)); };
eval(require("fs").readFileSync(process.argv[2], "utf8"));
if (process.argv[3] === "events") {
  wins[0].caption = "{a} renamed"; wins[0].captionChanged.fire();
  wins[1].caption = "panel"; wins[1].captionChanged.fire();       // not a window we report
  var b = win("{b}"); wins.push(b); workspace.windowAdded.fire(b);
  b.desktops = [d2]; b.desktopsChanged.fire();
  workspace.windowRemoved.fire(wins[0]); wins.splice(0, 1);        // KWin still lists it as it says so
}
console.log(JSON.stringify({calls: calls, a: {desktops: wins[0].desktops.map(function (d) { return d.id; }),
  output: wins[0].output.name, closed: wins[0].closed, active: workspace.activeWindow === wins[0]}}));
"""


@unittest.skipIf(kwin is None or shutil.which("node") is None, "needs GLib and Node")
class TheScripts(unittest.TestCase):
    """The JavaScript Sidelit hands KWin, run in Node against a mock of KWin's script API (the
    real one is checked by tests/kwin_live.py)."""
    TO = (":1.7", "/io/github/kulaxa/sidelit/KWin/1")

    def run_js(self, source: str, *extra: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            mock_js, script = Path(tmp, "mock.js"), Path(tmp, "script.js")
            mock_js.write_text(MOCK_KWIN_JS)
            script.write_text(source)
            out = subprocess.run(["node", str(mock_js), str(script), *extra], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def reports(self, result):
        for service, path, iface, method, arg in result["calls"]:
            self.assertEqual((service, path, iface), (*self.TO, kwin.OUR_IFACE))
            yield method, (json.loads(arg) if method == "Windows" else arg)

    def test_the_daemons_script_reports_and_keeps_reporting(self):
        result = self.run_js(kwin.windows_script(self.TO, "sidelit-windows", watch=True), "events")
        reports = list(self.reports(result))
        first = reports[0][1]
        self.assertTrue(first["full"])
        self.assertEqual([w["id"] for w in first["windows"]], ["{a}", "{dlg}"])  # no panel
        self.assertEqual(first["windows"][0], {"id": "{a}", "app": "foot", "title": "{a}", "pid": 7, "desktops": ["d1"],
                                               "all": False, "output": "Virtual-0", "active": True})
        self.assertEqual(first["windows"][1]["all"], True)
        self.assertEqual(first["screens"], ["Virtual-0", "Virtual-1"])
        kinds = [(m, r["full"], [w["id"] for w in r["windows"]]) for m, r in reports[1:]]
        self.assertEqual(kinds, [
            ("Windows", False, ["{a}"]),               # the title, alone
            ("Windows", True, ["{a}", "{dlg}", "{b}"]),  # a window opened: all of them
            ("Windows", False, ["{b}"]),               # its desktop, alone
            ("Windows", True, ["{dlg}", "{b}"]),       # one closed
        ])
        self.assertEqual(reports[2][1]["windows"][0]["title"], "{a} renamed")
        self.assertNotIn("Done", [m for m, _ in reports])  # it never ends

    def test_a_one_shot_report(self):
        reports = list(self.reports(self.run_js(kwin.windows_script(self.TO, "sidelit-9-1", watch=False))))
        self.assertEqual([m for m, _ in reports], ["Windows", "Done"])
        self.assertEqual(reports[1][1], "sidelit-9-1")

    def test_actions(self):
        moved = self.run_js(kwin.action_script(self.TO, "s-1", "move", "{a}", {"desktop": "d2", "output": "Virtual-1"}))
        self.assertEqual((moved["a"]["desktops"], moved["a"]["output"]), (["d2"], "Virtual-1"))
        self.assertEqual(list(self.reports(moved)), [("Done", "s-1")])
        nowhere = self.run_js(kwin.action_script(self.TO, "s-2", "move", "{a}", {"desktop": "gone", "output": ""}))
        self.assertEqual((nowhere["a"]["desktops"], nowhere["a"]["output"]), (["d1"], "Virtual-0"))
        self.assertTrue(self.run_js(kwin.action_script(self.TO, "s-3", "close", "{a}"))["a"]["closed"])
        self.assertTrue(self.run_js(kwin.action_script(self.TO, "s-4", "focus", "{a}"))["a"]["active"])
        missing = self.run_js(kwin.action_script(self.TO, "s-5", "close", "{nope}"))
        self.assertFalse(missing["a"]["closed"])
        self.assertEqual(list(self.reports(missing)), [("Done", "s-5")])  # done all the same, so it is unloaded
        failing = self.run_js(kwin.action_script(self.TO, "s-7", "close", "{a}"), "throw")
        self.assertEqual(list(self.reports(failing)), [("Done", "s-7")])  # KWin threw: still done

    def test_quotes_in_names_stay_strings(self):
        tricky = '{x"); callDBus("evil'
        result = self.run_js(kwin.action_script(self.TO, "s-6", "close", tricky))
        self.assertEqual(list(self.reports(result)), [("Done", "s-6")])


@unittest.skipIf(kwin is None or shutil.which("dbus-daemon") is None, "needs GLib and dbus-daemon")
class AgainstFakeKWin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        conf = Path(cls.tmp.name, "bus.conf")
        conf.write_text(BUS_CONF.format(dir=cls.tmp.name))
        cls.bus_daemon = subprocess.Popen(["dbus-daemon", "--config-file", str(conf), "--nofork", "--print-address=1"],
                                          stdout=subprocess.PIPE, text=True)
        cls.address = cls.bus_daemon.stdout.readline().strip()
        cls.fake = None

    @classmethod
    def tearDownClass(cls):
        if cls.fake is not None:
            cls.fake.terminate()
            cls.fake.wait(5)
        cls.bus_daemon.terminate()
        cls.bus_daemon.wait(5)
        cls.bus_daemon.stdout.close()
        cls.tmp.cleanup()

    def setUp(self):
        if self.fake is None or self.fake.poll() is not None:
            self.start_fake()
        self.test_bus = self.connect()
        self.clients = []
        self.changes = []
        self.scripts = Path(tempfile.mkdtemp(dir=self.tmp.name))

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.spin(0.2)
        type(self).fake.terminate()  # a fresh KWin for every test
        type(self).fake.wait(5)
        self.test_bus.close_sync(None)

    # ---- helpers ----------------------------------------------------------------------------
    @classmethod
    def start_fake(cls):
        cls.fake = subprocess.Popen([sys.executable, FAKE, cls.address])
        probe = cls.connect()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            owned = probe.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                    "NameHasOwner", GLib.Variant("(s)", ("org.kde.KWin",)), None, 0, -1, None)
            if owned.unpack()[0]:
                break
            time.sleep(0.02)
        probe.close_sync(None)

    @classmethod
    def connect(cls):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        return Gio.DBusConnection.new_for_address_sync(cls.address, flags, None, None)

    def client(self, primary=True, bus=None):
        c = kwin.KWinClient(on_change=lambda: self.changes.append(1), primary=primary,
                            bus=bus or self.connect(), script_dir=self.scripts)
        self.clients.append(c)
        return c

    def drive(self, method, *args):
        """Call the fake's /Test interface."""
        sig = "(" + "s" * len(args) + ")"
        reply = self.test_bus.call_sync("org.kde.KWin", "/Test", "org.kde.KWin.Test", method,
                                        GLib.Variant(sig, args) if args else None, None, 0, 5000, None)
        out = reply.unpack()
        return out[0] if out else None

    def state(self):
        self.spin(0.2)  # what the clients sent on their own connections has reached it
        return json.loads(self.drive("State"))

    def open(self, **spec):
        return self.drive("OpenWindow", json.dumps(spec))

    @staticmethod
    def spin(seconds, until=None):
        ctx, end = GLib.MainContext.default(), time.monotonic() + seconds
        while time.monotonic() < end and not (until and until()):
            if not ctx.iteration(False):
                time.sleep(0.005)
        return bool(until and until())

    # ---- desktops ---------------------------------------------------------------------------
    def test_desktops_are_workspaces(self):
        c = self.client()
        names = [d.name for d in c.list()]
        self.assertEqual(names, ["Desktop 1", "Desktop 2", "Desktop 3"])
        self.assertEqual([d.index for d in c.list()], [1, 2, 3])
        self.assertIs(c.active(), c.list()[0])
        self.assertTrue(all(d.pinned and d.all_ids == {d.id} for d in c.list()))  # KDE keeps them all
        self.assertIs(c.resolve("Desktop 2"), c.list()[1])
        self.assertIs(c.resolve("3"), c.list()[2])
        self.assertIsNone(c.resolve("9"))
        self.assertNotIn("pin", c.caps)
        self.assertNotIn("move", c.caps)

    def test_switch_add_rename_remove(self):
        c = self.client()
        d1, d2, d3 = c.list()
        c.activate(d2)
        self.assertTrue(d2.active and not d1.active)
        self.assertEqual(self.state()["current"], d2.id)
        added = c.create("Fourth")
        self.assertEqual((added.name, added.index), ("Fourth", 4))
        c.rename(d3, "Third")
        self.assertEqual(d3.name, "Third")
        c.remove(added)
        self.assertTrue(added.removed)
        self.assertEqual(len(c.list()), 3)
        self.assertEqual([r[2] for r in self.state()["desktops"]], ["Desktop 1", "Desktop 2", "Third"])

    def test_desktops_changed_elsewhere(self):
        """KDE's own pager adds or renames one: the signal brings it in, the same objects stay."""
        c = self.client()
        d1 = c.list()[0]
        self.test_bus.call_sync("org.kde.KWin", "/VirtualDesktopManager", "org.kde.KWin.VirtualDesktopManager",
                                "setDesktopName", GLib.Variant("(ss)", (d1.id, "Mail")), None, 0, -1, None)
        self.assertTrue(self.spin(2, lambda: d1.name == "Mail"))
        self.assertIs(c.list()[0], d1)
        self.assertTrue(self.changes)

    # ---- windows ----------------------------------------------------------------------------
    def test_windows_are_there_on_connecting(self):
        wid = self.open(app="org.kde.konsole", title="✳ Fix the tests", pid=4711, output="Virtual-1")
        c = self.client()
        tl = c.toplevels.get(wid)
        self.assertIsNotNone(tl)
        self.assertEqual((tl.app_id, tl.title, tl.pid, tl.identifier), ("org.kde.konsole", "✳ Fix the tests", 4711, wid))
        self.assertIs(c.workspace_of(tl), c.list()[0])
        self.assertEqual(c.toplevels_on(c.list()[0]), [tl])
        self.assertEqual(c.copy_ids_of(tl), {c.list()[0].id})
        self.assertEqual(c.output_names(), {"Virtual-0", "Virtual-1"})
        self.assertEqual([o.name for o in c.outputs_of(tl)], ["Virtual-1"])
        self.assertTrue(tl.focused)

    def test_changes_come_as_they_happen(self):
        c = self.client()
        a = self.open(app="foot", title="one")
        self.assertTrue(self.spin(2, lambda: a in c.toplevels))
        b = self.open(app="foot", title="two")
        self.assertTrue(self.spin(2, lambda: b in c.toplevels))
        self.assertTrue(c.toplevels[b].focused and not c.toplevels[a].focused)
        self.assertGreater(c.toplevels[b].focused_at, 0)
        self.drive("SetTitle", a, "one, renamed")
        self.assertTrue(self.spin(2, lambda: c.toplevels[a].title == "one, renamed"))
        self.assertIn(b, c.toplevels)  # a report of one window leaves the others be
        gone = c.toplevels[b]
        self.drive("CloseWindow", b)
        self.assertTrue(self.spin(2, lambda: b not in c.toplevels))
        self.assertTrue(gone.closed)
        self.assertIn(b, c.closed_focus)  # it had the keyboard as it went: you closed it

    def test_a_window_on_every_desktop_is_on_none(self):
        wid = self.open(app="org.kde.yakuake", title="drop-down", all=True)
        c = self.client()
        tl = c.toplevels[wid]
        self.assertIsNone(c.workspace_of(tl))
        self.assertFalse(any(tl in c.toplevels_on(d) for d in c.list()))
        self.assertEqual(c.copy_ids_of(tl), frozenset())

    def test_focus_close_and_move(self):
        a = self.open(app="foot", title="a")
        b = self.open(app="google-chrome", title="b")
        c = self.client()
        d1, d2, _ = c.list()
        self.assertTrue(c.can_move_windows)
        self.assertTrue(c.move_toplevel(c.toplevels[a], d2, "Virtual-1"))
        self.assertTrue(self.spin(2, lambda: c.workspace_of(c.toplevels[a]) is d2))
        self.assertEqual(c.toplevels[a].output, "Virtual-1")
        c.focus(c.toplevels[a])
        self.assertTrue(self.spin(2, lambda: c.toplevels[a].focused and d2.active))  # KWin follows it there
        c.close_toplevel(c.toplevels[b])
        self.assertTrue(self.spin(2, lambda: b not in c.toplevels))
        # each action was a script of its own, unloaded once it said Done: only the daemon's is left
        self.assertTrue(self.spin(2, lambda: not c._pending))
        self.spin(0.3)
        state = self.state()
        self.assertEqual(state["scripts"], ["sidelit-windows"])
        self.assertEqual([p.name for p in self.scripts.glob("*.js")], ["sidelit-windows.js"])
        self.assertEqual([line.split()[0] for line in state["log"]], ["windows", "move", "focus", "close"])

    def test_a_closed_window_cannot_be_acted_on(self):
        a = self.open(app="foot", title="a")
        c = self.client()
        tl = c.toplevels[a]
        self.drive("CloseWindow", a)
        self.assertTrue(self.spin(2, lambda: tl.closed))
        self.assertFalse(c.move_toplevel(tl, c.list()[1]))
        c.focus(tl)
        self.assertEqual(self.state()["log"], ["windows sidelit-windows"])

    def test_a_script_that_never_says_done_goes_later(self):
        a = self.open(app="foot", title="a")
        c = self.client()
        c._pending["sidelit-lost"] = time.monotonic() - kwin.STALE_S - 1
        c.focus(c.toplevels[a])
        self.assertNotIn("sidelit-lost", c._pending)
        self.assertTrue(self.spin(2, lambda: c.toplevels[a].focused and not c._pending))

    # ---- who reports where ------------------------------------------------------------------
    def test_a_one_shot_client_gets_one_report(self):
        a = self.open(app="foot", title="a")
        once = self.client(primary=False)
        self.assertIn(a, once.toplevels)
        once.wait(2)
        self.assertEqual(self.state()["scripts"], [])  # its script said Done and went
        self.assertEqual(list(self.scripts.glob("*.js")), [])
        self.open(app="foot", title="later")
        self.spin(0.5)
        self.assertEqual(len(once.toplevels), 1)  # nothing more reaches it

    def test_the_daemon_and_a_one_shot_side_by_side(self):
        daemon_client = self.client()
        once = self.client(primary=False)
        once.wait(2)
        a = self.open(app="foot", title="a")
        self.assertTrue(self.spin(2, lambda: a in daemon_client.toplevels))
        self.assertEqual(self.state()["watching"], ["sidelit-windows"])

    def test_only_one_daemon(self):
        first = self.client()
        with self.assertRaises(RuntimeError):
            self.client()
        # the one that failed took nothing of the first's
        self.assertEqual(self.state()["watching"], ["sidelit-windows"])
        a = self.open(app="foot", title="a")
        self.assertTrue(self.spin(2, lambda: a in first.toplevels))

    def test_only_kwin_may_report(self):
        a = self.open(app="foot", title="a")
        c = self.client()
        stranger, result = self.connect(), []
        stranger.call(*c.to, kwin.OUR_IFACE, "Windows", GLib.Variant("(s)", (json.dumps({"full": True}),)),
                      None, 0, 2000, None, lambda conn, res: result.append(self.reply_of(conn, res)))
        self.assertTrue(self.spin(2, lambda: result))
        self.assertIn("AccessDenied", result[0])
        self.assertIn(a, c.toplevels)  # its empty "report" emptied nothing
        stranger.close_sync(None)

    @staticmethod
    def reply_of(conn, res):
        try:
            conn.call_finish(res)
            return "accepted"
        except GLib.Error as exc:
            return exc.message + " " + str(Gio.DBusError.get_remote_error(exc))

    def test_closing_unloads_everything(self):
        c = self.client()
        c.close()
        self.clients.remove(c)
        self.spin(0.3)
        self.assertEqual(self.state()["scripts"], [])
        self.assertEqual(list(self.scripts.glob("*.js")), [])
        # and the name is free for the next daemon
        self.client()

    def test_kwin_restarting(self):
        """KWin crashed and came back (Plasma restarts it): the daemon loads its script again."""
        c = self.client()
        type(self).fake.terminate()
        type(self).fake.wait(5)
        self.spin(0.3)
        with self.assertRaises(RuntimeError):  # meanwhile, nothing to connect to
            kwin.KWinClient(primary=False, bus=self.connect(), script_dir=self.scripts)
        self.start_fake()
        self.test_bus.close_sync(None)
        self.test_bus = self.connect()
        self.assertTrue(self.spin(3, lambda: self._watching()))
        a = self.open(app="foot", title="after")
        self.assertTrue(self.spin(2, lambda: a in c.toplevels))
        self.assertEqual({d.name for d in c.list()}, {"Desktop 1", "Desktop 2", "Desktop 3"})

    def _watching(self):
        try:
            return self.state()["watching"] == ["sidelit-windows"]
        except GLib.Error:
            return False

    def test_no_kwin(self):
        type(self).fake.terminate()
        type(self).fake.wait(5)
        with self.assertRaises(RuntimeError) as caught:
            kwin.KWinClient(primary=True, bus=self.connect(), script_dir=self.scripts)
        self.assertIn("Plasma 6", str(caught.exception))
        self.start_fake()


@unittest.skipIf(daemon is None, "needs GLib")
class ClosingOnKDE(unittest.TestCase):
    """On KDE only "Close workspace" removes a desktop, and once its windows have gone; letting a
    workspace go (forgotten, emptied) leaves KDE's desktop alone."""

    def setUp(self):
        class Desk:
            caps = frozenset(("create", "rename", "remove", "focus", "close_toplevel"))

            def __init__(self):
                self.spaces = [kwin_like(1, "a"), kwin_like(2, "b")]
                self.windows = {"b": ["terminal"]}
                self.removed = []

            def list(self):
                return [s for s in self.spaces if s not in self.removed]

            def toplevels_on(self, ws):
                return self.windows.get(ws.id, [])

            def remove(self, ws):
                self.removed.append(ws)

        def kwin_like(index, ws_id):
            ws = mock.Mock(index=index, id=ws_id, all_ids=frozenset({ws_id}))
            return ws

        self.d = object.__new__(daemon.Daemon)
        self.d.ws = Desk()
        self.timers = []

    def test_letting_go_keeps_the_desktop(self):
        with mock.patch.object(daemon.config, "DRY_RUN", False):
            self.d.unpin(2)
        self.assertEqual(self.d.ws.removed, [])

    def test_closing_removes_it_once_empty(self):
        with mock.patch.object(daemon.GLib, "timeout_add", lambda ms, fn: self.timers.append(fn)), \
                mock.patch.object(daemon.config, "DRY_RUN", False):
            self.d._remove_when_empty(2)
            self.assertTrue(self.timers[0]())  # its terminal is still closing: again later
            self.assertEqual(self.d.ws.removed, [])
            self.d.ws.windows = {}
            self.assertFalse(self.timers[0]())
        self.assertEqual([w.id for w in self.d.ws.removed], ["b"])

    def test_never_the_last_one(self):
        self.d.ws.spaces = self.d.ws.spaces[1:]
        self.d.ws.windows = {}
        with mock.patch.object(daemon.GLib, "timeout_add", lambda ms, fn: self.timers.append(fn)):
            self.d._remove_when_empty(2)
            self.assertFalse(self.timers[0]())
        self.assertEqual(self.d.ws.removed, [])

    def test_cosmic_is_left_to_unpin(self):
        self.d.ws.caps = frozenset(("pin", "remove"))
        with mock.patch.object(daemon.GLib, "timeout_add", lambda ms, fn: self.timers.append(fn)):
            self.d._remove_when_empty(2)
        self.assertEqual(self.timers, [])


if __name__ == "__main__":
    unittest.main()
