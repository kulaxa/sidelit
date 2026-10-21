"""bin/sidelit, the launcher: what a test instance (SIDELIT_HOME) touches."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestInstance(unittest.TestCase):
    def test_start_writes_its_session_variables_in_its_own_folder(self):
        """A second desktop (a VNC one) starting a test instance must not rewrite the session
        variables the real Sidelit's units read."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, runtime, fakebin = tmp / "instance", tmp / "run", tmp / "bin"
            for d in (home, runtime, fakebin):
                d.mkdir()
            (fakebin / "systemctl").write_text("#!/bin/sh\nexit 0\n")  # the real Sidelit's units are there
            (fakebin / "systemctl").chmod(0o755)
            env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp), "SIDELIT_HOME": str(home),
                   "XDG_RUNTIME_DIR": str(runtime), "DISPLAY": ":5", "XDG_SESSION_TYPE": "x11"}
            r = subprocess.run(["sh", os.path.join(ROOT, "bin", "sidelit"), "start"], env=env,
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 1)  # not started: the units would run as the real Sidelit
            self.assertIn('DISPLAY=":5"', (home / "session.env").read_text())
            self.assertFalse((runtime / "sidelit" / "session.env").exists())


if __name__ == "__main__":
    unittest.main()
