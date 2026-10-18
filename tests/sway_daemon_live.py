"""The daemon on a real sway, by hand: not part of the unit tests.

Like sway_live.py, run it in a sway session you can spare or a headless one; it needs foot and
swaymsg. It starts a test daemon of its own (SIDELIT_HOME in a temporary directory, so not yours)
and drives it the way the sidebar does: a window becomes a kept workspace; '+' makes a new
numbered workspace and opens a terminal there; closing a workspace lets it go and sway drops it;
a window moved by hand is remembered where it went. It leaves one kept workspace with a
terminal, and says how to check that a login brings it back:

    python3 tests/sway_daemon_live.py
    (quit sway, start a fresh one, then in it:)
    python3 tests/sway_daemon_live.py --restore HOME NAME
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESTORE = sys.argv[1:2] == ["--restore"]
HOME = sys.argv[2] if RESTORE else tempfile.mkdtemp(prefix="sidelit-sway-daemon-")
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


def kept() -> dict:
    """Place on the sidebar -> (sway's name for it, kept), from `sidelit ws list`."""
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
    found = {}
    for line in out.stdout.splitlines():
        if " idx=" in line:
            idx = int(line.split("idx=")[1].split()[0])
            found[idx] = (line.split("name='")[1].rsplit("'", 1)[0], line[1] == "P")
    return found


def in_sway() -> list[str]:
    out = subprocess.run(["swaymsg", "-t", "get_workspaces", "-r"], capture_output=True, text=True).stdout
    return [w["name"] for w in json.loads(out or "[]")]


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
    check("the daemon runs on sway", bool(state))
    if not state:
        return 1
    check("pin in its caps, move not", "pin" in state["caps"] and "move" not in state["caps"], state["caps"])
    foot = subprocess.Popen(["foot", f"--working-directory={HOME}", f"--title={USER}@host:{HOME}", "sh"])
    first = until(lambda: next((t for t in tabs() if t["windows"] and t["active"]), None), 15)
    check("a window shows on its workspace's tab", first is not None, tabs())
    name = kept().get(first["space"], ("?", False))[0] if first else "?"
    check("with sway's key on the chip", first is not None and (first["shortcut"] == f"⊞ {int(name) % 10}" if name.isdigit()
                                                                 else True), first and (name, first["shortcut"]))
    home = until(lambda: next((t for t in tabs() if t["ws"] and t["windows"]), None), 25)
    check("and becomes a kept workspace once things are calm", home is not None, tabs())
    check("kept by Sidelit", bool(until(lambda: home and kept().get(home["space"], ("", False))[1], 10)), kept())

    before = set(in_sway())
    ws_id, made = new_workspace("a")
    added = kept().get(made["space"], ("", False))[0] if made else ""
    check("'+' makes a new numbered workspace and opens a terminal there",
          made is not None and added.isdigit() and added not in before and added in in_sway(), (added, before))
    check("and switches to it", made is not None and made["active"], made)
    ipc.call("close", ws=ws_id)
    check("closing it lets it go", bool(until(lambda: tab(ws_id) is None, 20)), tabs())
    check("and sway drops it, emptied (Sidelit went on to another one)", bool(until(lambda: added not in in_sway(), 15)),
          in_sway())
    check("and Sidelit no more lists it", bool(until(lambda: added not in [n for n, _ in kept().values()], 10)), kept())

    ws_id, made = new_workspace("b")
    title = made["windows"][0]["title"] if made else "?"
    place = until(lambda: home and next((t["space"] for t in tabs() if t["ws"] == home["ws"]), None), 5) or 1
    there = len(windows_on(place))
    subprocess.run([sys.executable, "-m", "sidelit", "ws", "movewin", title, kept()[place][0]], capture_output=True, cwd=ROOT)
    moved = until(lambda: len(windows_on(next((t["space"] for t in tabs() if t["ws"] == home["ws"]), place))) == there + 1, 10)
    check("a window moved by hand stays where you put it", bool(moved))
    check("the emptied workspace is let go", bool(until(lambda: tab(ws_id) is None, 20)))

    kept_tabs = len([t for t in tabs() if t["ws"]])
    place = next((t["space"] for t in tabs() if t["ws"] == home["ws"]), None) if home else None
    old = kept().get(place, ("", False))[0]
    subprocess.run(["swaymsg", f'rename workspace "{old}" to "{old}: renamed"'], capture_output=True)
    check("a workspace you rename in sway itself", bool(until(lambda: f"{old}: renamed" in in_sway(), 5)), in_sway())
    time.sleep(3)
    check("is still the same kept workspace, windows and all",
          home is not None and bool(tab(home["ws"])) and tab(home["ws"])["windows"]
          and len([t for t in tabs() if t["ws"]]) == kept_tabs, tabs())
    check("and no stand-in is left under its old name", old not in [n for n, _ in kept().values()], kept())

    ws_id, made = new_workspace("c")
    check("a third workspace is kept, for the login check", bool(until(lambda: (t := tab(ws_id)) and t["ws"] and t, 10)))
    now = tab(ws_id)
    left = kept().get(now["space"], ("?", False))[0] if now else "?"
    foot.terminate()
    print(f"     left workspace {left} with a terminal, layout in {HOME}", flush=True)
    print(f"     next: in a fresh sway, python3 tests/sway_daemon_live.py --restore {HOME} {left}", flush=True)
    return 0


def restore(name: str) -> int:
    check(f"this is a fresh sway: no workspace {name} yet", name not in in_sway(), in_sway())
    back = until(lambda: name in in_sway() and next((i for i, (n, k) in kept().items() if n == name and k), None), 40)
    check(f"the login brings workspace {name} back", back is not None, (in_sway(), kept()))
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
