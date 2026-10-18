"""The daemon on a real GNOME Shell with Sidelit's extension, by hand: not part of the unit tests.

Like gnome_live.py, run it in a GNOME session you can spare or a headless one; it needs foot. It
starts a test daemon of its own (SIDELIT_HOME in a temporary directory, so not yours) and drives
it the way the sidebar does: a window becomes a kept (pinned) workspace, '+' pins GNOME's empty
workspace with a terminal on it, closing a workspace lets GNOME drop it, a window moved by hand
is remembered where it went, a workspace dragged to another place moves there in GNOME too.

With --real (only in a throwaway session: it runs as the real Sidelit, with your config) the
daemon holds Sidelit's own bus name, so the extension runs the sidebar: it is clicked with a
virtual pointer, and with --shot DIR screenshots are saved there.

    python3 tests/gnome_daemon_live.py [--real] [--shot DIR]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = "--real" in sys.argv
STATIC = bool(os.environ.get("GNOME_STATIC"))  # GNOME set to a fixed number of workspaces (the harness)
SHOTS = sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else None
HOME = tempfile.mkdtemp(prefix="sidelit-daemon-live-")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "sidelit") \
    if REAL else HOME
os.environ.update(XDG_CURRENT_DESKTOP="GNOME", PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
if not REAL:
    os.environ["SIDELIT_HOME"] = HOME
os.makedirs(CONFIG_DIR, exist_ok=True)
with open(os.path.join(CONFIG_DIR, "config.json"), "w") as f:
    json.dump({"terminal": "foot --working-directory={cwd}", "terminal_cmd": "foot --working-directory={cwd} {command}",
               "check_turns": False, "restore_on_login": False,
               # SIDELIT_TEST_SIDEBAR=gtk3: the GTK 3 sidebar the extension runs
               "sidebar": os.environ.get("SIDELIT_TEST_SIDEBAR", "auto"),
               "templates": [{"name": "Empty terminal", "steps": [{"type": "terminal"}]}]}, f)
sys.path.insert(0, ROOT)

from sidelit import ipc  # noqa: E402  (after SIDELIT_HOME: it names the socket)
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


def tab(ws_id):
    return next((t for t in tabs() if t["ws"] == ws_id), None)


def ws_list() -> list[str]:
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "list"], capture_output=True, text=True, cwd=ROOT)
    return [line for line in out.stdout.splitlines() if " idx=" in line]


def windows_on(index: int) -> list[str]:
    out = subprocess.run([sys.executable, "-m", "sidelit", "ws", "toplevels"], capture_output=True, text=True, cwd=ROOT)
    return [line for line in out.stdout.splitlines() if line.startswith(f"ws {index}:")]


def shell(code: str):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    ok, out = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval",
                            GLib.Variant("(s)", (code,)), None, 0, 5000, None).unpack()
    return out if ok else None


def screenshot(name: str):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync("org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot", "org.gnome.Shell.Screenshot",
                  "Screenshot", GLib.Variant("(bbs)", (False, False, path)), None, 0, 10000, None)
    print(f"     screenshot {path}", flush=True)


def click(x: int, y: int):
    """A click at (x, y), through a virtual pointer (GNOME Shell's own input, as remote desktop does)."""
    for _ in range(10):  # (GNOME may still be opening the overview it starts with)
        shell("Main.overview.hide()")
        time.sleep(0.5)
        if "SurfaceActor" in (shell(f"String(global.stage.get_actor_at_pos(imports.gi.Clutter.PickMode.REACTIVE, {x}, {y}))") or ""):
            break
    shell(f"""(() => {{
        const C = imports.gi.Clutter;
        const backend = global.stage.context?.get_backend?.() ?? C.get_default_backend();
        globalThis._sidelitPointer ??= backend.get_default_seat().create_virtual_device(C.InputDeviceType.POINTER_DEVICE);
        const p = globalThis._sidelitPointer, t = imports.gi.GLib.get_monotonic_time();
        p.notify_absolute_motion(t, {x}, {y});
        p.notify_absolute_motion(t, {x}, {y});  // (a new device's first motion lands only halfway)
    }})()""")
    time.sleep(0.4)  # the window sees the pointer come in before the press
    for state in ("PRESSED", "RELEASED"):
        shell(f"""(() => {{
            const C = imports.gi.Clutter;
            globalThis._sidelitPointer.notify_button(imports.gi.GLib.get_monotonic_time(), C.BUTTON_PRIMARY,
                C.ButtonState.{state});
        }})()""")
        time.sleep(0.1)


def new_workspace():
    ws_id = ipc.call("new", template="Empty terminal", cwd="/tmp")
    found = until(lambda: (t := tab(ws_id)) and t["windows"] and t, 20)
    return ws_id, found


def main() -> int:
    log = open(os.path.join(HOME, "daemon.log"), "w")
    daemon = subprocess.Popen([sys.executable, "-m", "sidelit", "daemon"], stdout=log, stderr=log, cwd=ROOT)
    foot = None
    try:
        state = until(lambda: ipc.call("state"), 20)
        check("the daemon runs on GNOME", bool(state))
        if not state:
            return 1
        if STATIC:
            check("fixed workspaces: move but no pin in its caps", "pin" not in state["caps"] and "move" in state["caps"],
                  state["caps"])
        else:
            check("pin and move in its caps", "pin" in state["caps"] and "move" in state["caps"], state["caps"])
        if REAL:
            shell("Main.overview.hide()")  # (GNOME starts in the overview, which hides every window)
        foot = subprocess.Popen(["foot", "--working-directory=/tmp", "sh"])
        first = until(lambda: next((t for t in tabs() if t["windows"]), None), 15)
        check("a window shows on its workspace's tab", first is not None and first["space"] == 1, first)
        check("with GNOME's shortcut on the chip", first is not None and first["shortcut"] == "⊞ Home", first)
        kept = until(lambda: next((t for t in tabs() if t["ws"] and t["space"] == 1), None), 25)
        check("and becomes a kept workspace once things are calm", kept is not None, tabs())
        check("which GNOME keeps (pinned)", bool(until(lambda: ws_list()[0][1] == "P", 10)), ws_list())

        count = len(ws_list())
        ws_id, made = new_workspace()
        check("'+' gives a workspace a terminal", made is not None, made)
        check("and switches to it", made is not None and made["active"], made)
        check("GNOME keeps an empty one after it" if not STATIC else "a fixed set gets one more",
              bool(until(lambda: len(ws_list()) == count + 1, 10)), ws_list())
        if REAL:
            time.sleep(1)
            screenshot("gnome-sidebar.png")
        ipc.call("close", ws=ws_id)
        check("closing it lets GNOME drop the workspace", bool(until(lambda: len(ws_list()) == count, 20)), ws_list())
        check("and its tab", tab(ws_id) is None)

        ws_id, made = new_workspace()
        space = made["space"] if made else 2
        title = (made["windows"][0]["title"] if made else "")[:12]
        subprocess.run([sys.executable, "-m", "sidelit", "ws", "movewin", title, "1"], capture_output=True, cwd=ROOT)
        moved = until(lambda: len(windows_on(1)) == 2, 10)
        check("a window moved by hand stays where you put it", bool(moved) and not windows_on(space))
        gone = until(lambda: tab(ws_id) is None, 20)
        check("the emptied workspace is let go", bool(gone))

        ws_a, made_a = new_workspace()
        ws_b, made_b = new_workspace()
        if made_a and made_b:
            ipc.call("reorder", space=made_b["space"], other=made_a["space"], before=True)
            order = until(lambda: (a := tab(ws_a)) and (b := tab(ws_b)) and b["space"] < a["space"] and (a, b), 10)
            check("dragging a tab before another reorders GNOME's workspaces", bool(order), [(t["ws"], t["space"]) for t in tabs()])

        if REAL:
            _sidebar_clicks()

        kept = next((t for t in tabs() if t["ws"] and t["space"] == 1), None)
        if kept is not None:
            ipc.call("forget", ws=kept["ws"])
            time.sleep(4)
        check("forgetting a workspace keeps its windows", kept is not None and len(windows_on(1)) == 2
              and foot.poll() is None)
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


def _sidebar_clicks():
    """The sidebar the extension runs: a click on a tab switches to its workspace."""
    found = until(lambda: json.loads(json.loads(shell(
        "JSON.stringify(global.display.list_all_windows().filter(w => w.get_title() === 'Sidelit')"
        ".map(w => { const r = w.get_frame_rect(); return [r.x, r.y, r.width, r.height]; }))") or '"[]"')), 20)
    check("the extension shows the sidebar", bool(found), found)
    if not found:
        return
    x, y, w, h = found[0]
    click(x + 40, y + 28)  # on the header, which does nothing: the overview GNOME started with is gone after it
    time.sleep(1)
    before = {t["ws"] for t in tabs() if t["ws"]}
    click(x + w // 2, y + h - 38)  # "New workspace", at the bottom
    made = until(lambda: next((t for t in tabs() if t["ws"] and t["ws"] not in before and t["windows"]), None), 20)
    check("a click on New workspace in the sidebar makes one, with a terminal", made is not None, tabs())
    other = next((t for t in tabs() if t["space"] and not t["active"] and t["ws"]), None)
    if other is None:
        return
    # the keyboard in a window first (the terminal in the middle of the screen), then one click
    # on a tab: the sidebar takes it at once, though it never takes the keyboard
    click(x + w + 400, y + 300)
    time.sleep(1)
    focused = shell("String(global.display.get_focus_window()?.get_wm_class())")
    check("a click in a window gives that window the keyboard", "foot" in (focused or ""), focused)
    # the tabs are listed under the header in workspace order, about 68 pixels apart
    rows = sorted(t["space"] for t in tabs() if t["space"])
    click(x + w // 2, y + 86 + rows.index(other["space"]) * 68)
    check("a click on a tab in the sidebar switches to it", bool(until(lambda: tab(other["ws"])["active"], 5)),
          other["space"])
    time.sleep(1)
    screenshot("gnome-sidebar-switched.png")
    # the closed workspaces' search takes typing (a dock gets the keyboard only for its popups)
    click(x + w - 30, y + 28)
    time.sleep(1)
    typed = shell("""(() => {
        const C = imports.gi.Clutter;
        const backend = global.stage.context?.get_backend?.() ?? C.get_default_backend();
        const k = backend.get_default_seat().create_virtual_device(C.InputDeviceType.KEYBOARD_DEVICE);
        for (const key of [C.KEY_f, C.KEY_o, C.KEY_o]) {
            const t = imports.gi.GLib.get_monotonic_time();
            k.notify_keyval(t, key, C.KeyState.PRESSED);
            k.notify_keyval(t + 1000, key, C.KeyState.RELEASED);
        }
        return String(global.display.get_focus_window()?.get_title());
    })()""")
    check("the history's search gets the keyboard", typed == '"Sidelit"', typed)
    time.sleep(1)
    screenshot("gnome-sidebar-history.png")


if __name__ == "__main__":
    sys.exit(main())
