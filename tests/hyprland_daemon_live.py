"""The daemon on a real Hyprland, by hand: not part of the unit tests.

Like hyprland_live.py, run it in a Hyprland session you can spare or a headless one; it needs
foot. It starts a test daemon of its own (SIDELIT_HOME in a temporary directory, so not yours)
and drives it the way the sidebar does: a window becomes a kept workspace, which Hyprland then
keeps while empty; '+' adds a workspace with a terminal under the next number; closing one
takes it out of Hyprland; a window moved by hand is remembered where it went, and the emptied
workspace goes. It leaves one kept workspace with a terminal, and says how to check that a
login brings it back:

    python3 tests/hyprland_daemon_live.py
    (quit Hyprland, start a fresh one, then in it:)
    python3 tests/hyprland_daemon_live.py --restore HOME NUMBER

The second run starts the daemon on that layout as a login would, and checks that the
workspace comes back under the same number, with its terminal.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESTORE = sys.argv[1:2] == ["--restore"]
HOME = sys.argv[2] if RESTORE else tempfile.mkdtemp(prefix="sidelit-hypr-daemon-")
os.environ.update(SIDELIT_HOME=HOME, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
USER = os.environ.get("USER") or "tester"
with open(os.path.join(HOME, "config.json"), "w") as f:
    # a terminal comes back only where its directory is known: the prompt title ("user@host:dir")
    # tells it, and foot sets one here where the shell does not
    json.dump({"terminal": f"foot --working-directory={{cwd}} --title={USER}@host:{{cwd}}",
               "terminal_cmd": "foot --working-directory={cwd} sh -c {command}",
               "check_turns": False, "restore_on_login": RESTORE, "restore_delay": 1,
               "templates": [{"name": "Empty terminal", "steps": [{"type": "terminal"}]}]}, f)
sys.path.insert(0, ROOT)

from sidelit import ipc  # noqa: E402  (after SIDELIT_HOME: it names the socket)

failed = []


def check(what, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failed.append(what)


def until(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            value = fn()
        except (ConnectionError, RuntimeError, OSError):
            value = None
        if value:
            return value
        time.sleep(0.5)
    return None


def tabs():
    return ipc.call("state").get("tabs") or []


def tab(ws_id):
    return next((t for t in tabs() if t["ws"] == ws_id), None)


def hyprland_workspaces() -> dict:
    """Place on the sidebar -> (Hyprland's number, pinned), from `sidelit ws list`."""
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
    found = {}
    for line in out.stdout.splitlines():
        if " idx=" in line:
            idx = int(line.split("idx=")[1].split()[0])
            number = line.split("id='")[1].split("'")[0]
            found[idx] = (number, line[1] == "P")
    return found


def windows_on(index: int) -> list[str]:
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "toplevels"], capture_output=True, text=True, cwd=ROOT)
    return [line for line in out.stdout.splitlines() if line.startswith(f"ws {index}:")]


def new_workspace(folder: str):
    """'+' with a terminal in a folder of its own (its title, with the folder, tells it apart)."""
    cwd = os.path.join(HOME, folder)
    os.makedirs(cwd, exist_ok=True)
    ws_id = ipc.call("new", template="Empty terminal", cwd=cwd)
    found = until(lambda: (t := tab(ws_id)) and t["windows"] and t, 20)
    return ws_id, found


def run() -> int:
    state = until(lambda: ipc.call("state"), 15)
    check("the daemon runs on Hyprland", bool(state))
    if not state:
        return 1
    check("pin in its caps, no move", "pin" in state["caps"] and "move" not in state["caps"], state["caps"])
    start = hyprland_workspaces()
    first_number = start[1][0] if 1 in start else "?"
    foot = subprocess.Popen(["foot", f"--working-directory={HOME}", f"--title={USER}@host:{HOME}", "sh"])
    first = until(lambda: next((t for t in tabs() if t["windows"]), None), 15)
    check("a window shows on its workspace's tab", first is not None, tabs())
    check("with Hyprland's key on the chip", first is not None and first["shortcut"] == f"⊞ {first_number}",
          first and first["shortcut"])
    kept = until(lambda: next((t for t in tabs() if t["ws"] and t["windows"]), None), 25)
    check("and becomes a kept workspace once things are calm", kept is not None, tabs())
    check("which Hyprland now keeps (persistent)",
          bool(until(lambda: kept and hyprland_workspaces().get(kept["space"], ("", False))[1], 10)), hyprland_workspaces())

    count = len(hyprland_workspaces())
    numbers = {n for n, _ in hyprland_workspaces().values()}
    ws_id, made = new_workspace("a")
    now = hyprland_workspaces()
    check("'+' adds a workspace with a terminal on it", made is not None and len(now) == count + 1, now)
    added = now.get(made["space"], ("?", False)) if made else ("?", False)
    check("under the next number, kept", added[1] and added[0] not in numbers
          and int(added[0]) == max(int(n) for n in numbers if n.isdigit()) + 1, (added, numbers))
    check("and switches to it", made is not None and made["active"], made)
    ipc.call("close", ws=ws_id)
    check("closing it unpins it", bool(until(lambda: not hyprland_workspaces().get(made["space"], ("", False))[1], 20)),
          hyprland_workspaces())
    check("and its tab goes", bool(until(lambda: tab(ws_id) is None, 10)))
    # (Sidelit then goes to the next workspace; with two screens that may be on the other one,
    # and Hyprland keeps a workspace a screen shows: go back to the first, on its screen)
    ipc.call("activate", ws=kept["ws"])
    check("and Hyprland drops it, emptied", bool(until(lambda: len(hyprland_workspaces()) == count, 20)),
          hyprland_workspaces())

    ws_id, made = new_workspace("b")
    title = made["windows"][0]["title"] if made else "?"
    there = len(windows_on(kept["space"] if kept else 1))
    subprocess.run([sys.executable, "-m", "sidelit", "ws", "movewin", title, first_number], capture_output=True, cwd=ROOT)
    moved = until(lambda: len(windows_on(kept["space"] if kept else 1)) == there + 1, 10)
    check("a window moved by hand stays where you put it", bool(moved))
    gone = until(lambda: tab(ws_id) is None, 20)
    check("the emptied workspace is let go", bool(gone))
    # (Hyprland keeps the workspace you look at, as COSMIC does: it goes once you leave it)
    ipc.call("activate", ws=kept["ws"] if kept else None)
    check("and Hyprland drops it once you leave", bool(until(lambda: len(hyprland_workspaces()) == count, 10)),
          hyprland_workspaces())

    ws_id, made = new_workspace("c")
    left = hyprland_workspaces().get(made["space"], ("?", False))[0] if made else "?"
    kept_again = until(lambda: (t := tab(ws_id)) and t["ws"] and t, 10)
    check("a third workspace is kept, for the login check", kept_again is not None)
    foot.terminate()
    print(f"     left workspace {left} with a terminal, layout in {HOME}", flush=True)
    print(f"     next: in a fresh Hyprland, python3 tests/hyprland_daemon_live.py --restore {HOME} {left}", flush=True)
    return 0


def restore(number: str) -> int:
    live = {n for n, _ in hyprland_workspaces().values()}
    # (a new workspace gets the number after every one in use or in the layout: getting this one
    # back means it was asked for by its number)
    check("this is a fresh Hyprland: workspace " + number + " is not there yet", number not in live, live)
    back = until(lambda: next((i for i, (n, pinned) in hyprland_workspaces().items() if n == number and pinned), None), 40)
    check(f"the login brings workspace {number} back, kept", back is not None, hyprland_workspaces())
    check("with its terminal on it", bool(back and until(lambda: windows_on(back), 30)), windows_on(back or 0))
    restored = until(lambda: next((t for t in tabs() if t["ws"] and t["space"] == back and t["windows"]), None), 20)
    check("and its tab", restored is not None, tabs())
    return 0


def main() -> int:
    log = open(os.path.join(HOME, "daemon.log"), "a")
    daemon = subprocess.Popen([sys.executable, "-m", "sidelit", "daemon"], stdout=log, stderr=log, cwd=ROOT)
    try:
        if RESTORE:
            restore(sys.argv[3])
        else:
            run()
    finally:
        daemon.terminate()
        daemon.wait(10)
        log.close()
    if failed:
        print(open(os.path.join(HOME, "daemon.log")).read()[-4000:])
    print("FAILED: " + ", ".join(failed) if failed else "all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
