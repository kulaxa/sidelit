#!/usr/bin/env python3
"""Native messaging host for the Sidelit Chrome extension.

Chrome starts one of these per profile when the extension connects, and talks to it over
stdin/stdout (a 4-byte little-endian length, then JSON). It relays:
  extension -> daemon   every message, as a `chrome` request on the daemon socket
  daemon -> extension   `chrome` events pushed to this connection (open a window, report)
It outlives daemon restarts: it reconnects, says hello again and asks for a fresh report.
Standard library only; it exits when Chrome closes the port.
"""
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time


def socket_path() -> str:
    home = os.environ.get("SIDELIT_HOME")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    if home:  # same rule as config.SOCKET
        return os.path.join(runtime, f"sidelit-{hashlib.sha1(home.encode()).hexdigest()[:8]}.sock")
    return os.path.join(runtime, "sidelit", "daemon.sock")


def read_message(stream):
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack("<I", head)
    body = stream.read(size)
    if len(body) < size:
        return None
    return json.loads(body)


def encode(msg) -> bytes:
    body = json.dumps(msg, ensure_ascii=False).encode()
    return struct.pack("<I", len(body)) + body


class Host:
    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.out_lock = threading.Lock()
        self.sock_lock = threading.Lock()
        self.sock = None
        self.hello = None     # the extension's hello, replayed on every reconnect
        self.last = None      # its last report, sent as soon as we are connected
        self.next_id = 1
        self.done = False

    def to_extension(self, msg):
        with self.out_lock:
            self.stdout.write(encode(msg))
            self.stdout.flush()

    def to_daemon(self, msg) -> bool:
        with self.sock_lock:
            if self.sock is None:
                return False
            line = json.dumps({"id": self.next_id, "method": "chrome", "params": msg}) + "\n"
            self.next_id += 1
            try:
                self.sock.sendall(line.encode())
                return True
            except OSError:
                self._close()
                return False

    def _close(self):
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass
        self.sock = None

    def daemon_loop(self):
        """Stay connected; forward pushed commands to the extension."""
        while not self.done:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(socket_path())
            except OSError:
                time.sleep(2)
                continue
            with self.sock_lock:  # whoever sees the other first sends the hello: never both
                self.sock = s
                hello, last = self.hello, self.last
            if hello:
                self.to_daemon(hello)
                if last:
                    self.to_daemon(last)
                self.to_extension({"cmd": "snapshot"})
            buf = b""
            while not self.done:
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("event") == "chrome" and isinstance(msg.get("data"), dict):
                        self.to_extension(msg["data"])
            with self.sock_lock:
                if self.sock is s:
                    self._close()
            time.sleep(1)

    def run(self):
        threading.Thread(target=self.daemon_loop, daemon=True).start()
        while True:
            try:
                msg = read_message(self.stdin)
            except (OSError, ValueError):
                msg = None
            if msg is None:
                self.done = True
                return 0
            if not isinstance(msg, dict):
                continue
            with self.sock_lock:
                if msg.get("type") == "hello":
                    self.hello = msg
                elif msg.get("type") == "windows":
                    self.last = msg
                connected = self.sock is not None
            if connected:
                self.to_daemon(msg)


def main() -> int:
    return Host(sys.stdin.buffer, sys.stdout.buffer).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
