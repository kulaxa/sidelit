"""Which sidebar runs (GTK 4 or the GTK 3 fallback), and the shared helpers both draw from.
Neither needs a display: sidebar4 and sidebar_common import no GTK."""
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
    def test_usage_levels(self):
        self.assertEqual([sidebar_common.usage_level(p) for p in (0, 69, 70, 89, 90, 100)],
                         [None, None, "warn", "warn", "high", "high"])

    def test_closing_asks_only_when_there_are_windows(self):
        self.assertIsNone(sidebar_common.close_question({"windows": []}))
        q, detail = sidebar_common.close_question({"claude": {"title": "Fix"},
                                                   "windows": [{"app": "google-chrome"}, {"app": "x.Terminator"}]})
        self.assertEqual(q, "Close workspace – Fix?")
        self.assertIn("2 windows (Terminator, chrome)", detail)

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
        tab = {"space": 3, "claude": {"title": "T", "session": "s1", "cwd": "/w"},
               "missing": [{"slot": "m1", "what": "chrome 'Docs'"}]}
        entries = ui.tab_menu(tab)
        labels = [e[0] for e in entries if e is not sidebar_common.SEPARATOR]
        self.assertIn("Fork session", labels)
        self.assertIn("missing: chrome 'Docs'", labels)
        self.assertIn(sidebar_common.SEPARATOR, entries)
        dict(e for e in entries if e is not sidebar_common.SEPARATOR)["Open terminal here"]()
        self.assertEqual(ui.sent[-1], ("terminal", {"space": 3}))

    def test_a_package_install_updates_with_its_own_package_tool(self):
        with mock.patch.object(sidebar_common, "checkout_root", return_value=None), \
                mock.patch.object(sidebar_common, "login_shell", return_value="bash"):
            with mock.patch.object(sidebar_common, "package_tool", return_value="pacman"):
                self.assertIn(f"sudo pacman -U ./{sidebar_common.LATEST_ARCH}", sidebar_common.update_command())
            with mock.patch.object(sidebar_common, "package_tool", return_value="apt"):
                self.assertIn(f"sudo apt install ./{sidebar_common.LATEST_DEB}", sidebar_common.update_command())


if __name__ == "__main__":
    unittest.main()
