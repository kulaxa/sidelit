"""A restore once per compositor: after a login, and after COSMIC restarted itself."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import session  # noqa: E402
from sidelit.session import Compositor  # noqa: E402


class OncePerCompositor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / "restored"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_login_restores_once(self):
        comp = Compositor(pid=4708, started=1000, age=5)
        self.assertTrue(session.needs_restore(comp, self.marker))
        session.mark_restored(comp, self.marker)
        self.assertFalse(session.needs_restore(comp, self.marker))  # the daemon restarting: nothing to do

    def test_the_compositor_restarting_mid_session_restores_again(self):
        """COSMIC crashed on a lid close and restarted: every window died, the session did not."""
        session.mark_restored(Compositor(pid=4708, started=1000, age=5), self.marker)
        again = Compositor(pid=2345464, started=9_000_000, age=2)
        self.assertTrue(session.needs_restore(again, self.marker))

    def test_an_old_compositor_is_not_a_fresh_start(self):
        self.assertFalse(session.needs_restore(Compositor(pid=1, started=1, age=session.FRESH_S + 1), self.marker))
        self.assertFalse(session.needs_restore(None, self.marker))

    def test_the_compositor_is_found_in_proc(self):
        with tempfile.TemporaryDirectory() as proc:
            Path(proc, "uptime").write_text("1000.00 5000.00\n")
            for pid, comm, start in (("4708", "cosmic-comp", 100 * os.sysconf("SC_CLK_TCK")), ("5000", "zsh", 1)):
                Path(proc, pid).mkdir()
                Path(proc, pid, "comm").write_text(comm + "\n")
                fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
                Path(proc, pid, "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
            comp = session.compositor(proc)
            self.assertEqual(comp.pid, 4708)
            self.assertAlmostEqual(comp.age, 900.0)


if __name__ == "__main__":
    unittest.main()
