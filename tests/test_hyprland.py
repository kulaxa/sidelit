"""The Hyprland backend: which session picks it, finding Hyprland's sockets, the shortcut chips,
and the client against a fake Hyprland (fake_hyprland.py), in both of the forms Hyprland takes
commands in: a Lua config's (0.55 and later) and the classic one's. None of it touches a real
Hyprland."""
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sidelit import backend, hyprland  # noqa: E402
from fake_hyprland import FakeHyprland  # noqa: E402

try:
    from sidelit import daemon
except (ImportError, ValueError):  # no GLib bindings
    daemon = None


class WhichBackend(unittest.TestCase):
    def test_the_session_names_it(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "Hyprland"}), "hyprland")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "Hyprland:wlroots"}), "hyprland")

    def test_started_by_hand_its_socket_variable_does(self):
        self.assertEqual(backend.chosen({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}), "hyprland")
        # (COSMIC says it is COSMIC: a variable left over from elsewhere does not count)
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "HYPRLAND_INSTANCE_SIGNATURE": "abc"}),
                         "cosmic")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "HYPRLAND_INSTANCE_SIGNATURE": "abc"}), "kde")

    def test_the_override(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "SIDELIT_BACKEND": "Hyprland"}), "hyprland")

    def test_open_client_gives_hyprlands(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "Hyprland"}), \
                mock.patch.object(hyprland, "HyprlandClient") as made:
            backend.open_client(primary=False)
        made.assert_called_once_with(None, primary=False)


class FindingTheSockets(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-hypr-run-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)

    def instance(self, sig, pid=None, display="wayland-1", age=0):
        d = self.run_dir / "hypr" / sig
        d.mkdir(parents=True)
        (d / ".socket.sock").touch()
        (d / "hyprland.lock").write_text(f"{pid or os.getpid()}\n{display}\n")
        if age:
            os.utime(d, (time.time() - age, time.time() - age))
        return d

    def test_by_its_signature(self):
        d = self.instance("sig1")
        self.instance("sig2", display="wayland-9")
        self.assertEqual(hyprland.instance_dir({"XDG_RUNTIME_DIR": str(self.run_dir), "HYPRLAND_INSTANCE_SIGNATURE": "sig1"}), d)

    def test_a_signature_from_before_hyprland_restarted(self):
        """A unit keeps the signature it was started with: the Hyprland running now, then."""
        self.instance("crashed", pid=2 ** 22 + 12345)  # (beyond pid_max: no such process)
        now = self.instance("now", display="wayland-1")
        env = {"XDG_RUNTIME_DIR": str(self.run_dir), "WAYLAND_DISPLAY": "wayland-1"}
        with mock.patch("sys.stderr"):
            self.assertEqual(hyprland.instance_dir({**env, "HYPRLAND_INSTANCE_SIGNATURE": "crashed"}), now)
            self.assertEqual(hyprland.instance_dir({**env, "HYPRLAND_INSTANCE_SIGNATURE": "never"}), now)
        shutil.rmtree(now)
        with self.assertRaises(RuntimeError):
            hyprland.instance_dir({**env, "HYPRLAND_INSTANCE_SIGNATURE": "crashed"})

    def test_without_it_the_hyprland_on_our_display(self):
        mine = self.instance("old", display="wayland-1", age=60)
        self.instance("new", display="wayland-2")
        env = {"XDG_RUNTIME_DIR": str(self.run_dir), "WAYLAND_DISPLAY": "wayland-1"}
        self.assertEqual(hyprland.instance_dir(env), mine)
        env["WAYLAND_DISPLAY"] = "wayland-7"  # none of them: the newest
        self.assertEqual(hyprland.instance_dir(env).name, "new")

    def test_a_crashed_one_is_passed_over(self):
        dead = 2 ** 22 + 12345  # (beyond pid_max: no such process)
        self.instance("crashed", pid=dead)
        with self.assertRaises(RuntimeError):
            hyprland.instance_dir({"XDG_RUNTIME_DIR": str(self.run_dir)})
        alive = self.instance("alive", age=600)
        self.assertEqual(hyprland.instance_dir({"XDG_RUNTIME_DIR": str(self.run_dir)}), alive)


class Shortcuts(unittest.TestCase):
    def test_chip_text(self):
        self.assertEqual(hyprland.chip_text(64, "3"), "⊞ 3")
        self.assertEqual(hyprland.chip_text(4 | 8, "F3"), "Ctrl Alt F3")
        self.assertEqual(hyprland.chip_text(64 | 1, "1"), "⊞ Shift 1")

    def test_classic_binds_say_where_they_go(self):
        binds = [{"modmask": 64, "key": "1", "dispatcher": "workspace", "arg": "1"},
                 {"modmask": 8, "key": "F2", "dispatcher": "workspace", "arg": "2"},
                 {"modmask": 64, "key": "", "keycode": 12, "dispatcher": "workspace", "arg": "3"},  # code:12
                 {"modmask": 64, "key": "0", "dispatcher": "workspace", "arg": "10"},
                 {"modmask": 64 | 1, "key": "1", "dispatcher": "movetoworkspace", "arg": "1"},
                 {"modmask": 64, "key": "S", "dispatcher": "workspace", "arg": "name:scratch"},
                 {"modmask": 64, "key": "plus", "dispatcher": "workspace", "arg": "e+1"},       # relative: no tab
                 {"modmask": 4 | 8, "key": "1", "dispatcher": "workspace", "arg": "1"}]          # a second key for 1
        keys = hyprland.switch_keys(binds, lua=False)
        self.assertEqual(keys, {"1": "⊞ 1", "2": "Alt F2", "3": "⊞ 3", "10": "⊞ 0", "name:scratch": "⊞ S"})

    def test_lua_binds_are_taken_as_hyprlands_own(self):
        binds = [{"modmask": 64, "key": str(n % 10), "dispatcher": "__lua", "arg": str(20 + n)} for n in range(1, 11)]
        binds.append({"modmask": 65, "key": "1", "dispatcher": "__lua", "arg": "99"})  # (moves a window there)
        keys = hyprland.switch_keys(binds, lua=True)
        self.assertEqual(keys["1"], "⊞ 1")
        self.assertEqual(keys["10"], "⊞ 0")
        self.assertEqual(len(keys), 10)
        self.assertEqual(hyprland.switch_keys(binds, lua=False), {})  # (not in a classic config)


class LuaQuoting(unittest.TestCase):
    def test_strings(self):
        self.assertEqual(hyprland._lua('a "b" \\ c'), '"a \\"b\\" \\\\ c"')
        self.assertEqual(hyprland._lua("one\ntwo"), '"one\\010two"')
        self.assertEqual(hyprland._lua("ž ✳"), '"ž ✳"')


class AgainstFakeHyprland(unittest.TestCase):
    """The client's side of everything, with a Lua config (LUA) and a classic one."""
    LUA = True

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-hypr-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.fake = FakeHyprland(self.run_dir, lua=self.LUA, monitors=("DP-1", "HDMI-A-1"))
        self.addCleanup(self.fake.stop)
        self.env = {"XDG_RUNTIME_DIR": str(self.run_dir), "HYPRLAND_INSTANCE_SIGNATURE": self.fake.signature}
        self.state_dir = self.run_dir / "sidelit"
        self.clients = []
        self.changes = 0

    def tearDown(self):
        for c in self.clients:
            c.close()

    def client(self, primary=True):
        def changed():
            self.changes += 1
        listening = len(self.fake._listeners)
        c = hyprland.HyprlandClient(changed, primary=primary, env=self.env, state_dir=self.state_dir)
        self.clients.append(c)
        end = time.monotonic() + 2
        while primary and len(self.fake._listeners) == listening and time.monotonic() < end:
            time.sleep(0.005)  # (the fake takes the event socket's connection in a thread of its own)
        return c

    def pump(self, c, until=None, secs=2.0):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            c.pump()
            if until is None or until():
                return True
            time.sleep(0.01)
        return False

    def sent(self, since=0):
        return [r for r in self.fake.requests[since:] if not r.startswith("j/")]

    # ---- reading ----
    def test_it_knows_which_config_it_has(self):
        self.assertEqual(self.client().lua, self.LUA)

    def test_workspaces_numbers_first_then_names(self):
        self.fake.switch("name:notes")   # named, on DP-1
        self.fake.open_window(title="notes", focus=False)
        self.fake.switch("4")
        self.fake.open_window(title="on 3", ws=3, focus=False)
        self.fake.special_window()
        c = self.client()
        listed = [(w.index, w.id, w.name) for w in c.list()]
        self.assertEqual(listed, [(1, "2", "2"), (2, "3", "3"), (3, "4", "4"), (4, "name:notes", "notes")])
        self.assertEqual(c.active().id, "4")  # the screen with the keyboard's
        self.assertEqual(c.resolve("3").id, "3")          # its own number
        self.assertEqual(c.resolve("notes").id, "name:notes")
        self.assertFalse(c.workspaces["3"].pinned)

    def test_windows(self):
        a = self.fake.open_window("kitty", "✳ Claude Code", ws=1, pid=777)
        s = self.fake.special_window()
        c = self.client()
        tl = next(t for t in c.toplevels.values() if t.address == a)
        self.assertEqual((tl.app_id, tl.title, tl.pid, tl.output), ("kitty", "✳ Claude Code", 777, "DP-1"))
        # Hyprland's stable id, not the address, after this Hyprland's token: each Hyprland
        # numbers its windows the same way from its start
        self.assertEqual(tl.identifier, f"{c.token}-{self.fake.windows[a]['stable']}")
        self.assertEqual(c.workspace_of(tl).id, "1")
        self.assertEqual(c.copy_ids_of(tl), frozenset({"1"}))
        self.assertTrue(tl.focused)
        self.assertEqual(c.toplevels_on(c.workspaces["1"]), [tl])
        mixer = next(t for t in c.toplevels.values() if t.address == s)
        self.assertIsNone(c.workspace_of(mixer))  # a scratchpad's window is on no workspace
        self.assertEqual(c.output_names(), {"DP-1", "HDMI-A-1"})
        self.assertEqual(c.shortcut_label(c.workspaces["1"], 2), "")  # (no binds)

    def test_window_ids_are_this_hyprlands(self):
        a = self.fake.open_window("kitty", "one", ws=1)
        c = self.client()
        again = FakeHyprland(self.run_dir, lua=self.LUA, signature="abc123_1792338343_2")  # (the next login's)
        self.addCleanup(again.stop)
        b = again.open_window("kitty", "one", ws=1)
        other = hyprland.HyprlandClient(None, primary=False, env={**self.env, "HYPRLAND_INSTANCE_SIGNATURE": again.signature},
                                        state_dir=self.state_dir)
        self.clients.append(other)
        self.assertEqual(self.fake.windows[a]["stable"], again.windows[b]["stable"])
        mine = next(t for t in c.toplevels.values() if t.address == a)
        theirs = next(t for t in other.toplevels.values() if t.address == b)
        self.assertNotEqual(mine.identifier, theirs.identifier)

    def test_a_title_comes_from_the_event_alone(self):
        a = self.fake.open_window(title="⠋ Claude Code")
        c = self.client()
        tl = next(iter(c.toplevels.values()))
        before = len(self.fake.requests)
        self.fake.set_title(a, "✳ Claude Code")
        self.assertTrue(self.pump(c, lambda: tl.title == "✳ Claude Code"))
        self.assertEqual(self.fake.requests[before:], [])  # nothing asked: Claudes retitle ten times a second
        self.assertGreater(self.changes, 0)

    def test_windows_coming_moving_and_going(self):
        c = self.client()
        a = self.fake.open_window("foot", "shell", ws=1)
        self.assertTrue(self.pump(c, lambda: len(c.toplevels) == 1))
        tl = next(iter(c.toplevels.values()))
        self.assertTrue(tl.focused and tl.focused_at > 0)
        self.fake.switch("2")
        self.fake._move(a, "2", follow=False)
        self.assertTrue(self.pump(c, lambda: c.workspace_of(tl) is not None and c.workspace_of(tl).id == "2"))
        self.fake.close_window(a)
        self.assertTrue(self.pump(c, lambda: not c.toplevels))
        self.assertTrue(tl.closed)
        self.assertIn(tl.identifier, c.closed_focus)  # it had the keyboard a moment before it went

    def test_hyprland_quitting(self):
        c = self.client()
        self.fake.stop()
        with self.assertRaises(ConnectionError):
            self.pump(c, until=lambda: False, secs=1.0)

    def test_no_hyprland(self):
        self.fake.stop()
        with self.assertRaises(RuntimeError), mock.patch("sys.stderr"):
            self.client()

    # ---- acting ----
    def test_focus_close_and_move(self):
        a = self.fake.open_window("foot", "one", ws=1)
        b = self.fake.open_window("foot", "two", ws=1)
        c = self.client()
        tl_a = next(t for t in c.toplevels.values() if t.address == a)
        before = len(self.fake.requests)
        c.focus(tl_a)
        self.assertEqual(self.fake.focused, a)
        self.assertTrue(c.move_toplevel(tl_a, c.workspaces["2"]))
        self.assertEqual(self.fake.where(a), 2)
        self.assertEqual(self.fake.monitors[0]["ws"], 1)  # silently: you stay where you are
        tl_b = next(t for t in c.toplevels.values() if t.address == b)
        c.close_toplevel(tl_b)
        self.assertNotIn(b, self.fake.windows)
        if self.LUA:
            self.assertEqual(self.sent(before), [
                f'dispatch hl.dsp.focus({{window = "address:{a}"}})',
                f'dispatch hl.dsp.window.move({{workspace = "2", follow = false, window = "address:{a}"}})',
                f'dispatch hl.dsp.window.close({{window = "address:{b}"}})'])
        else:
            self.assertEqual(self.sent(before), [f"dispatch focuswindow address:{a}",
                                                 f"dispatch movetoworkspacesilent 2,address:{a}",
                                                 f"dispatch closewindow address:{b}"])

    def test_moving_to_a_screen_takes_the_workspace_there(self):
        a = self.fake.open_window("foot", "one", ws=1)
        self.fake.switch("3")  # on DP-1, with nothing on it yet
        self.fake.open_window("foot", "two", ws=3)
        c = self.client()
        tl = next(t for t in c.toplevels.values() if t.address == a)
        self.assertTrue(c.move_toplevel(tl, c.workspaces["3"], "HDMI-A-1"))
        self.assertEqual(self.fake.where(a), 3)
        self.assertEqual(self.fake.workspaces[3]["monitor"], 1)
        before = len(self.fake.requests)
        c.refresh()
        self.assertTrue(c.move_toplevel(tl, c.workspaces["3"], "HDMI-A-1"))  # there already: nothing to do
        self.assertTrue(c.move_toplevel(tl, c.workspaces["3"], "DP-9"))      # no such screen: left alone
        self.assertEqual(self.sent(before), [])

    def test_activate_and_rename(self):
        c = self.client()
        before = len(self.fake.requests)
        c.activate(c.workspaces["1"])  # (you are on it: nothing, or back-and-forth would take you away)
        self.assertEqual(self.sent(before), [])
        c.activate(c.workspaces["2"])
        self.assertEqual(self.fake.focused_monitor, 1)
        self.pump(c, lambda: c.active() is not None and c.active().id == "2")
        self.assertEqual(c.active().id, "2")
        c.rename(c.workspaces["2"], "web\nstuff")
        self.assertEqual(self.fake.workspaces[2]["name"], "web stuff")
        self.assertEqual(c.workspaces["2"].name, "web stuff")
        self.assertEqual(c.workspaces["2"].id, "2")  # still Sidelit's id for it

    # ---- keeping workspaces ----
    def test_create_brings_back_the_number_it_had(self):
        c = self.client()
        ws = c.create("Workspace", want=("7",))
        self.assertEqual(ws.id, "7")
        self.assertTrue(ws.pinned)
        self.assertIn(7, self.fake.workspaces)
        self.assertEqual(json.loads(self.state_file().read_text())["ours"],
                         {"7": {"screen": "DP-1", "bound": False, "own": True}})  # (and the screen it is on)

    def state_file(self) -> Path:
        return self.state_dir / f"hyprland-{self.fake.signature}.json"

    def test_create_else_takes_the_number_after_all_known(self):
        self.fake.config_rule("7", persistent=None, monitor="HDMI-A-1")  # your config puts 7 on the big screen
        c = self.client()
        c.preferred_ids = {"5", "cb8061aa", "123456"}  # the layout's ids: 5 is kept, the others another desktop's
        ws = c.create("Workspace", want=("2", "cb8061aa"))  # 2 is in use; the other is not Hyprland's
        self.assertEqual(ws.id, "6")
        self.assertEqual(c.create().id, "8")  # (7 is your config's)

    def test_create_brings_back_the_number_it_had_last(self):
        c = self.client()
        # (a kept workspace that came back as 9 once, its 4 being taken then: 9 is where it was last)
        self.assertEqual(c.create("Workspace", want=("4", "9")).id, "9")

    def test_a_workspace_your_config_puts_on_a_screen_stays_there(self):
        self.fake.config_rule("3", persistent=None, monitor="HDMI-A-1")
        c = self.client()
        made = c.create(want=("3",))  # (the number a kept workspace had: wanted, so made all the same)
        self.assertEqual((made.id, made.monitor), ("3", "HDMI-A-1"))
        self.assertEqual(self.fake.rule("3")["monitor"], "HDMI-A-1")
        self.fake.to_monitor("3", "DP-1")  # moved by hand: your config's rule stays as it is
        self.pump(c)
        self.assertEqual(self.fake.rule("3")["monitor"], "HDMI-A-1")
        c.remove(c.workspaces["3"])
        self.assertEqual(self.fake.rule("3")["monitor"], "HDMI-A-1")
        self.assertFalse(self.fake.rule("3")["persistent"])

    def test_pinning_and_unpinning(self):
        a = self.fake.open_window("foot", "one", ws=1)
        c = self.client()
        ws = c.workspaces["1"]
        c.pin(ws, True)
        self.assertTrue(ws.pinned and ws.copies_pinned)
        self.fake.close_window(a)
        self.fake.switch("3")  # (on DP-1, where 1 was shown)
        self.pump(c)
        self.assertIn(1, self.fake.workspaces)  # empty and not shown, and kept
        c.remove(c.workspaces["1"])
        self.assertNotIn(1, self.fake.workspaces)  # Hyprland drops it once it is unpinned
        self.pump(c, lambda: "1" not in c.workspaces)
        self.assertTrue(ws.removed)
        if self.LUA:  # Sidelit's own rule is switched off: nothing of it is left
            self.assertIsNone(self.fake.rule("1"))
        else:         # a classic config's rule keeps its screen until the config is reloaded
            self.assertFalse(self.fake.rule("1")["persistent"])

    def test_pinning_leaves_every_workspace_on_its_screen(self):
        """Hyprland moves a persistent workspace whose rule names no screen to the one with the
        keyboard, whenever it applies its rules: Sidelit's rules name the screen."""
        c = self.client()
        c.pin(c.workspaces["1"], True)  # on DP-1
        self.fake.switch("2")            # the keyboard goes to HDMI-A-1
        self.pump(c)
        made = c.create(want=("5",))
        self.assertEqual((made.id, made.monitor), ("5", "HDMI-A-1"))  # made where you are
        self.assertEqual(self.fake.workspaces[1]["monitor"], 0)       # 1 stayed on DP-1
        self.assertEqual(self.fake.rule("1")["monitor"], "DP-1")

    def test_a_workspace_you_move_takes_its_pin_along(self):
        c = self.client()
        c.pin(c.workspaces["1"], True)
        self.fake.to_monitor("1", "HDMI-A-1")  # you move it (its screen is still there)
        self.assertTrue(self.pump(c, lambda: self.fake.rule("1").get("monitor") == "HDMI-A-1"))
        c.create(want=("6",))  # Hyprland applies its rules again: 1 stays where you put it
        self.assertEqual(self.fake.workspaces[1]["monitor"], 1)

    def test_two_workspaces_swapped_settle(self):
        """Swapping the workspaces of two screens makes both rules change together. Taken one by
        one, each rule set makes Hyprland move the other workspace back, and so on for ever."""
        self.fake.open_window("foot", "left", ws=1, focus=False)
        self.fake.open_window("foot", "right", ws=2, focus=False)
        c = self.client()
        c.pin(c.workspaces["1"], True)
        c.pin(c.workspaces["2"], True)
        self.fake.swap()
        for _ in range(10):
            self.pump(c, secs=0.05)
        events = len(self.fake.events)
        for _ in range(10):
            self.pump(c, secs=0.05)
        self.assertEqual(len(self.fake.events), events)  # nothing moves any more
        self.assertEqual((self.fake.workspaces[1]["monitor"], self.fake.workspaces[2]["monitor"]), (1, 0))
        self.assertEqual((self.fake.rule("1")["monitor"], self.fake.rule("2")["monitor"]), ("HDMI-A-1", "DP-1"))
        # each screen shows the one swapped onto it (a classic config's rules, one at a time,
        # would leave an empty workspace on one of them), and the keyboard is where it was
        self.assertEqual((self.fake.mon(0)["ws"], self.fake.mon(1)["ws"]), (2, 1))
        self.assertEqual(self.fake.focused_monitor, 0)

    def test_a_number_your_config_puts_on_a_screen_that_is_away(self):
        """At a login undocked, a kept workspace whose number your config puts on the big screen
        cannot be made (Hyprland skips a rule whose screen is not there): another number, at once."""
        self.fake.config_rule("6", persistent=None, monitor="HDMI-A-1")
        self.fake.config_rule("8", persistent=True, monitor="HDMI-A-1")
        c = self.client()
        self.fake.unplug("HDMI-A-1")
        self.pump(c)
        start = time.monotonic()
        made = c.create(want=("6", "8"))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertNotIn(made.id, ("6", "8"))
        self.assertEqual(made.monitor, "DP-1")

    def test_a_screen_named_by_its_description(self):
        self.fake.config_rule("4", persistent=None, monitor="desc:Fake Monitors HDMI-A-1")
        c = self.client()
        self.assertEqual(c._screen("desc:Fake Monitors HDMI-A-1"), "HDMI-A-1")
        self.assertIsNone(c._screen("desc:Some Other"))
        self.assertEqual(c.create().id, "3")  # (on DP-1: 4 would open on the big screen)
        self.fake.switch("2")                  # the keyboard on the big screen
        self.pump(c)
        made = c.create(want=("4",))
        self.assertEqual((made.id, made.monitor, made.pinned), ("4", "HDMI-A-1", True))

    def test_a_pin_set_again_before_the_reload_is_seen_stays_sidelits(self):
        """Hyprland reloads its config (autoreload, as you save it) and a one-shot pins again
        before the daemon has heard of the reload: that rule is Sidelit's, not your config's."""
        daemon_side = self.client()
        daemon_side.create(want=("4",))
        self.fake.reload()
        one_shot = self.client(primary=False)
        one_shot.create(want=("4",))
        self.pump(daemon_side)
        daemon_side.remove(daemon_side.workspaces["4"])
        self.assertFalse(self.fake._persistent(4))

    def test_a_rename_that_fails_changes_nothing(self):
        self.fake.switch("name:notes")
        c = self.client()
        c.pin(c.workspaces["name:notes"], True)
        c.rename(c.workspaces["name:notes"], "  ")
        self.assertEqual(set(json.loads(self.state_file().read_text())["ours"]), {"name:notes"})

    def test_a_screen_that_goes_gets_its_workspaces_back(self):
        a = self.fake.open_window("foot", "on the big screen", ws=2, focus=False)
        self.fake.switch("2")
        self.fake.switch("4")  # (and an empty kept one there)
        c = self.client()
        c.pin(c.workspaces["4"], True)
        self.fake.switch("2")
        self.pump(c)
        c.pin(c.workspaces["2"], True)  # on HDMI-A-1
        self.fake.unplug("HDMI-A-1")     # Hyprland puts them on DP-1 meanwhile...
        self.pump(c, lambda: c.workspaces["2"].monitor == "DP-1")
        c.create(want=("6",))            # ...and, applying its rules, lets their pins lapse
        self.pump(c)
        self.assertFalse(self.fake._persistent(2))
        self.assertTrue(c.workspaces["2"].pinned and c.workspaces["2"].copies_pinned)  # still Sidelit's
        self.assertEqual(self.fake.rule("2")["monitor"], "HDMI-A-1")  # ...and the rule still says where it was
        self.assertNotIn(4, self.fake.workspaces)  # (the empty one went meanwhile)
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: "2" in c.workspaces and c.workspaces["2"].monitor == "HDMI-A-1"))
        self.assertEqual(self.fake.where(a), 2)
        self.assertTrue(self.pump(c, lambda: "4" in c.workspaces and c.workspaces["4"].monitor == "HDMI-A-1"))
        self.assertTrue(self.fake._persistent(2) and self.fake._persistent(4))

    def test_your_own_persistent_workspaces_stay_so(self):
        self.fake.config_rule("5", persistent=True)
        c = self.client()
        before = len(self.fake.requests)
        c.pin(c.workspaces["5"], False)
        self.assertEqual(self.sent(before), [])
        self.assertTrue(self.fake._persistent(5))

    def test_a_config_reload_keeps_sidelits_pins(self):
        c = self.client()
        c.create(want=("4",))
        self.fake.reload()  # Hyprland drops the rules added since it read its config...
        self.assertTrue(self.pump(c, lambda: c.workspaces.get("4") is not None and c.workspaces["4"].pinned))
        self.assertTrue(self.fake._persistent(4))  # ...and Sidelit adds them again

    def test_a_restarted_daemon_still_knows_what_it_pinned(self):
        c = self.client()
        c.create(want=("4",))
        c.close()
        before = len(self.fake.requests)
        again = self.client()
        self.assertFalse(any(r.startswith(("eval hl.workspace_rule", "keyword")) for r in self.fake.requests[before:]))
        again.remove(again.workspaces["4"])  # (its rule was there: not set again, nor taken for yours)
        self.assertNotIn(4, self.fake.workspaces)

    def test_a_one_shot_pin_and_the_daemon_share_their_notes(self):
        daemon_side = self.client()
        one_shot = self.client(primary=False)  # `sidelit ws pin`
        one_shot.pin(one_shot.workspaces["2"], True)
        daemon_side.create(want=("5",))        # (the daemon writes the notes after it)
        self.assertEqual(set(json.loads(self.state_file().read_text())["ours"]), {"2", "5"})
        daemon_side.remove(daemon_side.workspaces["2"])  # the daemon knows the one-shot's pin
        self.assertFalse(self.fake._persistent(2))

    def test_renaming_a_named_workspace_takes_its_pin_along(self):
        self.fake.switch("name:notes")
        c = self.client()
        c.pin(c.workspaces["name:notes"], True)
        c.rename(c.workspaces["name:notes"], "ideas")
        self.assertIn("name:ideas", c.workspaces)
        self.assertTrue(c.workspaces["name:ideas"].pinned)
        self.assertEqual(set(json.loads(self.state_file().read_text())["ours"]), {"name:ideas"})
        self.fake.switch("1")  # (left, empty: kept under its new name, and the old name brings nothing back)
        self.assertTrue(any(w["name"] == "ideas" for w in self.fake.workspaces.values()))
        self.assertFalse(any(w["name"] == "notes" for w in self.fake.workspaces.values()))

    def test_one_shot_commands_have_no_event_socket(self):
        c = self.client(primary=False)
        self.assertEqual(c.fd(), -1)
        self.fake.open_window("foot", "new", ws=1)
        start = time.monotonic()
        c.wait(0.5)  # a short pause, then all of it read again
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertEqual(len(c.toplevels), 1)
        c.roundtrip()


class AgainstFakeClassicHyprland(AgainstFakeHyprland):
    LUA = False


class LeavingAClosedWorkspace(unittest.TestCase):
    """You close the workspace you are on: once it is empty Sidelit goes to another, so that it
    is no longer shown and goes. On Hyprland that one is on the same screen: the next one in the
    list may be shown on the other, and the closed one would stay on its screen."""

    def leave(self, spaces):
        from sidelit.core.engine import Engine
        from sidelit.core.model import Layout, Obs
        act = mock.Mock()
        e = Engine(Layout(), act, now=0.0)
        e.obs = Obs(spaces=spaces, windows={}, outputs=frozenset({"DP-1", "HDMI-A-1"}))
        e.leave_when_empty(2, 1.0)
        e._leave_closed(e.obs, 2.0)
        return act.activate.call_args[0][0] if act.activate.called else None

    def test_the_nearest_on_the_same_screen(self):
        from sidelit.core.model import Space
        spaces = [Space(1, frozenset({"1"}), True, False, output="DP-1"),
                  Space(2, frozenset({"2"}), False, True, output="DP-1"),
                  Space(3, frozenset({"3"}), True, False, output="HDMI-A-1"),
                  Space(4, frozenset({"4"}), True, False, output="DP-1")]
        self.assertEqual(self.leave(spaces), 1)  # (the nearest on DP-1; 3, the next, is on the other screen)

    def test_elsewhere_the_next_one(self):
        from sidelit.core.model import Space
        spaces = [Space(i, frozenset({str(i)}), True, i == 2) for i in (1, 2, 3)]  # (every screen shows them)
        self.assertEqual(self.leave(spaces), 3)


@unittest.skipIf(daemon is None, "needs GLib")
class TheDaemonOnHyprland(unittest.TestCase):
    def test_a_new_kept_workspace_is_made_not_found(self):
        """No empty workspace waits at the end on Hyprland: the daemon asks for one, under the
        number the kept workspace had."""
        d = object.__new__(daemon.Daemon)
        made = hyprland.Space(id="3", coords=(2,), persistent=True)
        d.ws = mock.Mock(caps=hyprland.HyprlandClient.caps, spare_workspace=False)
        d.ws.create.return_value = made
        with mock.patch.object(daemon.config, "DRY_RUN", False):
            space = d.pin_new_space(want=("3", "x"))
        d.ws.create.assert_called_once_with("Workspace", want=("3", "x"))
        self.assertEqual((space.index, space.ids, space.pinned), (2, frozenset({"3"}), True))


if __name__ == "__main__":
    unittest.main()
