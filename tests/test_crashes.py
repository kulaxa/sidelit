"""Reading the crash reporter's log."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import crashes  # noqa: E402

LOG = """\
ERROR: apport (pid 32329) 2026-09-12 16:04:21,042: host pid 31919 crashed in a separate mount namespace, ignoring
INFO: apport (pid 870551) 2026-10-03 19:26:59,900: called for global pid 829290, signal 11, core limit 0, dump mode 1
ERROR: apport (pid 870551) 2026-10-03 19:27:00,905: gdbus call error: Error: GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name is not activatable

INFO: apport (pid 870551) 2026-10-03 19:27:00,909: executable: /usr/bin/cosmic-settings (command line "/usr/bin/cosmic-settings")
ERROR: apport (pid 870551) 2026-10-03 19:27:00,917: report /var/crash/_usr_bin_cosmic-settings.1000.crash already exists and unseen, skipping to avoid disk usage DoS
"""


def at(stamp):
    return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))


class Apport(unittest.TestCase):
    def test_each_crash_with_its_executable(self):
        found = crashes.parse_apport(LOG, 0)
        self.assertEqual([(e.pid, e.exe, e.crashed) for e in found],
                         [(31919, "", True), (829290, "cosmic-settings", True)])
        self.assertAlmostEqual(found[1].t, at("2026-10-03 19:26:59") + 0.9)

    def test_only_since(self):
        self.assertEqual([e.pid for e in crashes.parse_apport(LOG, at("2026-10-01 00:00:00"))], [829290])


if __name__ == "__main__":
    unittest.main()
