"""The engine on the niri backend against a fake niri (sim_backend.py): a login, a screen that
comes back, a workspace renamed in niri itself. None of it touches a real niri."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sidelit import niri  # noqa: E402
from sidelit.core.model import Layout, Slot, Workspace  # noqa: E402
from fake_niri import FakeNiri  # noqa: E402
from sim_backend import Sim  # noqa: E402


class EngineOnNiri(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-niri-engine-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.home = self.run_dir / "home"
        for d in ("a", "b", "c"):
            (self.home / d).mkdir(parents=True)

    def sim(self, outputs=("DP-1",), layout=None, open_launched=True, pid=None) -> Sim:
        self.fake = FakeNiri(self.run_dir, outputs=outputs, pid=pid)
        self.addCleanup(self.fake.stop)
        client = niri.NiriClient(None, primary=True, env={"NIRI_SOCKET": self.fake.path, "XDG_RUNTIME_DIR": str(self.run_dir)})
        self.addCleanup(client.close)

        def opened(spec):  # a terminal Sidelit starts opens on the workspace you are on, titled by its folder
            if open_launched and spec.get("type") == "terminal":
                self.fake.open_window("foot", f"alex@laptop: {spec['cwd']}")
        return Sim(client, layout, on_launch=opened)

    def terminal(self, sid, folder, **kw) -> Slot:
        return Slot(id=sid, kind="terminal", app="foot", title=f"alex@laptop: {self.home / folder}",
                    state={"cwd": str(self.home / folder)}, **kw)

    def test_a_login_does_not_take_new_windows_for_old_ones(self):
        before = self.sim()
        self.fake.open_window("foot", f"alex@laptop: {self.home / 'a'}")  # (that niri's window 1)
        before.advance(15)
        slot = before.layout.workspaces[0].slots[0]
        self.assertTrue(slot.window)
        self.fake.stop()
        # the next login: another niri, which numbers its windows from 1 again
        s = self.sim(layout=before.layout, pid=os.getppid())
        self.fake.open_window("foot", "alex@laptop: ~")  # (its window 1: a plain shell)
        s.engine.start_restore(s.t, "login", delay=1)
        s.advance(30)
        self.assertEqual([f for f in s.journal("bound") if f["why"] == "same window as before"], [])
        self.assertIn(str(self.home / "a"), [spec.get("cwd") for spec in s.act.launched])  # (it is started again)

    def test_a_workspace_back_on_its_screen_stays_there(self):
        s = self.sim(outputs=("DP-1", "HDMI-A-1"))
        self.fake.open_window("foot", f"alex@laptop: {self.home / 'a'}", focus=False)
        b1 = self.fake.open_window("foot", f"alex@laptop: {self.home / 'b'}", ws=self.fake.active["HDMI-A-1"], focus=False)
        s.advance(20)
        b = self.fake.where(b1)
        self.fake.unplug("HDMI-A-1")  # (niri takes B to DP-1)
        s.advance(60)
        self.fake.switch(b)
        self.fake.open_window("foot", f"alex@laptop: {self.home / 'c'}", ws=b)  # one more window on B, undocked
        s.advance(20)
        self.fake.plug_back()  # (niri takes B back to HDMI-A-1)
        s.advance(30)
        moves = len(s.journal("move"))
        s.advance(120)
        self.assertEqual(len(s.journal("move")), moves)  # it stays: no window pulls it to its own screen
        self.assertEqual(self.fake.output_of(b), "HDMI-A-1")
        self.assertEqual(s.journal("gave_up"), [])

    def test_a_workspace_renamed_in_niri_keeps_its_place_in_the_layout(self):
        s = self.sim()
        a = self.fake.open_window("foot", f"alex@laptop: {self.home / 'a'}")
        s.advance(15)
        self.assertEqual(len(self.layout_ws(s)), 1)
        lw = s.layout.workspaces[0]
        s.engine.rename(lw.id, "My project")
        self.fake.name(self.fake.where(a), "foo")  # niri msg action set-workspace-name foo
        s.advance(30)
        self.assertEqual(self.layout_ws(s), [(lw.id, "My project", ["alex@laptop: " + str(self.home / "a")])])
        self.assertIn("name:foo", lw.ids)
        self.assertEqual(self.fake.names()[:1], ["foo"])

    def test_a_name_you_gave_comes_back_after_a_login(self):
        layout = Layout(workspaces=[Workspace(id="wC", ids=["name:chat"], slots=[self.terminal("sC", "c")])])
        s = self.sim(layout=layout)
        s.engine.start_restore(s.t, "login", delay=1)
        s.advance(30)
        found = self.fake.by_name("chat")
        self.assertIsNotNone(found)
        self.assertEqual([w["title"] for w in self.fake.windows_on(found[1]["id"])], [f"alex@laptop: {self.home / 'c'}"])

    def test_a_workspace_renamed_in_niri_comes_back_under_that_name_login_after_login(self):
        # (renamed from sidelit-1 to foo: the layout learnt foo after it)
        layout = Layout(workspaces=[Workspace(id="wC", ids=["name:sidelit-1", "name:foo"], slots=[self.terminal("sC", "c")])])
        pid = None
        for login in (1, 2, 3):
            s = self.sim(layout=layout, pid=pid)
            s.engine.start_restore(s.t, "login", delay=1)
            s.advance(30)
            self.assertIsNotNone(self.fake.by_name("foo"), f"login {login}: {self.fake.names()}")
            self.fake.stop()
            layout, pid = s.layout, (os.getppid() if pid is None else None)

    def test_a_workspace_you_move_to_another_screen_stays_there(self):
        # it has windows that are not open (an app that is not running): they do not pull it back
        s = self.sim(outputs=("DP-1", "HDMI-A-1"))
        a = self.fake.open_window("foot", f"alex@laptop: {self.home / 'a'}")
        s.advance(20)
        lw = s.layout.workspaces[0]
        for n in (1, 2):
            lw.slots.append(Slot(id=f"sx{n}", kind="generic", app=f"app{n}", title=f"App {n}", output="DP-1"))
        ws = self.fake.where(a)
        self.fake._act({"MoveWorkspaceToMonitor": {"output": "HDMI-A-1", "reference": {"Id": ws}}})  # (you, Mod+Shift+Right)
        s.advance(60)
        self.assertEqual(self.fake.output_of(ws), "HDMI-A-1")
        self.assertEqual(s.journal("move"), [])

    @staticmethod
    def layout_ws(s):
        return [(lw.id, lw.name, [x.title for x in lw.slots]) for lw in s.layout.workspaces]


if __name__ == "__main__":
    unittest.main()
