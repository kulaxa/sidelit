"""When a tab calls you back (yellow, breathing): the rule the sidebar draws and the report sends."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit.tabstate import Attention  # noqa: E402


class AttentionTest(unittest.TestCase):
    def setUp(self):
        self.a = Attention()

    def run_states(self, *states, active=False, item=False):
        return [self.a.update("t", s, active, item=item) for s in states]

    def test_a_claude_that_hands_back_while_you_are_elsewhere(self):
        self.assertEqual(self.run_states("busy", "checking", "done", "done"), [False, False, True, True])
        self.assertFalse(self.a.update("t", "done", True))  # you went there
        self.assertFalse(self.a.update("t", "done", False))

    def test_not_while_you_watch_nor_when_it_only_waits(self):
        self.assertEqual(self.run_states("busy", "done", active=True), [False, False])
        self.assertEqual(self.run_states("busy", "checking", "external"), [False, False, False])

    def test_clicking_the_tab_is_seeing_it(self):
        self.run_states("busy", "waiting")
        self.a.seen("t")
        self.assertFalse(self.a.update("t", "waiting", False))

    def test_an_item_that_starts_waiting(self):
        self.assertEqual(self.run_states("working", "waiting", "waiting", item=True), [False, True, True])
        self.assertFalse(self.a.update("t", "waiting", True, item=True))

    def test_an_item_already_waiting_when_first_seen_does_not(self):
        """(the sidebar starting, or the tab drawn plain until its source answered)"""
        self.a.update("t", "other", False)
        self.assertFalse(self.a.update("t", "waiting", False, item=True))
        self.assertFalse(self.a.update("u", "waiting", False, item=True))


if __name__ == "__main__":
    unittest.main()
