# Test machines

Sidelit on other distributions and desktops, each in a virtual machine that logs in by itself
and that `vm.py` drives from outside: it installs a checkout there with its own `get.sh`, runs
commands in the desktop session, takes screenshots, clicks and types. A person can watch, or
take over, in a window.

| Machine | What it is | Built from |
|---|---|---|
| `ubuntu-gnome` | Ubuntu 24.04, GNOME 46 (Wayland) | Ubuntu's cloud image, with `ubuntu-desktop-minimal` |
| `fedora-kde` | Fedora 44, KDE Plasma 6 (Wayland) | Fedora's cloud image, with its KDE desktop group |
| `mint-cinnamon` | Linux Mint 22.3, Cinnamon (X11) | Mint's installer ISO, run by itself from a preseed |
| `arch-hyprland` | Arch Linux, Hyprland (Wayland) | Arch's cloud image, with Hyprland, kitty and greetd |

Each has a user `tester` (password `tester`, sudo without one) who is logged in at boot, with
the screen never locking or blanking, and no first-login wizard, upgrade offer or update in the
background to get in the way. Nothing of Sidelit is on them until you install it.

## What it needs

QEMU with KVM, once: `sudo apt-get install qemu-system-x86 qemu-system-modules-opengl
qemu-utils` (and `qemu-system-gui` for `--window`). Hyprland needs 3D, which a GPU on the host
draws (virgl; on a PC with an NVIDIA and another GPU, the other one: `SIDELIT_VM_RENDERNODE`
picks one). About 4 GB of memory for each running machine, and 2 to 15 GB of disk for each
built one, in `$SIDELIT_VMS` (default `~/.local/share/sidelit-vms`, which may be a link to
another disk).

## Using it

```bash
python3 tests/vm/vm.py list                     # which are built, which run, and who started them
python3 tests/vm/vm.py build fedora-kde         # once, 8 to 30 minutes: the clean machine
python3 tests/vm/vm.py start fedora-kde         # a fresh copy of the clean machine, logged in
python3 tests/vm/vm.py install fedora-kde       # this checkout's HEAD, with its get.sh
python3 tests/vm/vm.py shot fedora-kde          # a screenshot; prints the PNG's path
python3 tests/vm/vm.py run fedora-kde sidelit status     # in the desktop session
python3 tests/vm/vm.py stop fedora-kde
```

- `start` always begins from the clean machine (what the last run changed is dropped);
  `start --resume` goes on with the last run's disk instead. `start --window` (and `build
  --window`) shows the screen in a window on this desktop.
- `install` copies this checkout's HEAD (a git bundle) and its `get.sh` there and runs the
  one-line install from it, in the desktop session, as a person would in a terminal: what it
  says is what they would see. Changes not committed are not in it. `install --github` runs the
  real one-line install from GitHub instead.
- `sync` copies this checkout's files as they are, committed or not, over the installed copy
  and restarts Sidelit: quick turns while working. (On GNOME, a change to the Shell extension
  needs `relogin` too.)
- `run` runs a shell command in the desktop session (its `WAYLAND_DISPLAY` or `DISPLAY`, its
  session bus); `ssh` runs one outside it, or opens a shell.
- `relogin` logs out and back in: as a real logout, it ends every process of `tester`, their
  systemd user services (Sidelit's) and ssh sessions too, and the login manager logs in anew.
  `reboot` restarts the machine. Both wait for the desktop.
- `shot [FILE]` takes a screenshot, at the machine's 1920×1080.
- `click X Y [--button right] [--double]`, `drag X1 Y1 X2 Y2`, `key ctrl+alt+t`, `type TEXT`:
  the pointer and the keyboard, in the screenshot's pixels.

Two jobs cannot use one machine at once: `start` refuses a running one and says who started
it. `stop` shuts it down cleanly, and has to come first.

Keys go where the keyboard is: after a click on Sidelit's sidebar, that is the sidebar (Return
there starts a template), so click into a window before typing into it.

## How long, how big

Measured on an 8-core desktop with the machines on a USB SSD:

| | `ubuntu-gnome` | `fedora-kde` | `mint-cinnamon` | `arch-hyprland` |
|---|---|---|---|---|
| `build` (once) | 15 min | 18 min | 20 to 30 min | 8 min |
| on disk | 6.5 GB | 9 GB | 15 GB | 2 GB |

A `start` takes about a minute, `install` one or two, `relogin` and `reboot` under one; a
screenshot, a click or keys about a second. A run's own disk grows by a few hundred MB, dropped
at the next fresh `start`. Each running machine takes 4 GB of memory and 4 CPU threads.

## What they cannot show

One screen each (a second one, and unplugging it, is not set up), a virtual GPU (nothing of
NVIDIA's or a laptop's), no Chrome, and no Claude Code, Codex or OpenCode until a test installs
one (the sidebar runs without them).

## How it works

QEMU with KVM, headless, each machine on its own ports on 127.0.0.1 (ssh on 2240 + n, its
screen over VNC on 5940 + n). A cloud image is set up on its first boot by cloud-init, from a
small web server this script runs (QEMU shows the host to the guest as 10.0.2.2); Mint's
installer reads a preseed slipped into its initramfs, and installs ssh at the first boot. A
build ends with a boot of its own: the desktop has to come up by itself and let ssh in, and its
screenshot is kept as `base.png`. The clean machine is `base.qcow2`, never changed again; a
run's disk is a copy-on-write layer over it, `run.qcow2`. QEMU does not wait for the guest's
flushes to reach the disk, which makes installing packages many times faster on a slow disk: a
crash of this computer (not of a machine) can spoil a disk, and the machine is built anew.
Screenshots and input go over VNC, which also reads a 3D screen (QEMU's own screendump cannot).

To build a machine anew (a newer release, say), delete its download in `downloads/` and run
`build MACHINE --again`. Another machine is an entry in `MACHINES` in `vm.py`: a cloud image
and the script that installs its desktop on first boot.
