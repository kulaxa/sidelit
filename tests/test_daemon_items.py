"""The daemon's side of an item's workspace: which directory it gets. Needs GLib (skipped without it)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sidelit import daemon
except (ImportError, ValueError):  # no GLib bindings
    daemon = None


@unittest.skipIf(daemon is None, "needs GLib")
class ItemFolder(unittest.TestCase):
    def test_a_folder_under_home_and_nothing_else(self):
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "src" / "app").mkdir(parents=True)
        with mock.patch.object(Path, "home", return_value=home):
            cwd = daemon.Daemon._item_cwd
            self.assertEqual(cwd("src/app"), str(home / "src" / "app"))
            self.assertEqual(cwd(str(home / "src")), str(home / "src"))
            for outside in ("../", "/etc", "src/missing", "", None, 3):
                self.assertEqual(cwd(outside), str(home), outside)



@unittest.skipIf(daemon is None, "needs GLib")
class FocusAfterSlide(unittest.TestCase):
    """A session tab's terminal gets the keyboard after COSMIC's workspace slide, not during it
    (activating a window then makes the switch jump instead of slide)."""

    def setUp(self):
        class Ws:
            def __init__(self, index, active=False):
                self.index, self.active = index, active

        class Desk:
            def __init__(self):
                self.spaces, self.focused = [Ws(1, active=True), Ws(2), Ws(3)], []

            def list(self):
                return self.spaces

            def activate(self, ws):
                for w in self.spaces:
                    w.active = w is ws

            def focus(self, tl):
                self.focused.append(tl)

        d = object.__new__(daemon.Daemon)
        d.ws = Desk()
        d._focus_pending = 0
        d._switched_at = 0.0
        d.attention = daemon.Attention()
        d._tl = {"w1": "terminal-1", "w2": "terminal-2", "w3": "terminal-3"}
        d.engine = mock.Mock()
        d.engine.obs.windows_on = lambda index: [index]
        d.engine.obs.focused_at = {}
        d._claudes_on = lambda obs, wins, now: [{"key": f"s{wins[0]}", "window": f"w{wins[0]}", "calls": 0.0}]
        d._lw_for = lambda params: (object(), params["space"])
        d._space = lambda index: next(w for w in d.ws.spaces if w.index == index)
        self.d, self.timers, self.removed = d, {}, []
        ids = iter(range(100, 200))

        def timeout_add(ms, fn, *args):
            tid = next(ids)
            self.timers[tid] = (ms, fn, args)
            return tid
        patches = [mock.patch.object(daemon.GLib, "timeout_add", side_effect=timeout_add),
                   mock.patch.object(daemon.GLib, "source_remove", side_effect=self.removed.append)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fire(self, tid):
        ms, fn, args = self.timers.pop(tid)
        fn(*args)

    def test_another_workspace_slides_first_then_claude_gets_the_keyboard(self):
        self.d.cmd_activate({"space": 2})
        self.assertEqual(self.d.ws.focused, [])
        (tid, (ms, _, _)), = self.timers.items()
        self.assertEqual(ms, daemon.FOCUS_AFTER_SWITCH_MS)
        self.fire(tid)
        self.assertEqual(self.d.ws.focused, ["terminal-2"])

    def test_the_workspace_you_are_on_focuses_at_once(self):
        self.d.cmd_activate({"space": 1})
        self.assertEqual(self.d.ws.focused, ["terminal-1"])
        self.assertEqual(self.timers, {})

    def test_a_quick_second_click_drops_the_first_focus(self):
        self.d.cmd_activate({"space": 2})
        first = next(iter(self.timers))
        self.d.cmd_activate({"space": 3})
        self.assertEqual(self.removed, [first])
        self.fire(next(t for t in self.timers if t != first))
        self.assertEqual(self.d.ws.focused, ["terminal-3"])

    def test_no_focus_if_you_moved_on_during_the_slide(self):
        self.d.cmd_activate({"space": 2})
        tid = next(iter(self.timers))
        self.d.ws.activate(self.d.ws.spaces[0])  # you went elsewhere (a keyboard shortcut)
        self.fire(tid)
        self.assertEqual(self.d.ws.focused, [])


def claude(sid, title, status="done", started=0.0, typed=0.0, pane=None):
    """A Claude as the detector reports it (detect.Detector.claudes_for)."""
    return {"session": sid, "title": title, "status": status, "agents": 0, "cwd": "/src/pricing", "pid": 1,
            "pane": pane or {}, "started": started, "prompt_t": typed, "how": "launch", "hooked": True}


@unittest.skipIf(daemon is None, "needs GLib")
class Boxes(unittest.TestCase):
    """A workspace's Claudes as the daemon shows them: a box each, who calls you, where you land."""

    def setUp(self):
        from sidelit.core.model import Obs, Space, Win
        self.Obs, self.Space, self.Win = Obs, Space, Win
        d = object.__new__(daemon.Daemon)
        d.cfg = {"check_turns": False}
        d.attention = daemon.Attention()
        from sidelit import agents
        d.detector = mock.Mock(hooked={}, agents=agents.every())
        d._model = lambda sid: "Opus 5.5"
        d._unmatched = {}
        d.journal = lambda kind, **fields: None
        d.engine = mock.Mock()
        self.d = d
        self.windows, self.claudes, self.focused_at = {}, {}, {}

    def window(self, wid, title, *claudes, focused=False, space=1):
        self.windows[wid] = self.Win(id=wid, app="terminator", title=title, space=space, output="eDP-1",
                                     focused=focused)
        if claudes:
            self.claudes[wid] = {**claudes[0], "more": list(claudes[1:])}

    def boxes(self, now=100.0, space=1):
        obs = self.Obs(spaces=[self.Space(index=1, ids=frozenset({"a"}), pinned=True, active=True)],
                       windows=dict(self.windows), outputs=frozenset(), claude=dict(self.claudes),
                       focused_at=dict(self.focused_at))
        self.d.engine.obs = obs
        return self.d._claudes_on(obs, obs.windows_on(space), now)

    def test_three_claudes_two_sharing_a_window_are_three_boxes_in_start_order(self):
        self.window("A", "✳ Pricing API", claude("s1", "Pricing API", started=10.0),
                    claude("s3", "Ask about CSV", started=30.0))
        self.window("B", "✳ Fix flaky test", claude("s2", "Fix flaky test", status="busy", started=20.0))
        got = self.boxes()
        self.assertEqual([(b["key"], b["title"], b["state"], b["window"]) for b in got], [
            ("s1", "Pricing API", "done", "A"), ("s2", "Fix flaky test", "busy", "B"), ("s3", "Ask about CSV", "done", "A")])

    def test_the_second_claude_calls_while_you_type_in_the_first(self):
        """Not only from another workspace: whenever you are not typing in its own terminal."""
        self.window("A", "✳ Pricing API", claude("s1", "Pricing API", status="busy"), focused=True)
        self.window("B", "✳ Review tests", claude("s2", "Review tests", status="busy"))
        self.boxes(100.0)
        self.window("A", "✳ Pricing API", claude("s1", "Pricing API"), focused=True)
        self.window("B", "✳ Review tests", claude("s2", "Review tests"))
        got = {b["key"]: b for b in self.boxes(105.0)}
        self.assertEqual((got["s1"]["calls"], got["s2"]["calls"]), (0.0, 105.0))
        self.assertEqual((got["s1"]["typing"], got["s2"]["typing"]), (True, False))
        self.window("B", "✳ Review tests", claude("s2", "Review tests"), focused=True)  # you went to it
        self.assertEqual({b["key"]: b["calls"] for b in self.boxes(110.0)}, {"s1": 0.0, "s2": 0.0})

    def test_in_a_split_window_you_type_in_the_pane_whose_title_it_shows(self):
        self.window("A", "⠂ Pricing API", claude("s1", "Pricing API", status="busy"),
                    claude("s2", "Review tests", status="busy"), focused=True)
        self.boxes(100.0)
        self.window("A", "✳ Pricing API", claude("s1", "Pricing API"), claude("s2", "Review tests"), focused=True)
        got = {b["key"]: b for b in self.boxes(105.0)}
        self.assertEqual((got["s1"]["typing"], got["s2"]["calls"]), (True, 105.0))
        # a shell in the pane with the keyboard: you are typing in neither
        self.window("A", "alex@laptop: ~", claude("s1", "Pricing API"), claude("s2", "Review tests"), focused=True)
        self.assertEqual([b["typing"] for b in self.boxes(106.0)], [False, False])
        # two with the same title: no telling which, so both
        self.window("A", "✳ Same", claude("s1", "Same"), claude("s2", "Same"), focused=True)
        self.assertEqual([b["typing"] for b in self.boxes(107.0)], [True, True])

    def test_a_new_tab_with_no_name_yet_is_the_one_you_type_in(self):
        """Its terminal says "Claude Code" until it has a name; the other one finishing calls you."""
        self.window("A", "⠂ Claude Code", claude("s1", "Fix Test", status="busy"), claude("s2", ""), focused=True)
        self.boxes(100.0)
        self.window("A", "✳ Claude Code", claude("s1", "Fix Test"), claude("s2", ""), focused=True)
        got = {b["key"]: b for b in self.boxes(105.0)}
        self.assertEqual((got["s1"]["typing"], got["s2"]["typing"], got["s1"]["calls"]), (False, True, 105.0))
        self.assertEqual([b["title"] for b in self.boxes(106.0)], ["Fix Test", ""])  # ("New session" in the sidebar)

    def test_a_window_titled_claude_code_with_no_session_is_a_new_session(self):
        self.window("A", "✳ Claude Code")
        self.assertEqual([b["title"] for b in self.boxes()], [""])

    def test_going_there_lands_on_the_one_that_waited_longest(self):
        for sid, wid in (("s1", "A"), ("s2", "B"), ("s3", "C")):
            self.window(wid, f"✳ {sid}", claude(sid, sid, status="busy"))
        self.boxes(100.0)
        self.window("B", "✳ s2", claude("s2", "s2"))
        self.boxes(101.0)
        self.window("C", "✳ s3", claude("s3", "s3"))
        self.window("A", "✳ s1", claude("s1", "s1"))
        boxes = self.boxes(102.0)
        self.assertEqual([b["calls"] for b in boxes], [102.0, 101.0, 102.0])
        self.assertEqual(self.d._landing(boxes)["key"], "s2")

    def test_else_on_the_one_you_used_last(self):
        self.window("A", "✳ s1", claude("s1", "s1", typed=50.0), claude("s3", "s3", typed=70.0))
        self.window("B", "✳ s2", claude("s2", "s2", typed=90.0))
        self.focused_at = {"A": 80.0, "B": 60.0}
        self.assertEqual(self.d._landing(self.boxes())["key"], "s3")  # its window had the keyboard last
        self.focused_at = {}
        self.assertEqual(self.d._landing(self.boxes())["key"], "s2")
        self.assertIsNone(self.d._landing([]))

    def test_a_claude_titled_window_with_no_session_is_a_box_only_alone(self):
        self.window("A", "✳ Remote work")
        self.assertEqual([(b["key"], b["session"], b["title"]) for b in self.boxes()], [("w:A", None, "Remote work")])
        self.window("B", "✳ Pricing API", claude("s1", "Pricing API"))
        self.assertEqual([b["key"] for b in self.boxes()], ["s1"])

    def test_a_codex_with_no_session_matched_is_codexs_box(self):
        """(before you trusted its hooks: its spinner looks like Claude's, its title is its project)"""
        from sidelit import agents
        codex = agents.get("codex")
        with mock.patch.object(codex.terminals, "seen", {900: (1.0, "/src/pricing", ["codex"], 2.0)}), \
                mock.patch.object(codex.terminals, "live", {900}):
            self.window("A", "⠸ pricing")
            self.assertEqual([(b["agent"], b["title"], b["state"]) for b in self.boxes()], [("codex", "pricing", "busy")])
            self.window("A", "pricing")
            self.assertEqual([(b["agent"], b["state"]) for b in self.boxes()], [("codex", "done")])
            self.window("A", "⠸ Fix tests")  # (a Claude's)
            self.assertEqual([b["agent"] for b in self.boxes()], ["claude"])
            del self.windows["A"]  # the file manager on that folder has its name too: not a Codex
            self.windows["F"] = self.Win(id="F", app="org.gnome.Nautilus", title="pricing", space=1, output="eDP-1")
            self.assertEqual(self.boxes(), [])

    def test_the_history_lists_every_claude(self):
        h = {"id": "h1", "closed": 1.0, "workspace": {"cwd": "/src/pricing", "slots": [
            {"id": "x", "kind": "terminal", "app": "terminator", "title": "✳ Pricing API",
             "state": {"session": "s1", "claude_title": "Pricing API",
                       "more": [{"session": "s2", "claude_title": "Review tests", "cwd": "/src/pricing"}]}},
            {"id": "y", "kind": "browser", "app": "google-chrome", "title": "Docs"}]}}
        item = daemon.Daemon._history_item(h)
        self.assertEqual(item["what"], ["Claude 'Pricing API'", "Claude 'Review tests'", "chrome 'Docs'"])
        self.assertEqual(item["title"], "Pricing API")


@unittest.skipIf(daemon is None, "needs GLib")
class Landing(unittest.TestCase):
    """A click on a box goes to that Claude; switching workspace yourself (Super+N) lands on the
    Claude there that needs you."""

    def setUp(self):
        class Ws:
            def __init__(self, index, active=False):
                self.index, self.active = index, active

        class Desk:
            def __init__(self):
                self.spaces, self.focused = [Ws(1, active=True), Ws(2)], []

            def list(self):
                return self.spaces

            def activate(self, ws):
                for w in self.spaces:
                    w.active = w is ws

            def focus(self, tl):
                self.focused.append(tl)

        d = object.__new__(daemon.Daemon)
        d.ws = Desk()
        d._focus_pending = 0
        d._switched_at = 0.0
        d._active_space = 1
        d.attention = daemon.Attention()
        d.journal = lambda kind, **fields: None
        d._tl = {"A": "terminal-A", "B": "terminal-B"}
        d.engine = mock.Mock()
        d.engine.calm = lambda now: True
        d.engine.owner = {1: "w-one", 2: "w-two"}
        d._space = lambda index: next(w for w in d.ws.spaces if w.index == index)
        self.boxes = {1: [], 2: [{"key": "s1", "window": "A", "calls": 0.0, "pane": {}, "title": "One"},
                             {"key": "s2", "window": "B", "calls": 90.0, "pane": {"TMUX_PANE": "%4"}, "title": "Two"}]}
        d._claudes_on = lambda obs, wins, now: self.boxes[wins]
        d.engine.obs.windows_on = lambda index: index
        self.d, self.timers = d, {}
        self.spaces()

        def timeout_add(ms, fn, *args):
            self.timers[len(self.timers) + 1] = (fn, args)
            return len(self.timers)
        self.panes = []
        for patch in (mock.patch.object(daemon.GLib, "timeout_add", side_effect=timeout_add),
                      mock.patch.object(daemon.panes, "focus", side_effect=self.panes.append)):
            patch.start()
            self.addCleanup(patch.stop)

    def spaces(self):
        self.d.engine.obs.spaces = [mock.Mock(index=w.index, active=w.active, ids=frozenset()) for w in self.d.ws.spaces]

    def fire_all(self):
        for fn, args in list(self.timers.values()):
            fn(*args)
        self.timers.clear()

    def test_a_box_click_focuses_its_terminal_and_pane(self):
        self.d._last_view = {"tabs": [{"space": 2, "claudes": self.boxes[2]}]}
        self.assertTrue(self.d.cmd_focus({"key": "s2"}))
        self.fire_all()  # (after the slide to workspace 2)
        self.assertEqual(self.d.ws.focused, ["terminal-B"])
        self.assertEqual(self.panes, [{"TMUX_PANE": "%4"}])
        self.assertEqual(self.d.cmd_focus({"key": "gone"}), False)

    def test_super_n_lands_on_the_claude_that_needs_you(self):
        self.d.ws.activate(self.d.ws.spaces[1])  # you pressed Super+2
        self.spaces()
        self.d._landed(1000.0)
        self.assertEqual(self.d.ws.focused, [])  # not during the slide
        self.fire_all()
        self.assertEqual(self.d.ws.focused, ["terminal-B"])

    def test_nothing_happens_where_no_claude_needs_you_or_when_we_switched(self):
        self.boxes[2][1]["calls"] = 0.0
        self.d.ws.activate(self.d.ws.spaces[1])
        self.spaces()
        self.d._landed(1000.0)
        self.boxes[2][1]["calls"] = 90.0
        self.d.ws.activate(self.d.ws.spaces[0])
        self.spaces()
        self.d._landed(1001.0)  # back on 1: nothing calls there
        self.d._switched_at = 1001.5
        self.d.ws.activate(self.d.ws.spaces[1])  # a switch of ours
        self.spaces()
        self.d._landed(1002.0)
        self.fire_all()
        self.assertEqual(self.d.ws.focused, [])

    def test_your_workspace_renumbered_is_not_a_switch(self):
        """Dragging its tab up, or closing one before it, changes its number, not where you are."""
        self.d.ws.activate(self.d.ws.spaces[1])
        self.d._active_space = "w-two"
        self.d.ws.spaces[1].index, self.d.ws.spaces[0].index = 1, 2  # workspace two is now first
        self.d.engine.owner = {1: "w-two", 2: "w-one"}
        self.boxes[1] = self.boxes.pop(2)
        self.spaces()
        self.d._landed(1000.0)
        self.fire_all()
        self.assertEqual(self.d.ws.focused, [])


@unittest.skipIf(daemon is None, "needs GLib")
class Panes(unittest.TestCase):
    """What brings one pane of a terminal window forward, where the terminal offers a way."""

    def test_each_terminal_its_own_way_and_terminator_none(self):
        from sidelit import panes
        with mock.patch.object(panes.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(panes.focus_commands({"TMUX_PANE": "%4", "TMUX": "/tmp/tmux-1000/default,77,0"})[0],
                             [["tmux", "-S", "/tmp/tmux-1000/default", "select-window", "-t", "%4"],
                              ["tmux", "-S", "/tmp/tmux-1000/default", "select-pane", "-t", "%4"]])
            self.assertEqual(panes.focus_commands({"WEZTERM_PANE": "3", "WEZTERM_UNIX_SOCKET": "/run/w"}),
                             ([["wezterm", "cli", "activate-pane", "--pane-id", "3"]], {"WEZTERM_UNIX_SOCKET": "/run/w"}))
            self.assertEqual(panes.focus_commands({"KITTY_WINDOW_ID": "2", "KITTY_LISTEN_ON": "unix:/tmp/k"})[0],
                             [["kitty", "@", "--to", "unix:/tmp/k", "focus-window", "--match", "id:2"]])
            self.assertEqual(panes.focus_commands({"KITTY_WINDOW_ID": "2"})[0], [])  # no remote control
            self.assertEqual(panes.focus_commands({"TERMINATOR_UUID": "urn:uuid:x"})[0], [])
            self.assertEqual(panes.focus_commands({"TMUX_PANE": "%4; rm -rf"})[0], [])
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(panes.terminator, "SOCKETS", Path(tmp)):
                self.assertFalse(panes.focus({"TERMINATOR_UUID": "urn:uuid:x"}))  # no Terminator with the plugin
        with mock.patch.object(panes.shutil, "which", return_value=None):
            self.assertFalse(panes.can_focus({"TMUX_PANE": "%4"}))  # not installed here

    def test_terminator_is_asked_over_its_plugins_socket(self):
        import socket
        from sidelit import panes
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(panes.terminator, "SOCKETS", Path(tmp)):
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
                listener.bind(str(Path(tmp) / f"{os.getpid()}.sock"))
                Path(tmp, "999999999.sock").touch()  # a Terminator that is gone
                self.assertTrue(panes.can_focus({"TERMINATOR_UUID": "urn:uuid:x"}))
                self.assertTrue(panes.focus({"TERMINATOR_UUID": "urn:uuid:x"}))
                self.assertEqual(json.loads(listener.recv(4096)), {"focus": "urn:uuid:x"})
                self.assertFalse(Path(tmp, "999999999.sock").exists())
            # tmux inside a Terminator pane: tmux's pane, not Terminator's
            with mock.patch.object(panes.shutil, "which", return_value="/usr/bin/x"):
                self.assertEqual(panes.terminator_uuid({"TMUX_PANE": "%4", "TERMINATOR_UUID": "urn:uuid:x"}), "")
            self.assertEqual(panes.terminator_uuid({"TERMINATOR_UUID": "not a uuid"}), "")


class TerminatorConfig(unittest.TestCase):
    """Sidelit's Terminator plugin in enabled_plugins, every other line as it was."""

    def test_enabled_and_taken_out(self):
        from sidelit import terminator as t
        mine = "[global_config]\n  tab_position = left\n  enabled_plugins = APTURLHandler\n[keybindings]\n"
        on = t.with_plugin(mine, True)
        self.assertEqual(on, "[global_config]\n  tab_position = left\n  enabled_plugins = APTURLHandler, SidelitTerminator\n[keybindings]\n")
        self.assertEqual(t.with_plugin(on, True), on)
        self.assertEqual(t.with_plugin(on, False), mine.replace("APTURLHandler", "APTURLHandler,"))
        self.assertEqual(t.enabled_plugins(on), ["APTURLHandler", "SidelitTerminator"])
        # one item ends in a comma, none is a lone comma: ConfigObj's lists, as Terminator reads them
        alone = t.with_plugin("[global_config]\n  enabled_plugins = ,\n", True)
        self.assertEqual(alone, "[global_config]\n  enabled_plugins = SidelitTerminator,\n")
        self.assertEqual(t.enabled_plugins(alone), ["SidelitTerminator"])
        self.assertEqual(t.with_plugin(alone, False), "[global_config]\n  enabled_plugins = ,\n")

    def test_terminators_defaults_kept_when_the_config_does_not_say(self):
        from sidelit import terminator as t
        on = t.with_plugin("[global_config]\n  handle_size = 6\n[profiles]\n  [[default]]\n", True)
        self.assertEqual(t.enabled_plugins(on), t.DEFAULT_PLUGINS + ["SidelitTerminator"])
        self.assertIn("  handle_size = 6\n", on)
        self.assertEqual(t.enabled_plugins(t.with_plugin("", True)), t.DEFAULT_PLUGINS + ["SidelitTerminator"])
        self.assertIsNone(t.enabled_plugins("[profiles]\n  enabled_plugins = X\n"))  # (another section's)
        self.assertEqual(t.with_plugin("[profiles]\n", False), "[profiles]\n")


if __name__ == "__main__":
    unittest.main()
