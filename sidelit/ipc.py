"""The daemon's socket: newline-delimited JSON over $XDG_RUNTIME_DIR/sidelit/daemon.sock.

  request   {"id": 1, "method": "rename", "params": {...}}
  reply     {"id": 1, "result": ...}  or  {"id": 1, "error": "..."}
  push      {"event": "state", "data": {...}}   to clients that sent "subscribe"

The server and the sidebar's client run on a GLib main loop; `call` is a blocking
one-shot for the CLI.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
from pathlib import Path
from typing import Callable

from sidelit.config import SOCKET

MAX_LINE = 4 * 1024 * 1024


class _Conn:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbuf = b""
        self.outbuf = b""
        self.subscribed = False
        self.watch_in = None
        self.watch_out = None


class Server:
    def __init__(self, handler: Callable[[str, dict, "_Conn"], object], path: Path = SOCKET):
        from gi.repository import GLib
        self.GLib = GLib
        self.handler = handler
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.sock.listen(16)
        self.sock.setblocking(False)
        self.conns: list[_Conn] = []
        self.on_drop: Callable[["_Conn"], None] | None = None  # a client went away
        GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._accept)

    def close(self):
        for c in list(self.conns):
            self._drop(c)
        try:
            self.sock.close()
            self.path.unlink()
        except OSError:
            pass

    def _accept(self, *_):
        try:
            s, _ = self.sock.accept()
        except OSError:
            return True
        s.setblocking(False)
        c = _Conn(s)
        self.conns.append(c)
        c.watch_in = self.GLib.io_add_watch(s.fileno(), self.GLib.PRIORITY_DEFAULT,
                                            self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR,
                                            lambda *a, c=c: self._read(c))
        return True

    def _drop(self, c: _Conn):
        for w in (c.watch_in, c.watch_out):
            if w:
                self.GLib.source_remove(w)
        c.watch_in = c.watch_out = None
        try:
            c.sock.close()
        except OSError:
            pass
        if c in self.conns:
            self.conns.remove(c)
            if self.on_drop is not None:
                try:
                    self.on_drop(c)
                except Exception as e:  # noqa: BLE001
                    print(f"ipc: on_drop: {e}", file=sys.stderr, flush=True)

    def _read(self, c: _Conn):
        try:
            data = c.sock.recv(65536)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:
            c.watch_in = None
            self._drop(c)
            return False
        c.inbuf += data
        if len(c.inbuf) > MAX_LINE:
            self._drop(c)
            return False
        while b"\n" in c.inbuf:
            line, c.inbuf = c.inbuf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line)
                method = str(req.get("method") or "")
                params = req.get("params") or {}
            except ValueError:
                self._send(c, {"error": "bad request"})
                continue
            rid = req.get("id")
            if method == "subscribe":
                c.subscribed = True
                self._send(c, {"id": rid, "result": True})
                try:
                    self.handler("subscribed", {}, c)
                except Exception as e:  # noqa: BLE001
                    print(f"ipc: subscribe handler: {e}", file=sys.stderr, flush=True)
                continue
            try:
                result = self.handler(method, params, c)
                self._send(c, {"id": rid, "result": result})
            except (ValueError, KeyError, RuntimeError) as e:  # a bad or impossible request
                self._send(c, {"id": rid, "error": str(e) if not isinstance(e, KeyError) else f"missing {e}"})
            except Exception as e:  # noqa: BLE001 - a bug: log it, and still answer
                import traceback
                traceback.print_exc()
                self._send(c, {"id": rid, "error": f"{type(e).__name__}: {e}"})
        return True

    def _send(self, c: _Conn, msg: dict):
        if c not in self.conns:
            return
        c.outbuf += (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode()
        self._flush(c)

    def _flush(self, c: _Conn):
        while c.outbuf:
            try:
                n = c.sock.send(c.outbuf)
            except BlockingIOError:
                break
            except OSError:
                self._drop(c)
                return False
            c.outbuf = c.outbuf[n:]
        if c.outbuf and c.watch_out is None:
            c.watch_out = self.GLib.io_add_watch(c.sock.fileno(), self.GLib.PRIORITY_DEFAULT, self.GLib.IO_OUT,
                                                 lambda *a, c=c: self._on_writable(c))
        if len(c.outbuf) > 16 * MAX_LINE:  # a subscriber that stopped reading
            self._drop(c)
        return False

    def _on_writable(self, c: _Conn):
        c.watch_out = None
        self._flush(c)
        return False

    def send_event(self, conn: "_Conn", event: str, data) -> bool:
        """An event to one client, subscribed or not (the Chrome host's commands)."""
        if conn not in self.conns:
            return False
        self._send(conn, {"event": event, "data": data})
        return True

    def push(self, event: str, data, only: "_Conn | None" = None):
        for c in list(self.conns):
            if c.subscribed and (only is None or c is only):
                self._send(c, {"event": event, "data": data})


def call(method: str, timeout: float = 10.0, path: Path = SOCKET, **params):
    """One request to the daemon. Raises ConnectionError when it is not running."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except OSError as e:
        raise ConnectionError(f"the daemon is not running ({e.strerror or e})") from e
    try:
        s.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("the daemon closed the connection")
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0])
    finally:
        s.close()
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply.get("result")


class Client:
    """A subscribed connection for the sidebar, on the GLib loop: reconnects by itself,
    `on_state(data)` for every push, `on_connection(bool)` when it comes and goes."""

    def __init__(self, on_state: Callable[[dict], None], on_connection: Callable[[bool], None] | None = None,
                 path: Path = SOCKET):
        from gi.repository import GLib
        self.GLib = GLib
        self.path = Path(path)
        self.on_state = on_state
        self.on_connection = on_connection or (lambda up: None)
        self.sock = None
        self.buf = b""
        self._next = 1
        self._callbacks: dict[int, Callable] = {}
        self.connected = False
        self._watch = None
        self._connect()

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(self.path))
        except OSError:
            s.close()
            self.GLib.timeout_add(1000, self._retry)
            return
        s.setblocking(False)
        self.sock = s
        self.buf = b""
        self.connected = True
        self._watch = self.GLib.io_add_watch(s.fileno(), self.GLib.PRIORITY_DEFAULT,
                                             self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, self._read)
        self._raw({"id": 0, "method": "subscribe"})
        self.on_connection(True)

    def _retry(self):
        self._connect()
        return False

    def _lost(self):
        if self._watch:
            self.GLib.source_remove(self._watch)
            self._watch = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        was = self.connected
        self.connected = False
        for cb in self._callbacks.values():
            try:
                cb(None, "the daemon went away")
            except Exception:  # noqa: BLE001
                pass
        self._callbacks.clear()
        if was:
            self.on_connection(False)
        self.GLib.timeout_add(1000, self._retry)

    def _read(self, *_):
        try:
            data = self.sock.recv(1 << 20)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:
            self._watch = None
            self._lost()
            return False
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "state":
                self.on_state(msg.get("data") or {})
            elif msg.get("id") in self._callbacks:
                cb = self._callbacks.pop(msg["id"])
                cb(msg.get("result"), msg.get("error"))
        return True

    def _raw(self, msg: dict):
        try:
            self.sock.setblocking(True)
            self.sock.sendall((json.dumps(msg) + "\n").encode())
            self.sock.setblocking(False)
        except OSError as e:
            if e.errno not in (errno.EAGAIN,):
                self._lost()

    def request(self, method: str, callback: Callable | None = None, **params):
        """Send a command; `callback(result, error)` when it is answered."""
        if not self.connected:
            if callback:
                callback(None, "the daemon is not running")
            return
        rid = self._next
        self._next += 1
        if callback:
            self._callbacks[rid] = callback
        self._raw({"id": rid, "method": method, "params": params})
