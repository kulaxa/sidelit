"""The Hyprland backend against a real Hyprland, by hand: not part of the unit tests.

Run it in a Hyprland session you can spare, or a headless one (its HYPRLAND_INSTANCE_SIGNATURE
and WAYLAND_DISPLAY in the environment); it needs foot. It adds a workspace and opens, retitles,
moves and closes a window of its own, reloads Hyprland's config once, and with two screens moves
a workspace between them. Works with a Lua config and with a classic one. Prints each check;
exits 1 if any failed.

    PYTHONPATH=. python3 tests/hyprland_live.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import backend, hyprland  # noqa: E402

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


def rules(c) -> dict:
    return {r["workspaceString"]: r.get("monitor") for r in c._json("workspacerules") or []
            if r.get("enabled", True) and r.get("persistent")}


def swapped(c, tl, a: str, b: str, home: str, other: str):
    """The workspaces on the two screens swapped (Hyprland's swapactiveworkspaces): both rules
    change, and nothing moves on after that."""
    c.focus(tl)  # (a is shown on `other`; b goes on `home`)
    c.activate(c.workspaces[b])
    pump(c, 2, lambda: c.active() is not None and c.active().id == b)
    c._dispatch(f"swapactiveworkspaces {home} {other}",
                f"hl.dsp.workspace.swap_monitors({{monitor1 = \"{home}\", monitor2 = \"{other}\"}})")
    settled = pump(c, 5, lambda: c.workspaces[a].monitor == home and c.workspaces[b].monitor == other
                   and rules(c).get(a) == home and rules(c).get(b) == other)
    check("two workspaces swapped between the screens: both pins follow", settled,
          (c.workspaces[a].monitor, c.workspaces[b].monitor, rules(c)))
    where = (c.workspaces[a].monitor, c.workspaces[b].monitor)
    pump(c, 3)
    check("and nothing moves on", (c.workspaces[a].monitor, c.workspaces[b].monitor) == where and
          rules(c).get(a) == home and rules(c).get(b) == other)
    check("each screen shows the one swapped onto it", c.shown.get(home) == a and c.shown.get(other) == b, c.shown)


def unplugged(c, tl, ref: str):
    """Unplugging a (headless) screen and plugging it back: the pin keeps its screen meanwhile,
    and the workspace goes back there."""
    screen = next((s.name for s in c.screens if s.name.startswith("HEADLESS")), None)
    if screen is None:
        print("     (no headless screen to unplug: not tried)", flush=True)
        return
    c.move_toplevel(tl, c.workspaces[ref], screen)
    pump(c, 3, lambda: c.workspaces[ref].monitor == screen)
    c._request(f"output remove {screen}")
    check("unplugging its screen, the workspace goes to the other",
          pump(c, 5, lambda: screen not in c.output_names() and c.workspaces.get(ref) is not None
               and c.workspaces[ref].monitor != screen))
    extra = c.create("Workspace")  # (Hyprland applies its rules: the unplugged screen's pins lapse meanwhile)
    pump(c, 1)
    check("its pin keeps the unplugged screen", rules(c).get(ref) == screen and c.workspaces[ref].pinned, rules(c))
    if extra is not None:
        c.remove(extra)
    c._request(f"output create headless {screen}")
    check("plugged back, the workspace goes back there",
          pump(c, 8, lambda: c.workspaces.get(ref) is not None and c.workspaces[ref].monitor == screen),
          c.workspaces.get(ref) and c.workspaces[ref].monitor)


def main() -> int:
    check("this session picks the hyprland backend", backend.chosen() == "hyprland", backend.chosen())
    state = Path(tempfile.mkdtemp(prefix="sidelit-hypr-live-"))
    c = hyprland.HyprlandClient(lambda: None, primary=True, state_dir=state)
    print(f"     Hyprland at {c.dir}, {'Lua' if c.lua else 'classic'} config; "
          f"chips {c._keys or 'none'}", flush=True)
    start = c.active()
    check("it is on a workspace", start is not None)
    before = {w.id for w in c.list()}

    made = c.create("Workspace")
    check("a new workspace is made, pinned", made is not None and made.pinned and made.id not in before,
          [(w.id, w.pinned) for w in c.list()])
    check("and you stay where you were", c.active() is not None and c.active().id == start.id)
    pump(c, 0.5)
    check("it stays while empty and not shown", made is not None and made.id in c.workspaces)

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
        check("on the workspace you are on", c.workspace_of(tl) is not None and c.workspace_of(tl).id == start.id)
        check("its new title comes with the event", pump(c, 5, lambda: tl.title == f"{title} renamed"), tl.title)

        check("moving it", c.move_toplevel(tl, made))
        check("puts it there", pump(c, 3, lambda: c.workspace_of(tl) is not None and c.workspace_of(tl).id == made.id))
        check("and you stay", c.active() is not None and c.active().id == start.id)

        c.focus(tl)
        check("focusing it goes there", pump(c, 3, lambda: c.active() is not None and c.active().id == made.id
                                          and tl.focused))
        c.activate(c.workspaces[start.id] if start.id in c.workspaces else c.list()[0])
        check("switching back", pump(c, 3, lambda: c.active() is not None and c.active().id != made.id))

        c._request("reload")
        check("a config reload keeps Sidelit's pin",
              pump(c, 3, lambda: made.id in c.workspaces and c.workspaces[made.id].pinned
                   and c._json("workspacerules") and any(r.get("workspaceString") == made.id and r.get("persistent")
                                                         for r in c._json("workspacerules"))))

        if len(c.screens) > 1:
            other = next(s.name for s in c.screens if s.name != c.workspaces[made.id].monitor)
            check("moving to another screen", c.move_toplevel(tl, c.workspaces[made.id], other))
            check("takes the workspace there", pump(c, 3, lambda: c.workspaces[made.id].monitor == other
                                                     and tl.output == other), (c.workspaces[made.id].monitor, tl.output))
            check("and its pin follows", pump(c, 2, lambda: c._ours.get(made.id, {}).get("screen") == other), c._ours)
            # Hyprland moves a persistent workspace whose rule names no screen to the one with the
            # keyboard whenever it applies its rules: pinning another one must leave it be
            home = next(s.name for s in c.screens if s.name != other)
            if c.focused_screen != home:
                c.activate(next(w for w in c.list() if w.monitor == home))
                pump(c, 2, lambda: c.focused_screen == home)
            extra = c.create("Workspace")
            pump(c, 1)
            check("pinning one on another screen leaves it where it is",
                  c.workspaces[made.id].monitor == other and extra is not None and extra.monitor == home,
                  (c.workspaces[made.id].monitor, extra and extra.monitor))
            if extra is not None:
                swapped(c, tl, made.id, extra.id, home, other)
                c.remove(c.workspaces[extra.id])
            unplugged(c, tl, made.id)
        else:
            print("     (one screen: the move between screens is not tried)", flush=True)

        c.close_toplevel(tl)
        check("closing it", pump(c, 5, lambda: tl.closed and foot.poll() is not None))
        check("it went with the keyboard noted", tl.identifier in c.closed_focus)
        c.remove(c.workspaces[made.id])

        def gone_or_shown():  # (Hyprland keeps a workspace a screen shows: it goes once you leave it)
            shown = {str((m.get("activeWorkspace") or {}).get("id")) for m in c._json("monitors") or []}
            return made.id not in c.workspaces or (not c.workspaces[made.id].pinned and made.id in shown)
        check("removed, the empty workspace goes", pump(c, 3, gone_or_shown))
    finally:
        if foot.poll() is None:
            foot.terminate()
        c.close()
        shutil.rmtree(state, ignore_errors=True)
    print("FAILED: " + ", ".join(failed) if failed else "all passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
