"""The X11 backend: which sessions pick it, how desktops keep their ids, and the client against a
fake X server with a window manager in it (fake_x11.py). Pure Python: nothing here needs an X
server, python-xlib or GLib (the shortcut chip, which shares GNOME's code, needs GLib)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sidelit import backend, x11, xdock  # noqa: E402
from fake_x11 import ALL, FakeX  # noqa: E402

try:
    from sidelit import gnome  # noqa: F401 (the chip's text)
except (ImportError, ValueError):
    gnome = None


def win(client, wid):
    """The client's window for X window `wid`."""
    return next(t for t in client.toplevels.values() if t.proxy == wid)


class WhichBackend(unittest.TestCase):
    def test_an_x11_session_gets_it(self):
        for desktop in ("X-Cinnamon", "XFCE", "MATE", "LXDE", ""):
            self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": desktop, "XDG_SESSION_TYPE": "x11"}), "x11", desktop)
        # Budgie, GNOME Flashback: a GNOME of their own, an EWMH window manager on X11
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "Budgie:GNOME", "XDG_SESSION_TYPE": "x11"}), "x11")
        # no session type: an X display without a Wayland one
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}), "x11")
        self.assertEqual(backend.chosen({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}), "cosmic")

    def test_kde_gnome_and_cosmic_keep_their_own(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11"}), "kde")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "x11"}), "gnome")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "DISPLAY": ":1"}), "cosmic")
        # Cinnamon's own Wayland session has no API for windows: not this one
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "X-Cinnamon", "XDG_SESSION_TYPE": "wayland"}), "cosmic")

    def test_sidelit_backend_names_it(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "SIDELIT_BACKEND": "X11"}), "x11")


class TheDock(unittest.TestCase):
    """The GTK 3 sidebar on X11: a dock with the strip it stands on reserved."""

    def test_when_gtk_draws_on_x11(self):
        from unittest import mock
        cases = [({"DISPLAY": ":0"}, True), ({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-1"}, False),
                 ({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-1", "GDK_BACKEND": "x11"}, True),
                 ({"DISPLAY": ":0", "GDK_BACKEND": "wayland,x11"}, False), ({}, False)]
        for env, x in cases:
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(xdock.on_x11(), x, env)

    def test_beside_the_desktops_own_panels(self):
        # Cinnamon's panel at the bottom (with Mint's menu in it): the sidebar stops above it
        self.assertEqual(xdock.span((0, 0, 1920, 1080), (0, 0, 1920, 1040)), (0, 0, 1040))
        # Xfce's and MATE's at the top, and our own strip already taken out on the left
        self.assertEqual(xdock.span((0, 0, 1920, 1080), (280, 28, 1640, 1052)), (0, 28, 1052))
        # a work area that is another screen's: the whole height
        self.assertEqual(xdock.span((1920, 0, 1920, 1080), (0, 1080, 1920, 1080)), (1920, 0, 1080))

    def test_the_strut(self):
        self.assertEqual(xdock.strut((0, 0, 1920, 1080), [], 280),
                         [280, 0, 0, 0, 0, 1079, 0, 0, 0, 0, 0, 0])
        # a second screen to the right, and HiDPI: the X server's pixels
        self.assertEqual(xdock.strut((0, 0, 1280, 720), [(1280, 0, 1920, 1080)], 280, scale=2),
                         [560, 0, 0, 0, 0, 1439, 0, 0, 0, 0, 0, 0])
        # a screen right of another one, below it: the strip from the X screen's left edge
        self.assertEqual(xdock.strut((1920, 0, 1920, 1080), [(0, 1080, 1920, 1080)], 280)[:6],
                         [2200, 0, 0, 0, 0, 1079])
        # beside it: that screen's strip would go too, so none
        self.assertIsNone(xdock.strut((1920, 0, 1920, 1080), [(0, 0, 1920, 1080)], 280))


class TheirApps(unittest.TestCase):
    """Linux Mint's, Xfce's and MATE's own terminals and file managers."""

    def test_their_terminals_open_in_the_workspace_folder(self):
        from unittest import mock
        from sidelit import config, launch
        for exe in ("gnome-terminal", "xfce4-terminal", "mate-terminal"):
            with mock.patch.object(config.shutil, "which", lambda name, exe=exe: name == exe):
                plain, run = config._default_terminal()
            cfg = {"terminal": plain, "terminal_cmd": run}
            argv = launch.terminal_argv(cfg, "/home/me/my project", "claude --resume 1 || exec bash")
            self.assertEqual(argv[:2], [exe, "--working-directory=/home/me/my project"])
            self.assertEqual(argv[-3:], ["sh", "-c", "claude --resume 1 || exec bash"])
            self.assertEqual(launch.terminal_argv(cfg, "/tmp", None), [exe, "--working-directory=/tmp"])

    def test_they_are_known(self):
        from sidelit.core import titles
        # (as they named themselves under xfwm4, through x11.app_id)
        for app in ("xfce4-terminal", "mate-terminal", "org.gnome.Terminal"):
            self.assertIn(app, titles.TERMINAL_APP_IDS)
        for app in ("org.Nemo", "org.xfce.Thunar", "org.mate.Caja"):
            self.assertIn(app, titles.FILE_MANAGERS)
        self.assertEqual(titles.folder_from_title("org.xfce.Thunar", "tmp - Thunar"), "tmp")


class AppIds(unittest.TestCase):
    def test_as_wayland_names_them(self):
        self.assertEqual(x11.app_id("org.gnome.Terminal", "gnome-terminal-server", "Gnome-terminal"), "org.gnome.Terminal")
        self.assertEqual(x11.app_id("", "google-chrome", "Google-chrome"), "google-chrome")
        self.assertEqual(x11.app_id("", "terminator", "Terminator"), "terminator")
        self.assertEqual(x11.app_id("", "Navigator", "firefox"), "firefox")
        self.assertEqual(x11.app_id("", "xterm", ""), "xterm")
        self.assertEqual(x11.app_id("", "", ""), "")


class Realign(unittest.TestCase):
    """Which desktop came or went, when the window manager only says how many there are."""
    ids = ["a", "b", "c", "d"]
    names = ["mail", "sidelit", "blog", "music"]

    def new(self):
        return "new"

    def test_the_same_number_keeps_every_id(self):
        self.assertEqual(x11.realign(self.ids, self.names, ["x", "y", "z", "w"], 4, []), self.ids)

    def test_with_nothing_to_go_on_it_is_the_end(self):
        self.assertEqual(x11.realign(self.ids, self.names, ["", "", ""], 3, []), ["a", "b", "c"])
        self.assertEqual(x11.realign(self.ids, self.names, [], 5, [], new_id=self.new), self.ids + ["new"])

    def test_the_names_say_which_went(self):
        got = x11.realign(self.ids, self.names, ["mail", "blog", "music"], 3, [])
        self.assertEqual(got, ["a", "c", "d"])

    def test_the_windows_say_which_went(self):
        # Muffin names unnamed desktops by position: those names say nothing
        known = ["Workspace 1", "Workspace 2", "Workspace 3", "Workspace 4"]
        now = ["Workspace 1", "Workspace 2", "Workspace 3"]
        windows = [("c", 1), ("d", 2), ("a", 0)]  # c's and d's windows moved down a place
        self.assertEqual(x11.realign(self.ids, known, now, 3, windows), ["a", "c", "d"])

    def test_the_last_ones_windows_go_to_the_one_before(self):
        """xfwm4 drops the last desktop and moves its two windows onto the one before: that is
        the last one going, not its neighbour."""
        windows = [("a", 0), ("b", 1), ("c", 2), ("d", 2), ("d", 2)]
        self.assertEqual(x11.realign(self.ids, ["", "", "", ""], ["", "", ""], 3, windows), ["a", "b", "c"])
        # Cinnamon removing one in the middle sends its windows to the one before too
        windows = [("a", 0), ("b", 0), ("c", 1), ("d", 2)]
        self.assertEqual(x11.realign(self.ids, ["", "", "", ""], ["", "", ""], 3, windows), ["a", "c", "d"])

    def test_one_added_in_the_middle(self):
        got = x11.realign(self.ids, self.names, ["mail", "new one", "sidelit", "blog", "music"], 5, [],
                          new_id=self.new)
        self.assertEqual(got, ["a", "new", "b", "c", "d"])

    def test_our_own_change_explains_itself(self):
        self.assertEqual(x11.realign(self.ids, self.names, ["", "", ""], 3, [], hint=("remove", 0)), ["b", "c", "d"])
        self.assertEqual(x11.realign(self.ids, self.names, [], 5, [], hint=("add", 4), new_id=self.new),
                         self.ids + ["new"])
        # (a hint for another kind of change is not this one's)
        self.assertEqual(x11.realign(self.ids, self.names, ["", "", ""], 3, [], hint=("add", 0)), ["a", "b", "c"])

    def test_several_at_once_are_at_the_end(self):
        self.assertEqual(x11.realign(self.ids, self.names, [], 2, []), ["a", "b"])
        self.assertEqual(x11.realign(self.ids, self.names, [], 6, [], new_id=self.new), self.ids + ["new", "new"])


class Shortcuts(unittest.TestCase):
    XML = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-keyboard-shortcuts" version="1.0">
  <property name="commands" type="empty"/>
  <property name="xfwm4" type="empty">
    <property name="default" type="empty">
      <property name="&lt;Primary&gt;F1" type="string" value="workspace_1_key"/>
      <property name="&lt;Primary&gt;F2" type="string" value="workspace_2_key"/>
      <property name="&lt;Alt&gt;F4" type="string" value="close_window_key"/>
    </property>
    {custom}
  </property>
</channel>"""

    def test_xfce_defaults_and_your_own(self):
        self.assertEqual(x11.xfce_switch_keys(self.XML.format(custom="")), {1: "<Primary>F1", 2: "<Primary>F2"})
        mine = ('<property name="custom" type="empty">'
                '<property name="&lt;Super&gt;1" type="string" value="workspace_1_key"/></property>')
        self.assertEqual(x11.xfce_switch_keys(self.XML.format(custom=mine)), {1: "<Super>1"})
        self.assertEqual(x11.xfce_switch_keys("not xml"), {})
        self.assertEqual(x11.xfce_switch_keys(None), {})

    def test_cinnamon_and_mate_settings(self):
        cinnamon = {"switch-to-workspace-1": ["<Control><Alt>1", ""], "switch-to-workspace-2": []}
        self.assertEqual(x11.settings_switch_keys("s", True, cinnamon.get), {1: "<Control><Alt>1"})
        mate = {"switch-to-workspace-1": "disabled", "switch-to-workspace-3": "<Mod4>3"}
        self.assertEqual(x11.settings_switch_keys("s", False, mate.get), {3: "<Mod4>3"})

    @unittest.skipIf(gnome is None, "needs GLib")
    def test_the_chip(self):
        x = FakeX()
        with tempfile.TemporaryDirectory() as tmp:
            c = x11.X11Client(conn=x, ids_path=Path(tmp) / "ids.json")
            c._shortcuts = (10 ** 9, {1: "<Primary>F1"})
            first, second = c.list()[:2]
            self.assertEqual(c.shortcut_label(first, 3), "Ctrl F1")
            self.assertEqual(c.shortcut_label(second, 3), "")


class AgainstFakeX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ids_path = Path(self.tmp.name) / "x11-desktops.json"
        self.x = FakeX()
        self.changes = 0
        self.c = self.client()

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, x=None, primary=True):
        def changed():
            self.changes += 1
        return x11.X11Client(changed, primary=primary, conn=x or self.x, ids_path=self.ids_path)

    def names(self):
        return [w.name for w in self.c.list()]

    def ids(self):
        return [w.id for w in self.c.list()]

    def test_the_desktops(self):
        spaces = self.c.list()
        self.assertEqual([(w.index, w.name, w.active) for w in spaces],
                         [(1, "Desktop 1", True), (2, "Desktop 2", False), (3, "Desktop 3", False)])
        self.assertEqual(len(set(self.ids())), 3)
        self.assertTrue(all(w.pinned for w in spaces))  # (an X11 desktop stays until removed)
        self.assertEqual(json.loads(self.ids_path.read_text())["ids"], self.ids())
        self.assertNotIn("pin", self.c.caps)
        self.assertNotIn("move", self.c.caps)

    def test_windows_and_what_they_say(self):
        term = self.x.open("✳ Fix the tests", ("terminator", "Terminator"), desktop=1, pid=4242)
        files = self.x.open("Home", ("nemo", "Nemo"), desktop=2, gtk_app="org.Nemo")
        self.x.open("panel", ("xfce4-panel", "Xfce4-panel"), kind="_NET_WM_WINDOW_TYPE_DOCK")
        sticky = self.x.open("notes", desktop=ALL)
        self.c.pump()
        tops = self.c.toplevels
        self.assertEqual(sorted(t.title for t in tops.values()), ["Home", "notes", "✳ Fix the tests"])
        t = win(self.c, term)
        self.assertEqual((t.app_id, t.pid, self.c.workspace_of(t).index), ("terminator", 4242, 2))
        self.assertEqual(self.c.copy_ids_of(t), frozenset({self.c.list()[1].id}))
        self.assertEqual(win(self.c, files).app_id, "org.Nemo")
        s = win(self.c, sticky)
        self.assertIsNone(self.c.workspace_of(s))  # on every desktop: on none of the tabs
        self.assertEqual([w.title for w in self.c.toplevels_on(self.c.list()[1])], ["✳ Fix the tests"])

    def test_window_identifiers_are_this_x_servers(self):
        """Window numbers start over with every X server: a window after the next login is
        never taken for a remembered one from before."""
        wid = self.x.open("term")
        self.c.pump()
        same = self.client(primary=False)
        self.assertEqual(win(same, wid).identifier, win(self.c, wid).identifier)
        later = FakeX()
        self.assertEqual(later.open("another window"), wid)  # (the same number, on a new X server)
        after = self.client(later)
        self.assertNotEqual(win(after, wid).identifier, win(self.c, wid).identifier)

    def test_clients_starting_together_agree_on_the_token(self):
        x = FakeX()
        x.set_strings(x11.SESSION_PROP, ["first"], append=True)  # (both saw none, both appended)
        x.set_strings(x11.SESSION_PROP, ["second"], append=True)
        self.assertEqual(x11.session_token(x), "first")

    def test_changes_come_as_they_happen(self):
        term = self.x.open("✳ idle")
        self.c.pump()
        before = self.changes
        self.x.retitle(term, "⠋ working")
        self.x._wm(term, "_NET_ACTIVE_WINDOW", [2, 0, 0, 0, 0])
        self.c.pump()
        self.assertGreater(self.changes, before)
        t = win(self.c, term)
        self.assertEqual(t.title, "⠋ working")
        self.assertTrue(t.focused and t.focused_at > 0)
        self.x.close_window(term)
        self.c.pump()
        self.assertEqual(self.c.toplevels, {})
        self.assertIn(f"{self.c._token}-0x{term:x}", self.c.closed_focus)  # it had the keyboard as it closed

    def test_events_left_in_the_queue_are_taken_in_when_asked(self):
        """python-xlib reads events while it waits for an answer, and the daemon's watch on the
        connection never hears of those: list() takes them in."""
        self.x.open("a window", desktop=2)
        self.assertEqual(len(self.c.toplevels_on(self.c.list()[2])), 1)

    def test_the_actions_are_a_pagers(self):
        term = self.x.open("term")
        self.c.pump()
        t = win(self.c, term)
        third = self.c.list()[2]
        self.assertTrue(self.c.move_toplevel(t, third))
        self.c.activate(third)
        self.c.focus(t)
        self.c.close_toplevel(t)
        kinds = [(wid == self.x.root, kind, data[:2]) for wid, kind, data in self.x.sent]
        self.assertEqual(kinds, [(False, "_NET_WM_DESKTOP", [2, 2]), (True, "_NET_CURRENT_DESKTOP", [2, kinds[1][2][1]]),
                                 (False, "_NET_ACTIVE_WINDOW", [2, kinds[2][2][1]]),
                                 (False, "_NET_CLOSE_WINDOW", [kinds[3][2][0], 2])])
        self.c.pump()
        self.assertEqual(self.c.toplevels, {})
        self.assertTrue(self.c.list()[2].active)

    def test_focus_goes_to_the_windows_desktop(self):
        """xfwm4 brings a window to your desktop when asked to focus it: switch to its own first."""
        term = self.x.open("term", desktop=2)
        self.c.pump()
        t = win(self.c, term)
        self.c.focus(t)
        self.c.pump()
        self.assertEqual((self.x.current, self.x.desktop_of(term)), (2, 2))
        self.assertTrue(t.focused)

    def test_a_new_desktop_at_the_end_with_its_name(self):
        ws = self.c.create("sidelit")
        self.assertEqual((ws.index, ws.name), (4, "sidelit"))
        self.assertEqual(self.x.names, ["Desktop 1", "Desktop 2", "Desktop 3", "sidelit"])
        self.assertEqual(self.ids()[3], ws.id)
        self.assertEqual(json.loads(self.ids_path.read_text())["names"][3], "sidelit")

    def test_rename(self):
        self.c.rename(self.c.list()[1], "blog")
        self.assertEqual(self.x.names, ["Desktop 1", "blog", "Desktop 3"])
        self.assertEqual(self.names()[1], "blog")

    def test_removing_the_last_one(self):
        ids = self.ids()
        self.c.remove(self.c.list()[2])
        self.assertEqual(self.ids(), ids[:2])

    def test_removing_one_in_the_middle_moves_the_rest_down(self):
        ids = self.ids()
        a = self.x.open("on one", desktop=0)
        c = self.x.open("on three", desktop=2)
        self.x._wm(self.x.root, "_NET_CURRENT_DESKTOP", [2, 0])
        self.c.pump()
        self.c.remove(self.c.list()[1])
        self.assertEqual(self.ids(), [ids[0], ids[2]])
        self.assertEqual(self.names(), ["Desktop 1", "Desktop 3"])
        self.assertEqual((self.x.desktop_of(a), self.x.desktop_of(c)), (0, 1))
        self.assertEqual(self.c.active().id, ids[2])  # you stay where you were

    def test_a_window_manager_that_keeps_its_number_of_desktops(self):
        from unittest import mock
        self.x.fixed = True
        ids, term = self.ids(), self.x.open("on three", desktop=2)
        self.c.pump()
        with mock.patch.object(x11, "ACT_WAIT_S", 0.05), mock.patch("sys.stderr"):
            self.assertIsNone(self.c.create("more"))
            self.c.remove(self.c.list()[1])
        self.assertEqual((self.ids(), self.x.names, self.x.desktop_of(term)), (ids, ["Desktop 1", "Desktop 2", "Desktop 3"], 2))

    def test_xfce_dropping_its_last_desktop(self):
        """Xfce's panel drops the last desktop; xfwm4 moves its windows onto the one before."""
        ids = self.ids()
        self.x.open("on two", desktop=1)
        self.x.open("on three", desktop=2)
        self.x.open("also on three", desktop=2)
        self.c.pump()
        self.c.list()
        self.x._wm(self.x.root, "_NET_NUMBER_OF_DESKTOPS", [2, 0, 0, 0, 0])
        self.c.pump()
        self.assertEqual(self.ids(), ids[:2])

    def test_cinnamon_removing_one_in_the_middle(self):
        ids = self.ids()
        self.x.open("on two", desktop=1)
        self.x.open("on three", desktop=2)
        self.c.pump()
        self.c.list()
        self.x.remove_desktop(0, regenerate="Workspace")  # Muffin's made-up names: no help
        self.c.pump()
        self.assertEqual(self.ids(), ids[1:])  # the windows say it: both moved down a place

    def test_ids_come_back_at_the_next_login(self):
        self.c.rename(self.c.list()[2], "blog")
        ids = self.ids()
        later = FakeX(names=("Desktop 1", "Desktop 2", "blog"))  # a new X server, the same desktops
        self.c = self.client(later)
        self.assertEqual(self.ids(), ids)

    def test_a_desktop_removed_while_sidelit_was_away(self):
        self.c.rename(self.c.list()[0], "mail")
        ids = self.ids()
        later = FakeX(names=("Desktop 2", "Desktop 3"))
        self.c = self.client(later)
        self.assertEqual(self.ids(), ids[1:])

    def test_a_one_shot_command_does_not_keep_ids(self):
        saved = self.ids_path.read_text()
        once = self.client(primary=False)
        once.create("scratch")
        self.assertEqual(self.ids_path.read_text(), saved)
        self.assertEqual(len(once.list()), 4)

    def test_no_window_manager(self):
        with self.assertRaises(RuntimeError):
            x11.X11Client(conn=FakeX(wm=False), ids_path=self.ids_path, primary=False)


class Screens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.x = FakeX(monitors=(("DP-1", 0, 0, 1920, 1080), ("HDMI-1", 1920, 0, 2560, 1440)))
        self.c = x11.X11Client(conn=self.x, ids_path=Path(self.tmp.name) / "ids.json")

    def tearDown(self):
        self.tmp.cleanup()

    def window(self, **kw):
        wid = self.x.open("w", **kw)
        self.c.pump()
        return wid, win(self.c, wid)

    def test_which_screen(self):
        self.assertEqual(self.c.output_names(), {"DP-1", "HDMI-1"})
        _, left = self.window(geometry=(100, 100, 800, 600))
        _, right = self.window(geometry=(2000, 50, 800, 600))
        self.assertEqual(self.c.output_of(left).name, "DP-1")
        self.assertEqual([s.name for s in self.c.outputs_of(right)], ["HDMI-1"])

    def test_moving_to_another_screen(self):
        wid, w = self.window(geometry=(100, 200, 800, 600))
        self.c.move_toplevel(w, self.c.list()[0], "HDMI-1")
        self.assertEqual(self.x.geom[wid][:2], (2020, 200))
        self.c.pump()
        self.assertEqual(self.c.output_of(w).name, "HDMI-1")

    def test_a_maximised_window_is_maximised_there_again(self):
        maxed = ["_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"]
        wid, w = self.window(geometry=(0, 0, 1920, 1080), state=maxed)
        self.c.move_toplevel(w, self.c.list()[0], "HDMI-1")
        kinds = [kind for _, kind, _ in self.x.sent]
        self.assertEqual(kinds, ["_NET_WM_STATE", "_NET_MOVERESIZE_WINDOW", "_NET_WM_STATE"])
        self.assertEqual(sorted(self.x.get(wid, "_NET_WM_STATE")), sorted(maxed))
        self.assertEqual(self.x.geom[wid][:2], (1920, 0))

    def test_one_screen_needs_no_geometry(self):
        x = FakeX()
        c = x11.X11Client(conn=x, ids_path=Path(self.tmp.name) / "one.json")
        wid = x.open("w")
        x.geom.pop(wid)
        c.pump()
        self.assertEqual(c.output_of(win(c, wid)).name, "DP-1")


if __name__ == "__main__":
    unittest.main()
