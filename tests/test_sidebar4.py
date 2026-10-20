"""Which sidebar runs (GTK 4 or the GTK 3 fallback), and the shared helpers both draw from.
Neither needs a display: sidebar4 and sidebar_common import no GTK."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from sidelit import sidebar4, sidebar_common
except (ImportError, ValueError):  # no PyGObject at all
    sidebar4 = sidebar_common = None


@unittest.skipIf(sidebar4 is None, "needs PyGObject")
class ChoiceTest(unittest.TestCase):
    def install(self, d: Path):
        (d / "girepository-1.0").mkdir(parents=True)
        (d / sidebar4.LIB).write_bytes(b"")
        (d / "girepository-1.0" / sidebar4.TYPELIB).write_bytes(b"")

    def test_finds_a_layer_shell_where_it_is_pointed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.install(Path(tmp))
            with mock.patch.dict(os.environ, {"SIDELIT_GTK4_LAYER_SHELL": tmp}):
                lib, typelibs = sidebar4.layer_shell_paths(prefixes=[])
        self.assertEqual(lib, os.path.join(tmp, sidebar4.LIB))
        self.assertEqual(typelibs, os.path.join(tmp, "girepository-1.0"))

    def test_finds_one_built_into_a_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.install(Path(tmp) / "lib")
            with mock.patch.dict(os.environ, {"SIDELIT_GTK4_LAYER_SHELL": ""}):
                lib, _ = sidebar4.layer_shell_paths(prefixes=[Path(tmp)])
        self.assertEqual(lib, os.path.join(tmp, "lib", sidebar4.LIB))

    def test_a_library_without_its_typelib_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / sidebar4.LIB).write_bytes(b"")
            with mock.patch.dict(os.environ, {"SIDELIT_GTK4_LAYER_SHELL": tmp}):
                self.assertIsNone(sidebar4.layer_shell_paths(prefixes=[]))

    def test_the_environment_and_the_config_choose(self):
        with mock.patch.object(sidebar4.config, "load", return_value={"sidebar": "gtk3"}):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(sidebar4.CHOICE_ENV, None)
                self.assertEqual(sidebar4.choice(), "gtk3")
                self.assertFalse(sidebar4.should_run())
            with mock.patch.dict(os.environ, {sidebar4.CHOICE_ENV: "gtk4"}):
                self.assertEqual(sidebar4.choice(), "gtk4")
        with mock.patch.object(sidebar4.config, "load", return_value={"sidebar": "bogus"}):
            os.environ.pop(sidebar4.CHOICE_ENV, None)
            self.assertEqual(sidebar4.choice(), "auto")

    def test_gtk4_runs_only_with_a_layer_shell(self):
        with mock.patch.object(sidebar4, "choice", return_value="auto"):
            with mock.patch.object(sidebar4, "layer_shell_paths", return_value=None):
                self.assertFalse(sidebar4.should_run())
            with mock.patch.object(sidebar4, "layer_shell_paths", return_value=("/l.so", "/t")):
                self.assertTrue(sidebar4.should_run())


@unittest.skipIf(sidebar_common is None, "needs PyGObject")
class CommonTest(unittest.TestCase):
    def test_subagents_are_grouped_by_model_most_first(self):
        groups = sidebar_common.subagent_groups(["Sonnet 5.5", "Haiku 5.5", None, "Haiku 5.5", "GPT-5.5"], 6)
        self.assertEqual(groups, (("Haiku 5.5", "haiku", 2), ("GPT-5.5", "other", 1), ("Sonnet 5.5", "sonnet", 1),
                                  (sidebar_common.UNKNOWN_MODEL, "other", 2)))  # (one the daemon had no model for)
        self.assertEqual(sidebar_common.subagent_groups([], 0), ())
        self.assertEqual([sidebar_common.group_blocks(n) for n in (1, 4, 5)], [(1, ""), (4, ""), (1, "5")])
        css = sidebar_common.model_css()
        self.assertIn(".cw-box .cw-dots .cw-dot.cw-m-opus", css)
        self.assertIn(".cw-tip .cw-tip-row.cw-dot.cw-m-other", css)

    def test_usage_levels(self):
        self.assertEqual([sidebar_common.usage_level(p) for p in (0, 69, 70, 89, 90, 100)],
                         [None, None, "warn", "warn", "high", "high"])

    def test_usage_bars_are_what_the_plugin_wrote(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        data = {"at": 1, "limits": [
            {"kind": "seven_day", "percentUsed": 41.2, "resetsAt": "2026-10-21T09:00:00Z"},
            {"kind": "five_hour", "percentUsed": 23.5, "resetsAt": "2026-10-17T15:00:00+00:00"},
            {"kind": "weekly_opus", "percentUsed": 7},                                         # (one we do not know)
            {"kind": "spend_limit", "percentUsed": 63, "resetsAt": "2026-10-17T11:00:00Z"},     # its reset has passed
            {"kind": "five_hour", "percentUsed": "a lot"}, "junk"]}
        self.assertEqual(sidebar_common.usage_rows(data, now), [
            ("Session", 23.5, "2026-10-17T15:00:00+00:00", None), ("Week", 41.2, "2026-10-21T09:00:00Z", None),
            ("Weekly opus", 7.0, None, None)])
        for nothing in (None, {}, {"limits": None}, {"at": 5, "rows": [["Session", 50, None]]}):  # (the old cache)
            self.assertEqual(sidebar_common.usage_rows(nothing, now), [])
        self.assertTrue(sidebar_common.countdown("2999-01-01T00:00:00Z"))

    def test_a_models_week_is_named_after_it_and_says_how_old_it_is(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        read = lambda minutes: int((now.timestamp() - minutes * 60) * 1000)  # noqa: E731
        data = {"limits": [{"kind": "seven_day_fable", "label": "Fable", "percentUsed": 3, "at": read(5)},
                           {"kind": "seven_day", "percentUsed": 41}]}
        self.assertEqual(sidebar_common.usage_rows(data, now), [("Week", 41.0, None, None), ("Fable", 3.0, None, None)])
        old = {"limits": [{"kind": "seven_day_fable", "label": "Fable", "percentUsed": 3, "at": read(60)}]}
        stamp = datetime.fromtimestamp(read(60) / 1000).strftime("%H:%M")
        self.assertEqual(sidebar_common.usage_rows(old, now), [("Fable", 3.0, None, stamp)])
        self.assertEqual(sidebar_common.reset_line(None, stamp), f"as of {stamp}")
        self.assertTrue(sidebar_common.reset_line("2999-01-01T00:00:00Z", stamp).startswith(f"as of {stamp} · resets in "))

    def test_claude_codes_own_usage_reading_gives_its_models_weeks(self):
        from sidelit import claude
        reading = {"oauthAccount": {"accountUuid": "a1"}, "cachedUsageUtilization": {
            "accountUuid": "a1", "fetchedAtMs": 1792502221578, "utilization": {"limits": [
                {"kind": "session", "group": "session", "percent": 18},
                {"kind": "weekly_all", "group": "weekly", "percent": 45},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 2, "resets_at": "2026-10-22T06:00:00+00:00",
                 "scope": {"model": {"id": "claude/fable", "display_name": "Fable"}}}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            with mock.patch.object(claude, "GLOBAL_CONFIG", path), mock.patch.dict(claude._SCOPED, {"mtime": None, "limits": []}):
                self.assertEqual(claude.scoped_limits(), [])  # (no file)
                path.write_text(json.dumps(reading))
                self.assertEqual(claude.scoped_limits(), [
                    {"kind": "seven_day_fable", "label": "Fable", "percentUsed": 2,
                     "resetsAt": "2026-10-22T06:00:00+00:00", "at": 1792502221578}])
                other = {**reading, "oauthAccount": {"accountUuid": "b2"}}  # (another account's reading)
                path.write_text(json.dumps(other) + " ")
                self.assertEqual(claude.scoped_limits(), [])
                path.write_text("{ half written")
                self.assertEqual(claude.scoped_limits(), [])

    def test_usage_bars_follow_the_file_and_outlast_a_login(self):
        class Bars(sidebar_common.SidebarLogic):
            def __init__(self):
                self._usage_at, self._usage_rows = {}, None
                self.drawn = []

            def _render_usage(self, groups):
                self.drawn.append(groups)
        later = "2999-01-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            live, cache = Path(tmp) / "run/usage.json", Path(tmp) / "config/usage-cache.json"
            codex = (Path(tmp) / "run/usage-codex.json", Path(tmp) / "config/usage-cache-codex.json")
            with mock.patch.object(sidebar_common, "usage_sources",
                                   lambda: [("Claude Code", live, cache), ("Codex", *codex)]):
                bars = Bars()
                bars._refresh_usage()
                self.assertEqual(bars.drawn, [[]])  # nothing known: no bars
                live.parent.mkdir()
                live.write_text(json.dumps({"at": 1, "limits": [{"kind": "five_hour", "percentUsed": 10, "resetsAt": later}]}))
                bars._refresh_usage()
                bars._refresh_usage()  # (the same figures: not drawn again)
                self.assertEqual(bars.drawn[1:], [[("Claude Code", [("Session", 10.0, later, None)])]])
                live.write_text('{"at": 2, "limits": [{"kin')  # the plugin half way through writing it
                bars._refresh_usage()
                self.assertEqual(len(bars.drawn), 2)
                # another agent's limits come under its own name
                codex[0].write_text(json.dumps({"at": 3, "limits": [{"kind": "seven_day", "percentUsed": 55, "resetsAt": later}]}))
                bars._refresh_usage()
                self.assertEqual(bars.drawn[-1], [("Claude Code", [("Session", 10.0, later, None)]), ("Codex", [("Week", 55.0, later, None)])])
                live.unlink()  # a new login: no session has measured anything yet
                codex[0].unlink()
                fresh = Bars()
                fresh._refresh_usage()
                self.assertEqual(fresh.drawn, [[("Claude Code", [("Session", 10.0, later, None)]), ("Codex", [("Week", 55.0, later, None)])]])

    def test_each_agent_with_limits_has_its_files(self):
        names = [name for name, _, _ in sidebar_common.usage_sources()]
        self.assertEqual(names, ["Claude Code", "Codex"])
        self.assertEqual(sidebar_common.usage_sources()[0][1:], (sidebar_common.USAGE_FILE, sidebar_common.USAGE_CACHE))

    def test_closing_asks_only_when_there_are_windows(self):
        self.assertIsNone(sidebar_common.close_question({"windows": []}))
        q, detail = sidebar_common.close_question({"claudes": [{"title": "Fix"}],
                                                   "windows": [{"app": "google-chrome"}, {"app": "x.Terminator"}]})
        self.assertEqual(q, "Close workspace – Fix?")
        self.assertIn("2 windows (Terminator, chrome)", detail)
        self.assertIn("A Claude Code session stays", detail)
        q, detail = sidebar_common.close_question({"cwd": "/src/pricing", "claudes": [{"title": "A"}, {"title": "B"}],
                                                   "windows": [{"app": "terminator"}, {"app": "terminator"}]})
        self.assertEqual(q, "Close workspace – pricing?")
        self.assertIn("Its Claude Code sessions stay", detail)

    def test_history_lines_and_search(self):
        title, sub, needle, _ = sidebar_common.history_lines(
            {"title": "Fix tests", "closed": time.time(), "cwd": "/repo", "what": ["Claude 'Fix tests'", "chrome 'CI'"]})
        self.assertEqual(title, "Fix tests")
        self.assertIn("repo", sub)
        self.assertNotIn("Claude", sub)
        self.assertTrue(sidebar_common.matches(needle, "fix CI"))
        self.assertFalse(sidebar_common.matches(needle, "docs"))

    def test_a_closed_tab_says_so_under_its_title(self):
        info = sidebar_common.tab_info({"space": None, "last_title": "Notes",
                                        "missing": [{"slot": "a", "what": "code"}]})
        self.assertEqual((info["title"], info["sub"]), ("Notes", "closed · 1 window to reopen"))

    def test_menus_are_plain_lists(self):
        class Fake(sidebar_common.SidebarLogic):
            def __init__(self):
                self.cfg, self.state, self.sent = {"web_apps": []}, {"recent": []}, []

            def send(self, method, **params):
                self.sent.append((method, params))

        ui = Fake()
        tab = {"space": 3, "claudes": [{"title": "T", "session": "s1", "cwd": "/w"}],
               "missing": [{"slot": "m1", "what": "chrome 'Docs'"}]}
        entries = ui.tab_menu(tab)
        self.assertEqual(entries[:2], [("/w", None), sidebar_common.SEPARATOR])  # its folder, greyed out
        labels = [e[0] for e in entries if e is not sidebar_common.SEPARATOR]
        self.assertIn("Fork session", labels)
        self.assertIn("missing: chrome 'Docs'", labels)
        self.assertIn(sidebar_common.SEPARATOR, entries)
        dict(e for e in entries if e is not sidebar_common.SEPARATOR)["Open terminal here"]()
        self.assertEqual(ui.sent[-1], ("terminal", {"space": 3}))
        # with two Claudes, the card's menu forks the one whose box you right-clicked
        two = {**tab, "claudes": tab["claudes"] + [{"title": "U", "session": "s2", "cwd": "/v"}]}
        self.assertNotIn("Fork session", [e[0] for e in ui.tab_menu(two) if e is not sidebar_common.SEPARATOR])
        box = sidebar_common.tab_info(two)["boxes"][1]
        dict(e for e in ui.tab_menu(two, box) if e is not sidebar_common.SEPARATOR)["Fork session"]()
        self.assertEqual(ui.sent[-1], ("fork", {"session": "s2"}))

    def test_a_session_with_no_name_yet_is_a_new_session(self):
        """Not the workspace's last title, nor a placeholder: the daemon sends "" until it has a name."""
        tab = {"space": 2, "last_title": "Older Claude", "claudes": [{"key": "s1", "session": "s1", "title": ""}]}
        self.assertEqual(sidebar_common.tab_info(tab)["boxes"][0]["title"], "New session")

    def test_no_release_to_fetch_is_not_worth_a_line_every_hour(self):
        """(a 404 while the repository is private)"""
        import io
        import urllib.error

        class Fake(sidebar_common.SidebarLogic):
            pass

        for error, said in ((urllib.error.HTTPError("u", 404, "Not Found", {}, None), ""),
                            (urllib.error.URLError("no network"), "update check failed")):
            err = io.StringIO()
            with mock.patch.object(sidebar_common.urllib.request, "urlopen", side_effect=error), \
                    mock.patch.object(sidebar_common.threading, "Thread",
                                      side_effect=lambda target, daemon: mock.Mock(start=target)), \
                    mock.patch.object(sidebar_common.sys, "stderr", err):
                Fake()._check_update()
            if said:
                self.assertIn(said, err.getvalue())
            else:
                self.assertEqual(err.getvalue(), "")

    def test_a_workspace_with_claudes_is_a_box_each(self):
        tab = {"space": 2, "ws": "w1", "cwd": "/src/pricing", "active": False, "claudes": [
            {"key": "s1", "session": "s1", "title": "Pricing API", "state": "busy", "model": "Opus 5.5", "agents": 2,
             "models": ["Haiku 5.5", "Haiku 5.5"]},
            {"key": "s2", "session": "s2", "title": "Review tests", "state": "done", "model": "Sonnet 5.5",
             "calls": 1000.0},
            {"key": "s3", "session": "s3", "title": "Deploy", "state": "external", "on": "prod deploy",
             "model": "Haiku 5.5"}]}
        info = sidebar_common.tab_info(tab)
        # headed by its number; no tooltip on an open card (its folder heads its menu)
        self.assertEqual((info["title"], info["sub"], info["tip"], info["path"]), ("Workspace 2", "", "", "/src/pricing"))
        self.assertEqual([(b["glyph"], b["title"], b["sub"], b["cls"], b["agents"], b["calls"]) for b in info["boxes"]], [
            ("◐", "Pricing API", "Opus 5.5", "working", 2, False),  # subagents are dots, not words
            ("✳", "Review tests", "Sonnet 5.5", "idle", 0, True),
            ("✳", "Deploy", "Haiku 5.5 · waiting on prod deploy", "shell", 0, False)])
        self.assertEqual([b["subagents"] for b in info["boxes"]], [(("Haiku 5.5", "haiku", 2),), (), ()])
        classes = dict(sidebar_common.tab_classes(tab, info, True))
        self.assertTrue(classes["card"] and classes["attention"])
        self.assertFalse(classes["claude"] or classes["working"])  # each box carries its own state
        self.assertEqual(dict(sidebar_common.box_classes(info["boxes"][1]))["attention"], True)
        # a name you gave it heads the card instead
        self.assertTrue(info["head"])
        named = sidebar_common.tab_info({**tab, "name": "Billing"})
        self.assertEqual((named["title"], named["head"]), ("Billing", True))
        self.assertTrue(sidebar_common.tab_info({**tab, "claudes": []})["head"])  # nothing else to name it
        # closed: named after its first Claude with a name, else the last one it had
        unnamed = {"key": "s0", "session": "s0", "title": ""}
        self.assertEqual(sidebar_common.tab_info({**tab, "space": None, "claudes": [unnamed, *tab["claudes"]]})["title"],
                         "Pricing API")
        self.assertEqual(sidebar_common.tab_info({**tab, "space": None, "claudes": [unnamed], "last_title": "Old"})["title"], "Old")
        # one Claude: one box, the same card
        one = sidebar_common.tab_info({**tab, "claudes": tab["claudes"][:1]})
        self.assertEqual((len(one["boxes"]), one["session_id"]), (1, "s1"))

    def test_an_app_without_a_desktop_entry_has_no_icon_and_draws(self):
        # (GIO raises for an entry that is not there; that once stopped the sidebar drawing)
        with mock.patch.object(sidebar_common, "_APPS", {}), \
                mock.patch.object(sidebar_common.launch, "desktop_app", side_effect=ImportError):
            self.assertEqual(sidebar_common.app_entry("firefox"), (None, ""))  # (no GIO at all)
        try:
            import gi  # noqa: F401
        except ImportError:
            return
        with mock.patch.object(sidebar_common, "_APPS", {}):
            self.assertEqual(sidebar_common.app_entry("no-such-app-sidelit-test"), (None, ""))

    def test_other_windows_are_pills_under_the_claudes(self):
        tab = {"space": 1, "ws": "w1", "name": "Home", "claudes": [{"key": "s1", "title": "Fix", "window": "T"}],
               "windows": [{"id": "T", "app": "terminator", "title": "✳ Fix"},
                           {"id": "C", "app": "google-chrome", "title": "CI - Google Chrome"},
                           {"id": "D", "app": "discord", "title": "(264) #office | Acme - Discord"},
                           {"id": "E", "app": "google-chrome", "title": "Docs - Google Chrome", "tabs": 26}],
               "missing": [{"slot": "x", "what": "code 'notes'", "app": "code"}]}
        info = sidebar_common.tab_info(tab)
        self.assertEqual(info["title"], "Home")
        # the Claude's own terminal is its box, not a pill too; a missing window is a dim pill
        self.assertEqual([(a["key"], a["window"], a["title"], a["missing"], a["tabs"], a["unread"])
                          for a in info["apps"]], [
            ("C", "C", "CI", False, 0, 0), ("D", "D", "#office | Acme", False, 0, 264),
            ("E", "E", "Docs", False, 26, 0), ("x", None, "code 'notes'", True, 0, 0)])
        empty = sidebar_common.tab_info({"space": 3, "ws": "w3", "claudes": [], "windows": []})
        self.assertEqual((empty["title"], empty["sub"], empty["apps"]), ("Workspace 3", "empty", []))
        self.assertEqual(sidebar_common.window_label("Notes — Text Editor", "org.gnome.TextEditor"), "Notes")
        self.assertEqual(sidebar_common.window_label("a - b", "kitty"), "a - b")  # not the app's name: kept

    def test_a_box_click_goes_to_its_claude_and_the_card_lands_where_needed(self):
        class Fake(sidebar_common.SidebarLogic):
            def __init__(self):
                self.cfg, self.state, self.sent, self._items = {}, {}, [], {}
                self._attention = sidebar_common.Attention()

            def send(self, method, **params):
                self.sent.append((method, params))

        ui = Fake()
        tab = {"key": "w:w1", "space": 2, "ws": "w1", "claudes": [{"key": "s1"}, {"key": "s2"}]}
        ui.on_box_clicked(tab, sidebar_common.tab_info(tab)["boxes"][1])
        ui.on_tab_clicked(tab)
        ui.on_app_clicked(tab, {"window": "C"})
        ui.on_app_clicked(tab, {"window": None})  # missing: the workspace, which reopens it
        self.assertEqual(ui.sent, [("focus", {"key": "s2"}), ("activate", {"ws": "w1"}),
                                   ("focus", {"window": "C"}), ("activate", {"ws": "w1"})])

    def test_a_package_install_updates_with_its_own_package_tool(self):
        with mock.patch.object(sidebar_common, "checkout_root", return_value=None), \
                mock.patch.object(sidebar_common, "login_shell", return_value="bash"):
            with mock.patch.object(sidebar_common, "package_tool", return_value="pacman"):
                self.assertIn(f"sudo pacman -U ./{sidebar_common.LATEST_ARCH}", sidebar_common.update_command())
            with mock.patch.object(sidebar_common, "package_tool", return_value="apt"):
                self.assertIn(f"sudo apt install ./{sidebar_common.LATEST_DEB}", sidebar_common.update_command())

    def test_a_checkout_updates_the_way_the_one_line_install_does(self):
        # (get.sh pull: it also follows a repository that began its history anew, tests/test_get.py)
        with mock.patch.object(sidebar_common, "checkout_root", return_value="/home/u/.local/share/sidelit"), \
                mock.patch.object(sidebar_common, "login_shell", return_value="bash"):
            self.assertIn("bash /home/u/.local/share/sidelit/get.sh pull /home/u/.local/share/sidelit"
                          " && /home/u/.local/share/sidelit/bin/sidelit restart", sidebar_common.update_command())


if __name__ == "__main__":
    unittest.main()
