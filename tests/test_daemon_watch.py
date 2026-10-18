"""The daemon's watch on its compositor connection: a backend that makes its event connection
again (niri drops a reader that falls behind, and so does sway) is followed, not taken for a
compositor that went. Against a fake niri; needs GLib (skipped without it)."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

try:
    from sidelit import daemon
    from gi.repository import GLib
except (ImportError, ValueError):  # no GLib bindings
    daemon = None

from sidelit import niri  # noqa: E402
from fake_niri import FakeNiri  # noqa: E402


@unittest.skipIf(daemon is None, "needs GLib")
class Watch(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-watch-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.fake = FakeNiri(self.run_dir)
        self.addCleanup(self.fake.stop)
        self.client = niri.NiriClient(None, primary=True, env={"NIRI_SOCKET": self.fake.path,
                                                               "XDG_RUNTIME_DIR": str(self.run_dir)})
        self.addCleanup(self.client.close)

    def daemon(self):
        """The daemon's watch, on its own: the methods, on a stand-in for the rest of it."""
        d = types.SimpleNamespace(ws=self.client, _watching=None, exit_code=0, stopped=False)
        d.shutdown = lambda: setattr(d, "stopped", True)
        d._watch = types.MethodType(daemon.Daemon._watch, d)
        d._on_wayland = types.MethodType(daemon.Daemon._on_wayland, d)
        d._watch()

        def unwatch():
            source = GLib.main_context_default().find_source_by_id(d._watching[2]) if d._watching[2] else None
            if source is not None:
                source.destroy()
        self.addCleanup(unwatch)
        return d

    def run_loop(self, secs, *at):
        """The main loop for `secs` seconds; `at`: (seconds, what to do then)."""
        loop = GLib.MainLoop()
        for when, what in at:
            GLib.timeout_add(int(when * 1000), lambda what=what: (what(), False)[1])
        GLib.timeout_add(int(secs * 1000), lambda: (loop.quit(), False)[1])
        loop.run()

    def test_a_dropped_stream_is_followed(self):
        d = self.daemon()
        opened = []
        with mock.patch("sys.stderr"):
            self.run_loop(1.5, (0.1, self.fake.drop_streams),
                          (0.6, lambda: opened.append(self.fake.open_window("foot", "after"))))
        self.assertFalse(d.stopped)
        self.assertEqual(self.client.connections, 2)
        self.assertEqual(d._watching[:2], (self.client.fd(), 2))
        self.assertIn(f"{self.client.token}-{opened[0]}", self.client.toplevels)  # (heard on the new one)

    def test_the_same_connection_keeps_its_watch(self):
        d = self.daemon()
        first = d._watching
        self.assertFalse(d._watch())
        self.assertEqual(d._watching, first)

    def test_a_connection_lost_inside_an_action_stops_the_daemon(self):
        d = self.daemon()
        self.fake.stop()  # (niri goes while the daemon acts: the client cannot open its stream again)
        with mock.patch("sys.stderr"), self.assertRaises(ConnectionError):
            self.client.pump()
        with mock.patch.object(daemon, "say"):
            d._watch()  # (what the step does next)
        self.assertTrue(d.stopped)
        self.assertEqual(d.exit_code, 1)

    def test_a_compositor_that_went_stops_the_daemon(self):
        d = self.daemon()
        with mock.patch("sys.stderr"), mock.patch.object(daemon, "say"):
            self.run_loop(1.0, (0.1, self.fake.stop))
        self.assertTrue(d.stopped)
        self.assertEqual(d.exit_code, 1)


@unittest.skipIf(daemon is None, "needs GLib")
class Reorder(unittest.TestCase):
    """Putting workspaces back in order never moves one to another screen, where each is on one."""

    def test_only_on_one_screen(self):
        spaces = [types.SimpleNamespace(index=i, coords=(i,), monitor=m) for i, m in ((1, "DP-1"), (2, "DP-1"), (3, "HDMI-A-1"))]
        moved = []
        ws = types.SimpleNamespace(caps={"move"}, list=lambda: spaces, move=lambda a, b, before: moved.append((a.index, b.index)))
        d = types.SimpleNamespace(ws=ws, _dry=lambda what: False)
        d._space = types.MethodType(daemon.Daemon._space, d)
        self.assertFalse(daemon.Daemon.move_space(d, 3, 1))
        self.assertTrue(daemon.Daemon.move_space(d, 2, 1))
        self.assertEqual(moved, [(2, 1)])


if __name__ == "__main__":
    unittest.main()
