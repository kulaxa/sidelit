"""A login restore on a real GNOME Shell with Sidelit's extension, by hand: not part of the unit
tests. It needs foot, and the shell's unsafe mode (to turn the extension off and on).

Like the other live tests, run it in a GNOME session you can spare or a headless one. A test
daemon (SIDELIT_HOME in a temporary directory) gets two kept workspaces with a terminal each;
then a login is played: the daemon stops, the terminals close, the extension is turned off and
on (GNOME forgets the kept workspaces and their ids, as at a real login) and its empty
workspaces go. The daemon started again must bring both workspaces and their terminals back.

    python3 tests/gnome_restore_live.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="sidelit-restore-live-")
os.environ.update(SIDELIT_HOME=HOME, XDG_CURRENT_DESKTOP="GNOME", PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
# the shell's prompt title ("user@host:dir") tells which directory a terminal is in, and a
# terminal comes back only where that is known
os.environ.setdefault("USER", "tester")
with open(os.path.join(HOME, "config.json"), "w") as f:
    # (and where the shell sets no such title, as Ubuntu's does not for foot, foot's own does)
    json.dump({"terminal": "foot --working-directory={cwd} --title=tester@host:{cwd}",
               "terminal_cmd": "foot --working-directory={cwd} {command}",
               "check_turns": False, "restore_on_login": True, "restore_delay": 2,
               "templates": [{"name": "Empty terminal", "steps": [{"type": "terminal"}]}]}, f)
sys.path.insert(0, ROOT)

from sidelit import config, gnome, ipc  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402

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


def shell(code: str):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval",
                         GLib.Variant("(s)", (code,)), None, 0, 5000, None).unpack()


def start_daemon(log):
    return subprocess.Popen([sys.executable, "-m", "sidelit", "daemon"], stdout=log, stderr=log, cwd=ROOT)


def main() -> int:
    log = open(os.path.join(HOME, "daemon.log"), "w")
    daemon = start_daemon(log)
    try:
        check("the daemon runs", bool(until(lambda: ipc.call("state"), 20)))
        made = []
        for cwd in ("/tmp", "/"):
            ws_id = ipc.call("new", template="Empty terminal", cwd=cwd)
            made.append(ws_id)
            until(lambda w=ws_id: next((t for t in tabs() if t["ws"] == w and t["windows"]), None), 20)
        check("two kept workspaces with a terminal each",
              all(next((t for t in tabs() if t["ws"] == w and t["windows"]), None) for w in made), tabs())
        time.sleep(3)  # (the layout is saved a moment after a change)

        # a logout: the daemon goes first (systemd stops it), then the windows
        daemon.terminate()
        daemon.wait(10)
        subprocess.run(["pkill", "-f", "foot --working-directory"], check=False)
        time.sleep(1)
        # a login: a GNOME Shell that knows none of it (new ids, nothing kept), its windows gone
        shell(f"Main.extensionManager.disableExtension('{gnome.UUID}')")
        time.sleep(1)
        shell(f"Main.extensionManager.enableExtension('{gnome.UUID}')")
        time.sleep(1)
        config.RESTORED_MARKER.unlink(missing_ok=True)  # (this shell had its restore already)

        daemon = start_daemon(log)
        check("the daemon starts again", bool(until(lambda: ipc.call("state"), 20)))
        back = until(lambda: all(next((t for t in tabs() if t["ws"] == w and t["windows"] and t["space"]), None)
                                 for w in made), 60)
        check("the login restore brings both workspaces back, a terminal on each", bool(back),
              [(t["ws"], t["space"], len(t["windows"])) for t in tabs()])
        spaces = [next(t["space"] for t in tabs() if t["ws"] == w) for w in made] if back else []
        check("each on a workspace of its own", len(set(spaces)) == 2, spaces)
        out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
        kept = [line for line in out.stdout.splitlines() if " idx=" in line and line[1] == "P"]
        check("which GNOME keeps (pinned)", len(kept) >= 2, out.stdout)
    finally:
        daemon.terminate()
        daemon.wait(10)
        subprocess.run(["pkill", "-f", "foot --working-directory"], check=False)
        log.close()
    if failed:
        print(open(os.path.join(HOME, "daemon.log")).read()[-5000:])
    print("FAILED: " + ", ".join(failed) if failed else "all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
