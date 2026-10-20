#!/usr/bin/env python3
"""Test machines: Sidelit on other distributions and desktops, each in a virtual machine (QEMU
with KVM) that logs in by itself, driven from outside: this script installs a checkout there,
runs commands in its desktop session, takes screenshots, clicks and types. Not part of the unit
tests; README.md beside it says how to use it.

    python3 tests/vm/vm.py list
    python3 tests/vm/vm.py build ubuntu-gnome     # once: a clean install, kept as the base
    python3 tests/vm/vm.py start ubuntu-gnome     # a fresh copy of the base, logged in
    python3 tests/vm/vm.py install ubuntu-gnome   # this checkout, with its own get.sh
    python3 tests/vm/vm.py shot ubuntu-gnome      # prints where the screenshot went
    python3 tests/vm/vm.py stop ubuntu-gnome

It needs qemu-system-x86, qemu-utils and (for Hyprland's 3D) qemu-system-modules-opengl, and
read and write access to /dev/kvm. The machines live in $SIDELIT_VMS (default
~/.local/share/sidelit-vms), about 10 to 20 GB each; their ssh key in
~/.local/state/sidelit-vms.
"""
from __future__ import annotations

import argparse
import http.server
import io
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USER = "tester"  # its password is the same; sudo asks for none
SCREEN = (1920, 1080)
DISK = "40G"  # what the guest sees; the file grows only as far as it is used


class Fail(Exception):
    pass


@dataclass(frozen=True)
class Machine:
    name: str
    title: str
    url: str  # the cloud image, or the installer ISO, it is built from
    index: int  # its ports: ssh on 2240 + index, VNC on 5940 + index (both on 127.0.0.1)
    session: str  # the process that is there once the desktop is up
    provision: str = ""  # a cloud image's first boot: bash, as root
    gl: bool = False  # the desktop needs 3D: virgl, drawn by the host's GPU
    memory: int = 4096  # MB

    @property
    def ssh_port(self) -> int:
        return 2240 + self.index

    @property
    def vnc_port(self) -> int:
        return 5940 + self.index

    @property
    def dir(self) -> Path:
        return home() / self.name


# ---- what each machine is ------------------------------------------------------------------------
# The provisioning scripts run with their output on the serial port; a line starting with
# SIDELIT-VM: is a step this script shows while it waits.
PRELUDE = """\
set -euxo pipefail
step() { { set +x; } 2>/dev/null; echo "SIDELIT-VM: $*"; set -x; }
"""

# the screen never blanks nor locks (screenshots show the desktop), for GNOME and Cinnamon
DCONF = """\
mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
if [ ! -f /etc/dconf/profile/user ]; then printf 'user-db:user\\nsystem-db:local\\n' > /etc/dconf/profile/user
elif ! grep -q '^system-db:local' /etc/dconf/profile/user; then echo system-db:local >> /etc/dconf/profile/user
fi
cat > /etc/dconf/db/local.d/00-sidelit-vm <<'EOF'
[org/gnome/desktop/session]
idle-delay=uint32 0
[org/gnome/desktop/screensaver]
lock-enabled=false
[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
[org/cinnamon/desktop/session]
idle-delay=uint32 0
[org/cinnamon/desktop/screensaver]
lock-enabled=false
[org/cinnamon/settings-daemon/plugins/power]
sleep-display-ac=0
sleep-inactive-ac-timeout=0
EOF
dconf update
"""

UBUNTU_GNOME = PRELUDE + """\
export DEBIAN_FRONTEND=noninteractive
apt="apt-get -y -q -o DPkg::Lock::Timeout=900"
step "Installing Ubuntu's desktop (GNOME)"
$apt update
$apt install ubuntu-desktop-minimal dconf-cli
# the cloud image's kernel may lack the virtual GPU's driver
modinfo virtio_gpu >/dev/null 2>&1 || $apt install "linux-modules-extra-$(uname -r)"
step "Logging in by itself, on Wayland"
cat > /etc/gdm3/custom.conf <<'EOF'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=tester
EOF
install -d -o tester -g tester /home/tester/.config
echo yes > /home/tester/.config/gnome-initial-setup-done
chown tester: /home/tester/.config/gnome-initial-setup-done
""" + DCONF + """\
systemctl set-default graphical.target
"""

FEDORA_KDE = PRELUDE + """\
step "Installing KDE Plasma"
dnf -y install @kde-desktop-environment
dnf -y remove plasma-welcome || true
systemctl disable initial-setup.service || true
systemctl set-default graphical.target
step "Logging in by itself"
if [ -e /usr/lib/systemd/system/plasmalogin.service ]; then
    # Plasma Login Manager (Fedora 44 on), SDDM's successor
    cat /usr/lib/plasmalogin/defaults.conf || true
    mkdir -p /etc/plasmalogin.conf.d
    printf '[Autologin]\\nUser=tester\\nSession=plasma\\n' > /etc/plasmalogin.conf.d/90-sidelit-vm.conf
    [ -e /etc/plasmalogin.conf ] || cp /etc/plasmalogin.conf.d/90-sidelit-vm.conf /etc/plasmalogin.conf
    systemctl enable plasmalogin.service
else
    mkdir -p /etc/sddm.conf.d
    printf '[Autologin]\\nUser=tester\\nSession=plasma\\n' > /etc/sddm.conf.d/90-sidelit-vm.conf
    systemctl enable sddm.service
fi
install -d -o tester -g tester /home/tester/.config
cat > /home/tester/.config/kscreenlockerrc <<'EOF'
[Daemon]
Autolock=false
LockOnResume=false
EOF
cat > /home/tester/.config/powerdevilrc <<'EOF'
[AC][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
[AC][SuspendAndShutdown]
AutoSuspendAction=0
EOF
chown tester: /home/tester/.config/kscreenlockerrc /home/tester/.config/powerdevilrc
"""

ARCH_HYPRLAND = PRELUDE + """\
step "Updating Arch"
pacman -Sy --noconfirm --needed archlinux-keyring
pacman -Su --noconfirm
step "Installing Hyprland"
pacman -S --noconfirm --needed hyprland kitty xdg-desktop-portal-hyprland hyprpolkitagent \\
    noto-fonts greetd mesa
ls /usr/share/hypr || true
# its example config as the user's own: as after a first edit, without the banner that says the
# config was made for you
install -d -o tester -g tester /home/tester/.config/hypr
for f in hyprland.lua hyprland.conf; do
    if [ -f "/usr/share/hypr/$f" ]; then
        grep -v autogenerated "/usr/share/hypr/$f" > "/home/tester/.config/hypr/$f"
        chown tester: "/home/tester/.config/hypr/$f"
        break
    fi
done
step "Logging in by itself"
start=Hyprland
! command -v start-hyprland >/dev/null || start=start-hyprland
cat > /etc/greetd/config.toml <<EOF
[terminal]
vt = 1
[default_session]
command = "agreety --cmd $start"
user = "greeter"
[initial_session]
command = "$start"
user = "tester"
EOF
systemctl enable greetd.service
systemctl set-default graphical.target
"""

# Mint has no cloud image: its own installer (ubiquity) runs by itself from this preseed, put
# into the installer's initramfs as /preseed.cfg (casper reads it from there); @URL@ is this
# script's seed server, @KEY@ the ssh key
MINT_PRESEED = """\
d-i debian-installer/locale string en_US.UTF-8
d-i localechooser/supported-locales multiselect en_US.UTF-8
d-i keyboard-configuration/layoutcode string us
d-i keyboard-configuration/xkb-keymap select us
d-i console-setup/ask_detect boolean false
d-i time/zone string Etc/UTC
d-i clock-setup/utc boolean true
d-i netcfg/get_hostname string mint-cinnamon
d-i partman-auto/disk string /dev/vda
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman/confirm_write_new_label boolean true
d-i passwd/user-fullname string Tester
d-i passwd/username string tester
d-i passwd/user-password password tester
d-i passwd/user-password-again password tester
d-i passwd/auto-login boolean true
d-i user-setup/allow-password-weak boolean true
d-i user-setup/encrypt-home boolean false
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string /dev/vda
ubiquity ubiquity/use_nonfree boolean false
ubiquity ubiquity/download_updates boolean false
ubiquity ubiquity/success_command string wget -qO- @URL@/finish.sh | sh
ubiquity ubiquity/reboot boolean true
"""
MINT_CMDLINE = ("boot=casper automatic-ubiquity noprompt username=mint hostname=mint "
                "console=tty0 console=ttyS0,115200 --")

# after the install, still in the installer, the new system mounted on /target: sudo without a
# password, logging in by itself, the ssh key, and ssh itself, installed on the first boot (the
# installer has no copy of it)
MINT_FINISH = """\
set -x
exec > /dev/ttyS0 2>&1
echo "SIDELIT-VM: Mint is installed; setting it up"
T=/target
echo 'tester ALL=(ALL) NOPASSWD: ALL' > $T/etc/sudoers.d/90-sidelit-vm
chmod 440 $T/etc/sudoers.d/90-sidelit-vm
mkdir -p $T/etc/lightdm/lightdm.conf.d
printf '[Seat:*]\\nautologin-user=tester\\nautologin-user-timeout=0\\n' \\
    > $T/etc/lightdm/lightdm.conf.d/90-sidelit-vm.conf
mkdir -p $T/home/tester/.ssh $T/home/tester/.linuxmint/mintwelcome
echo '@KEY@' > $T/home/tester/.ssh/authorized_keys
chmod 700 $T/home/tester/.ssh
touch $T/home/tester/.linuxmint/mintwelcome/norun.flag
chroot $T chown -R tester:tester /home/tester/.ssh /home/tester/.linuxmint
cat > $T/root/sidelit-vm-dconf.sh <<'DCONF'
""" + DCONF + """\
DCONF
chroot $T sh /root/sidelit-vm-dconf.sh
cat > $T/etc/systemd/system/sidelit-vm-ssh.service <<'EOF'
[Unit]
Description=ssh for the test machine's driver
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'apt-get -o DPkg::Lock::Timeout=900 update -q && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 install -y -q openssh-server && systemctl enable --now ssh && systemctl disable sidelit-vm-ssh.service'
[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/sidelit-vm-ssh.service $T/etc/systemd/system/multi-user.target.wants/
echo "SIDELIT-VM: set up"
"""

MACHINES = {m.name: m for m in (
    Machine("ubuntu-gnome", "Ubuntu 24.04, GNOME 46 (Wayland)",
            "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
            1, "gnome-shell", UBUNTU_GNOME),
    Machine("fedora-kde", "Fedora 44, KDE Plasma 6 (Wayland)",
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/"
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
            2, "plasmashell", FEDORA_KDE),
    Machine("mint-cinnamon", "Linux Mint 22.3, Cinnamon (X11)",
            "https://pub.linuxmint.io/stable/22.3/linuxmint-22.3-cinnamon-64bit.iso",
            3, "cinnamon"),
    Machine("arch-hyprland", "Arch Linux, Hyprland (Wayland)",
            "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
            4, "Hyprland", ARCH_HYPRLAND, gl=True),
)}


# ---- where things are ----------------------------------------------------------------------------
def home() -> Path:
    path = Path(os.environ.get("SIDELIT_VMS") or Path.home() / ".local/share/sidelit-vms")
    if path.is_symlink() and not path.exists():
        raise Fail(f"{path} points to {os.readlink(path)}, which is not there (a disk not plugged in?)")
    return path


def runtime() -> Path:
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "sidelit-vms"
    path.mkdir(parents=True, exist_ok=True)
    return path


def key() -> Path:
    """The ssh key every machine lets in (on the home disk: ssh wants it private, which a disk
    without Unix permissions cannot say)."""
    path = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sidelit-vms/key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "sidelit-vm", "-f", str(path)],
                       check=True)
    return path


def say(text: str):
    print(f"→ {text}", flush=True)


# ---- QEMU ----------------------------------------------------------------------------------------
def rendernode() -> str:
    """The GPU that draws a 3D machine's screen: SIDELIT_VM_RENDERNODE, else the first that is not
    NVIDIA's (virgl is at home on Mesa), else the first."""
    if os.environ.get("SIDELIT_VM_RENDERNODE"):
        return os.environ["SIDELIT_VM_RENDERNODE"]
    nodes = sorted(Path("/dev/dri").glob("renderD*"))
    if not nodes:
        raise Fail("No GPU render node in /dev/dri: a 3D machine needs one.")
    for node in nodes:
        driver = Path(f"/sys/class/drm/{node.name}/device/driver")
        if driver.exists() and driver.resolve().name != "nvidia":
            return str(node)
    return str(nodes[0])


def qemu_args(m: Machine, disk: Path, *, window: bool) -> list[str]:
    gpu = "virtio-vga-gl" if m.gl else "virtio-vga"
    if window:
        display = "gtk,gl=on" if m.gl else "gtk"
    else:
        display = f"egl-headless,rendernode={rendernode()}" if m.gl else "none"
    run = runtime()
    return [
        "qemu-system-x86_64", "-name", m.name,
        "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", "4", "-m", str(m.memory),
        "-drive", f"file={disk},if=virtio,discard=unmap",
        "-nic", f"user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{m.ssh_port}-:22",
        "-device", "qemu-xhci", "-device", "usb-tablet",
        "-device", f"{gpu},xres={SCREEN[0]},yres={SCREEN[1]}",
        "-display", display,
        # the screen, for screenshots and input (QEMU's own screendump cannot read a 3D screen)
        "-vnc", f"127.0.0.1:{m.vnc_port - 5900}",
        "-qmp", f"unix:{run / (m.name + '.qmp')},server=on,wait=off",
        "-serial", f"file:{m.dir / 'serial.log'}",
        "-pidfile", str(run / (m.name + ".pid")),
    ]


def pid(m: Machine) -> int | None:
    """QEMU's process for `m`, while it runs."""
    try:
        number = int((runtime() / (m.name + ".pid")).read_text().strip())
        cmdline = Path(f"/proc/{number}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return number if b"qemu" in cmdline and m.name.encode() in cmdline else None


def launch(m: Machine, disk: Path, *, extra: list[str] = (), window: bool = False) -> subprocess.Popen:
    if pid(m):
        raise Fail(f"{m.name} is running already: 'vm.py stop {m.name}' first.")
    m.dir.mkdir(parents=True, exist_ok=True)
    (runtime() / (m.name + ".qmp")).unlink(missing_ok=True)
    log = open(m.dir / "qemu.log", "w")
    proc = subprocess.Popen(qemu_args(m, disk, window=window) + list(extra), stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 30
    while not (runtime() / (m.name + ".qmp")).exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise Fail(f"QEMU did not start {m.name}: {(m.dir / 'qemu.log').read_text().strip()}")
        time.sleep(0.2)
    return proc


def qmp(m: Machine, command: str, **arguments):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(30)
        sock.connect(str(runtime() / (m.name + ".qmp")))
        stream = sock.makefile("rw")
        json.loads(stream.readline())  # the greeting
        for message in ({"execute": "qmp_capabilities"}, {"execute": command, "arguments": arguments}):
            stream.write(json.dumps(message) + "\n")
            stream.flush()
            while True:
                reply = json.loads(stream.readline())
                if "error" in reply:
                    raise Fail(f"QEMU: {reply['error'].get('desc')}")
                if "return" in reply:
                    break
        return reply["return"]


def wait_gone(m: Machine, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while pid(m) and time.monotonic() < deadline:
        time.sleep(0.5)
    return not pid(m)


# ---- inside the machine: ssh ----------------------------------------------------------------------
def ssh_command(m: Machine, *, tty: bool = False) -> list[str]:
    return ["ssh", "-i", str(key()), "-p", str(m.ssh_port), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"] + (["-t"] if tty else []) + [f"{USER}@127.0.0.1"]


def ssh(m: Machine, command: str, *, data: bytes | None = None, check: bool = True, capture: bool = True,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(ssh_command(m) + [command], input=data, capture_output=capture,
                                timeout=timeout, stdin=None if data is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        if check:
            raise Fail(f"{m.name} took too long for: {command}")
        return subprocess.CompletedProcess(command, 255, b"", b"timed out")
    if check and result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace").strip()
        raise Fail(f"In {m.name}, '{command}' failed ({result.returncode}){': ' + err if err else ''}")
    return result


# runs `$1` with the desktop session's environment (WAYLAND_DISPLAY or DISPLAY, the session bus,
# XDG_CURRENT_DESKTOP…), which every desktop here gives systemd's user manager
IN_SESSION = 'set -a; eval "$(systemctl --user show-environment)"; set +a; cd; eval "$1"'


def in_session(command: str) -> str:
    return "bash -c " + shlex.quote(IN_SESSION) + " sidelit-vm " + shlex.quote(command)


def wait_ssh(m: Machine, seconds: float, proc: subprocess.Popen | None = None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise Fail(f"{m.name} stopped while starting (its serial log: {m.dir / 'serial.log'})")
        if ssh(m, "true", check=False, timeout=20).returncode == 0:
            return
        time.sleep(3)
    raise Fail(f"{m.name} did not let ssh in within {seconds / 60:.0f} minutes (its serial log: "
               f"{m.dir / 'serial.log'})")


def session_pid(m: Machine) -> str | None:
    """The desktop's main process, once the session's environment is in systemd too."""
    probe = (f"p=$(pgrep -u {USER} -x -o {shlex.quote(m.session)}) && "
             "systemctl --user show-environment | grep -qE '^(WAYLAND_DISPLAY|DISPLAY)=' && echo $p")
    result = ssh(m, probe, check=False, timeout=20)
    return result.stdout.decode().strip() or None if result.returncode == 0 else None


def wait_desktop(m: Machine, seconds: float = 300, *, not_pid: str | None = None) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = session_pid(m)
        if found and found != not_pid:
            time.sleep(5)  # panels, the dock, the wallpaper
            return found
        time.sleep(2)
    raise Fail(f"{m.name}'s desktop did not come up within {seconds / 60:.0f} minutes ('vm.py shot "
               f"{m.name}' shows where it is)")


# ---- the screen: VNC ------------------------------------------------------------------------------
KEYSYMS = {
    "ctrl": 0xffe3, "control": 0xffe3, "shift": 0xffe1, "alt": 0xffe9, "super": 0xffeb, "win": 0xffeb,
    "meta": 0xffeb, "return": 0xff0d, "enter": 0xff0d, "tab": 0xff09, "escape": 0xff1b, "esc": 0xff1b,
    "backspace": 0xff08, "delete": 0xffff, "insert": 0xff63, "home": 0xff50, "end": 0xff57,
    "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54, "pageup": 0xff55,
    "pagedown": 0xff56, "space": 0x20, "print": 0xff61, "menu": 0xff67,
    **{f"f{n}": 0xffbe + n - 1 for n in range(1, 13)},
}
SHIFTED = set('~!@#$%^&*()_+{}|:"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ')
BUTTONS = {"left": 1, "middle": 2, "right": 4}


def combo(text: str) -> list[int]:
    """'ctrl+alt+t' → the keysyms to press in order (and release in reverse)."""
    keys = []
    for part in text.split("+") if text != "+" else ["+"]:
        if part.lower() in KEYSYMS:
            keys.append(KEYSYMS[part.lower()])
        elif len(part) == 1 and 0x20 <= ord(part) < 0x7f:
            keys.append(ord(part))
        else:
            raise Fail(f"No key called {part!r} (keys: a letter, a digit, {', '.join(sorted(KEYSYMS))})")
    return keys


def typed(text: str) -> list[tuple[int, bool]]:
    """`text` as (keysym, down) events, with Shift held where a keyboard needs it."""
    events = []
    for char in text:
        if char == "\n":
            sym = KEYSYMS["return"]
        elif char == "\t":
            sym = KEYSYMS["tab"]
        elif 0x20 <= ord(char) < 0x7f:
            sym = ord(char)
        else:
            raise Fail(f"Cannot type {char!r}: only ASCII")
        shift = char in SHIFTED
        if shift:
            events.append((KEYSYMS["shift"], True))
        events += [(sym, True), (sym, False)]
        if shift:
            events.append((KEYSYMS["shift"], False))
    return events


def png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    stride = width * 3
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


class Vnc:
    """Just enough of RFB (the VNC protocol) for a screenshot, the pointer and the keys."""

    def __init__(self, port: int):
        try:
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        except OSError:
            raise Fail(f"No screen on port {port}: is the machine running?")
        self.read(12)
        self.sock.sendall(b"RFB 003.008\n")
        if 1 not in self.read(self.read(1)[0]):
            raise Fail("The VNC server wants a password")
        self.sock.sendall(b"\x01")
        if struct.unpack(">I", self.read(4))[0] != 0:
            raise Fail("The VNC server refused")
        self.sock.sendall(b"\x01")  # shared
        self.width, self.height = struct.unpack(">HH", self.read(4))
        self.read(16)
        self.read(struct.unpack(">I", self.read(4))[0])
        # 32 bits a pixel, little-endian, red in the lowest byte: R, G, B, unused
        self.sock.sendall(struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 0, 8, 16))
        self.sock.sendall(struct.pack(">BxHii", 2, 2, 0, -223))  # raw, and screen size changes

    def read(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            part = self.sock.recv(n - len(data))
            if not part:
                raise Fail("The VNC connection closed")
            data += part
        return bytes(data)

    def close(self):
        self.sock.close()

    def screenshot(self) -> bytes:
        while True:
            self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
            frame, resized = bytearray(self.width * self.height * 4), False
            while True:
                kind = self.read(1)[0]
                if kind == 0:
                    break
                if kind == 2:  # bell
                    continue
                if kind == 3:  # clipboard
                    self.read(3)
                    self.read(struct.unpack(">I", self.read(4))[0])
                    continue
                raise Fail(f"Unexpected VNC message {kind}")
            self.read(1)
            for _ in range(struct.unpack(">H", self.read(2))[0]):
                x, y, w, h, encoding = struct.unpack(">HHHHi", self.read(12))
                if encoding == -223:
                    self.width, self.height, resized = w, h, True
                    continue
                data = self.read(w * h * 4)
                if resized:
                    continue
                for row in range(h):
                    start = ((y + row) * self.width + x) * 4
                    frame[start:start + w * 4] = data[row * w * 4:(row + 1) * w * 4]
            if not resized:
                rgb = bytearray(self.width * self.height * 3)
                rgb[0::3], rgb[1::3], rgb[2::3] = frame[0::4], frame[1::4], frame[2::4]
                return png(self.width, self.height, bytes(rgb))

    def pointer(self, x: int, y: int, buttons: int = 0):
        self.sock.sendall(struct.pack(">BBHH", 5, buttons, x, y))

    def key(self, sym: int, down: bool):
        self.sock.sendall(struct.pack(">BBxxI", 4, int(down), sym))


def pause(seconds: float = 0.03):
    time.sleep(seconds)


# ---- building a machine --------------------------------------------------------------------------
class Seed:
    """A small web server for the machine being built: cloud-init's seed, Mint's last steps.
    QEMU's network shows the host's 127.0.0.1 to the guest as 10.0.2.2."""

    def __init__(self, files: dict[str, bytes]):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = files.get(self.path.split("?")[0].lstrip("/"))
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.files = files
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://10.0.2.2:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def download(m: Machine) -> Path:
    folder = home() / "downloads"
    path = folder / m.url.rsplit("/", 1)[1]
    if path.exists():
        return path
    folder.mkdir(parents=True, exist_ok=True)
    say(f"Downloading {m.url}")
    part = path.with_name(path.name + ".part")
    if subprocess.run(["curl", "-fL", "-sS", "--retry", "3", "-C", "-", "-o", str(part), m.url]).returncode:
        raise Fail(f"Could not download {m.url}")
    part.rename(path)
    return path


def follow(m: Machine, proc: subprocess.Popen, seconds: float) -> str:
    """Waits for the machine to power off, showing its steps; returns its serial log."""
    log, shown, deadline = m.dir / "serial.log", 0, time.monotonic() + seconds
    while proc.poll() is None:
        if time.monotonic() > deadline:
            qmp(m, "quit")
            raise Fail(f"{m.name} took over {seconds / 60:.0f} minutes; its serial log: {log}")
        lines = log.read_bytes().decode(errors="replace").splitlines() if log.exists() else []
        for line in lines[shown:]:
            line = line.strip("\r\x1b[0m ")
            if line.startswith("SIDELIT-VM: "):
                say(line[len("SIDELIT-VM: "):])
        shown = len(lines)
        time.sleep(2)
    return log.read_bytes().decode(errors="replace")


def provision_cloud(m: Machine, image: Path, disk: Path):
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(image), str(disk)], check=True)
    subprocess.run(["qemu-img", "resize", "-q", str(disk), DISK], check=True)
    config = {
        "hostname": m.name,
        "users": [{
            "name": USER, "gecos": "Tester", "shell": "/bin/bash", "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "lock_passwd": False, "plain_text_passwd": USER,
            "ssh_authorized_keys": [key().with_suffix(".pub").read_text().strip()],
        }],
        "write_files": [{"path": "/root/sidelit-vm-provision.sh", "permissions": "0755", "content": m.provision}],
        # cloud-init's work ends here: it never runs again, at the base's next boot or a copy's
        "runcmd": [["bash", "-c", "bash /root/sidelit-vm-provision.sh > /dev/ttyS0 2>&1; "
                    "echo SIDELIT-VM-BUILD-$? > /dev/ttyS0; touch /etc/cloud/cloud-init.disabled; poweroff"]],
    }
    files = {
        "meta-data": f"instance-id: sidelit-{m.name}\nlocal-hostname: {m.name}\n".encode(),
        "user-data": ("#cloud-config\n" + json.dumps(config, indent=1)).encode(),
        "vendor-data": b"",
    }
    with Seed(files) as seed:
        say(f"Setting up {m.title} (the first boot installs the desktop: 10 to 40 minutes)")
        proc = launch(m, disk, extra=["-smbios", f"type=1,serial=ds=nocloud;s={seed.url}/", "-no-reboot"])
        serial = follow(m, proc, 3 * 3600)
    if "SIDELIT-VM-BUILD-0" not in serial:
        raise Fail(f"Setting up {m.name} failed; its serial log: {m.dir / 'serial.log'}")


def iso_file(iso: Path, path: str) -> bytes:
    """A file from an ISO 9660 image, by its path, without mounting it (names compared as the
    plain ISO 9660 directory spells them: upper case, with ';1')."""
    with open(iso, "rb") as f:
        def sectors(lba: int, size: int) -> bytes:
            f.seek(lba * 2048)
            return f.read(size)

        root = sectors(16, 2048)[156:190]
        lba, size = struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0]
        for part in path.strip("/").split("/"):
            data, i, found = sectors(lba, size), 0, None
            while i < len(data) and found is None:
                length = data[i]
                if length == 0:  # the rest of this sector is empty
                    i = (i // 2048 + 1) * 2048
                    continue
                record = data[i:i + length]
                name = record[33:33 + record[32]].decode("latin1").split(";")[0].rstrip(".")
                if name.lower() == part.lower():
                    found = struct.unpack("<I", record[2:6])[0], struct.unpack("<I", record[10:14])[0]
                i += length
            if found is None:
                raise Fail(f"{iso.name} has no {path}")
            lba, size = found
        return sectors(lba, size)


def cpio(files: dict[str, bytes]) -> bytes:
    """A "newc" cpio archive: appended to an initramfs, the kernel unpacks it after the rest."""
    out = bytearray()

    def entry(name: str, data: bytes, mode: int):
        fields = [len(out) // 4 + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0]
        out.extend(b"070701" + b"".join(b"%08X" % v for v in fields) + name.encode() + b"\x00")
        out.extend(b"\x00" * (-len(out) % 4) + data + b"\x00" * (-len(data) % 4))

    for name, data in files.items():
        entry(name, data, 0o100644)
    entry("TRAILER!!!", b"", 0)
    return bytes(out) + b"\x00" * (-len(out) % 512)


def install_from_iso(m: Machine, iso: Path, disk: Path):
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), DISK], check=True)
    kernel, initrd = m.dir / "vmlinuz", m.dir / "initrd"
    kernel.write_bytes(iso_file(iso, "casper/vmlinuz"))
    pub = key().with_suffix(".pub").read_text().strip()
    with Seed({"finish.sh": MINT_FINISH.replace("@KEY@", pub).encode()}) as seed:
        preseed = MINT_PRESEED.replace("@URL@", seed.url).encode()
        # (the kernel reads an archive only from a 4-byte boundary, and skips the zeros before it)
        original = iso_file(iso, "casper/initrd.lz")
        initrd.write_bytes(original + b"\x00" * (-len(original) % 512) + cpio({"preseed.cfg": preseed}))
        say(f"Installing {m.title} with its own installer (10 to 30 minutes)")
        proc = launch(m, disk, extra=["-cdrom", str(iso), "-kernel", str(kernel), "-initrd", str(initrd),
                                      "-append", MINT_CMDLINE, "-no-reboot"])
        serial = follow(m, proc, 2 * 3600)
    kernel.unlink()
    initrd.unlink()
    if "SIDELIT-VM: set up" not in serial:
        raise Fail(f"Installing {m.name} failed; its serial log: {m.dir / 'serial.log'} ('vm.py build "
                   f"{m.name} --window' shows the installer)")


def build(m: Machine, *, again: bool = False):
    base, part = m.dir / "base.qcow2", m.dir / "base.part.qcow2"
    if base.exists() and not again:
        raise Fail(f"{m.name} is built already ('build --again' builds it anew)")
    if pid(m):
        raise Fail(f"{m.name} is running: 'vm.py stop {m.name}' first.")
    m.dir.mkdir(parents=True, exist_ok=True)
    image = download(m)
    part.unlink(missing_ok=True)
    if image.suffix == ".iso":
        install_from_iso(m, image, part)
    else:
        provision_cloud(m, image, part)
    # the first boot: the desktop comes up by itself and lets ssh in (Mint installs ssh now)
    say("Starting it once: the desktop must come up by itself")
    proc = launch(m, part)
    try:
        wait_ssh(m, 30 * 60, proc)
        wait_desktop(m, 10 * 60)
        (m.dir / "base.png").write_bytes(screenshot(m))
    finally:
        stop(m, quiet=True)
    part.replace(base)
    (m.dir / "run.qcow2").unlink(missing_ok=True)  # a copy of the old base
    print(f"✓ {m.name} is built: {m.title}. Its desktop: {m.dir / 'base.png'}")


# ---- using a machine -----------------------------------------------------------------------------
def need_running(m: Machine):
    if not pid(m):
        raise Fail(f"{m.name} is not running: 'vm.py start {m.name}' first.")


def start(m: Machine, *, resume: bool = False, window: bool = False):
    base, disk = m.dir / "base.qcow2", m.dir / "run.qcow2"
    if not base.exists():
        raise Fail(f"{m.name} is not built yet: 'vm.py build {m.name}' (10 to 40 minutes).")
    if pid(m):
        raise Fail(f"{m.name} is running already ({describe(m)}).")
    if not (resume and disk.exists()):
        disk.unlink(missing_ok=True)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(disk)],
                       check=True)
    available = next((int(line.split()[1]) // 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:")), 0)
    if available < m.memory + 2048:
        raise Fail(f"Only {available} MB of memory is free, and {m.name} takes {m.memory} MB: stop another "
                   "machine first ('vm.py list').")
    proc = launch(m, disk, window=window)
    (runtime() / (m.name + ".json")).write_text(json.dumps({"by": os.getcwd(), "since": time.time()}))
    say(f"Starting {m.title}{' (as it was left)' if resume else ''}")
    wait_ssh(m, 5 * 60, proc)
    wait_desktop(m)
    print(f"✓ {m.name} is up, logged in as {USER}: ssh on 127.0.0.1:{m.ssh_port}, its screen (VNC) on "
          f"127.0.0.1:{m.vnc_port}")


def stop(m: Machine, *, quiet: bool = False):
    if not pid(m):
        if not quiet:
            print(f"{m.name} is not running")
        return
    # a clean shutdown, so that a disk kept for --resume is whole
    ssh(m, "sudo systemctl --no-block poweroff", check=False, timeout=20)
    if not wait_gone(m, 90):
        qmp(m, "quit")
        wait_gone(m, 15)
    for suffix in (".qmp", ".pid", ".json"):
        (runtime() / (m.name + suffix)).unlink(missing_ok=True)
    if not quiet:
        print(f"✓ {m.name} is stopped")


def describe(m: Machine) -> str:
    try:
        info = json.loads((runtime() / (m.name + ".json")).read_text())
        return f"started {time.strftime('%H:%M', time.localtime(info['since']))} from {info['by']}"
    except (OSError, ValueError, KeyError):
        return "running"


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def install(m: Machine, *, github: bool = False) -> int:
    need_running(m)
    if github:
        command = "curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash"
        say("Installing Sidelit from GitHub, with the one-line install")
    else:
        branch = subprocess.run(["git", "-C", str(REPO), "symbolic-ref", "-q", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        if git("status", "--porcelain", "--untracked-files=no").strip():
            say("Note: changes not committed here are not in it ('vm.py sync' copies them over)")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "sidelit.bundle"
            git("bundle", "create", "-q", str(bundle), "HEAD", *([branch] if branch else []))
            ssh(m, "cat > ~/sidelit.bundle", data=bundle.read_bytes())
        ssh(m, "cat > ~/get.sh", data=git("show", "HEAD:get.sh"))
        command = "SIDELIT_REPO=$HOME/sidelit.bundle bash ~/get.sh"
        say(f"Installing Sidelit from this checkout ({branch or 'HEAD'}), with its get.sh")
    return ssh(m, in_session(command), check=False, capture=False).returncode


def sync(m: Machine):
    """This checkout's files as they are, committed or not, over the installed copy; restarts it."""
    need_running(m)
    names = [n for n in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
             if n and (REPO / n).is_file()]
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in names:
            tar.add(REPO / name, arcname=name)
    ssh(m, "test -d ~/.local/share/sidelit || { echo 'Sidelit is not installed here: vm.py install first' >&2; "
           "exit 1; }; tar -x -C ~/.local/share/sidelit", data=archive.getvalue())
    say(f"Copied {len(names)} files; restarting Sidelit")
    ssh(m, in_session("~/.local/bin/sidelit restart"), capture=False)


def relogin(m: Machine):
    need_running(m)
    before = session_pid(m)
    say("Logging out and back in")
    ssh(m, "sudo systemctl restart display-manager", check=False, timeout=60)
    wait_desktop(m, not_pid=before)
    print(f"✓ {m.name} is logged in again")


def reboot(m: Machine):
    need_running(m)
    say("Restarting")
    ssh(m, "sudo systemctl --no-block reboot", check=False, timeout=20)
    deadline = time.monotonic() + 120
    while ssh(m, "true", check=False, timeout=10).returncode == 0 and time.monotonic() < deadline:
        time.sleep(1)
    wait_ssh(m, 5 * 60)
    wait_desktop(m)
    print(f"✓ {m.name} is up again")


def screenshot(m: Machine) -> bytes:
    vnc = Vnc(m.vnc_port)
    try:
        return vnc.screenshot()
    finally:
        vnc.close()


def shot(m: Machine, path: str | None) -> Path:
    need_running(m)
    out = Path(path) if path else Path(tempfile.gettempdir()) / f"sidelit-vm-{m.name}-{time.strftime('%H%M%S')}.png"
    out.write_bytes(screenshot(m))
    print(out)
    return out


def click(m: Machine, x: int, y: int, *, button: str = "left", double: bool = False):
    need_running(m)
    vnc = Vnc(m.vnc_port)
    try:
        vnc.pointer(x, y)
        pause(0.1)
        for _ in range(2 if double else 1):
            vnc.pointer(x, y, BUTTONS[button])
            pause(0.05)
            vnc.pointer(x, y)
            pause(0.08)
    finally:
        vnc.close()


def drag(m: Machine, x1: int, y1: int, x2: int, y2: int):
    need_running(m)
    vnc = Vnc(m.vnc_port)
    try:
        vnc.pointer(x1, y1)
        pause(0.1)
        vnc.pointer(x1, y1, 1)
        pause(0.2)
        for step in range(1, 21):  # a drag the toolkit sees moving
            vnc.pointer(x1 + (x2 - x1) * step // 20, y1 + (y2 - y1) * step // 20, 1)
            pause(0.03)
        pause(0.2)
        vnc.pointer(x2, y2)
    finally:
        vnc.close()


def keys(m: Machine, combos: list[str]):
    need_running(m)
    parsed = [combo(c) for c in combos]
    vnc = Vnc(m.vnc_port)
    try:
        for syms in parsed:
            for sym in syms:
                vnc.key(sym, True)
                pause()
            for sym in reversed(syms):
                vnc.key(sym, False)
                pause()
            pause(0.1)
    finally:
        vnc.close()


def type_text(m: Machine, text: str):
    need_running(m)
    events = typed(text)
    vnc = Vnc(m.vnc_port)
    try:
        for sym, down in events:
            vnc.key(sym, down)
            pause(0.01)
    finally:
        vnc.close()


def listing():
    print(f"Machines in {home()}:")
    for m in MACHINES.values():
        base = m.dir / "base.qcow2"
        state = (describe(m) if pid(m) else "built" if base.exists() else "not built")
        size = sum(f.stat().st_size for f in m.dir.glob("*.qcow2")) // 2**30 if m.dir.exists() else 0
        print(f"  {m.name:14} {m.title:36} {state}{f', {size} GB' if size else ''}"
              f"{f'  (ssh :{m.ssh_port}, screen :{m.vnc_port})' if pid(m) else ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vm.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the machines, and which are built and running")

    def machine(name: str, help: str):
        p = sub.add_parser(name, help=help)
        p.add_argument("machine", choices=sorted(MACHINES))
        return p

    machine("build", "install the machine and keep it as its clean base").add_argument(
        "--again", action="store_true", help="build it anew")
    p = machine("start", "start a fresh copy of the clean machine, and wait until it is logged in")
    p.add_argument("--resume", action="store_true", help="the last run's disk, as it was left")
    p.add_argument("--window", action="store_true", help="show its screen in a window here")
    machine("stop", "shut it down")
    p = machine("install", "install this checkout's HEAD there with its get.sh, in the desktop session")
    p.add_argument("--github", action="store_true", help="the one-line install from GitHub instead")
    machine("sync", "copy this checkout's files over the installed Sidelit, and restart it")
    machine("ssh", "a command there (or a shell), not in the desktop session").add_argument(
        "cmd", nargs=argparse.REMAINDER)
    machine("run", "a command in its desktop session (as a terminal there would)").add_argument(
        "cmd", nargs=argparse.REMAINDER)
    machine("relogin", "log out and back in (it logs in by itself)")
    machine("reboot", "restart it and wait for the desktop")
    machine("shot", "a screenshot, as PNG; prints its path").add_argument("path", nargs="?")
    p = machine("click", "click at a point of the screen (pixels, as in a screenshot)")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", choices=sorted(BUTTONS), default="left")
    p.add_argument("--double", action="store_true")
    p = machine("drag", "drag with the left button from one point to another")
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=int)
    machine("key", "press keys: 'ctrl+alt+t', 'super', 'Return'…, one after the other").add_argument(
        "combos", nargs="+")
    machine("type", "type text (ASCII, on a US keyboard)").add_argument("text")
    args = parser.parse_args(argv)
    m = MACHINES.get(getattr(args, "machine", ""))
    try:
        if args.command == "list":
            listing()
        elif args.command == "build":
            build(m, again=args.again)
        elif args.command == "start":
            start(m, resume=args.resume, window=args.window)
        elif args.command == "stop":
            stop(m)
        elif args.command == "install":
            return install(m, github=args.github)
        elif args.command == "sync":
            sync(m)
        elif args.command == "ssh":
            need_running(m)
            if not args.cmd:
                os.execvp("ssh", ssh_command(m, tty=True))
            return ssh(m, " ".join(args.cmd), check=False, capture=False).returncode
        elif args.command == "run":
            need_running(m)
            return ssh(m, in_session(" ".join(args.cmd)), check=False, capture=False).returncode
        elif args.command == "relogin":
            relogin(m)
        elif args.command == "reboot":
            reboot(m)
        elif args.command == "shot":
            shot(m, args.path)
        elif args.command == "click":
            click(m, args.x, args.y, button=args.button, double=args.double)
        elif args.command == "drag":
            drag(m, args.x1, args.y1, args.x2, args.y2)
        elif args.command == "key":
            keys(m, args.combos)
        elif args.command == "type":
            type_text(m, args.text)
    except Fail as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
