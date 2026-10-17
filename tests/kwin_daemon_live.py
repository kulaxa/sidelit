"""The daemon on a real KWin (Plasma 6), by hand: not part of the unit tests.

Like kwin_live.py, run it in a KDE session you can spare or a headless one; it needs foot. It
starts a test daemon of its own (SIDELIT_HOME in a temporary directory, so not yours) and drives
it the way the sidebar does: a window becomes a kept workspace, '+' adds a desktop with a
terminal, closing a workspace removes its desktop, a window moved by hand is remembered where it
went, and forgetting a workspace or emptying one leaves KDE's desktops alone.

    python3 tests/kwin_daemon_live.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="sidelit-daemon-live-")
os.environ.update(SIDELIT_HOME=HOME, XDG_CURRENT_DESKTOP="KDE", PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
with open(os.path.join(HOME, "config.json"), "w") as f:
    json.dump({"terminal": "foot --working-directory={cwd}", "terminal_cmd": "foot --working-directory={cwd} {command}",
               "check_turns": False, "restore_on_login": False,
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
        except (ConnectionError, RuntimeError):
            value = None
        if value:
            return value
        time.sleep(0.5)
    return None


def tabs():
    return ipc.call("state").get("tabs") or []


def tab(ws_id):
    return next((t for t in tabs() if t["ws"] == ws_id), None)


def desktops() -> int:
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
    return sum(1 for line in out.stdout.splitlines() if " idx=" in line)


def windows_on(index: int) -> list[str]:
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "toplevels"], capture_output=True, text=True, cwd=ROOT)
    return [line for line in out.stdout.splitlines() if line.startswith(f"ws {index}:")]


def new_workspace():
    ws_id = ipc.call("new", template="Empty terminal", cwd="/tmp")
    found = until(lambda: (t := tab(ws_id)) and t["windows"] and t, 20)
    return ws_id, found


def main() -> int:
    log = open(os.path.join(HOME, "daemon.log"), "w")
    daemon = subprocess.Popen([sys.executable, "-m", "sidelit", "daemon"], stdout=log, stderr=log, cwd=ROOT)
    foot = None
    try:
        state = until(lambda: ipc.call("state"), 15)
        check("the daemon runs on KDE", bool(state))
        if not state:
            return 1
        check("no pin or move in its caps", "pin" not in state["caps"] and "move" not in state["caps"], state["caps"])
        foot = subprocess.Popen(["foot", "--working-directory=/tmp", "sh"])
        first = until(lambda: next((t for t in tabs() if t["windows"]), None), 15)
        check("a window shows on its desktop's tab", first is not None and first["space"] == 1, first)
        check("with KDE's shortcut on the chip", first is not None and first["shortcut"] == "Ctrl F1", first)
        kept = until(lambda: next((t for t in tabs() if t["ws"] and t["space"] == 1), None), 25)
        check("and becomes a kept workspace once things are calm", kept is not None, tabs())

        count = desktops()
        ws_id, made = new_workspace()
        check("'+' adds a desktop with a terminal on it", made is not None and desktops() == count + 1, made)
        check("and switches to it", made is not None and made["active"], made)
        ipc.call("close", ws=ws_id)
        check("closing it removes the desktop", bool(until(lambda: desktops() == count, 20)), desktops())
        check("and its tab", tab(ws_id) is None)

        ws_id, made = new_workspace()
        space = made["space"] if made else 2
        title = (made["windows"][0]["title"] if made else "")[:12]
        subprocess.run([sys.executable, "-m", "sidelit", "ws", "movewin", title, "1"], capture_output=True, cwd=ROOT)
        moved = until(lambda: len(windows_on(1)) == 2, 10)
        check("a window moved by hand stays where you put it", bool(moved) and not windows_on(space))
        gone = until(lambda: tab(ws_id) is None, 20)
        check("the emptied workspace is let go", bool(gone))
        check("but its KDE desktop stays", desktops() == count + 1, desktops())

        kept = next((t for t in tabs() if t["ws"] and t["space"] == 1), None)
        if kept is not None:
            ipc.call("forget", ws=kept["ws"])
            time.sleep(4)
        check("forgetting a workspace keeps its desktop and windows",
              kept is not None and len(windows_on(1)) == 2 and desktops() == count + 1 and foot.poll() is None)
    finally:
        if foot is not None:
            foot.terminate()
        daemon.terminate()
        daemon.wait(10)
        log.close()
    if failed:
        print(open(os.path.join(HOME, "daemon.log")).read()[-4000:])
    print("FAILED: " + ", ".join(failed) if failed else "all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
