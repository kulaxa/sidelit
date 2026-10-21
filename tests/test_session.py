"""A restore once per compositor: after a login, and after COSMIC restarted itself."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import session  # noqa: E402
from sidelit.session import Compositor  # noqa: E402

WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":1"}


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
            comp = session.compositor(proc, WAYLAND)
            self.assertEqual(comp.pid, 4708)
            self.assertAlmostEqual(comp.age, 900.0)

    def test_gnome_shell_is_gnomes_compositor(self):
        with tempfile.TemporaryDirectory() as proc:
            Path(proc, "uptime").write_text("1000.00 5000.00\n")
            tick = os.sysconf("SC_CLK_TCK")
            # (its calendar server's name is cut to 15 characters, as /proc/<pid>/comm has it)
            for pid, comm, start in (("2100", "gnome-shell", 990 * tick), ("2200", "gnome-shell-cal", 995 * tick)):
                Path(proc, pid).mkdir()
                Path(proc, pid, "comm").write_text(comm + "\n")
                fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
                Path(proc, pid, "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
            comp = session.compositor(proc, WAYLAND)
            self.assertEqual(comp.pid, 2100)
            self.assertTrue(session.needs_restore(comp, Path(proc, "restored")))


class OurOwnDesktop(unittest.TestCase):
    """A VNC desktop next to the one on the screen: each one's window manager starting is that
    desktop's login, not the other's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = self.tmp.name
        Path(self.proc, "uptime").write_text("1000.00 5000.00\n")
        tick = os.sysconf("SC_CLK_TCK")
        # COSMIC since the login 900 s ago, a VNC Xfce on :5 started 10 s ago, another on :6
        for pid, comm, start, display in (("4708", "cosmic-comp", 100, None), ("9000", "xfwm4", 990, ":5.0"),
                                          ("9100", "xfwm4", 995, ":6")):
            Path(self.proc, pid).mkdir()
            Path(self.proc, pid, "comm").write_text(comm + "\n")
            fields = ["S"] + ["0"] * 18 + [str(start * tick)] + ["0"] * 10
            Path(self.proc, pid, "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
            Path(self.proc, pid, "environ").write_bytes(b"HOME=/home/u\0" + (f"DISPLAY={display}\0".encode() if display else b""))

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_wayland_a_vnc_desktop_starting_is_not_our_login(self):
        comp = session.compositor(self.proc, WAYLAND)
        self.assertEqual(comp.pid, 4708)
        self.assertFalse(session.needs_restore(comp, Path(self.proc, "restored")))

    def test_on_x11_the_window_manager_of_our_display(self):
        self.assertEqual(session.compositor(self.proc, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":5"}).pid, 9000)
        self.assertEqual(session.compositor(self.proc, {"DISPLAY": "localhost:6.0"}).pid, 9100)

    def test_x11_displays_compare_without_screen_or_local_host(self):
        self.assertEqual([session.x11_display(d) for d in (":5.0", "localhost:5", "unix:5.1", "host:5", "", None)],
                         [":5", ":5", ":5", "host:5", "", ""])


if __name__ == "__main__":
    unittest.main()
