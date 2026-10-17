"""layout.json on disk, and the move from registry.json."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit.core.model import Layout, Slot, Workspace  # noqa: E402
from sidelit.core.store import Store, migrate_registry  # noqa: E402

REGISTRY = {
    "claudes": [
        {"name": "Workspace 1", "ws_id": "cb8061", "cwd": "/home/alex", "terminal_pids": [1],
         "custom_name": "Home", "last_session": "closed-long-ago", "last_title": "Plan the weekly release",
         "output": "HDMI-A-1",
         "windows": [{"app": "discord", "title": "general - Discord", "output": "HDMI-A-1", "id": "KOM0"},
                     {"app": "chrome-discord.com__app-Profile_1", "title": "Discord", "id": "X1"}]},
        {"name": "Workspace 2", "ws_id": "1708ac2", "cwd": "/home/alex/webshop", "last_session": "running-now",
         "last_title": "New Pricing Page", "output": "HDMI-A-1",
         "windows": [{"app": "google-chrome", "title": "Example - Google Chrome", "output": "eDP-1", "id": "iEY"}]},
    ],
    "active_ws": "1708ac2",
}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "layout.json"

    def tearDown(self):
        self.dir.cleanup()

    def sample(self):
        return Layout(workspaces=[Workspace(id="w1", ids=["a"], name="Home",
                                            slots=[Slot(id="s1", kind="terminal", app="terminator",
                                                        state={"session": "x", "cwd": "/tmp"})])], active="w1")

    def test_round_trip(self):
        store = Store(self.path)
        store.save(self.sample())
        back = store.load()
        self.assertEqual(back.to_json(), self.sample().to_json())

    def test_broken_file_falls_back_to_backup_and_is_set_aside(self):
        store = Store(self.path)
        store.save(self.sample())
        store.rotate()
        self.path.write_text("{not json")
        back = Store(self.path).load()
        self.assertEqual(back.workspaces[0].name, "Home")
        self.assertTrue(any(p.name.startswith("layout.json.broken-") for p in self.path.parent.iterdir()))

    def test_a_burst_of_restarts_keeps_the_older_backups(self):
        """(deploys, or a crash loop: every start used to push one more good copy out)"""
        store = Store(self.path)
        store.save(self.sample())
        first = self.path.read_text()
        self.assertTrue(Store(self.path).rotate_on_start())
        for name in ("a", "b", "c", "d", "e", "f"):  # restart after restart, the layout changing
            layout = self.sample()
            layout.workspaces[0].name = name
            again = Store(self.path)
            again.save(layout)
            self.assertFalse(again.rotate_on_start())
        self.assertEqual(self.path.with_name("layout.json.1").read_text(), first)
        self.assertFalse(self.path.with_name("layout.json.2").exists())

    def test_backups_rotate_hourly_by_their_age_on_disk(self):
        store = Store(self.path)
        store.save(self.sample())
        store.rotate_on_start()
        hour_ago = time.time() - 3700
        os.utime(self.path.with_name("layout.json.1"), (hour_ago, hour_ago))
        layout = self.sample()
        layout.workspaces[0].name = "Later"
        Store(self.path).save(layout)  # (a new daemon: no clock of its own to go by)
        self.assertTrue(self.path.with_name("layout.json.2").exists())

    def test_duplicate_ids_are_rejected(self):
        data = self.sample().to_json()
        data["workspaces"].append(dict(data["workspaces"][0]))
        self.path.write_text(json.dumps(data))
        self.assertIsNone(Store(self.path).load())

    def test_newer_schema_is_not_overwritten(self):
        data = self.sample().to_json()
        data["version"] = 99
        self.path.write_text(json.dumps(data))
        self.assertIsNone(Store(self.path).load())


class MigrationTest(unittest.TestCase):
    def test_mid_session_drops_closed_claudes_to_recent(self):
        lay = migrate_registry(REGISTRY, live_sessions={"running-now"}, fresh=False, now=1.0)
        home, shop = lay.workspaces
        self.assertEqual(home.name, "Home")
        self.assertEqual(home.ids, ["cb8061"])
        self.assertIsNone(home.claude)  # the phantom: not resumed at the next login
        self.assertEqual(lay.recent[0]["session"], "closed-long-ago")
        self.assertEqual(shop.claude.session, "running-now")
        self.assertEqual(lay.active, shop.id)
        kinds = {s.app: s.kind for s in home.slots}
        self.assertEqual(kinds, {"discord": "discord", "chrome-discord.com__app-Profile_1": "webapp"})
        webapp = next(s for s in home.slots if s.kind == "webapp")
        self.assertEqual(webapp.state["url"], "https://discord.com/app")
        self.assertEqual(next(s for s in home.slots if s.kind == "discord").window, "KOM0")

    def test_right_after_login_keeps_every_claude(self):
        lay = migrate_registry(REGISTRY, live_sessions=set(), fresh=True, now=1.0)
        self.assertEqual([w.claude.session for w in lay.workspaces], ["closed-long-ago", "running-now"])


if __name__ == "__main__":
    unittest.main()
