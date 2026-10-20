"""The test machines' driver (tests/vm/vm.py), the parts that need no machine: the keys, the
screenshot over VNC, the installer's files read from its ISO, the archive slipped into its
initramfs."""
import importlib.util
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "vm", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vm", "vm.py"))
vm = importlib.util.module_from_spec(spec)
sys.modules["vm"] = vm  # (its dataclass looks itself up there)
spec.loader.exec_module(vm)


class Keys(unittest.TestCase):
    def test_a_combo_is_its_keys_in_order(self):
        self.assertEqual(vm.combo("ctrl+alt+t"), [0xffe3, 0xffe9, ord("t")])
        self.assertEqual(vm.combo("Super"), [0xffeb])
        self.assertEqual(vm.combo("Return"), [0xff0d])
        self.assertEqual(vm.combo("shift+F10"), [0xffe1, 0xffc7])
        self.assertEqual(vm.combo("+"), [ord("+")])

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(vm.Fail):
            vm.combo("ctrl+hyper")

    def test_typing_holds_shift_where_a_keyboard_needs_it(self):
        shift = vm.KEYSYMS["shift"]
        self.assertEqual(vm.typed("a!"), [(ord("a"), True), (ord("a"), False),
                                          (shift, True), (ord("!"), True), (ord("!"), False), (shift, False)])
        self.assertEqual(vm.typed("B\n")[:4], [(shift, True), (ord("B"), True), (ord("B"), False), (shift, False)])
        self.assertEqual(vm.typed("\n"), [(0xff0d, True), (0xff0d, False)])
        with self.assertRaises(vm.Fail):
            vm.typed("č")


def read_png(data: bytes):
    """(width, height, rgb) of a PNG as vm.png writes it."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunks[kind] = chunks.get(kind, b"") + data[pos + 8:pos + 8 + length]
        pos += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = [raw[y * (width * 3 + 1):(y + 1) * (width * 3 + 1)] for y in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, b"".join(row[1:] for row in rows)


class FakeVncServer:
    """QEMU's side of RFB, as far as a screenshot goes: a 4×2 screen, sent as two rectangles,
    once after a screen size change when `resize` (QEMU tells the new size first)."""

    def __init__(self, resize=False):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.resize = resize
        self.received = []
        threading.Thread(target=self.serve, daemon=True).start()

    def read(self, conn, n):
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        return data

    def serve(self):
        conn, _ = self.listener.accept()
        self.listener.close()
        with conn:
            conn.sendall(b"RFB 003.008\n")
            self.read(conn, 12)
            conn.sendall(b"\x01\x01")  # one security type: none
            self.read(conn, 1)
            conn.sendall(struct.pack(">I", 0))
            self.read(conn, 1)
            width = 2 if self.resize else 4
            conn.sendall(struct.pack(">HH", width, 2) + b"\x00" * 16 + struct.pack(">I", 4) + b"QEMU")
            self.received.append(self.read(conn, 20))  # SetPixelFormat
            self.received.append(self.read(conn, 4 + 4 * 2))  # SetEncodings (two)
            # pixel (x, y) is R=x*10, G=y*10, B=99, then the unused byte
            pixel = lambda x, y: bytes((x * 10, y * 10, 99, 0))  # noqa: E731
            if self.resize:
                self.received.append(self.read(conn, 10))  # FramebufferUpdateRequest, 2×2
                conn.sendall(b"\x00\x00" + struct.pack(">H", 2) + struct.pack(">HHHHi", 0, 0, 4, 2, -223)
                             + struct.pack(">HHHHi", 0, 0, 2, 2, 0) + b"".join(pixel(x, y) for y in range(2) for x in range(2)))
            self.received.append(self.read(conn, 10))  # FramebufferUpdateRequest, 4×2
            conn.sendall(b"\x02")  # a bell first, which is skipped
            conn.sendall(b"\x00\x00" + struct.pack(">H", 2)
                         + struct.pack(">HHHHi", 0, 0, 4, 1, 0) + b"".join(pixel(x, 0) for x in range(4))
                         + struct.pack(">HHHHi", 1, 1, 3, 1, 0) + b"".join(pixel(x, 1) for x in range(1, 4)))
            self.read(conn, 6)  # a PointerEvent
            self.received.append("pointer")


class Screenshot(unittest.TestCase):
    def check(self, server):
        vnc = vm.Vnc(server.port)
        width, height, rgb = read_png(vnc.screenshot())
        vnc.pointer(1, 1)
        vnc.close()
        self.assertEqual((width, height), (4, 2))
        expected = b"".join(bytes((x * 10, y * 10, 99)) for y in range(2) for x in range(4))
        # (0, 1) was in neither rectangle: black
        expected = expected[:12] + b"\x00\x00\x00" + expected[15:]
        self.assertEqual(rgb, expected)

    def test_the_screen_comes_back_as_a_png_in_its_colours(self):
        server = FakeVncServer()
        self.check(server)
        # red in the lowest byte, 32 bits, true colour; raw and screen size changes only
        self.assertEqual(server.received[0][4:8], bytes((32, 24, 0, 1)))
        self.assertEqual(server.received[0][14:17], bytes((0, 8, 16)))
        self.assertEqual(server.received[1], struct.pack(">BxHii", 2, 2, 0, -223))

    def test_a_screen_that_changed_size_is_asked_for_again_whole(self):
        server = FakeVncServer(resize=True)
        self.check(server)
        self.assertEqual(struct.unpack(">BBHHHH", server.received[2]), (3, 0, 0, 0, 2, 2))
        self.assertEqual(struct.unpack(">BBHHHH", server.received[3]), (3, 0, 0, 0, 4, 2))


def iso(files: dict) -> bytes:
    """A tiny ISO 9660 image with `files` ({'DIR/NAME.EXT;1': data}), all in one folder."""
    def record(name: bytes, lba: int, size: int, folder: bool) -> bytes:
        body = (struct.pack("<I", lba) + struct.pack(">I", lba) + struct.pack("<I", size) + struct.pack(">I", size)
                + b"\x00" * 7 + bytes((2 if folder else 0, 0, 0)) + b"\x01\x00\x00\x01" + bytes((len(name),)) + name)
        body = bytes((0,)) + body
        if len(body) % 2 == 0:
            body += b"\x00"
        return bytes((len(body) + 1,)) + body

    (folder,) = {path.split("/")[0] for path in files}
    # sector 16: the volume descriptor; 18: the root folder; 19: `folder`; 20 on: the files
    image = bytearray(b"\x00" * 2048 * 20)
    root = record(b"\x00", 18, 2048, True) + record(b"\x01", 18, 2048, True) + record(folder.encode(), 19, 2048, True)
    listing, lba = record(b"\x00", 19, 2048, True) + record(b"\x01", 18, 2048, True), 20
    for path, data in files.items():
        listing += record(path.split("/")[1].encode(), lba, len(data), False)
        image += data + b"\x00" * (-len(data) % 2048)
        lba += (len(data) + 2047) // 2048
    image[18 * 2048:18 * 2048 + len(root)] = root
    image[19 * 2048:19 * 2048 + len(listing)] = listing
    image[16 * 2048 + 156:16 * 2048 + 156 + 34] = record(b"\x00", 18, 2048, True)
    return bytes(image)


class Installer(unittest.TestCase):
    def test_a_file_is_read_from_the_iso_by_its_path_in_any_case(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mint.iso"
            path.write_bytes(iso({"CASPER/VMLINUZ.;1": b"kernel" * 1000, "CASPER/INITRD.LZ;1": b"initrd"}))
            self.assertEqual(vm.iso_file(path, "casper/vmlinuz"), b"kernel" * 1000)
            self.assertEqual(vm.iso_file(path, "/casper/initrd.lz"), b"initrd")
            with self.assertRaises(vm.Fail):
                vm.iso_file(path, "casper/filesystem.squashfs")

    def test_the_preseed_goes_into_the_initramfs_as_a_newc_archive(self):
        archive = vm.cpio({"preseed.cfg": b"d-i x string y\n"})
        self.assertEqual(len(archive) % 512, 0)
        self.assertEqual(archive[:6], b"070701")
        fields = [int(archive[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        self.assertEqual(fields[1], 0o100644)  # a plain file
        self.assertEqual(fields[6], 15)  # its size
        self.assertEqual(archive[110:110 + fields[11]], b"preseed.cfg\x00")
        start = 110 + fields[11] + (-(110 + fields[11]) % 4)
        self.assertEqual(archive[start:start + 15], b"d-i x string y\n")
        self.assertIn(b"TRAILER!!!\x00", archive)

    def test_each_machine_has_its_own_ports(self):
        ports = [p for m in vm.MACHINES.values() for p in (m.ssh_port, m.vnc_port)]
        self.assertEqual(len(ports), len(set(ports)))
        for m in vm.MACHINES.values():
            self.assertTrue(m.provision or m.url.endswith(".iso"), m.name)


if __name__ == "__main__":
    unittest.main()
