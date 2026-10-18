"""The engine on the sway backend against a fake sway (sim_backend.py): '+', a login, a crash, a
screen that comes back, a workspace renamed in sway itself. None of it touches a real sway."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sidelit import sway  # noqa: E402
from sidelit.core.model import Layout, Slot, Workspace  # noqa: E402
from fake_sway import FakeSway  # noqa: E402
from sim_backend import Sim  # noqa: E402


class EngineOnSway(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="sidelit-sway-engine-"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.home = self.run_dir / "home"
        for d in ("a", "b", "c"):
            (self.home / d).mkdir(parents=True)

    def sim(self, outputs=("DP-1",), layout=None, pid=None) -> Sim:
        self.fake = FakeSway(self.run_dir, outputs=outputs, pid=pid)
        self.addCleanup(self.fake.stop)
        client = sway.SwayClient(None, primary=True, env={"SWAYSOCK": self.fake.path, "XDG_RUNTIME_DIR": str(self.run_dir)},
                                 state_dir=self.run_dir / "state")
        self.addCleanup(client.close)

        def opened(spec):  # a terminal Sidelit starts opens on the workspace you are on, titled by its folder
            if spec.get("type") == "terminal" and self.fake.screens:  # (with no screen on, nowhere to be seen)
                self.fake.open_window("foot", f"alex@laptop: {spec['cwd']}")
        return Sim(client, layout, on_launch=opened)

    def title(self, folder) -> str:
        return f"alex@laptop: {self.home / folder}"

    def terminal(self, sid, folder) -> Slot:
        return Slot(id=sid, kind="terminal", app="foot", title=self.title(folder), state={"cwd": str(self.home / folder)})

    def window(self, title) -> dict:
        return next(w for w in self.fake.windows.values() if w["title"] == title)

    def test_plus_makes_the_next_number_and_opens_there(self):
        s = self.sim()
        self.fake.open_window("foot", self.title("a"))
        s.advance(15)
        lw = s.engine.create_workspace(s.t, cwd=str(self.home / "b"))  # ('+', as the daemon does it:)
        space = s.engine.primary[lw.id]
        s.act.activate(space.index)
        s.act.launch({"type": "terminal", "cwd": str(self.home / "b")})
        s.engine.expect_window(lw.id, s.t, lambda w: w.app == "foot")
        s.advance(10)
        self.assertEqual(self.window(self.title("b"))["ws"], "2")
        self.assertEqual(self.fake.shown["DP-1"], "2")
        self.assertEqual([lw.ids for lw in s.layout.workspaces], [["1"], ["2"]])

    def test_a_login_brings_kept_workspaces_back_under_their_numbers(self):
        layout = Layout(workspaces=[Workspace(id="wA", ids=["3"], slots=[self.terminal("sA", "a")]),
                                    Workspace(id="wB", ids=["name:mail"], slots=[self.terminal("sB", "b")])])
        s = self.sim(layout=layout)
        s.engine.start_restore(s.t, "login", delay=1)
        s.advance(40)
        self.assertEqual(self.window(self.title("a"))["ws"], "3")
        self.assertEqual(self.window(self.title("b"))["ws"], "mail")
        self.assertEqual(s.journal("gave_up"), [])

    def test_a_crash_comes_back_on_its_workspace_without_taking_you_there(self):
        s = self.sim()
        self.fake.open_window("foot", self.title("a"))  # (you, on 1)
        crashed = self.fake.open_window("foot", self.title("b"), ws="4", focus=False)
        s.advance(20)
        self.assertTrue(s.client.workspaces["4"].pinned)
        self.fake.close_window(crashed)  # (it never had the keyboard: not you closing it)
        s.advance(5)
        self.assertNotIn("4", self.fake.names())  # sway dropped it; Sidelit keeps it
        self.assertIn("4", s.client.workspaces)
        s.advance(90)
        self.assertEqual(self.window(self.title("b"))["ws"], "4")
        self.assertEqual(self.fake.shown["DP-1"], "1")

    def test_with_the_lid_closed_nothing_is_started_twice(self):
        s = self.sim()
        self.fake.open_window("foot", self.title("a"))
        self.fake.open_window("foot", self.title("b"), ws="4", focus=False)
        s.advance(20)
        self.fake.unplug("DP-1")  # (the only screen goes: sway lists no screen, no workspace, no window)
        s.advance(180)
        self.assertEqual(s.act.launched, [])
        self.fake.plug_back()
        s.advance(20)
        self.assertEqual(s.act.launched, [])
        self.assertEqual(len(s.engine.bind), 2)

    def test_a_daemon_started_with_the_lid_closed_starts_nothing(self):
        layout = Layout(workspaces=[Workspace(id="wA", ids=["4"], slots=[self.terminal("sA", "a")])])
        self.fake = None
        fake = FakeSway(self.run_dir, outputs=("DP-1",))
        self.addCleanup(fake.stop)
        a = fake.open_window("foot", self.title("a"), ws="4", focus=False)
        fake.unplug("DP-1")  # (the lid is closed when the daemon starts again)
        client = sway.SwayClient(None, primary=True, env={"SWAYSOCK": fake.path, "XDG_RUNTIME_DIR": str(self.run_dir)},
                                 state_dir=self.run_dir / "state")
        self.addCleanup(client.close)
        s = Sim(client, layout)
        s.advance(180)
        self.assertEqual(s.act.launched, [])
        fake.plug_back()
        s.advance(30)
        self.assertEqual(s.act.launched, [])
        self.assertEqual(list(s.engine.bind), [f"{client.token}-{a}"])

    def test_a_crash_with_the_lid_closed_waits_for_a_screen(self):
        s = self.sim()
        self.fake.open_window("foot", self.title("a"))
        crashed = self.fake.open_window("foot", self.title("b"), ws="4", focus=False)
        s.advance(20)
        self.fake.close_window(crashed)  # (it never had the keyboard: not you closing it)
        s.advance(1)
        self.fake.unplug("DP-1")  # (and the lid closes a moment later)
        s.advance(180)
        self.assertEqual(s.act.launched, [])
        self.fake.plug_back()
        s.advance(60)
        self.assertEqual(len(s.act.launched), 1)  # (once, now that it can be seen)

    def test_a_workspace_back_on_its_screen_stays_there(self):
        s = self.sim(outputs=("DP-1", "HDMI-A-1"))
        self.fake.open_window("foot", self.title("a"), focus=False)
        self.fake.open_window("foot", self.title("b"), ws="2", focus=False)  # (2 is on HDMI-A-1)
        s.advance(20)
        self.fake.unplug("HDMI-A-1")  # (sway takes 2 to DP-1)
        s.advance(60)
        self.fake.switch("2")
        self.fake.open_window("foot", self.title("c"), ws="2")  # one more window on 2, undocked
        s.advance(20)
        self.fake.plug_back()  # (sway takes 2 back to HDMI-A-1)
        s.advance(30)
        moves = len(s.journal("move"))
        s.advance(120)
        self.assertEqual(len(s.journal("move")), moves)
        self.assertEqual(self.fake.output_of("2"), "HDMI-A-1")

    def test_a_workspace_renamed_in_sway_keeps_its_place_in_the_layout(self):
        s = self.sim()
        self.fake.open_window("foot", self.title("a"), ws="4", focus=False)
        self.fake.open_window("foot", self.title("b"))
        s.advance(15)
        lw = next(lw for lw in s.layout.workspaces if lw.ids == ["4"])
        s.engine.rename(lw.id, "Mail")
        self.fake.rename("4", "4: mail")
        s.advance(30)
        self.assertEqual(len(s.layout.workspaces), 2)
        self.assertEqual(lw.name, "Mail")
        self.assertIn("4: mail", lw.ids)
        self.assertEqual([w.id for w in s.client.list()], ["1", "4: mail"])  # (no stand-in left for 4)


if __name__ == "__main__":
    unittest.main()
