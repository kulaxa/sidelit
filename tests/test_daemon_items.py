"""The daemon's side of an item's workspace: which directory it gets. Needs GLib (skipped without it)."""
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
        d._tl = {"w1": "terminal-1", "w2": "terminal-2", "w3": "terminal-3"}
        d.engine = mock.Mock()
        d.engine.obs.windows_on = lambda index: [index]
        d._claude_on = lambda obs, wins: {"window": f"w{wins[0]}"}
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


if __name__ == "__main__":
    unittest.main()
