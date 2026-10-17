"""The Wayland wire client refuses requests the compositor would disconnect us for."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import wire  # noqa: E402
from sidelit.wsclient import protocol_files  # noqa: E402


class DestroyedObjects(unittest.TestCase):
    def test_a_request_naming_a_destroyed_object_is_refused_before_it_is_sent(self):
        """cosmic-comp answers `Unknown id` and hangs up: a window that closed a moment ago
        must never be named in a close or move request."""
        d = wire.Display(protocol_files())
        manager = d._new_proxy(d.protocol["zcosmic_toplevel_manager_v1"], 4)
        handle = d._new_proxy(d.protocol["zcosmic_toplevel_handle_v1"], 3)
        manager.close(handle)
        queued = len(d._outbuf)
        handle.destroy()
        with self.assertRaises(wire.WaylandError):
            manager.close(handle)
        self.assertEqual(len(d._outbuf), queued + 8)  # only the destroy went out
        self.assertIn("close", d.sent[0])


if __name__ == "__main__":
    unittest.main()
