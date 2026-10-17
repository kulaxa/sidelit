"""The history panel: closed workspaces by day, searchable. Needs GTK (skipped without it, as on CI)."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from sidelit import sidebar
except (ImportError, ValueError):  # no GTK, no layer shell
    sidebar = None


@unittest.skipIf(sidebar is None, "needs GTK 3 and gtk-layer-shell")
class HistoryPanelTest(unittest.TestCase):
    def entry(self, i, title, days_ago=0, what=(), cwd="/repo"):
        closed = (datetime.now() - timedelta(days=days_ago)).timestamp() - i
        return {"id": f"h{i}", "title": title, "closed": closed, "cwd": cwd, "what": list(what)}

    def setUp(self):
        self.picked = []
        self.panel = sidebar.HistoryPanel(self.picked.append, 400)
        self.panel.set_items([
            self.entry(1, "Fix tests", what=["Claude 'Fix tests'", "chrome 'CI' +3 tabs"]),
            self.entry(2, "Write docs", days_ago=1, cwd="/home/docs"),
            self.entry(3, "Pricing page", days_ago=9, what=["Claude 'Pricing page'"], cwd="/webshop"),
        ])

    def shown(self):
        return [r.entry["title"] for r in self.panel.list.get_children() if self.panel._visible(r)]

    def test_days(self):
        today = date(2026, 9, 30)
        at = lambda d: datetime(d.year, d.month, d.day, 12).timestamp()  # noqa: E731
        self.assertEqual(sidebar.day_label(at(today), today), "Today")
        self.assertEqual(sidebar.day_label(at(today - timedelta(days=1)), today), "Yesterday")
        self.assertEqual(sidebar.day_label(at(today - timedelta(days=2)), today), "Monday")
        self.assertEqual(sidebar.day_label(at(today - timedelta(days=8)), today), "Tue 22 Sep")

    def test_every_entry_is_there_newest_first(self):
        self.assertEqual(self.shown(), ["Fix tests", "Write docs", "Pricing page"])
        self.assertEqual([r.day for r in self.panel.list.get_children()][:2], ["Today", "Yesterday"])

    def test_search_matches_titles_windows_and_folders(self):
        for text, want in (("docs", ["Write docs"]), ("chrome", ["Fix tests"]), ("webshop", ["Pricing page"]),
                           ("fix CI", ["Fix tests"]), ("nothing like it", [])):
            self.panel.search.set_text(text)
            self.panel._refilter()
            self.assertEqual(self.shown(), want, text)

    def test_enter_brings_back_the_first_match(self):
        self.panel.search.set_text("pricing")
        self.panel._refilter()
        self.panel._pick_first()
        self.assertEqual(self.picked, ["h3"])

    def test_the_second_line_does_not_repeat_the_title(self):
        row = self.panel.list.get_children()[0]
        sub = row.get_child().get_children()[1].get_text()
        self.assertIn("repo", sub)
        self.assertIn("chrome", sub)
        self.assertNotIn("Claude", sub)


@unittest.skipIf(sidebar is None, "needs GTK 3 and gtk-layer-shell")
class TabInfoTest(unittest.TestCase):
    def sub(self, **claude):
        return sidebar.tab_info({"space": 1, "claude": {"title": "Fix tests", "model": "Opus 5.5", **claude}})["sub"]

    def test_subagents_are_counted_under_the_main_threads_model(self):
        self.assertEqual(self.sub(state="busy"), "Opus 5.5")
        self.assertEqual(self.sub(state="busy", agents=1), "Opus 5.5 · 1 subagent")
        self.assertEqual(self.sub(state="waiting", agents=3), "Opus 5.5 · 3 subagents · waiting for you")

    ITEM = {"id": "j1", "title": "Port the login screen", "state": "waiting", "detail": "Allow",
            "url": "https://jobs.example.test/job/j1", "updated": 0}

    def test_a_workspace_opened_for_an_item_is_drawn_from_it(self):
        tab = {"space": 2, "ws": "w1", "name": "Port the login", "claude": None, "item": {"source": "jobs", "id": "j1"},
               "windows": [{"app": "chrome-jobs.example.test__job_j1-Default"}]}
        info = sidebar.tab_info(tab, self.ITEM)
        self.assertEqual((info["title"], info["sub"], info["cls"]), ("Port the login screen", "waiting for you · Allow", "waiting"))
        self.assertTrue(info["has_claude"])  # the coloured stripe
        self.assertTrue(info["item"])
        for state, cls in (("working", "working"), ("failed", "failed"), ("idle", "idle"), ("done", "idle")):
            self.assertEqual(sidebar.tab_info(tab, {**self.ITEM, "state": state})["cls"], cls)
        # listed in its source's group only while it has no workspace of its own
        src = {"key": "jobs", "items": [self.ITEM, {**self.ITEM, "id": "j2"}]}
        bound = sidebar.bound_items([tab, {"space": 1, "claude": None}])
        self.assertEqual(bound, {("jobs", "j1")})
        self.assertEqual([i["id"] for i in sidebar.group_items(src, bound)], ["j2"])
        self.assertEqual([i["id"] for i in sidebar.group_items({**src, "key": "other"}, bound)], ["j1", "j2"])
        # gone from its source's feed: a plain tab again, under the name it was given
        plain = sidebar.tab_info(tab)
        self.assertEqual((plain["title"], plain["has_claude"], plain["cls"]), ("Port the login", False, None))


if __name__ == "__main__":
    unittest.main()
