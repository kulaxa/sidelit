"""The niri backend against a real niri, by hand: not part of the unit tests.

Run it in a niri session you can spare, or a nested one (its NIRI_SOCKET and WAYLAND_DISPLAY in
the environment); it needs foot. It keeps a workspace of its own (named, then unnamed again),
opens, retitles, moves, focuses and closes a window of its own, and reorders two workspaces.
Prints each check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/niri_live.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import backend, niri  # noqa: E402

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


def main() -> int:
    check("this session picks the niri backend", backend.chosen() == "niri", backend.chosen())
    c = niri.NiriClient(lambda: None, primary=True)
    print(f"     niri at {c.path}; screens {[s.name for s in c.screens]}", flush=True)
    start = c.active()
    check("it is on a workspace", start is not None)

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
        here = c.workspace_of(tl)
        check("on the workspace you are on", here is not None and here.active)
        check("its new title comes on the event stream", pump(c, 5, lambda: tl.title == f"{title} renamed"), tl.title)

        made = c.create("Workspace")
        check("a new workspace: the empty one at the end, named", made is not None and made.pinned
              and made.id == f"name:{made.name}" and made.name.startswith("sidelit-") and not c.toplevels_on(made),
              made and (made.id, made.idx))
        check("and niri adds another empty one after it",
              pump(c, 3, lambda: any(w.monitor == made.monitor and w.idx > made.idx and not w.name for w in c.list())))
        check("you stay where you were", c.active() is not None and c.active() is here)

        check("moving the window there", c.move_toplevel(tl, made))
        check("puts it there", pump(c, 3, lambda: c.workspace_of(tl) is made))
        check("and you stay", c.active() is here, c.active() and c.active().idx)

        c.focus(tl)
        check("focusing it goes there", pump(c, 3, lambda: c.active() is made and tl.focused))
        c.activate(here)
        check("switching back", pump(c, 3, lambda: c.active() is here))

        before = (made.idx, here.idx)
        c.move(made, here, before=True)
        check("reordering puts it before the other", pump(c, 3, lambda: made.idx < here.idx), (before, made.idx, here.idx))
        c.move(made, here, before=False)
        check("and after it again", pump(c, 3, lambda: made.idx > here.idx), (made.idx, here.idx))

        c.close_toplevel(tl)
        check("closing it", pump(c, 5, lambda: tl.closed and foot.poll() is not None))
        check("it went with the keyboard noted", tl.identifier in c.closed_focus)
        check("the named workspace stays, empty", pump(c, 2, lambda: not made.removed and not c.toplevels_on(made)))
        c.remove(made)
        check("unnamed, niri drops it", pump(c, 3, lambda: made.removed))
    finally:
        if foot.poll() is None:
            foot.terminate()
        c.close()
    print("FAILED: " + ", ".join(failed) if failed else "all passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
