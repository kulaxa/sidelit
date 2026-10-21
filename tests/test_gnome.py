"""The GNOME backend: which desktop picks it, GNOME's shortcuts on the chips, the sidebar on GNOME,
and the client against a fake GNOME Shell with Sidelit's extension (fake_gnome.py) on a private
session bus. The bus tests need PyGObject and dbus-daemon (skipped without them, as on CI's slim
image); none of them ever touches your own session bus. The extension itself is checked against
a real GNOME Shell by tests/gnome_live.py and tests/gnome_daemon_live.py."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import backend  # noqa: E402

try:
    from sidelit import gnome
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # no GLib bindings
    gnome = None

FAKE = os.path.join(ROOT, "tests", "fake_gnome.py")
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir={dir}</listen>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>"""


class WhichBackend(unittest.TestCase):
    def test_gnome_sessions(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "zorin:GNOME", "pop:GNOME", "GNOME-Classic:GNOME"):
            self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": desktop}), "gnome", desktop)

    def test_not_gnome_shell(self):
        # these name GNOME but run a shell of their own
        for desktop in ("Budgie:GNOME", "GNOME-Flashback:GNOME", "Unity:Unity7:ubuntu", "Pantheon"):
            self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": desktop}), "cosmic", desktop)

    def test_the_override(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "SIDELIT_BACKEND": "gnome"}), "gnome")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "GNOME", "SIDELIT_BACKEND": "cosmic"}), "cosmic")


@unittest.skipIf(gnome is None, "needs GLib")
class Shortcuts(unittest.TestCase):
    def test_chip_text(self):
        self.assertEqual(gnome.chip_text("<Super>Home"), "⊞ Home")
        self.assertEqual(gnome.chip_text("<Control><Alt>1"), "Ctrl Alt 1")
        self.assertEqual(gnome.chip_text("<Primary><Shift>Page_Up"), "Ctrl Shift PgUp")
        self.assertEqual(gnome.chip_text("<Super>KP_3"), "⊞ 3")
        self.assertEqual(gnome.chip_text("<Super>"), "")

    def test_gnomes_settings(self):
        bound = {"switch-to-workspace-1": ["<Super>Home"], "switch-to-workspace-3": ["", "<Super>3"],
                 "switch-to-workspace-last": ["<Super>End"]}
        keys = gnome.switch_shortcuts(lambda key: bound.get(key, []))
        self.assertEqual(keys, {1: "<Super>Home", 3: "<Super>3", "last": "<Super>End"})

    def test_the_chips(self):
        client = object.__new__(gnome.GnomeClient)
        client._shortcuts = (time.monotonic(), {1: "<Super>Home", "last": "<Super>End"})
        label = lambda n, total: client.shortcut_label(gnome.Space(id=str(n), coords=(n,)), total)  # noqa: E731
        self.assertEqual([label(1, 3), label(2, 3), label(3, 3)], ["⊞ Home", "", "⊞ End"])
        self.assertEqual(label(1, 1), "⊞ Home")  # the first one wins when it is also the last


class TheSidebarOnGnome(unittest.TestCase):
    @unittest.skipIf(gnome is None, "needs GLib")
    def test_outside_gnome_shell_it_is_not_needed(self):
        """systemd's unit started it, or someone by hand: on GNOME the extension runs the sidebar."""
        from sidelit import __main__ as entry
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            os.environ.pop("SIDELIT_HOSTED", None)
            os.environ.pop("SIDELIT_BACKEND", None)
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(entry.main(["sidebar"]), 4)
        self.assertIn("GNOME Shell extension", err.getvalue())

    def test_the_unit_takes_4_as_not_needed(self):
        unit = Path(ROOT, "data", "systemd", "sidelit-sidebar.service").read_text()
        self.assertIn("RestartPreventExitStatus=4", unit)
        self.assertIn("SuccessExitStatus=4", unit)

    @unittest.skipIf(gnome is None, "needs GLib")
    def test_gtk4_is_enough_when_hosted(self):
        from sidelit import sidebar4
        with mock.patch.dict(os.environ, {"SIDELIT_HOSTED": "gnome", "SIDELIT_SIDEBAR": "auto"}), \
                mock.patch.object(sidebar4, "layer_shell_paths", return_value=None), \
                mock.patch.object(sidebar4, "gtk4_available", return_value=True):
            self.assertTrue(sidebar4.should_run())
        with mock.patch.dict(os.environ, {"SIDELIT_HOSTED": "", "SIDELIT_SIDEBAR": "auto"}), \
                mock.patch.object(sidebar4, "layer_shell_paths", return_value=None):
            self.assertFalse(sidebar4.should_run())


class TheExtensionFiles(unittest.TestCase):
    """What extensions.gnome.org asks of an extension (the parts a test can see)."""
    DIR = Path(ROOT, "gnome-extension")

    def test_metadata(self):
        meta = json.loads((self.DIR / "metadata.json").read_text())
        self.assertEqual(meta["uuid"], "sidelit@kulaxa.github.io")
        self.assertEqual(meta["uuid"], gnome.UUID if gnome else meta["uuid"])
        self.assertTrue({"46", "48", "50", "51"} <= set(meta["shell-version"]))
        self.assertEqual(set(meta["session-modes"]), {"user", "unlock-dialog"})
        self.assertNotIn("version", meta)  # extensions.gnome.org sets it

    def test_only_gjs_no_binaries(self):
        for f in self.DIR.iterdir():
            self.assertIn(f.suffix, {".js", ".json"}, f.name)
            if f.suffix == ".js":
                text = f.read_text()
                self.assertTrue(text.startswith("// SPDX-License-Identifier: MIT"), f.name)
                for banned in ("gi://Gtk", "gi://Gdk", "gi://Adw", "imports.lang", "imports.mainloop", "ByteArray"):
                    self.assertNotIn(banned, text, f.name)

    def test_unlock_dialog_is_explained_in_disable(self):
        text = (self.DIR / "extension.js").read_text()
        disable = text[text.index("disable()") - 900:text.index("disable()")]
        self.assertIn("unlock-dialog", disable)

    @unittest.skipIf(shutil.which("node") is None, "needs Node")
    def test_the_javascript_parses(self):
        for f in self.DIR.glob("*.js"):
            out = subprocess.run(["node", "--check", "--input-type=module"], input=f.read_text(),
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, f"{f.name}: {out.stderr}")


@unittest.skipIf(gnome is None or shutil.which("dbus-daemon") is None, "needs GLib and dbus-daemon")
class AgainstFakeGnome(unittest.TestCase):
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
        self.fake = None
        self.test_bus = self.connect()
        self.clients = []
        self.changes = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.spin(0.1)
        if self.fake is not None:  # a fresh GNOME Shell for every test
            self.fake.terminate()
            self.fake.wait(5)
        self.test_bus.close_sync(None)

    # ---- helpers ----------------------------------------------------------------------------
    def start_fake(self, *args, wait=True):
        self.fake = subprocess.Popen([sys.executable, FAKE, self.address, *args])
        end = time.monotonic() + 10
        while wait and time.monotonic() < end:
            owned = self.test_bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                            "NameHasOwner", GLib.Variant("(s)", ("org.gnome.Shell",)), None, 0, -1, None)
            if owned.unpack()[0]:
                break
            time.sleep(0.02)

    @classmethod
    def connect(cls):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        return Gio.DBusConnection.new_for_address_sync(cls.address, flags, None, None)

    def client(self, primary=True, wait_s=None, bus=None):
        with redirect_stderr(io.StringIO()):
            c = gnome.GnomeClient(on_change=lambda: self.changes.append(1), primary=primary,
                                  bus=bus or self.connect(), wait_s=wait_s)
        self.clients.append(c)
        return c

    def drive(self, method, *args):
        """Call the fake's /Test interface."""
        sig = "(" + "".join("b" if isinstance(a, bool) else "s" for a in args) + ")"
        reply = self.test_bus.call_sync("org.gnome.Shell", "/Test", "org.gnome.Shell.Test", method,
                                        GLib.Variant(sig, args) if args else None, None, 0, 5000, None)
        out = reply.unpack()
        return out[0] if out else None

    def state(self):
        self.spin(0.1)
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

    # ---- workspaces -------------------------------------------------------------------------
    def test_dynamic_workspaces(self):
        self.start_fake()
        c = self.client()
        first = c.list()
        self.assertEqual([w.index for w in first], [1, 2])
        self.assertIs(c.active(), first[0])
        self.assertFalse(any(w.pinned for w in first))
        self.assertEqual(c.caps, frozenset({"create", "rename", "remove", "pin", "move", "focus", "close_toplevel"}))
        self.assertEqual(c.output_names(), {"DP-1", "HDMI-A-1"})

    def test_pinning_keeps_a_workspace_the_daemons_way(self):
        """pin_new_space: the empty one at the end is pinned, and GNOME adds another after it."""
        self.start_fake()
        c = self.client()
        last = c.list()[-1]
        c.pin(last, True)
        self.assertTrue(last.pinned and last.copies_pinned)
        self.assertTrue(self.spin(2, lambda: len(c.list()) == 3))
        self.assertIsNot(c.list()[-1], last)
        c.activate(c.list()[0])
        c.pin(last, False)
        self.assertTrue(self.spin(2, lambda: last.removed or c.list()[-1] is last))
        self.assertEqual(len(c.list()), 2)  # one empty one at the end, as GNOME keeps it

    def test_create_rename_move_remove(self):
        self.start_fake()
        c = self.client()
        made = c.create("Mail")
        self.assertEqual((made.name, made.pinned), ("Mail", True))
        other = c.create("Chat")
        self.spin(0.3)
        c.move(other, c.list()[0], before=True)
        self.assertEqual([w.name for w in c.list()][:3], ["Chat", "", "Mail"])
        c.rename(other, "Talk")
        self.assertIs(c.resolve("Talk"), other)
        self.assertIs(c.resolve("1"), other)
        c.remove(other)
        self.assertTrue(other.removed)
        self.assertNotIn(other, c.list())

    def test_a_report_that_arrives_late_is_old_news(self):
        """A Changed signal sent just before the answer to GetState is handled after it."""
        self.start_fake()
        c = self.client()
        before = json.loads(c._bus_call(gnome.SHELL, gnome.PATH, gnome.IFACE, "GetState")[0])
        made = c.create("Kept")
        self.assertFalse(c._apply(before))
        self.assertFalse(made.removed)
        self.assertIs(c.resolve("Kept"), made)

    def test_fixed_workspaces_need_no_pin(self):
        self.start_fake("--static")
        c = self.client()
        self.assertEqual(len(c.list()), 4)
        self.assertNotIn("pin", c.caps)
        self.assertTrue(all(w.pinned for w in c.list()))
        made = c.create("Fifth")
        self.assertEqual((made.index, made.name), (5, "Fifth"))

    # ---- windows ----------------------------------------------------------------------------
    def test_windows_are_there_on_connecting(self):
        self.start_fake()
        wid = self.open(app="org.gnome.Ptyxis", title="✳ Fix the tests", pid=4711, output="HDMI-A-1")
        c = self.client()
        tl = c.toplevels[wid]
        self.assertEqual((tl.app_id, tl.title, tl.pid, tl.identifier), ("org.gnome.Ptyxis", "✳ Fix the tests", 4711, wid))
        self.assertIs(c.workspace_of(tl), c.list()[0])
        self.assertEqual(c.copy_ids_of(tl), {c.list()[0].id})
        self.assertEqual(c.output_of(tl).name, "HDMI-A-1")
        self.assertTrue(tl.focused and tl.focused_at > 0)
        self.assertEqual(c.toplevels_on(c.list()[0]), [tl])

    def test_an_x11_window_goes_by_its_instance(self):
        self.start_fake()
        wid = self.open(app="Google-chrome", instance="google-chrome", x11=True, title="Inbox")
        self.assertEqual(self.client().toplevels[wid].app_id, "google-chrome")

    def test_changes_come_as_they_happen(self):
        self.start_fake()
        c = self.client()
        wid = self.open(app="foot", title="one")
        self.assertTrue(self.spin(2, lambda: wid in c.toplevels))
        tl = c.toplevels[wid]
        self.changes.clear()
        self.drive("SetTitle", wid, "two")
        self.assertTrue(self.spin(2, lambda: tl.title == "two"))
        self.assertTrue(self.changes)
        self.drive("CloseWindow", wid)
        self.assertTrue(self.spin(2, lambda: wid not in c.toplevels))
        self.assertTrue(tl.closed)
        self.assertIn(wid, c.closed_focus)  # it had the keyboard: closed by you

    def test_a_window_on_every_workspace_is_on_none(self):
        self.start_fake()
        wid = self.open(app="foot", all=True)
        c = self.client()
        self.assertIsNone(c.workspace_of(c.toplevels[wid]))
        self.assertEqual(c.copy_ids_of(c.toplevels[wid]), frozenset())

    def test_focus_close_and_move(self):
        self.start_fake()
        a = self.open(app="foot", title="a", focus=False)
        b = self.open(app="foot", title="b")
        c = self.client()
        ws = c.create("Elsewhere")
        ta, tb = c.toplevels[a], c.toplevels[b]
        self.assertTrue(c.move_toplevel(ta, ws, "HDMI-A-1"))
        self.assertTrue(self.spin(2, lambda: c.workspace_of(ta) is ws and ta.output == "HDMI-A-1"))
        c.focus(ta)
        self.assertTrue(self.spin(2, lambda: ta.focused and not tb.focused))
        c.close_toplevel(tb)
        self.assertTrue(self.spin(2, lambda: b not in c.toplevels))
        log = self.state()["log"]
        self.assertIn("MoveWindow", log)
        self.assertIn("FocusWindow", log)
        self.assertIn("CloseWindow", log)

    def test_a_closed_window_is_left_alone(self):
        self.start_fake()
        wid = self.open(app="foot")
        c = self.client()
        tl = c.toplevels[wid]
        self.drive("CloseWindow", wid)
        self.assertTrue(self.spin(2, lambda: tl.closed))
        c.focus(tl)
        c.close_toplevel(tl)
        self.assertFalse(c.move_toplevel(tl, c.list()[0]))
        self.assertNotIn("FocusWindow", self.state()["log"])

    def test_while_locked_nothing_changes(self):
        self.start_fake()
        wid = self.open(app="foot")
        c = self.client()
        self.drive("Lock", True)
        self.assertTrue(self.spin(2, lambda: c.locked))
        with redirect_stderr(io.StringIO()):
            self.assertFalse(c.move_toplevel(c.toplevels[wid], c.list()[-1]))
            self.assertIsNone(c.create("No"))

    # ---- the extension coming and going ------------------------------------------------------
    def test_turned_off_and_on_everything_keeps_its_id(self):
        self.start_fake()
        wid = self.open(app="foot")
        c = self.client()
        kept = c.create("Kept")
        c.move_toplevel(c.toplevels[wid], kept)
        self.assertTrue(self.spin(2, lambda: c.workspace_of(c.toplevels[wid]) is kept))
        ids = [w.id for w in c.list()]
        tl = c.toplevels[wid]
        self.drive("Extension", False)
        self.assertTrue(self.spin(2, lambda: not c.online))
        self.assertIs(c.toplevels[wid], tl)  # its windows did not go
        self.drive("Extension", True)
        self.assertTrue(self.spin(2, lambda: c.online))
        self.assertEqual([w.id for w in c.list()], ids)
        self.assertNotEqual(self.state()["internal_ids"], ids)  # (the extension's own ids are new)
        self.assertIs(c.workspace_of(tl), c.workspaces[kept.id])
        self.assertFalse(c.workspaces[kept.id].pinned)  # its pin went with it: the daemon pins it again
        c.pin(c.workspaces[kept.id], True)  # an old id reaches the extension as its new one
        self.assertTrue(c.workspaces[kept.id].pinned)

    def test_one_shot_without_the_extension(self):
        self.start_fake("--off")
        with self.assertRaises(RuntimeError) as cm:
            self.client(primary=False)
        self.assertIn("gnome-extensions enable sidelit@kulaxa.github.io", str(cm.exception))

    def test_the_daemon_waits_for_the_extension(self):
        self.start_fake("--off")
        GLib.timeout_add(600, lambda: (self.drive("Extension", True), False)[1])
        c = self.client(wait_s=10)
        self.assertTrue(c.online)
        self.assertEqual(len(c.list()), 2)

    def test_no_gnome_shell_at_all(self):
        with self.assertRaises(RuntimeError):
            self.client(primary=False)
        with self.assertRaises(RuntimeError):
            self.client(wait_s=0.5)

    def test_only_one_daemon(self):
        self.start_fake()
        self.client()
        with self.assertRaises(RuntimeError) as cm:
            self.client()
        self.assertIn("another Sidelit daemon", str(cm.exception))
        self.client(primary=False)  # a one-shot command is fine beside it

    def test_the_daemon_holds_the_name_the_extension_runs_the_sidebar_for(self):
        self.start_fake()
        c = self.client()
        owner = self.test_bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                        "NameHasOwner", GLib.Variant("(s)", (gnome.BUS_NAME,)), None, 0, -1, None)
        self.assertTrue(owner.unpack()[0])
        c.close()
        self.clients.remove(c)
        owner = self.test_bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                        "NameHasOwner", GLib.Variant("(s)", (gnome.BUS_NAME,)), None, 0, -1, None)
        self.assertFalse(owner.unpack()[0])

    # ---- the sidebar's place -----------------------------------------------------------------
    def test_the_sidebar_says_its_screen_and_side(self):
        self.start_fake()
        bus = self.connect()
        self.assertEqual(gnome.place_sidebar("HDMI-A-1", "right", bus=bus), "right")
        self.assertEqual(self.state()["placed"], ["HDMI-A-1", "right"])
        # the GTK 3 sidebar knows no connector names: the extension picks the screen by the rule
        self.assertEqual(gnome.place_sidebar("rightmost", "left", bus=bus), "left")
        self.assertEqual(self.state()["placed"], ["rightmost", "left"])
        self.assertNotIn("PlaceSidebar", self.state()["log"])
        bus.close_sync(None)

    def test_an_extension_from_before_the_side_docks_it_on_the_left(self):
        """GNOME Shell keeps running the extension it loaded at login, after an update installed
        a newer one: it has PlaceSidebar alone."""
        self.start_fake("--old")
        bus = self.connect()
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gnome.place_sidebar("HDMI-A-1", "right", bus=bus), "left")
            self.assertEqual(self.state()["placed"], ["HDMI-A-1", "left"])
            # a rule is no connector to it: '' is its leftmost screen
            self.assertEqual(gnome.place_sidebar("rightmost", "right", bus=bus), "left")
            self.assertEqual(self.state()["placed"], ["", "left"])
        self.assertIn("log out and back in", err.getvalue())
        bus.close_sync(None)

    def test_without_the_extension_the_sidebar_is_not_placed(self):
        self.start_fake("--off")
        bus = self.connect()
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gnome.place_sidebar("HDMI-A-1", "right", bus=bus), "")
        self.assertNotIn("log out", err.getvalue())  # (no older extension: none at all)
        self.assertIn("PlaceSidebarAt", err.getvalue())
        bus.close_sync(None)

    def test_gnome_shell_restarting(self):
        """GNOME on X11 restarts its shell (Alt+F2 r) with the windows still there."""
        self.start_fake()
        wid = self.open(app="foot")
        c = self.client()
        self.fake.terminate()
        self.fake.wait(5)
        self.assertTrue(self.spin(2, lambda: not c.online))
        self.assertIn(wid, c.toplevels)
        self.start_fake()
        self.assertTrue(self.spin(3, lambda: c.online))
        self.assertTrue(self.spin(2, lambda: wid not in c.toplevels))  # a new shell: its own windows


if __name__ == "__main__":
    unittest.main()
