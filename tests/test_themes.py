"""Which theme the sidebar wears, and what a theme hands it. Needs no display."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import themes  # noqa: E402


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(themes.ENV, None)

    def test_no_theme_in_the_config_is_the_default_and_so_is_the_old_seasonal_one(self):
        self.assertEqual(themes.wanted({}), "default")
        self.assertEqual(themes.wanted({"theme": "auto"}), "default")
        self.assertEqual(themes.chosen({"theme": "auto"}), "default")

    def test_a_named_theme_holds_and_the_environment_wins(self):
        self.assertEqual(themes.wanted({"theme": "Halloween"}), "halloween")
        os.environ[themes.ENV] = "halloween"
        self.assertEqual(themes.wanted({"theme": "default"}), "halloween")
        self.assertEqual(themes.chosen({"theme": "default"}), "default")  # (what the settings tick)


class MenuTest(unittest.TestCase):
    def test_the_menu_lists_the_built_in_themes_and_the_users_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("mine", "halloween"):  # (a file named like a built-in one is not listed twice)
                (Path(tmp) / f"{name}.css").write_text("")
            with mock.patch.object(themes, "USER_THEMES", Path(tmp)):
                names = [n for n, _, _ in themes.choices()]
        self.assertEqual(names, ["default", "halloween", "mine"])

    def test_every_theme_has_a_picture(self):
        for name in ("default", "halloween", "mine"):
            svg = themes.preview(name)
            self.assertTrue(svg.startswith(b"<svg") and svg.endswith(b"</svg>"), name)
        self.assertIn(b"#a6ff4d", themes.preview("halloween"))
        self.assertNotIn(b"#a6ff4d", themes.preview("default"))

    def test_picking_one_changes_only_that_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"width": 300}')
            with mock.patch.multiple(themes.config, CONFIG=cfg, CONFIG_DIR=Path(tmp)):
                self.assertTrue(themes.config.put("theme", "halloween"))
                self.assertEqual(json.loads(cfg.read_text()), {"width": 300, "theme": "halloween"})
                cfg.write_text('{"width": 3')  # half edited: left alone
                self.assertFalse(themes.config.put("theme", "default"))
                self.assertEqual(cfg.read_text(), '{"width": 3')


class LoadTest(unittest.TestCase):
    def test_a_user_css_file_is_a_theme_and_an_unknown_name_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mine.css").write_text(".cw-panel { border-color: red; }")
            with mock.patch.object(themes, "USER_THEMES", Path(tmp)):
                self.assertIn("red", themes.load("mine").css)
                self.assertIs(themes.load("nope"), themes.THEMES["default"])

    def test_images_are_written_out_and_the_css_points_at_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(themes.config, "RUNTIME_DIR", Path(tmp)):
                css = themes.css(themes.THEMES["halloween"])
            web = Path(tmp) / "theme" / "halloween" / "web.svg"
            self.assertTrue(web.read_bytes().startswith(b"<svg"))
            self.assertIn(web.as_uri(), css)
            self.assertNotIn("{images}", css)

    def test_without_a_place_for_images_the_colours_alone(self):
        with mock.patch.object(themes.config, "RUNTIME_DIR", Path("/proc/no/such")):
            css = themes.css(themes.THEMES["halloween"])
        self.assertNotIn("url(", css)
        self.assertIn("#ff7a1a", css)


if __name__ == "__main__":
    unittest.main()
