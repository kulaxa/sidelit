"""Which compositor we are running under, and whether its windows still need bringing back.

Every window lives and dies with the compositor. A login starts one, and so does COSMIC
restarting itself after a crash (the user session, and with it our runtime dir, lives on).
Either way nothing is on screen and everything in the layout is missing, so the restore
runs once per compositor. The marker records which compositor was restored; a new one
(a different pid or start time) means its windows were never brought back.

On X11 windows live with the X server, which often runs as root, so the window manager stands
in for it: a login starts one. (One restarted in a running session, as Cinnamon's "Restart
Cinnamon" does, finds every window still there, and a restore then finds nothing missing.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

COMPOSITORS = ("cosmic-comp", "kwin_wayland", "gnome-shell", "Hyprland",
               # X11 window managers (x11.py): Cinnamon's is cinnamon itself, Xfce's xfwm4, MATE's marco
               "cinnamon", "xfwm4", "marco", "openbox", "metacity", "budgie-wm", "fluxbox", "icewm")
FRESH_S = 600   # a compositor younger than this has just started: we are part of its start-up


@dataclass(frozen=True)
class Compositor:
    pid: int
    started: int   # clock ticks after boot (/proc/<pid>/stat field 22): with the pid, unique per boot
    age: float     # seconds

    @property
    def ident(self) -> str:
        return f"{self.pid}:{self.started}"


def compositor(proc: str = "/proc") -> Compositor | None:
    """The youngest compositor of ours (not the greeter's)."""
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
                if f.read().strip() not in COMPOSITORS:
                    continue
            if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
                continue  # the greeter's compositor
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
