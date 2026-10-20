# Sidelit's Terminator plugin: lets the Sidelit sidebar bring one tab or split forward.
"""Terminator has no way to focus one of its terminals from outside: its D-Bus interface lists
and splits them but cannot focus one, and the terminals Sidelit starts run without it (-u). So a
click on a session's box could only bring its window forward, and with two sessions in one
window (tabs, or splits) you picked the pane yourself.

`sidelit hooks install` copies this file into Terminator's plugin folder
(~/.config/terminator/plugins/) and enables it in Terminator's config. Each Terminator process
then listens on a socket of its own, $XDG_RUNTIME_DIR/sidelit/terminator/<pid>.sock, for
{"focus": "<TERMINATOR_UUID>"} (the variable each of its terminals has, which Sidelit's hook
records), and shows that terminal: its tab comes to the front, and the keyboard goes to it.

It must never get in Terminator's way: standard library and Terminator's own modules only,
nothing raised, nothing printed.
"""
import json
import os
import socket

from gi.repository import GLib

import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator

AVAILABLE = ["SidelitTerminator"]


def _folder():
    """Where Sidelit keeps its sockets (the same rule as Sidelit's config.RUNTIME_DIR)."""
    home = os.environ.get("SIDELIT_HOME")
    return os.path.join(home, "terminator") if home else \
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "sidelit", "terminator")


class SidelitTerminator(plugin.Plugin):
    capabilities = ["sidelit"]

    def __init__(self):
        plugin.Plugin.__init__(self)
        self.sock, self.path, self.watch = None, None, 0
        try:
            folder = _folder()
            os.makedirs(folder, mode=0o700, exist_ok=True)
            self.path = os.path.join(folder, "%d.sock" % os.getpid())
            if os.path.exists(self.path):
                os.unlink(self.path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC)
            self.sock.bind(self.path)
            os.chmod(self.path, 0o600)
            self.watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_message)
        except OSError:
            self._close()

    def _on_message(self, *_):
        try:
            data = self.sock.recv(4096)
            uuid = json.loads(data.decode("utf-8")).get("focus")
        except (OSError, ValueError, AttributeError):
            return True
        try:
            for term in Terminator().terminals:
                if term.uuid.urn == uuid:
                    term.ensure_visible_and_focussed()
                    break
        except Exception:  # (a Terminator whose insides differ: the window still comes forward)
            pass
        return True

    def _close(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.path = None

    def unload(self):
        self._close()
