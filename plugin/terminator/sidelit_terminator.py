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

It also keeps <pid>.json beside the socket: each of its windows, with its title and the terminals
in it, and since when it has had just those ({"windows": [{"title", "terminals", "since"}]}).
Nothing outside Terminator can tell which of its windows a terminal is in: they are all in one
process, so a pane dragged to another window would lose its box until you typed into it again
(sidelit/detect.py reads the file: terminator.windows()).

It must never get in Terminator's way: standard library and Terminator's own modules only,
nothing raised, nothing printed.
"""
import json
import os
import socket
import time

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
        self.map_path, self.timer, self.written = None, 0, None
        self.since = {}  # window uuid -> (its terminals, since when)
        folder = _folder()
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
        except OSError:
            return
        self.map_path = os.path.join(folder, "%d.json" % os.getpid())
        self.timer = GLib.timeout_add_seconds(1, self._publish)
        try:
            self.path = os.path.join(folder, "%d.sock" % os.getpid())
            if os.path.exists(self.path):
                os.unlink(self.path)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC)
            self.sock.bind(self.path)
            os.chmod(self.path, 0o600)
            self.watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_message)
        except OSError:
            self._close_socket()

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

    def _publish(self):
        """Write <pid>.json when what it says changed (every second it looks)."""
        try:
            now = time.time()
            found = {}  # window uuid -> (title, [terminal uuids])
            for term in Terminator().terminals:
                top = term.get_toplevel()
                key = getattr(getattr(top, "uuid", None), "urn", None)
                if key:
                    found.setdefault(key, (top.get_title() or "", []))[1].append(term.uuid.urn)
            windows = []
            for key, (title, terms) in sorted(found.items()):
                terms = sorted(terms)
                was = self.since.get(key)
                if was is None or was[0] != terms:
                    self.since[key] = (terms, now)
                windows.append({"title": title, "terminals": terms, "since": self.since[key][1]})
            for key in [k for k in self.since if k not in found]:
                del self.since[key]
            data = json.dumps({"windows": windows})
            if data != self.written:
                tmp = self.map_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self.map_path)
                self.written = data
        except Exception:  # (a Terminator whose insides differ: no file, as before)
            pass
        return True

    def _close(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.map_path:
            try:
                os.unlink(self.map_path)
            except OSError:
                pass
            self.map_path = None
        self._close_socket()

    def _close_socket(self):
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
