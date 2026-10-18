"""The sway backend: which session picks it, finding sway's socket, reading its config's keys,
and the client against a fake sway (fake_sway.py). None of it touches a real sway."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sidelit import backend, sway  # noqa: E402
from fake_sway import FakeSway  # noqa: E402


class WhichBackend(unittest.TestCase):
    def test_the_session_names_it(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "sway"}), "sway")

    def test_started_by_hand_its_socket_variable_does(self):
        self.assertEqual(backend.chosen({"SWAYSOCK": "/run/user/1000/sway-ipc.1000.42.sock"}), "sway")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "SWAYSOCK": "/x"}), "cosmic")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "niri", "SWAYSOCK": "/x"}), "niri")

    def test_the_override(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "SIDELIT_BACKEND": "sway"}), "sway")

    def test_open_client_gives_sways(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "sway"}), \
                mock.patch.object(sway, "SwayClient") as made:
            backend.open_client(primary=False)
        made.assert_called_once_with(None, primary=False)


class FindingTheSocket(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-sway-run-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)

    def sock(self, pid=None, age=0, uid=None):
        p = self.run_dir / f"sway-ipc.{os.getuid() if uid is None else uid}.{pid or os.getpid()}.sock"
        p.touch()
        if age:
            os.utime(p, (time.time() - age, time.time() - age))
        return str(p)

    def test_swaysock(self):
        p = self.sock()
        self.assertEqual(sway.socket_path({"SWAYSOCK": p, "XDG_RUNTIME_DIR": str(self.run_dir)}), p)
        self.assertEqual(sway.socket_path({"I3SOCK": p, "XDG_RUNTIME_DIR": str(self.run_dir)}), p)

    def test_without_it_the_newest_sway_running(self):
        self.sock(pid=os.getppid(), age=60)
        mine = self.sock()
        self.sock(pid=2 ** 22 + 12345)       # (beyond pid_max: a sway that has gone)
        self.sock(uid=os.getuid() + 1)        # (another user's)
        env = {"XDG_RUNTIME_DIR": str(self.run_dir)}
        self.assertEqual(sway.socket_path(env), mine)
        with mock.patch("sys.stderr"):  # (one from before sway restarted: the one running now)
            self.assertEqual(sway.socket_path({**env, "SWAYSOCK": str(self.run_dir / "gone.sock")}), mine)

    def test_none(self):
        with self.assertRaises(RuntimeError):
            sway.socket_path({"XDG_RUNTIME_DIR": str(self.run_dir)})

    def test_the_socket_of_a_sway_that_crashed(self):
        mine = self.sock()
        dead = self.sock(pid=2 ** 22 + 12345)  # (a sway that crashed left it behind)
        with mock.patch("sys.stderr"):
            self.assertEqual(sway.socket_path({"SWAYSOCK": dead, "XDG_RUNTIME_DIR": str(self.run_dir)}), mine)


class Ids(unittest.TestCase):
    def test_ids_are_names_written_as_hyprlands(self):
        self.assertEqual(sway.ws_ref("3"), "3")
        self.assertEqual(sway.ws_ref("3: web"), "3: web")
        self.assertEqual(sway.ws_ref("web"), "name:web")
        self.assertEqual(sway.name_of("name:web"), "web")
        self.assertEqual(sway.number_of("3: web"), 3)
        self.assertEqual(sway.number_of("web"), -1)

    def test_what_can_be_made(self):
        for ref in ("4", "4: mail", "name:chat"):
            self.assertTrue(sway._bringable(ref), ref)
        # another desktop's ids (COSMIC's, niri's), and words sway takes for something else
        for ref in ("cb8061aa", "3fa2b1c0", "sidelit-3", "name:next", "name:__i3_scratch", "name:", "name: x"):
            self.assertFalse(sway._bringable(ref), ref)

    def test_quoting(self):
        self.assertEqual(sway.quote("3: web"), '"3: web"')
        self.assertEqual(sway.quote('say "hi"'), "'say \"hi\"'")
        self.assertEqual(sway.quote("a\\b"), '"a\\b"')  # (sway leaves backslashes)
        self.assertEqual(sway.quote("$down payment"), '"$$down payment"')  # (else sway puts its $down in)
        self.assertEqual(sway.quote("a\\$b"), '"a\\$b"')  # (sway leaves a $ after one backslash)
        self.assertEqual(sway.quote("a\\\\$b"), '"a\\\\$$b"')  # (but not after two)

    def test_words_sway_takes_for_something_else(self):
        for name in ("output", "Gaps", "to", "NUMBER"):  # (workspace … output X; rename workspace to X)
            self.assertFalse(sway._bringable(f"name:{name}"), name)


class TheKeys(unittest.TestCase):
    def keys(self, text, path="/etc/sway/config"):
        return sway.switch_keys(sway.config_lines(text, path))

    def test_swayss_own_config(self):
        from fake_sway import DEFAULT_CONFIG
        keys = self.keys(DEFAULT_CONFIG)
        self.assertEqual(keys[("number", 1)], "⊞ 1")
        self.assertEqual(keys[("number", 10)], "⊞ 0")
        self.assertNotIn(("number", 4), keys)  # (only in the resize mode)
        self.assertEqual(len([k for k in keys if k[0] == "number"]), 4)  # (moving a window is not going there)

    def test_variables_names_flags_and_blocks(self):
        text = "\n".join([
            "set $mod Mod1", "set $ws3 \"3: web\"", "set $left h",
            "bindsym --to-code $mod+Ctrl+3 workspace $ws3",
            "bindsym $mod+3 workspace --no-auto-back-and-forth number $ws3",
            "bindsym {", "    $mod+m workspace mail", "    $mod+Tab workspace back_and_forth", "}",
            "bindcode Mod4+12 workspace number 3",
            "bindsym $mod+9 \\", "    workspace number 9; exec notify-send nine",
        ])
        keys = self.keys(text)
        self.assertEqual(keys[("name", "3: web")], "Ctrl Alt 3")
        self.assertEqual(keys[("number", 3)], "⊞ 3")  # (Super first)
        self.assertEqual(keys[("name", "mail")], "Alt m")
        self.assertEqual(keys[("number", 9)], "Alt 9")
        self.assertNotIn(("name", "back_and_forth"), keys)

    def test_sways_own_reading(self):
        text = "\n".join([
            "set $mod Mod4",
            "# bindsym $mod+7 workspace number 7 \\",  # (a comment never continues)
            "bindsym $mod+8 workspace NUMBER 8",
            'mode "default" {', "    bindsym $mod+6 workspace number 6", "}",
            'workspace 6 output HDMI-A-1 DP-1', 'workspace "3: web" output $right', "set $right DP-2",
            "workspace 4 gaps inner 10",
        ])
        keys, outputs = sway.parse_config(sway.config_lines(text))
        self.assertEqual(keys, {("number", 8): "⊞ 8", ("number", 6): "⊞ 6"})
        text = "\n".join([
            "    # an indented comment goes on \\",  # (as in sway, it takes the next line with it)
            "bindsym Mod4+1 workspace number 1",
            'mode --pango_markup "default" {', "    bindsym Mod4+2 workspace number 2", "}",
            'mode "Default" {', "    bindsym Mod4+3 workspace number 3", "}",  # (mode names have a case)
            "bindsym Mod4+4 workspace number 4 \\  ",  # (only a backslash right before the line's end continues)
        ])
        keys, _ = sway.parse_config(sway.config_lines(text))
        self.assertEqual(sorted(keys), [("number", 2), ("number", 4)])
        self.assertEqual(outputs, {"6": ["HDMI-A-1", "DP-1"], "3: web": ["$right"]})  # (set after: not yet)

    def test_includes(self):
        d = Path(tempfile.mkdtemp(prefix="sidelit-sway-config-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "config.d").mkdir()
        (d / "config.d" / "50-keys.conf").write_text("bindsym $mod+5 workspace number 5\n")
        (d / "config.d" / "loop.conf").write_text(f"include {d}/config.d/*\n")
        keys = self.keys("set $mod Mod4\ninclude config.d/*\n", str(d / "config"))
        self.assertEqual(keys[("number", 5)], "⊞ 5")
        keys = self.keys(f"set $mod Mod4\nset $conf {d}\ninclude $conf/config.d/50-*\n", "/etc/sway/config")
        self.assertEqual(keys[("number", 5)], "⊞ 5")  # (the config's variables in the path)


class AgainstFakeSway(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-sway-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.fake = FakeSway(self.run_dir, outputs=("DP-1", "HDMI-A-1"))
        self.addCleanup(self.fake.stop)
        self.env = {"SWAYSOCK": self.fake.path, "XDG_RUNTIME_DIR": str(self.run_dir)}
        self.clients = []
        self.changes = 0

    def tearDown(self):
        for c in self.clients:
            c.close()

    def client(self, primary=True):
        def changed():
            self.changes += 1
        c = sway.SwayClient(changed, primary=primary, env=self.env, state_dir=self.run_dir)
        self.clients.append(c)
        return c

    def pump(self, c, until=None, secs=2.0):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            c.pump()
            if until is None or until():
                return True
            time.sleep(0.01)
        return False

    def commands(self, since=0):
        return self.fake.commands[since:]

    @staticmethod
    def key(c, wid):
        """A window's identifier: this sway's token and its container id."""
        return f"{c.token}-{wid}"

    # ---- reading ----
    def test_workspaces_by_number_then_named(self):
        a = self.fake.open_window("kitty", "one", ws="web")
        b = self.fake.open_window("org.gnome.Nautilus", "files", ws="3", xwayland=True)
        self.fake.open_window("foot", "two", ws="3")  # (two side by side: a split container)
        c = self.client()
        listed = [(w.index, w.id, w.monitor, w.pinned) for w in c.list()]
        self.assertEqual(listed, [(1, "1", "DP-1", False), (2, "2", "HDMI-A-1", False),
                                  (3, "3", "DP-1", False), (4, "name:web", "DP-1", False)])
        self.assertEqual(c.active().id, "1")
        tl = c.toplevels[self.key(c, a)]
        self.assertEqual((tl.app_id, tl.title, tl.pid, tl.output), ("kitty", "one", 4242, "DP-1"))
        self.assertIs(c.workspace_of(tl), c.workspaces["name:web"])
        self.assertEqual(c.copy_ids_of(tl), frozenset({"name:web"}))
        self.assertEqual(c.toplevels[self.key(c, b)].app_id, "org.gnome.Nautilus")  # (an X11 window's class)
        self.assertEqual(len(c.toplevels_on(c.workspaces["3"])), 2)
        self.assertEqual(c.output_names(), {"DP-1", "HDMI-A-1"})
        self.assertEqual([s.name for s in c.screens], ["DP-1", "HDMI-A-1"])

    def test_the_scratchpad_is_no_workspace(self):
        a = self.fake.open_window("foot", "aside", ws="4")
        self.fake.to_scratchpad(a)
        c = self.client()
        tl = c.toplevels[self.key(c, a)]
        self.assertIsNone(c.workspace_of(tl))
        self.assertNotIn("name:__i3_scratch", c.workspaces)

    def test_the_chips(self):
        self.fake.open_window("foot", "one", ws="web")
        c = self.client()
        self.assertEqual([c.shortcut_label(w, 4) for w in c.list()], ["⊞ 1", "⊞ 2", ""])

    def test_changes_come_as_events(self):
        c = self.client()
        before = len(self.fake.asked)
        a = self.fake.open_window("foot", "⠋ Claude Code")
        self.assertTrue(self.pump(c, lambda: self.key(c, a) in c.toplevels))
        tl = c.toplevels[self.key(c, a)]
        self.assertTrue(tl.focused and tl.focused_at > 0)
        asked = len(self.fake.asked)
        self.fake.set_title(a, "✳ Claude Code")
        self.assertTrue(self.pump(c, lambda: tl.title == "✳ Claude Code"))
        self.assertEqual(self.fake.asked[asked:], [])  # (a title comes in the event: nothing asked)
        b = self.fake.open_window("foot", "other")
        self.assertTrue(self.pump(c, lambda: self.key(c, b) in c.toplevels and c.toplevels[self.key(c, b)].focused))
        self.assertFalse(tl.focused)
        self.fake.close_window(b)
        self.assertTrue(self.pump(c, lambda: c.toplevels.get(self.key(c, b)) is None))
        self.assertGreater(len(self.fake.asked), before)
        self.assertGreater(self.changes, 0)

    def test_a_dropped_event_connection_is_made_again(self):
        c = self.client()
        self.fake.drop_streams()  # (as sway drops one whose events pile up)
        with mock.patch("sys.stderr"):
            self.pump(c, secs=0.3)
        a = self.fake.open_window("foot", "after")
        self.assertTrue(self.pump(c, lambda: self.key(c, a) in c.toplevels))

    def test_sway_quitting(self):
        c = self.client()
        self.fake.stop(shutdown=True)
        with self.assertRaises(ConnectionError), mock.patch("sys.stderr"):
            self.pump(c, until=lambda: False, secs=1.0)

    def test_sway_gone_without_a_word(self):
        c = self.client()
        self.fake.stop()
        with self.assertRaises(ConnectionError), mock.patch("sys.stderr"):
            self.pump(c, until=lambda: False, secs=1.0)

    # ---- acting ----
    def test_focus_close_and_move(self):
        a = self.fake.open_window("foot", "one")
        b = self.fake.open_window("foot", "two")
        self.fake.open_window("foot", "three", ws="5", focus=False)
        c = self.client()
        target = c.workspaces["5"]
        c.focus(c.toplevels[self.key(c, a)])
        self.assertEqual(self.fake.focused_window, a)
        self.assertTrue(c.move_toplevel(c.toplevels[self.key(c, a)], target))
        self.assertEqual(self.fake.where(a), "5")
        self.assertEqual(self.fake.shown["DP-1"], "1")  # (you stay where you are)
        c.close_toplevel(c.toplevels[self.key(c, b)])
        self.assertNotIn(b, self.fake.windows)
        self.assertEqual(self.commands(), [
            f"[con_id={a}] focus",
            f'[con_id={a}] move --no-auto-back-and-forth container to workspace "5"',
            f"[con_id={b}] kill"])

    def test_moving_to_a_screen_takes_the_workspace_there(self):
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        b = self.fake.open_window("foot", "here")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        self.assertTrue(c.move_toplevel(c.toplevels[self.key(c, a)], ws, "HDMI-A-1"))
        self.assertEqual(self.fake.output_of("4"), "HDMI-A-1")
        self.assertEqual((self.fake.focused_window, self.fake.focused_output), (b, "DP-1"))  # (and back)

    def test_activate_skips_the_one_you_are_on(self):
        c = self.client()
        before = len(self.fake.commands)
        c.activate(c.active())
        self.assertEqual(self.commands(before), [])
        c.activate(c.workspaces["2"])
        self.assertEqual(self.fake.focused_output, "HDMI-A-1")
        self.assertEqual(self.commands(before), ['workspace --no-auto-back-and-forth "2"'])

    # ---- keeping workspaces ----
    def test_a_kept_workspace_stays_after_sway_drops_it(self):
        self.fake.open_window("foot", "home")  # (on 1, which so stays when you leave it)
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        ws = c.workspaces["4"]
        self.assertFalse(ws.pinned)
        c.pin(ws, True)
        self.assertTrue(ws.pinned and ws.copies_pinned)
        self.fake.close_window(a)
        self.assertTrue(self.pump(c, lambda: "4" not in self.fake.names()))  # sway dropped it
        self.assertIs(c.workspaces.get("4"), ws)  # Sidelit still has it: a stand-in
        self.assertTrue(ws.pinned and not ws.removed and ws.proxy is None and ws.monitor == "DP-1")
        self.assertEqual([w.id for w in c.list()], ["1", "2", "4"])
        c.activate(ws)  # going there makes it again
        self.assertEqual(self.fake.shown["DP-1"], "4")
        self.assertTrue(self.pump(c, lambda: ws.proxy is not None and ws.active))
        c.activate(c.workspaces["1"])
        self.pump(c, lambda: "4" not in self.fake.names())
        c.remove(ws)  # let go, the stand-in goes
        self.assertTrue(ws.removed)
        self.assertEqual([w.id for w in c.list()], ["1", "2"])

    def test_a_window_moved_to_a_stand_in_makes_it_again(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        made = c.create("Workspace")
        self.assertEqual((made.id, made.pinned, made.proxy, made.monitor), ("3", True, None, "DP-1"))
        self.assertNotIn("3", self.fake.names())  # (nothing asked of sway yet)
        self.assertTrue(c.move_toplevel(c.toplevels[self.key(c, a)], made))
        self.assertEqual(self.fake.where(a), "3")
        self.assertTrue(self.pump(c, lambda: made.proxy is not None and c.workspace_of(c.toplevels[self.key(c, a)]) is made))
        self.assertEqual(self.fake.shown["DP-1"], "1")  # (and you stay where you are)

    def test_create_numbers_after_every_one_in_use_or_known(self):
        c = self.client()
        c.preferred_ids = {"7", "name:web", "cb8061aa"}
        self.assertEqual(c.create().id, "8")
        self.assertEqual(c.create().id, "9")  # (the one before is kept now)

    def test_create_brings_back_the_name_it_had(self):
        c = self.client()
        self.assertEqual(c.create(want=("cb8061aa", "name:web")).id, "name:web")  # (not another desktop's id)
        self.assertEqual(c.create(want=("4",)).id, "4")
        self.assertEqual(c.create(want=("1", "4")).id, "5")  # (both in use: the next number)
        # renamed in sway: the name it had last (the layout learns them in turn)
        self.assertEqual(c.create(want=("6", "name:mine")).id, "name:mine")

    def test_a_stand_in_goes_back_to_its_screen(self):
        self.fake.switch("2")  # (you are on HDMI-A-1)
        c = self.client()
        self.pump(c)
        made = c.create()
        self.assertEqual(made.monitor, "HDMI-A-1")
        self.fake.switch("1")
        self.pump(c, lambda: c.active().id == "1")
        before = len(self.fake.commands)
        c.activate(made)
        self.assertEqual(self.commands(before), ['focus output "HDMI-A-1"', f'workspace --no-auto-back-and-forth "{made.id}"'])
        self.assertEqual(self.fake.output_of(made.id), "HDMI-A-1")

    def test_what_is_kept_outlives_the_daemon(self):
        c = self.client()
        made = c.create()
        c.close()
        self.clients.remove(c)
        again = self.client()
        self.assertTrue(again.workspaces[made.id].pinned)
        # a one-shot `sidelit ws unpin` (no event connection) and the daemon share the record
        once = self.client(primary=False)
        once.pin(once.workspaces[made.id], False)
        self.assertTrue(self.pump(again, lambda: made.id not in again.workspaces))

    def test_a_new_sway_forgets_the_last_ones_record(self):
        old = self.run_dir / f"sway-{2 ** 22 + 12345}.json"  # (the sway of the last login: gone)
        old.write_text(json.dumps({"kept": {"9": {"screen": "DP-1"}}}))
        c = self.client()
        self.assertNotIn("9", c.workspaces)
        self.assertFalse(old.exists())

    def test_a_workspace_renamed_in_sway_is_still_kept_and_known(self):
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        ws = c.workspaces["4"]
        c.pin(ws, True)
        self.fake.rename("4", "mail")  # (swaymsg rename workspace 4 to mail)
        self.assertTrue(self.pump(c, lambda: "name:mail" in c.workspaces and "4" not in c.workspaces))
        mail = c.workspaces["name:mail"]
        self.assertTrue(mail.pinned)  # (what is kept follows: no stand-in left for "4")
        self.assertEqual(mail.all_ids, {"name:mail", "4"})
        self.assertEqual(c.copy_ids_of(c.toplevels[self.key(c, a)]), {"name:mail", "4"})
        self.fake.open_window("foot", "two", ws="4", focus=False)  # another one is 4 now: no more this one's
        self.assertTrue(self.pump(c, lambda: mail.all_ids == {"name:mail"}))

    def test_window_ids_are_this_sways(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        run = Path(tempfile.mkdtemp(prefix="sidelit-sway-again-"))
        self.addCleanup(shutil.rmtree, run, True)
        again = FakeSway(run, outputs=("DP-1", "HDMI-A-1"), pid=os.getppid())  # (the next login's: numbered alike)
        self.addCleanup(again.stop)
        b = again.open_window("foot", "one")
        other = sway.SwayClient(None, primary=False, env={"SWAYSOCK": again.path, "XDG_RUNTIME_DIR": str(run)},
                                state_dir=run)
        self.clients.append(other)
        self.assertEqual(a, b)
        self.assertNotEqual(self.key(other, b), self.key(c, a))

    def test_a_dropped_connection_counts_as_a_new_one(self):
        c = self.client()
        self.assertEqual(c.connections, 1)
        self.fake.drop_streams()
        with mock.patch("sys.stderr"):
            self.pump(c, lambda: c.connections == 2)
        self.assertEqual(c.connections, 2)

    def test_rename_takes_the_keep_along(self):
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        c.pin(c.workspaces["4"], True)
        c.rename(c.workspaces["4"], "4: mail")
        self.assertEqual(self.fake.where(a), "4: mail")
        self.assertTrue(c.workspaces["4: mail"].pinned)
        self.assertNotIn("4", c.workspaces)

    def test_a_screen_that_goes(self):
        a = self.fake.open_window("foot", "big", ws="2")
        c = self.client()
        ws = c.workspaces["2"]
        c.pin(ws, True)
        self.fake.unplug("HDMI-A-1")
        self.assertTrue(self.pump(c, lambda: c.output_names() == {"DP-1"} and ws.monitor == "DP-1"))
        self.assertTrue(ws.pinned)
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: ws.monitor == "HDMI-A-1"))

    def test_with_no_screen_on_everything_stays_where_it_was(self):
        self.fake.unplug("HDMI-A-1")
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        c.pin(c.workspaces["4"], True)
        self.fake.unplug("DP-1")  # (the laptop's lid closed: sway lists no screen, no workspace)
        self.pump(c, secs=0.3)
        self.assertIn(self.key(c, a), c.toplevels)
        self.assertEqual(c.workspace_of(c.toplevels[self.key(c, a)]).id, "4")
        self.assertEqual(c.output_names(), {"DP-1"})
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: c.workspaces["4"].proxy is not None))
        self.assertFalse(c.toplevels[self.key(c, a)].closed)

    def test_a_sticky_window_is_on_no_workspace(self):
        a = self.fake.open_window("mpv", "picture in picture", sticky=True)
        c = self.client()
        self.assertIsNone(c.workspace_of(c.toplevels[self.key(c, a)]))

    def test_a_kept_workspace_comes_back_on_the_screen_it_was_moved_to(self):
        a = self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        ws = c.workspaces["4"]
        c.pin(ws, True)
        self.fake.move_workspace("4", "HDMI-A-1")  # (you, Super+Shift+Right)
        self.assertTrue(self.pump(c, lambda: ws.monitor == "HDMI-A-1"))
        self.fake.switch("5")  # (on HDMI-A-1: 4 is not shown any more)
        self.fake.switch("1")
        self.fake.close_window(a)
        self.assertTrue(self.pump(c, lambda: ws.proxy is None))  # (sway dropped it: a stand-in)
        self.assertEqual(ws.monitor, "HDMI-A-1")
        c.activate(ws)
        self.assertEqual(self.fake.output_of("4"), "HDMI-A-1")

    def test_another_sway_still_running_keeps_its_record(self):
        theirs = self.run_dir / f"sway-{os.getppid()}.json"  # (a nested sway, say)
        theirs.write_text(json.dumps({"kept": {"7": {"screen": "DP-1"}}}))
        self.client()
        self.assertTrue(theirs.exists())

    def test_a_reload_reads_the_keys_again(self):
        c = self.client()
        self.assertEqual(c.shortcut_label(c.workspaces["1"], 2), "⊞ 1")
        self.fake.reload("bindsym Mod1+1 workspace number 1\n")
        self.assertTrue(self.pump(c, lambda: c.shortcut_label(c.workspaces["1"], 2) == "Alt 1"))

    def test_an_event_in_pieces(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        self.fake.send_split(a, "two")
        self.assertTrue(self.pump(c, lambda: c.toplevels[self.key(c, a)].title == "two"))

    def test_create_passes_over_a_number_your_config_puts_on_another_screen(self):
        # (by connector in any case, by make, model and serial in quotes; sway adds the lines up)
        self.fake.config += 'workspace 3 output hdmi-a-1\nworkspace 4 output "Fake HDMI-A-1 0001"\n' \
                            'workspace 5 output DP-9\nworkspace 5 output HDMI-A-1\n'
        c = self.client()
        made = c.create()
        self.assertEqual((made.id, made.monitor), ("6", "DP-1"))
        made = c.create(want=("3",))  # (asked for by name: made where your config puts it)
        self.assertEqual((made.id, made.monitor), ("3", "HDMI-A-1"))
        c.activate(made)
        self.assertEqual(self.fake.output_of("3"), "HDMI-A-1")  # (and sway does make it there)

    def test_a_screen_that_goes_for_a_moment_keeps_a_kept_workspace_there(self):
        a = self.fake.open_window("foot", "big", ws="2", focus=False)  # (2 is on HDMI-A-1)
        c = self.client()
        ws = c.workspaces["2"]
        c.pin(ws, True)
        self.fake.unplug("HDMI-A-1")  # (asleep: sway takes 2 to DP-1 meanwhile)
        self.assertTrue(self.pump(c, lambda: ws.monitor == "DP-1"))
        self.fake.close_window(a)
        self.fake.switch("1")
        self.assertTrue(self.pump(c, lambda: ws.proxy is None))
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: c.output_names() == {"DP-1", "HDMI-A-1"}))
        self.assertEqual(ws.monitor, "HDMI-A-1")  # (its stand-in goes back there, as sway would take it)

    def test_blind_after_a_daemon_restart_with_the_lid_closed(self):
        self.fake.unplug("HDMI-A-1")
        self.fake.unplug("DP-1")
        c = self.client()
        self.assertTrue(c.blind)
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: not c.blind and c.output_names() == {"DP-1"}))

    def test_names_go_in_any_case(self):
        self.fake.open_window("foot", "one", ws="mail", focus=False)
        c = self.client()
        self.assertEqual(c.create(want=("name:Mail",)).id, "3")  # (sway's "mail" would be the one)
        before = len(self.fake.commands)
        c.rename(c.workspaces["name:mail"], "Mail")
        self.assertEqual(self.commands(before), [])

    def test_a_dollar_in_a_name(self):
        self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        c.rename(c.workspaces["4"], "4: $mod money")
        self.assertIn("4: $mod money", self.fake.names())  # (not "4: Mod4 money")

    def test_a_one_shot_pin_is_seen_without_an_event(self):
        self.fake.open_window("foot", "one", ws="4", focus=False)
        c = self.client()
        once = self.client(primary=False)
        once.pin(once.workspaces["4"], True)
        self.assertTrue(next(w for w in c.list() if w.id == "4").pinned)

    def test_one_shot_commands_have_no_event_connection(self):
        c = self.client(primary=False)
        self.assertEqual(c.fd(), -1)
        a = self.fake.open_window("foot", "new")
        start = time.monotonic()
        c.wait(0.5)
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertIn(self.key(c, a), c.toplevels)


if __name__ == "__main__":
    unittest.main()
