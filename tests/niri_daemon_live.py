"""The daemon on a real niri, by hand: not part of the unit tests.

Like niri_live.py, run it in a niri session you can spare or a nested one; it needs foot. It
starts a test daemon of its own (SIDELIT_HOME in a temporary directory, so not yours) and drives
it the way the sidebar does: a window becomes a kept workspace, which niri then keeps by its name;
'+' names the empty workspace at the end and opens a terminal there; closing a workspace takes its
name off so niri drops it; a window moved by hand is remembered where it went. It leaves one kept
workspace with a terminal, and says how to check that a login brings it back:

    python3 tests/niri_daemon_live.py
    (quit niri, start a fresh one, then in it:)
    python3 tests/niri_daemon_live.py --restore HOME NAME
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESTORE = sys.argv[1:2] == ["--restore"]
HOME = sys.argv[2] if RESTORE else tempfile.mkdtemp(prefix="sidelit-niri-daemon-")
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


def niri_workspaces() -> dict:
    """Place on the sidebar -> (niri's name for it, kept), from `sidelit ws list`."""
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
    found = {}
    for line in out.stdout.splitlines():
        if " idx=" in line:
            idx = int(line.split("idx=")[1].split()[0])
            name = line.split("name='")[1].rsplit("'", 1)[0]
            found[idx] = (name, line[1] == "P")
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


def names() -> set:
    return {n for n, _ in niri_workspaces().values() if n}


def run() -> int:
    state = until(lambda: ipc.call("state"), 15)
    check("the daemon runs on niri", bool(state))
    if not state:
        return 1
    check("pin and move in its caps", "pin" in state["caps"] and "move" in state["caps"], state["caps"])
    foot = subprocess.Popen(["foot", f"--working-directory={HOME}", f"--title={USER}@host:{HOME}", "sh"])
    first = until(lambda: next((t for t in tabs() if t["windows"]), None), 15)
    check("a window shows on its workspace's tab", first is not None, tabs())
    check("with niri's key on the chip", first is not None and first["shortcut"] == "⊞ 1", first and first["shortcut"])
    kept = until(lambda: next((t for t in tabs() if t["ws"] and t["windows"]), None), 25)
    check("and becomes a kept workspace once things are calm", kept is not None, tabs())
    check("which niri now keeps by its name",
          bool(until(lambda: kept and niri_workspaces().get(kept["space"], ("", False))[0].startswith("sidelit-"), 10)),
          niri_workspaces())

    before = names()
    ws_id, made = new_workspace("a")
    now = niri_workspaces()
    added = now.get(made["space"], ("", False)) if made else ("", False)
    check("'+' names a workspace and opens a terminal there", made is not None and added[0].startswith("sidelit-")
          and added[0] not in before, (added, before))
    check("and switches to it", made is not None and made["active"], made)
    ipc.call("close", ws=ws_id)
    check("closing it takes its name off", bool(until(lambda: added[0] not in names(), 20)), niri_workspaces())
    check("and its tab goes", bool(until(lambda: tab(ws_id) is None, 10)))
    check("and niri drops it, emptied (Sidelit went on to the next one)",
          bool(until(lambda: len([n for n, _ in niri_workspaces().values() if not n]) <= 1, 15)), niri_workspaces())

    ws_id, made = new_workspace("b")
    title = made["windows"][0]["title"] if made else "?"
    there = len(windows_on(kept["space"] if kept else 1))
    subprocess.run([sys.executable, "-m", "sidelit", "ws", "movewin", title, niri_workspaces()[kept["space"]][0]],
                   capture_output=True, cwd=ROOT)
    moved = until(lambda: len(windows_on(kept["space"] if kept else 1)) == there + 1, 10)
    check("a window moved by hand stays where you put it", bool(moved))
    check("the emptied workspace is let go", bool(until(lambda: tab(ws_id) is None, 20)))

    kept_tabs = len([t for t in tabs() if t["ws"]])
    place = next((t["space"] for t in tabs() if t["ws"] == kept["ws"]), None) if kept else None
    old = niri_workspaces().get(place, ("", False))[0]
    subprocess.run(["niri", "msg", "action", "set-workspace-name", "--workspace", old, "renamed-in-niri"])
    check("a workspace you rename in niri itself",
          bool(until(lambda: "renamed-in-niri" in names(), 5)), niri_workspaces())
    time.sleep(3)
    check("is still the same kept workspace, windows and all",
          kept is not None and bool(tab(kept["ws"])) and tab(kept["ws"])["windows"]
          and len([t for t in tabs() if t["ws"]]) == kept_tabs, tabs())

    ws_id, made = new_workspace("c")
    check("a third workspace is kept, for the login check", bool(until(lambda: (t := tab(ws_id)) and t["ws"] and t, 10)))
    time.sleep(2)  # (the emptied one before it goes once you have left it: places shift)
    now = tab(ws_id)
    left = niri_workspaces().get(now["space"], ("?", False))[0] if now else "?"
    foot.terminate()
    print(f"     left workspace {left} with a terminal, layout in {HOME}", flush=True)
    print(f"     next: in a fresh niri, python3 tests/niri_daemon_live.py --restore {HOME} {left}", flush=True)
    return 0


def restore(name: str) -> int:
    check(f"this is a fresh niri: no workspace {name} yet", name not in names(), niri_workspaces())
    back = until(lambda: next((i for i, (n, kept) in niri_workspaces().items() if n == name and kept), None), 40)
    check(f"the login brings workspace {name} back", back is not None, niri_workspaces())
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
