"""Which compositor we are running under, and whether its windows still need bringing back.

Every window lives and dies with the compositor. A login starts one, and so does COSMIC
restarting itself after a crash (the user session, and with it our runtime dir, lives on).
Either way nothing is on screen and everything in the layout is missing, so the restore
runs once per compositor. The marker records which compositor was restored; a new one
(a different pid or start time) means its windows were never brought back.

On X11 windows live with the X server, which often runs as root, so the window manager stands
in for it: a login starts one. (One restarted in a running session, as Cinnamon's "Restart
Cinnamon" does, finds every window still there, and a restore then finds nothing missing.)

Only our own desktop's counts: you may have another one open at the same time (a VNC desktop
next to the one on the screen), and its window manager starting is not our login. Under
Wayland that is a Wayland compositor; under X11 the window manager of our $DISPLAY.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WAYLAND_COMPOSITORS = ("cosmic-comp", "kwin_wayland", "gnome-shell", "Hyprland", "niri", "sway")
# X11 window managers (x11.py): Cinnamon's is cinnamon itself, Xfce's xfwm4, MATE's marco
X11_WMS = ("cinnamon", "xfwm4", "marco", "openbox", "metacity", "budgie-wm", "fluxbox", "icewm")
COMPOSITORS = WAYLAND_COMPOSITORS + X11_WMS
FRESH_S = 600   # a compositor younger than this has just started: we are part of its start-up


@dataclass(frozen=True)
class Compositor:
    pid: int
    started: int   # clock ticks after boot (/proc/<pid>/stat field 22): with the pid, unique per boot
    age: float     # seconds

    @property
    def ident(self) -> str:
        return f"{self.pid}:{self.started}"


def x11_display(value: str | None) -> str:
    """An X display without its screen and the local host: ":5" for ":5.0" and "localhost:5"."""
    value = (value or "").strip()
    if ":" not in value:
        return ""
    host, _, number = value.rpartition(":")
    return (host if host not in ("", "localhost", "unix") else "") + ":" + number.split(".")[0]


def _display_of(proc: str, pid: str) -> str | None:
    """The X display a process was started on; None when its environment cannot be read."""
    try:
        with open(f"{proc}/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        return None
    for var in env:
        if var.startswith(b"DISPLAY="):
            return x11_display(var[8:].decode(errors="replace"))
    return ""


def compositor(proc: str = "/proc", env=None) -> Compositor | None:
    """The youngest compositor of ours (not the greeter's), of our own desktop."""
    env = os.environ if env is None else env
    x11 = (env.get("XDG_SESSION_TYPE") or "").lower() == "x11" or \
        bool(env.get("DISPLAY")) and not env.get("WAYLAND_DISPLAY")
    ours, display = (X11_WMS, x11_display(env.get("DISPLAY"))) if x11 else (WAYLAND_COMPOSITORS, "")
    try:
        with open(f"{proc}/uptime") as f:
            uptime = float(f.read().split()[0])
    except OSError:
        return None
    clk = os.sysconf("SC_CLK_TCK")
    best = None
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(f"{proc}/{pid}/comm") as f:
                if f.read().strip() not in ours:
                    continue
            if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
                continue  # the greeter's compositor
            if display and _display_of(proc, pid) not in (display, None):
                continue  # the window manager of another X display (a VNC desktop's, say)
            with open(f"{proc}/{pid}/stat") as f:
                stat = f.read()
            started = int(stat[stat.rindex(")") + 2:].split()[19])
            c = Compositor(pid=int(pid), started=started, age=uptime - started / clk)
            if best is None or c.age < best.age:
                best = c
        except (OSError, ValueError, IndexError):
            continue
    return best


def needs_restore(comp: Compositor | None, marker: Path) -> bool:
    """Has this compositor just started, and not had its windows brought back yet?"""
    if comp is None or comp.age >= FRESH_S:
        return False
    try:
        done = marker.read_text().strip()
    except OSError:
        return True
    return done != comp.ident   # (an empty marker is from 0.4.0: one per login, not per compositor)


def mark_restored(comp: Compositor, marker: Path):
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(comp.ident + "\n")
    except OSError:
        pass
