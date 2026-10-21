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
        self.backs = []
        self.panel = sidebar.HistoryPanel(self.picked.append, lambda: self.backs.append(True))
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

    def test_the_last_days_newest_first_and_older_on_request(self):
        self.assertEqual(self.shown(), ["Fix tests", "Write docs"])
        self.assertEqual([r.day for r in self.panel.list.get_children()][:2], ["Today", "Yesterday"])
        self.assertEqual(self.panel.older.get_label(), "Show 1 older")
        self.panel._show_older()
        self.assertEqual(self.shown(), ["Fix tests", "Write docs", "Pricing page"])
        self.assertFalse(self.panel.older.get_visible())
        self.panel.reset()  # opened again: the recent days only, loading
        self.assertEqual(self.panel.list.get_children(), [])

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
    def box(self, **claude):
        tab = {"space": 1, "claudes": [{"key": "s1", "title": "Fix tests", "model": "Opus 5.5", **claude}]}
        return sidebar.tab_info(tab)["boxes"][0]

    def test_subagents_are_dots_beside_the_main_threads_model(self):
        self.assertEqual((self.box(state="busy")["sub"], self.box(state="busy")["agents"]), ("Opus 5.5", 0))
        self.assertEqual((self.box(state="busy", agents=1)["sub"], self.box(state="busy", agents=1)["agents"]),
                         ("Opus 5.5", 1))
        self.assertEqual(self.box(state="waiting", agents=3)["sub"], "Opus 5.5 · waiting for you")

    ITEM = {"id": "j1", "title": "Port the login screen", "state": "waiting", "detail": "Allow",
            "url": "https://jobs.example.test/job/j1", "updated": 0}

    def test_a_workspace_opened_for_an_item_is_drawn_from_it(self):
        tab = {"space": 2, "ws": "w1", "name": "Port the login", "claudes": [], "item": {"source": "jobs", "id": "j1"},
               "windows": [{"app": "chrome-jobs.example.test__job_j1-Default"}]}
        info = sidebar.tab_info(tab, self.ITEM)
        self.assertEqual((info["title"], info["sub"], info["cls"]), ("Port the login screen", "waiting for you · Allow", "waiting"))
        self.assertTrue(info["has_claude"])  # the coloured stripe
        self.assertTrue(info["item"])
        for state, cls in (("working", "working"), ("failed", "failed"), ("idle", "idle"), ("done", "idle")):
            self.assertEqual(sidebar.tab_info(tab, {**self.ITEM, "state": state})["cls"], cls)
        # listed in its source's group only while it has no workspace of its own
        src = {"key": "jobs", "items": [self.ITEM, {**self.ITEM, "id": "j2"}]}
        bound = sidebar.bound_items([tab, {"space": 1, "claudes": []}])
        self.assertEqual(bound, {("jobs", "j1")})
        self.assertEqual([i["id"] for i in sidebar.group_items(src, bound)], ["j2"])
        self.assertEqual([i["id"] for i in sidebar.group_items({**src, "key": "other"}, bound)], ["j1", "j2"])
        # gone from its source's feed: a plain tab again, under the name it was given
        plain = sidebar.tab_info(tab)
        self.assertEqual((plain["title"], plain["has_claude"], plain["cls"]), ("Port the login", False, None))

    def test_every_open_card_is_headed_by_its_name_or_number(self):
        tab = {"space": 3, "claudes": [{"key": "s1", "title": "Fix tests"}]}
        self.assertEqual((sidebar.tab_info(tab)["title"], sidebar.tab_info(tab)["head"]), ("Workspace 3", True))
        self.assertEqual(sidebar.tab_info({**tab, "name": "API"})["title"], "API")
        closed = sidebar.tab_info({**tab, "space": None})
        self.assertEqual((closed["title"], closed["head"]), ("Fix tests", False))


@unittest.skipIf(sidebar is None, "needs GTK 3 and gtk-layer-shell")
class SettingsPanelTest(unittest.TestCase):
    LONG = "a label far too long for the sidebar, which would widen it if it asked for all of it"

    def setUp(self):
        self.picked, self.placed, self.flipped, self.ran = [], [], [], []
        self.panel = sidebar.SettingsPanel(self.picked.append, lambda k, v: self.placed.append((k, v)),
                                           [(f"Restore after login, {self.LONG}", "restore_on_login")],
                                           lambda k, on: self.flipped.append((k, on)),
                                           [(f"Put everything back, {self.LONG}", "view-refresh-symbolic",
                                             lambda: self.ran.append("put back"))])
        self.panel.set_themes([("auto", "Seasonal", self.LONG), ("dusk", self.LONG, "")], "dusk")
        self.panel.set_switches({"restore_on_login": True})

    def cards(self):
        content = self.panel.get_children()[0].get_child().get_child()
        return [c for c in content.get_children() if isinstance(c, sidebar.Gtk.Button)]

    def test_the_theme_in_use_is_marked_and_a_click_picks_one(self):
        themes = self.panel.themes_box.get_children()
        self.assertEqual([b.get_style_context().has_class("picked") for b in themes], [False, True])
        themes[0].clicked()
        self.assertEqual(self.picked, ["auto"])

    def test_a_card_flips_its_switch_and_runs_its_action(self):
        switch, action = self.cards()
        self.assertTrue(self.panel.switches["restore_on_login"].get_active())
        switch.clicked()
        self.assertEqual(self.flipped, [("restore_on_login", False)])
        self.assertFalse(self.panel.switches["restore_on_login"].get_active())
        action.clicked()
        self.assertEqual(self.ran, ["put back"])

    def test_long_labels_do_not_widen_the_sidebar(self):
        self.panel.set_places(sidebar.place_choices({"monitor": self.LONG}, 2))
        self.panel.show_all()
        self.assertLessEqual(self.panel.get_preferred_width()[0], 280)

    def test_a_segment_shows_the_place_in_use_and_a_click_picks_another(self):
        self.panel.set_places(sidebar.place_choices({"side": "left", "monitor": "largest"}, 2))
        side, screen = self.panel.places_box.get_children()
        segments = [row.get_children()[1].get_children() for row in (side, screen)]
        picked = [[b.get_style_context().has_class("picked") for b in seg] for seg in segments]
        self.assertEqual(picked, [[True, False], [False, False]])  # ("largest": neither, and said)
        self.assertEqual([lbl.get_text() for lbl in screen.get_children()[0].get_children()], ["Screen", "largest"])
        segments[1][1].clicked()
        self.assertEqual(self.placed, [("monitor", "rightmost")])
        self.assertTrue(segments[1][1].get_style_context().has_class("picked"))
        self.assertEqual(len(screen.get_children()[0].get_children()), 1)
        # one screen: only the side
        self.panel.set_places(sidebar.place_choices({}, 1))
        self.assertEqual(len(self.panel.places_box.get_children()), 1)


@unittest.skipIf(sidebar is None, "needs GTK 3 and gtk-layer-shell")
class UsageRowTest(unittest.TestCase):
    def lay_out(self, width=None):
        """A limit's name and number in a row that wide (by default, just wide enough for both):
        (the gap between them, whether the name was cut short)."""
        row = sidebar.Sidebar._usage_row("Session", 38)
        row.show_all()
        name, value = row.get_children()[0].get_children()
        width = width or name.get_preferred_width()[1] + value.get_preferred_width()[1]
        row.get_preferred_width()
        row.get_preferred_height_for_width(width)
        rect = sidebar.Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, width, 40
        row.size_allocate(rect)
        left, right = name.get_allocation(), value.get_allocation()
        self.assertEqual(right.x + right.width, width)  # (the number at the right edge)
        return right.x - (left.x + left.width), name.get_layout().is_ellipsized()

    def test_a_name_gives_way_rather_than_touch_its_number(self):
        # (three limits side by side, compact: just room for both, none for a gap)
        gap, cut = self.lay_out()
        self.assertGreaterEqual(gap, 6)
        self.assertTrue(cut)
        self.assertEqual(self.lay_out(300), (6, False))  # (one to a line: the name whole)


if __name__ == "__main__":
    unittest.main()
