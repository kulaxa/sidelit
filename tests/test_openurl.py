"""The link handler: one click, one tab, and never itself as the browser."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import openurl  # noqa: E402


class OneClickOneTab(unittest.TestCase):
    def test_the_same_url_twice_at_once_is_opened_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "last.json")
            self.assertFalse(openurl.seen_just_now("https://a", 100.0, path))
            self.assertTrue(openurl.seen_just_now("https://a", 100.4, path))
            self.assertFalse(openurl.seen_just_now("https://b", 100.5, path))
            self.assertFalse(openurl.seen_just_now("https://a", 100.6, path))  # a different click in between
            self.assertFalse(openurl.seen_just_now("https://a", 103.0, path))  # clicked again later

    def test_we_are_never_our_own_browser(self):
        for exe in ("sidelit-open", "/home/x/.local/bin/sidelit-open",
                    "sidelit-open.desktop", "sidelit open",
                    # the old name's opener, still set in a moved-over shell rc or Terminator
                    "claude-workspace-open", "/home/x/.local/bin/claude-workspace-open",
                    "claude-workspace-open.desktop", "claude-workspace open"):
            self.assertTrue(openurl._is_self(exe), exe)
        self.assertFalse(openurl._is_self("google-chrome"))

    def test_links_that_are_not_web_addresses_are_left_alone(self):
        self.assertEqual(openurl.as_url("mailto:a@b.c"), "mailto:a@b.c")
        self.assertEqual(openurl.as_url("example.com"), "https://example.com")
        self.assertEqual(openurl.as_url("https://x.y/z"), "https://x.y/z")


if __name__ == "__main__":
    unittest.main()
