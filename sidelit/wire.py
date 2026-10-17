"""Minimal pure-Python Wayland client: enough of the wire protocol to drive
compositor-side extensions (workspaces, toplevels) from protocol XML files.

No compiled dependencies: the protocol XML is parsed at runtime and every request
and event is marshalled here. Only the client side is implemented. Semantics follow
libwayland where it matters: client object ids are allocated from 1 upwards
(reused after wl_display.delete_id), server-created ids live above 0xff000000,
a destructor request removes the object, and events are dispatched to per-object
callbacks registered in ``proxy.dispatcher[event_name]`` with the proxy as the
first argument, followed by the event arguments.

Usage::

    display = Display(["/usr/share/wayland/wayland.xml", "protocols/ext-workspace-v1.xml"])
    display.connect()
    registry = display.get_registry()
    registry.dispatcher["global"] = lambda reg, name, iface, ver: ...
    display.roundtrip()
    manager = registry.bind(name, "ext_workspace_manager_v1", 1)
"""
from __future__ import annotations

import array
import os
import select
import socket
import struct
import xml.etree.ElementTree as ET
from collections import deque
from typing import Callable, Optional

SERVER_ID_BASE = 0xFF000000


class WaylandError(Exception):
    pass


# --------------------------------------------------------------------------- protocol model
class Arg:
    __slots__ = ("name", "type", "interface", "allow_null")

    def __init__(self, el):
        self.name = el.get("name")
        self.type = el.get("type")
        self.interface = el.get("interface")
        self.allow_null = el.get("allow-null") == "true"


class Message:
    __slots__ = ("name", "opcode", "args", "destructor", "since")

    def __init__(self, el, opcode):
        self.name = el.get("name")
        self.opcode = opcode
        self.args = [Arg(a) for a in el.findall("arg")]
        self.destructor = el.get("type") == "destructor"
        self.since = int(el.get("since", "1"))


class Interface:
    def __init__(self, el):
        self.name = el.get("name")
        self.version = int(el.get("version", "1"))
        self.requests = [Message(r, i) for i, r in enumerate(el.findall("request"))]
        self.events = [Message(e, i) for i, e in enumerate(el.findall("event"))]
        self.request_by_name = {m.name: m for m in self.requests}


class Protocol:
    """All interfaces from a set of protocol XML files."""

    def __init__(self, xml_files):
        self.interfaces: dict[str, Interface] = {}
        for path in xml_files:
            root = ET.parse(path).getroot()
            for el in root.findall("interface"):
                iface = Interface(el)
                self.interfaces[iface.name] = iface

    def __getitem__(self, name) -> Interface:
        return self.interfaces[name]


# --------------------------------------------------------------------------- proxies
class Proxy:
    """A client-side object. Requests are methods (generated from the protocol);
    events call ``self.dispatcher[name](self, *args)``."""

    def __init__(self, display: "Display", iface: Interface, oid: int, version: int):
        self.display = display
        self.interface = iface
        self.id = oid
        self.version = version
        self.dispatcher: dict[str, Callable] = {}
        self.destroyed = False
        self.user_data = None

    def __repr__(self):
        return f"<{self.interface.name}#{self.id}>"

    def __getattr__(self, name):
        iface = self.__dict__.get("interface")
        if iface is not None and name in iface.request_by_name:
            msg = iface.request_by_name[name]
            return lambda *args: self.display._send_request(self, msg, args)
        raise AttributeError(name)

    def destroy(self):
        """Send the destructor request if the interface has one, else just forget the object."""
        if self.destroyed:
            return
        msg = self.interface.request_by_name.get("destroy")
        if msg is not None and msg.destructor:
            self.display._send_request(self, msg, ())
        else:
            self.display._forget(self)


class Display:
    def __init__(self, xml_files, socket_path: Optional[str] = None):
        self.protocol = Protocol(xml_files)
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None
        self.objects: dict[int, Proxy] = {}
        self._free_ids: list[int] = []
        self._next_id = 2
        self._inbuf = bytearray()
        self._outbuf = bytearray()
        self._fds_in: deque[int] = deque()
        self._error: Optional[str] = None
        self.sent: deque[str] = deque(maxlen=30)  # the last requests, for the log when the compositor objects
        # object 1 is wl_display itself
        self.display_proxy = Proxy(self, self.protocol["wl_display"], 1, 1)
        self.objects[1] = self.display_proxy
        self.display_proxy.dispatcher["error"] = self._on_error
        self.display_proxy.dispatcher["delete_id"] = self._on_delete_id

    # ---- connection ---------------------------------------------------------
    def connect(self):
        path = self.socket_path
        if path is None:
            name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
            path = name if name.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], name)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.setblocking(True)

    def disconnect(self):
        if self.sock is not None:
            try:
                self.flush()
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def get_fd(self) -> int:
        return self.sock.fileno()

    # ---- ids ----------------------------------------------------------------
    def _alloc_id(self) -> int:
        if self._free_ids:
            return self._free_ids.pop()
        oid = self._next_id
        self._next_id += 1
        return oid

    def _new_proxy(self, iface: Interface, version: int, oid: Optional[int] = None) -> Proxy:
        if oid is None:
            oid = self._alloc_id()
        proxy = Proxy(self, iface, oid, version)
        self.objects[oid] = proxy
        return proxy

    def _forget(self, proxy: Proxy):
        proxy.destroyed = True
        self.objects.pop(proxy.id, None)
        if proxy.id >= SERVER_ID_BASE:
            return  # server-allocated ids are never reused by us

    def _on_delete_id(self, _display, oid):
        self.objects.pop(oid, None)
        if oid < SERVER_ID_BASE:
            self._free_ids.append(oid)

    def _on_error(self, _display, obj, code, message):
        self._error = f"wayland protocol error on {obj!r}: code {code}: {message}"
        raise WaylandError(self._error)

    # ---- convenience --------------------------------------------------------
    def get_registry(self) -> Proxy:
        return self.display_proxy.get_registry()

    def sync(self) -> Proxy:
        return self.display_proxy.sync()

    def roundtrip(self):
        """Send everything queued and wait until the compositor has processed it."""
        done = [False]
        cb = self.sync()
        cb.dispatcher["done"] = lambda c, serial: done.__setitem__(0, True)
        self.flush()
        while not done[0]:
            self._read_blocking()
            self._dispatch_pending()
            if self._error:
                raise WaylandError(self._error)

    def dispatch(self, block: bool = True):
        """Read available events and dispatch them. With block=True wait for at least one read."""
        if block:
            self._read_blocking()
        else:
            self._read_nonblocking()
        self._dispatch_pending()

    def flush(self):
        while self._outbuf:
            sent = self.sock.send(bytes(self._outbuf))
            del self._outbuf[:sent]

    # ---- marshalling: requests ---------------------------------------------
    def _send_request(self, proxy: Proxy, msg: Message, args):
        if proxy.destroyed:
            raise WaylandError(f"request {msg.name} on destroyed {proxy!r}")
        body = bytearray()
        new_proxy = None
        ai = 0
        for arg in msg.args:
            t = arg.type
            if t == "new_id":
                if arg.interface is None:
                    # wl_registry.bind style: (name, interface_name, version, new_id) — the caller
                    # passes name, interface_name, version; we allocate the id
                    iface_name, version = args[ai], int(args[ai + 1])
                    ai += 2
                    new_proxy = self._new_proxy(self.protocol[iface_name], version)
                    body += _pack_string(iface_name)
                    body += struct.pack("<I", version)
                    body += struct.pack("<I", new_proxy.id)
                else:
                    new_proxy = self._new_proxy(self.protocol[arg.interface], proxy.version)
                    body += struct.pack("<I", new_proxy.id)
                continue
            value = args[ai]
            ai += 1
            if t in ("int",):
                body += struct.pack("<i", int(value))
            elif t in ("uint",):
                body += struct.pack("<I", int(value))
            elif t == "fixed":
                body += struct.pack("<i", int(round(float(value) * 256)))
            elif t == "string":
                body += _pack_string(value) if value is not None else struct.pack("<I", 0)
            elif t == "object":
                if value is not None and value.destroyed:
                    # its id may already be freed or reused: the compositor would disconnect us for it
                    raise WaylandError(f"request {msg.name} on {proxy!r} names destroyed {value!r}")
                body += struct.pack("<I", 0 if value is None else value.id)
            elif t == "array":
                data = bytes(value or b"")
                body += struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)
            elif t == "fd":
                raise WaylandError("sending fds is not supported")
            else:
                raise WaylandError(f"unknown arg type {t}")
        size = 8 + len(body)
        self.sent.append(f"{proxy!r}.{msg.name}({', '.join(repr(a) for a in args)})")
        self._outbuf += struct.pack("<II", proxy.id, (size << 16) | msg.opcode) + body
        if msg.destructor:
            self._forget(proxy)
        return new_proxy

    # ---- reading: events ----------------------------------------------------
    def _read_blocking(self):
        self.flush()
        select.select([self.sock], [], [])
        self._recv()

    def _read_nonblocking(self):
        self.flush()
        r, _, _ = select.select([self.sock], [], [], 0)
        if r:
            self._recv()

    def _recv(self):
        fds = array.array("i")
        data, ancdata, _, _ = self.sock.recvmsg(65536, socket.CMSG_SPACE(64 * fds.itemsize))
        if not data:
            raise WaylandError("connection closed by compositor")
        for level, typ, cdata in ancdata:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds.frombytes(cdata[: len(cdata) - len(cdata) % fds.itemsize])
        self._fds_in.extend(fds)
        self._inbuf += data

    def _dispatch_pending(self):
        buf = self._inbuf
        while len(buf) >= 8:
            oid, size_op = struct.unpack_from("<II", buf, 0)
            size, opcode = size_op >> 16, size_op & 0xFFFF
            if size < 8 or len(buf) < size:
                break
            body = bytes(buf[8:size])
            del buf[:size]
            self._dispatch_event(oid, opcode, body)

    def _dispatch_event(self, oid: int, opcode: int, body: bytes):
        proxy = self.objects.get(oid)
        if proxy is None:
            return  # event for an object we already destroyed
        events = proxy.interface.events
        if opcode >= len(events):
            return
        msg = events[opcode]
        args = []
        off = 0
        for arg in msg.args:
            t = arg.type
            if t in ("int", "uint", "fixed", "object", "new_id"):
                (raw,) = struct.unpack_from("<i" if t == "int" else "<I", body, off)
                off += 4
                if t == "fixed":
                    args.append(raw / 256.0)
                elif t == "object":
                    args.append(self.objects.get(raw) if raw else None)
                elif t == "new_id":
                    args.append(self._new_proxy(self.protocol[arg.interface], proxy.version, raw))
                else:
                    args.append(raw)
            elif t == "string":
                (length,) = struct.unpack_from("<I", body, off)
                off += 4
                if length == 0:
                    args.append(None)
                else:
                    args.append(body[off : off + length - 1].decode("utf-8", "replace"))
                    off += length + (-length % 4)
            elif t == "array":
                (length,) = struct.unpack_from("<I", body, off)
                off += 4
                args.append(body[off : off + length])
                off += length + (-length % 4)
            elif t == "fd":
                args.append(self._fds_in.popleft() if self._fds_in else -1)
            else:
                raise WaylandError(f"unknown arg type {t}")
        handler = proxy.dispatcher.get(msg.name)
        if handler is not None:
            handler(proxy, *args)
        if msg.destructor:
            self._forget(proxy)


def _pack_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\0"
    return struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)
