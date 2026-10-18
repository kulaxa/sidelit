"""The sway backend against a real sway, by hand: not part of the unit tests.

Run it in a sway session you can spare, or a headless one (its SWAYSOCK and WAYLAND_DISPLAY in
the environment); it needs foot. It opens, retitles, moves, focuses and closes a window of its
own, keeps a workspace of its own (a stand-in, made by sway once a window goes there, kept after
sway drops it, then let go) and, with two screens, moves a workspace to the other screen and
back. Prints each check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/sway_live.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import backend, sway  # noqa: E402

failed = []


def check(what: str, ok: bool, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failed.append(what)


def pump(client, seconds: float, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        client.pump()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


def in_sway(name: str) -> bool:
    out = subprocess.run(["swaymsg", "-t", "get_workspaces", "-r"], capture_output=True, text=True).stdout
    return f'"name": "{name}"' in out


def main() -> int:
    check("this session picks the sway backend", backend.chosen() == "sway", backend.chosen())
    # (a record of its own: a Sidelit daemon on this sway keeps its own as it is)
    c = sway.SwayClient(lambda: None, primary=True, state_dir=Path(tempfile.mkdtemp(prefix="sidelit-sway-live-")))
    print(f"     sway at {c.path}; screens {[s.name for s in c.screens]}", flush=True)
    here = c.active()
    check("it is on a workspace", here is not None)
    if here is None:
        return 1
    # (kept, so that it is there to come back to once its window has gone elsewhere: sway drops
    # an empty workspace you leave)
    was_kept = here.pinned
    c.pin(here, True)
    if here is not None and 1 <= here.num <= 10:  # (sway's own config binds Super and 1…0)
        check("with sway's key on its chip", c.shortcut_label(here, 1) == f"⊞ {here.num % 10}", c.shortcut_label(here, 1))

    title = f"sidelit-live-{os.getpid()}"
    script = f"sleep 1.5; printf '\\033]2;{title} renamed\\007'; sleep 60"
    foot = subprocess.Popen(["foot", f"--title={title}", "sh", "-c", script])
    try:
        found = pump(c, 8, lambda: any(t.title.startswith(title) for t in c.toplevels.values()))
        tl = next((t for t in c.toplevels.values() if t.title.startswith(title)), None)
        check("a window that opens arrives by itself", found and tl is not None)
        if tl is None:
            return 1
        check("with its app, process and screen", tl.app_id == "foot" and tl.pid > 0 and tl.output in c.output_names(),
              (tl.app_id, tl.pid, tl.output))
        check("on the workspace you are on", c.workspace_of(tl) is here)
        check("its new title comes as an event", pump(c, 5, lambda: tl.title == f"{title} renamed"), tl.title)

        made = c.create("Workspace")
        check("a new workspace: kept, the next number, not made in sway yet", made is not None and made.pinned
              and made.id.isdigit() and made.proxy is None and not in_sway(made.name), made and made.id)
        check("you stay where you were", c.active() is here)

        check("moving the window there", c.move_toplevel(tl, made))
        check("makes it in sway, with the window", pump(c, 3, lambda: made.proxy is not None and c.workspace_of(tl) is made))
        check("and you stay", pump(c, 2, lambda: c.active() is here), c.active() and c.active().id)

        c.focus(tl)
        check("focusing it goes there", pump(c, 3, lambda: c.active() is made and tl.focused))
        c.activate(here)
        check("switching back", pump(c, 3, lambda: c.active() is here))

        others = [s.name for s in c.screens if s.name != made.monitor]
        if others:
            start = made.monitor
            check("moving it to the other screen takes the workspace there",
                  c.move_toplevel(tl, made, others[0]) and pump(c, 3, lambda: made.monitor == others[0]),
                  made.monitor)
            check("and you stay where you were", pump(c, 2, lambda: c.active() is here), c.active() and c.active().id)
            c.move_toplevel(tl, made, start)
            check("and back", pump(c, 3, lambda: made.monitor == start), made.monitor)
        else:
            print("     (one screen: no moving between screens)", flush=True)

        c.close_toplevel(tl)
        check("closing it", pump(c, 5, lambda: tl.closed and foot.poll() is not None))
        check("it went with the keyboard noted", tl.identifier in c.closed_focus)
        check("sway drops the emptied workspace", pump(c, 3, lambda: not in_sway(made.name)))
        check("Sidelit keeps it, a stand-in", not made.removed and made.pinned and made.proxy is None)
        c.activate(made)
        check("going there makes it again", pump(c, 3, lambda: made.proxy is not None and made.active))
        c.activate(here)
        pump(c, 1)
        c.remove(made)
        check("let go, it goes", made.removed and pump(c, 2, lambda: not in_sway(made.name)))
    finally:
        if foot.poll() is None:
            foot.terminate()
        if not was_kept:
            c.pin(here, False)
        c.close()
    print("FAILED: " + ", ".join(failed) if failed else "all passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
