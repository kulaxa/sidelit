"""The X11 backend and a daemon on a real X11 window manager, by hand: not part of the unit tests.

Run it in an X11 session you can spare, or a hidden one (Xvfb with xfwm4, Muffin/Cinnamon, Marco
or Openbox); it needs xterm. It adds, renames and removes desktops and opens, moves, focuses and
closes xterms of its own, then starts a test daemon (SIDELIT_HOME in a temporary directory, so
not yours) and drives it the way the sidebar does: a window shows on its desktop's tab, '+' adds
a desktop with a terminal, closing that workspace removes the desktop. Prints each check; exits
1 if any failed.

    PYTHONPATH=. python3 tests/x11_live.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="sidelit-x11-live-")
os.environ.update(SIDELIT_HOME=HOME, SIDELIT_BACKEND="x11", PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
with open(os.path.join(HOME, "config.json"), "w") as f:
    json.dump({"terminal": "xterm", "terminal_cmd": "xterm -e {command}", "check_turns": False,
               "restore_on_login": False, "templates": [{"name": "Empty terminal", "steps": [{"type": "terminal"}]}]}, f)
sys.path.insert(0, ROOT)

from sidelit import backend, ipc, x11  # noqa: E402  (after SIDELIT_HOME: it names the socket)

failed = []


def check(what, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failed.append(what)


def until(fn, secs, client=None):
    end = time.time() + secs
    while time.time() < end:
        if client is not None:
            client.roundtrip()
        try:
            value = fn()
        except (ConnectionError, RuntimeError):
            value = None
        if value:
            return value
        time.sleep(0.1 if client is not None else 0.5)
    return None


def xterm(title: str) -> subprocess.Popen:
    return subprocess.Popen(["xterm", "-T", title, "-e", "sleep", "600"])


def window(client, title: str):
    return next((t for t in client.toplevels.values() if t.title == title), None)


def root_names() -> list:
    out = subprocess.run(["xprop", "-root", "_NET_DESKTOP_NAMES"], capture_output=True, text=True).stdout
    return [n.strip().strip('"') for n in out.split("=", 1)[1].split(",")] if "=" in out else []


def the_backend() -> None:
    check("an X11 session picks the x11 backend",
          backend.chosen({"XDG_CURRENT_DESKTOP": os.environ.get("XDG_CURRENT_DESKTOP", ""), "XDG_SESSION_TYPE": "x11"}) == "x11")
    ids_path = os.path.join(HOME, "live-ids.json")
    from pathlib import Path
    c = x11.X11Client(primary=True, ids_path=Path(ids_path))
    terms = []
    try:
        start = c.list()
        check("the window manager's desktops are there", len(start) >= 1, start)
        before = [w.id for w in start]
        again = x11.X11Client(primary=True, ids_path=Path(ids_path))
        check("they keep their ids for the next client", [w.id for w in again.list()] == before)
        again.close()

        terms.append(xterm("live a"))
        a = until(lambda: window(c, "live a"), 10, c)
        check("a new window appears, with its app and process", a is not None and a.app_id == "xterm" and a.pid == terms[0].pid,
              a and (a.app_id, a.pid, terms[0].pid))
        check("on the desktop you are on", a is not None and c.workspace_of(a) is not None and c.workspace_of(a).active)

        one = c.create("live one")
        check("a new desktop at the end, with its name", one is not None and one.index == len(start) + 1
              and root_names()[len(start)] == "live one", (one, root_names()))
        two = c.create("live two")
        check("and another", two is not None and two.index == len(start) + 2, two)
        c.move_toplevel(a, two)
        check("a window goes to another desktop", bool(until(lambda: c.workspace_of(a) is two, 5, c)),
              c.workspace_of(a))
        c.activate(one)
        check("switching desktops", bool(until(lambda: c.active() is one, 5, c)), c.active())
        c.focus(a)
        check("focusing a window takes you to its desktop", bool(until(lambda: a.focused and c.active() is two, 5, c)),
              (a.focused, c.active()))
        c.rename(one, "live renamed")
        check("renaming", bool(until(lambda: "live renamed" in root_names(), 5, c)), root_names())

        ids = [w.id for w in c.list()]
        c.remove(one)  # in the middle (two comes after it)
        after = until(lambda: len(c.list()) == len(ids) - 1 and c.list(), 5, c)
        check("removing a desktop in the middle", bool(after) and [w.id for w in after] == ids[:-2] + [ids[-1]],
              (ids, after))
        check("the window after it went down with its desktop", bool(after) and c.workspace_of(a) is not None
              and c.workspace_of(a).id == two.id, c.workspace_of(a))
        check("and so did the name", root_names()[len(start)] == "live two", root_names())

        c.close_toplevel(a)
        check("closing a window", bool(until(lambda: window(c, "live a") is None, 5, c)))
        c.remove(two)
        check("removing the last desktop", bool(until(lambda: len(c.list()) == len(start), 5, c)), c.list())
    finally:
        for t in terms:
            t.terminate()
        c.close()


def cinnamon_removes_one() -> None:
    """Cinnamon removes a workspace in the middle by itself (its workspace switcher's menu, Expo):
    the windows after it say which one went. Driven through Cinnamon's Eval on the session bus."""
    from pathlib import Path

    def cinnamon(script: str) -> str:
        return subprocess.run(["gdbus", "call", "--session", "--dest", "org.Cinnamon", "--object-path", "/org/Cinnamon",
                               "--method", "org.Cinnamon.Eval", script], capture_output=True, text=True).stdout

    c = x11.X11Client(primary=True, ids_path=Path(HOME) / "cinnamon-ids.json")
    terms = []
    try:
        while len(c.list()) < 4:
            c.create("")
        for n in (3, 4):
            terms.append(xterm(f"live on {n}"))
            w = until(lambda: window(c, f"live on {n}"), 10, c)
            c.move_toplevel(w, c.list()[n - 1])
        until(lambda: all(c.workspace_of(window(c, f"live on {n}")).index == n for n in (3, 4)), 5, c)
        ids = [w.id for w in c.list()]
        cinnamon("Main._removeWorkspace(global.workspace_manager.get_workspace_by_index(1))")
        after = until(lambda: len(c.list()) == len(ids) - 1 and c.list(), 5, c)
        check("Cinnamon removing a workspace in the middle: the others keep their ids",
              bool(after) and [w.id for w in after] == ids[:1] + ids[2:], (ids, after))
        check("and their windows stay with them",
              all(c.workspace_of(window(c, f"live on {n}")).id == ids[n - 1] for n in (3, 4)))
    finally:
        for t in terms:
            t.terminate()
        c.close()


def tabs():
    return ipc.call("state").get("tabs") or []


def desktops() -> int:
    out = subprocess.run(["xprop", "-root", "_NET_NUMBER_OF_DESKTOPS"], capture_output=True, text=True).stdout
    return int(out.split("=")[1]) if "=" in out else 0


def the_daemon() -> None:
    log = open(os.path.join(HOME, "daemon.log"), "w")
    daemon = subprocess.Popen([sys.executable, "-m", "sidelit", "daemon"], stdout=log, stderr=log, cwd=ROOT)
    term = None
    try:
        state = until(lambda: ipc.call("state"), 15)
        check("the daemon runs on X11", bool(state))
        if not state:
            return
        check("no pin or move in its caps", "pin" not in state["caps"] and "move" not in state["caps"], state["caps"])
        term = xterm("live daemon")
        first = until(lambda: next((t for t in tabs() if any(w["title"] == "live daemon" for w in t["windows"])), None), 15)
        check("a window shows on its desktop's tab", first is not None and first["active"], first)
        count = desktops()
        ws_id = ipc.call("new", template="Empty terminal", cwd="/tmp")
        made = until(lambda: (t := next((t for t in tabs() if t["ws"] == ws_id), None)) and t["windows"] and t, 25)
        check("'+' adds a desktop with a terminal on it", made is not None and desktops() == count + 1, (made, desktops()))
        check("and switches to it", made is not None and made["active"], made)
        ipc.call("close", ws=ws_id)
        check("closing it removes the desktop", bool(until(lambda: desktops() == count, 20)), desktops())
        check("and its tab", not any(t["ws"] == ws_id for t in tabs()))
    finally:
        if term is not None:
            term.terminate()
        daemon.terminate()
        daemon.wait(10)
        log.close()
    if failed:
        print(open(os.path.join(HOME, "daemon.log")).read()[-3000:])


def main() -> int:
    the_backend()
    if "X-CINNAMON" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        cinnamon_removes_one()
    the_daemon()
    print("FAILED: " + ", ".join(failed) if failed else "all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
