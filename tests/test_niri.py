"""The niri backend: which session picks it, finding niri's socket, and the client against a fake
niri (fake_niri.py). None of it touches a real niri."""
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

from sidelit import backend, niri  # noqa: E402
from fake_niri import FakeNiri  # noqa: E402


class WhichBackend(unittest.TestCase):
    def test_the_session_names_it(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "niri"}), "niri")

    def test_started_by_hand_its_socket_variable_does(self):
        self.assertEqual(backend.chosen({"NIRI_SOCKET": "/run/user/1000/niri.wayland-1.42.sock"}), "niri")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "COSMIC", "NIRI_SOCKET": "/x"}), "cosmic")
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "Hyprland", "NIRI_SOCKET": "/x"}), "hyprland")

    def test_the_override(self):
        self.assertEqual(backend.chosen({"XDG_CURRENT_DESKTOP": "KDE", "SIDELIT_BACKEND": "niri"}), "niri")

    def test_open_client_gives_niris(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "niri"}), \
                mock.patch.object(niri, "NiriClient") as made:
            backend.open_client(primary=False)
        made.assert_called_once_with(None, primary=False)


class FindingTheSocket(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-niri-run-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)

    def sock(self, display, pid=None, age=0):
        p = self.run_dir / f"niri.{display}.{pid or os.getpid()}.sock"
        p.touch()
        if age:
            os.utime(p, (time.time() - age, time.time() - age))
        return str(p)

    def test_niri_socket(self):
        p = self.sock("wayland-1")
        self.assertEqual(niri.socket_path({"NIRI_SOCKET": p, "XDG_RUNTIME_DIR": str(self.run_dir)}), p)

    def test_without_it_the_niri_on_our_display(self):
        mine = self.sock("wayland-1", age=60)
        self.sock("wayland-2")
        self.sock("wayland-3", pid=2 ** 22 + 12345)  # (beyond pid_max: a niri that has gone)
        env = {"XDG_RUNTIME_DIR": str(self.run_dir), "WAYLAND_DISPLAY": "wayland-1"}
        self.assertEqual(niri.socket_path(env), mine)
        with mock.patch("sys.stderr"):  # (one from before niri restarted: the one running now)
            self.assertEqual(niri.socket_path({**env, "NIRI_SOCKET": str(self.run_dir / "gone.sock")}), mine)

    def test_none(self):
        with self.assertRaises(RuntimeError):
            niri.socket_path({"XDG_RUNTIME_DIR": str(self.run_dir)})


class AgainstFakeNiri(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-niri-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.fake = FakeNiri(self.run_dir, outputs=("DP-1", "HDMI-A-1"))
        self.addCleanup(self.fake.stop)
        self.env = {"NIRI_SOCKET": self.fake.path, "XDG_RUNTIME_DIR": str(self.run_dir)}
        self.clients = []
        self.changes = 0

    def tearDown(self):
        for c in self.clients:
            c.close()

    def client(self, primary=True):
        def changed():
            self.changes += 1
        c = niri.NiriClient(changed, primary=primary, env=self.env)
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

    def actions(self, since=0):
        return [r["Action"] for r in self.fake.requests[since:] if isinstance(r, dict) and "Action" in r]

    @staticmethod
    def key(c, wid):
        """A window's identifier: this niri's token and its number."""
        return f"{c.token}-{wid}"

    # ---- reading ----
    def test_workspaces_screen_by_screen(self):
        a = self.fake.open_window("kitty", "one", focus=False)  # on DP-1's first (it gets an empty one after)
        c = self.client()
        listed = [(w.index, w.monitor, w.idx, w.id) for w in c.list()]
        self.assertEqual(listed, [(1, "DP-1", 1, ""), (2, "DP-1", 2, ""), (3, "HDMI-A-1", 1, "")])
        self.assertEqual(c.active().monitor, "DP-1")
        self.assertFalse(c.list()[0].pinned)  # (no name: no id, as an unpinned one on COSMIC)
        tl = c.toplevels[self.key(c, a)]
        self.assertEqual((tl.app_id, tl.title, tl.pid, tl.output), ("kitty", "one", 4242, "DP-1"))
        self.assertIs(c.workspace_of(tl), c.list()[0])
        self.assertEqual(c.copy_ids_of(tl), frozenset())
        self.assertEqual(c.output_names(), {"DP-1", "HDMI-A-1"})

    def test_the_chips_are_for_the_screen_you_are_on(self):
        self.fake.open_window("foot", "one")  # (DP-1 gets an empty one after)
        c = self.client()
        self.assertEqual([c.shortcut_label(w, 3) for w in c.list()], ["⊞ 1", "⊞ 2", ""])

    def test_changes_come_on_the_event_stream(self):
        c = self.client()
        before = len(self.fake.requests)
        a = self.fake.open_window("foot", "⠋ Claude Code")
        self.assertTrue(self.pump(c, lambda: self.key(c, a) in c.toplevels))
        tl = c.toplevels[self.key(c, a)]
        self.assertTrue(tl.focused and tl.focused_at > 0)
        self.fake.set_title(a, "✳ Claude Code")
        self.assertTrue(self.pump(c, lambda: tl.title == "✳ Claude Code"))
        self.assertEqual(self.fake.requests[before:], [])  # nothing asked: it all came on the stream
        self.fake.close_window(a)
        self.assertTrue(self.pump(c, lambda: tl.closed))
        self.assertIn(tl.identifier, c.closed_focus)
        self.assertGreater(self.changes, 0)

    def test_a_dropped_event_stream_is_opened_again(self):
        c = self.client()
        self.fake.drop_streams()  # (as niri drops one that reads too slowly)
        with mock.patch("sys.stderr"):
            self.pump(c, secs=0.3)
        a = self.fake.open_window("foot", "after")
        self.assertTrue(self.pump(c, lambda: self.key(c, a) in c.toplevels))

    def test_niri_quitting(self):
        c = self.client()
        self.fake.stop()
        with self.assertRaises(ConnectionError), mock.patch("sys.stderr"):
            self.pump(c, until=lambda: False, secs=1.0)

    # ---- acting ----
    def test_focus_close_and_move(self):
        a = self.fake.open_window("foot", "one")
        b = self.fake.open_window("foot", "two")
        c = self.client()
        target = c.list()[1]  # (DP-1's empty one at the end)
        c.focus(c.toplevels[self.key(c, a)])
        self.assertEqual(self.fake.focused_window, a)
        self.assertTrue(c.move_toplevel(c.toplevels[self.key(c, a)], target))
        self.assertEqual(self.fake.where(a), target.proxy)
        c.close_toplevel(c.toplevels[self.key(c, b)])
        self.assertNotIn(b, self.fake.windows)
        self.assertEqual(self.actions(), [
            {"FocusWindow": {"id": a}},
            {"MoveWindowToWorkspace": {"window_id": a, "reference": {"Id": target.proxy}, "focus": False}},
            {"CloseWindow": {"id": b}}])

    def test_moving_to_a_screen_takes_the_workspace_there(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        self.assertTrue(c.move_toplevel(c.toplevels[self.key(c, a)], ws, "HDMI-A-1"))
        self.assertEqual(self.fake.output_of(ws.proxy), "HDMI-A-1")

    def test_activate_skips_the_one_you_are_on(self):
        c = self.client()
        before = len(self.fake.requests)
        c.activate(c.active())
        self.assertEqual(self.actions(before), [])
        other = c.list()[-1]
        c.activate(other)
        self.assertEqual(self.fake.focused_output, "HDMI-A-1")

    # ---- keeping workspaces ----
    def test_create_names_the_empty_one_where_you_are(self):
        self.fake.open_window("foot", "one")
        c = self.client()
        made = c.create("Workspace")
        self.assertEqual((made.id, made.monitor, made.pinned), ("name:sidelit-1", "DP-1", True))
        self.assertIn("sidelit-1", self.fake.names())
        self.assertTrue(self.pump(c, lambda: len([w for w in c.list() if w.monitor == "DP-1"]) == 3))  # (and niri adds one)
        self.assertEqual(c.create().id, "name:sidelit-2")

    def test_create_brings_back_the_name_it_had(self):
        c = self.client()
        c.preferred_ids = {"name:sidelit-4", "cb8061aa"}
        self.assertEqual(c.create(want=("cb8061aa", "name:sidelit-4")).id, "name:sidelit-4")  # (not another desktop's id)
        self.assertEqual(c.create(want=("name:sidelit-4",)).id, "name:sidelit-5")  # (in use: the next one)
        self.assertEqual(c.create(want=("name:chat", "3")).id, "name:chat")  # (yours too; not Hyprland's number)
        self.assertEqual(c.create(want=("name:Chat",)).id, "name:sidelit-6")  # (niri's names go in any case)
        # renamed in niri: the name it had last (the layout learns them in turn)
        self.assertEqual(c.create(want=("name:sidelit-7", "name:mine")).id, "name:mine")

    def test_pinning_and_unpinning(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        c.pin(ws, True)
        self.assertTrue(ws.pinned and ws.copies_pinned)
        self.assertEqual(ws.id, "name:sidelit-1")
        self.fake.close_window(a)
        self.fake.switch(self.fake.screens["DP-1"][-1]["id"])  # (leaving it, for DP-1's empty one)
        self.pump(c)
        self.assertIsNotNone(self.fake.find(ws.proxy))  # empty, left, and kept by its name
        c.remove(ws)
        self.assertIsNone(self.fake.find(ws.proxy))  # without it, niri drops it
        self.pump(c, lambda: ws.removed)
        self.assertTrue(ws.removed)

    def test_a_workspace_renamed_in_niri_is_still_known(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        c.pin(ws, True)
        self.fake.name(ws.proxy, "foo")  # (niri msg action set-workspace-name foo)
        self.assertTrue(self.pump(c, lambda: ws.id == "name:foo"))
        self.assertEqual(ws.all_ids, {"name:foo", "name:sidelit-1"})
        self.assertEqual(c.copy_ids_of(c.toplevels[self.key(c, a)]), {"name:foo", "name:sidelit-1"})
        other = self.fake.active["HDMI-A-1"]
        self.fake.name(other, "sidelit-1")  # another one has that name now: no more this one's
        self.assertTrue(self.pump(c, lambda: ws.all_ids == {"name:foo"}))

    def test_a_workspace_unnamed_in_niri_gets_its_name_back(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        c.pin(c.create(), True)  # (sidelit-1, elsewhere)
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        c.pin(ws, True)
        self.assertEqual(ws.id, "name:sidelit-2")
        self.fake._act({"UnsetWorkspaceName": {"reference": {"Id": ws.proxy}}})  # (niri msg action unset-workspace-name)
        self.assertTrue(self.pump(c, lambda: not ws.name))
        self.assertEqual((ws.id, ws.ids, ws.pinned), ("", {"name:sidelit-2"}, False))  # (still known)
        c.pin(ws, True)  # (the daemon keeps it again: under the name it had)
        self.assertEqual(ws.id, "name:sidelit-2")

    def test_a_workspace_sidelit_lets_go_is_known_by_no_name(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        c.pin(ws, True)
        c.pin(ws, False)
        self.assertTrue(self.pump(c, lambda: not ws.name))
        self.assertEqual(ws.all_ids, frozenset())
        c.pin(ws, True)  # (and a new name next time, as for any other)
        self.assertEqual(ws.id, "name:sidelit-1")

    def test_your_own_names_stay(self):
        a = self.fake.open_window("foot", "chat")
        self.fake.name(self.fake.where(a), "chat")
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        self.assertEqual((ws.id, ws.pinned), ("name:chat", True))
        before = len(self.fake.requests)
        c.pin(ws, False)
        self.assertEqual(self.actions(before), [])
        self.assertIn("chat", self.fake.names())

    def test_a_name_in_use_is_not_taken(self):
        self.fake.name(self.fake.active["HDMI-A-1"], "Sidelit-1")  # (niri compares names in any case)
        c = self.client()
        self.assertEqual(c.create().id, "name:sidelit-2")

    def test_window_ids_are_this_niris(self):
        a = self.fake.open_window("foot", "one")
        c = self.client()
        self.assertTrue(c.toplevels[self.key(c, a)].identifier.startswith(c.token + "-"))
        # another niri (another pid, or the same pid started again) numbers its windows from 1
        # too: its token tells them apart
        run = Path(tempfile.mkdtemp(prefix="sidelit-niri-again-"))
        self.addCleanup(shutil.rmtree, run, True)
        again = FakeNiri(run, pid=os.getppid())
        self.addCleanup(again.stop)
        b = again.open_window("foot", "one")
        other = niri.NiriClient(None, primary=False, env={"NIRI_SOCKET": again.path, "XDG_RUNTIME_DIR": str(run)})
        self.clients.append(other)
        self.assertEqual(a, b)
        self.assertNotEqual(self.key(other, b), self.key(c, a))

    def test_a_dropped_stream_counts_as_a_new_connection(self):
        c = self.client()
        self.assertEqual(c.connections, 1)
        self.fake.drop_streams()
        with mock.patch("sys.stderr"):
            self.pump(c, lambda: c.connections == 2)
        self.assertEqual(c.connections, 2)

    def test_a_move_to_another_screen_niri_refuses_goes_no_further(self):
        for title in ("a", "b"):
            self.fake.open_window("foot", title, focus=False)
            self.fake.switch(self.fake.screens["DP-1"][-1]["id"])
        c = self.client()
        first = c.list()[0]
        hdmi = next(w for w in c.list() if w.monitor == "HDMI-A-1")
        before = len(self.fake.requests)
        real = self.fake._act

        def refuse(action):  # (as niri does with the empty workspace at a screen's end, say)
            return {"Ok": "Handled"} if "MoveWorkspaceToMonitor" in action else real(action)
        with mock.patch.object(self.fake, "_act", side_effect=refuse), mock.patch.object(niri, "SYNC_S", 0.1):
            c.move(first, hdmi, before=True)
        self.assertEqual([list(a) for a in self.actions(before)], [["MoveWorkspaceToMonitor"]])

    def test_reordering(self):
        for title in ("a", "b", "c"):
            self.fake.open_window("foot", title, focus=False)
            self.fake.switch(self.fake.screens["DP-1"][-1]["id"])
        c = self.client()
        dp = [w for w in c.list() if w.monitor == "DP-1"]
        first, second, third = dp[0], dp[1], dp[2]
        c.move(third, first, before=True)
        self.assertEqual([ws["id"] for ws in self.fake.screens["DP-1"][:3]], [third.proxy, first.proxy, second.proxy])
        c.move(third, second, before=False)
        self.assertEqual([ws["id"] for ws in self.fake.screens["DP-1"][:3]], [first.proxy, second.proxy, third.proxy])
        hdmi = next(w for w in c.list() if w.monitor == "HDMI-A-1")
        c.move(first, hdmi, before=True)  # onto the other screen, before its one
        self.assertEqual(self.fake.screens["HDMI-A-1"][0]["id"], first.proxy)

    def test_a_screen_that_goes(self):
        a = self.fake.open_window("foot", "big", ws=self.fake.active["HDMI-A-1"], focus=False)
        c = self.client()
        ws = c.workspace_of(c.toplevels[self.key(c, a)])
        c.pin(ws, True)
        self.fake.unplug("HDMI-A-1")
        self.assertTrue(self.pump(c, lambda: c.output_names() == {"DP-1"} and ws.monitor == "DP-1"))
        self.assertTrue(ws.pinned)
        self.fake.plug_back()
        self.assertTrue(self.pump(c, lambda: ws.monitor == "HDMI-A-1"))
        self.assertEqual([s.name for s in c.screens], ["DP-1", "HDMI-A-1"])

    def test_one_shot_commands_have_no_event_socket(self):
        c = self.client(primary=False)
        self.assertEqual(c.fd(), -1)
        a = self.fake.open_window("foot", "new")
        start = time.monotonic()
        c.wait(0.5)
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertIn(self.key(c, a), c.toplevels)
        made = c.create()  # (waits by asking again)
        self.assertEqual(made.id, "name:sidelit-1")


if __name__ == "__main__":
    unittest.main()
