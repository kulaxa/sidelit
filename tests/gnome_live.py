"""The GNOME backend against a real GNOME Shell with Sidelit's extension, by hand: not part of
the unit tests.

Run it in a GNOME session you can spare, or a headless one (`gnome-shell --headless` with a
private session bus, the extension installed and enabled, and the shell's unsafe mode on for
the window checks that use org.gnome.Shell.Eval). It adds, keeps, reorders and removes
workspaces, opens, moves, focuses and closes two windows of its own (GTK 3 and foot), turns the
extension off and on, and with `--sidebar` also checks that the extension runs the sidebar and
docks it (it needs `sidelit` in ~/.local/bin). Prints each check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/gnome_live.py [--sidebar]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import backend, gnome  # noqa: E402  (first: it asks gi for the versions it needs)
from gi.repository import Gio, GLib  # noqa: E402

WINDOW = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
GLib.set_prgname(sys.argv[1])
win = Gtk.Window(title=sys.argv[2])
win.set_default_size(300, 200)
win.connect("destroy", Gtk.main_quit)
win.show_all()
GLib.timeout_add(4000, lambda: win.set_title(sys.argv[2] + " (renamed)") and False)
Gtk.main()
"""

failed = []


def check(what: str, ok: bool, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failed.append(what)


def spin(seconds: float, until=None):
    """Run the GLib main context (where D-Bus arrives) for `seconds`, or until `until()`."""
    ctx, end = GLib.MainContext.default(), time.monotonic() + seconds
    while time.monotonic() < end and not (until and until()):
        if not ctx.iteration(False):
            time.sleep(0.01)


def window(client, app: str):
    return next((t for t in client.toplevels.values() if t.app_id == app and not t.closed), None)


def shell_eval(code: str):
    """Run JavaScript in GNOME Shell (unsafe mode) and return its JSON result, or None."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    ok, out = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval",
                            GLib.Variant("(s)", (code,)), None, 0, 5000, None).unpack()
    if not ok:
        print(f"     eval failed: {out}", flush=True)
        return None
    try:
        value = json.loads(out) if out else None
        return json.loads(value) if isinstance(value, str) else value  # (code that returns JSON text)
    except ValueError:
        return out


def toggle_extension(on: bool):
    shell_eval(f"Main.extensionManager.{'enableExtension' if on else 'disableExtension'}('{gnome.UUID}')")


def main() -> int:
    check("XDG_CURRENT_DESKTOP=GNOME picks the gnome backend", backend.chosen({"XDG_CURRENT_DESKTOP": "GNOME"}) == "gnome")
    check("so does Ubuntu's ubuntu:GNOME", backend.chosen({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}) == "gnome")
    check("but not Budgie's Budgie:GNOME", backend.chosen({"XDG_CURRENT_DESKTOP": "Budgie:GNOME"}) == "cosmic")
    changes = []
    c = gnome.GnomeClient(on_change=lambda: changes.append(time.monotonic()), primary=True, wait_s=10)
    procs = []
    try:
        check("a second daemon is refused the bus name", _second_daemon_refused())
        check("dynamic workspaces: pin is in caps", "pin" in c.caps and "move" in c.caps, sorted(c.caps))
        spin(3, lambda: len(c.list()) >= 2)  # (just started, GNOME may not have added its second yet)
        first = c.list()
        check("GNOME keeps an empty workspace at the end", len(first) >= 2 and not c.toplevels_on(first[-1]),
              [w.index for w in first])
        check("numbered from 1, the first active", first[0].index == 1 and c.active() is first[0])
        check("screens by connector name", bool(c.output_names()), c.output_names())

        # what the daemon does to keep a new workspace (pin_new_space): pin the empty last one
        last = first[-1]
        c.pin(last, True)
        spin(3, lambda: len(c.list()) > len(first))
        check("pinning the last one keeps it", c.workspaces[last.id].pinned, last.id)
        check("and GNOME adds a new empty one after it", len(c.list()) == len(first) + 1 and c.list()[-1].id != last.id,
              [w.id[:8] for w in c.list()])
        made = c.create("Live 3")
        spin(3, lambda: c.list()[-1] is not made)
        check("create adds a kept workspace, named", made is not None and made.pinned and made.name == "Live 3",
              made)
        check("before the empty one at the end", made is not None and c.list()[-1] is not made and made in c.list(),
              [(w.name, w.pinned, w.active) for w in c.list()])
        w1, w2 = c.list()[0], c.workspaces[last.id]

        procs.append(subprocess.Popen([sys.executable, "-c", WINDOW, "sidelit-live-a", "Live A"]))
        if shutil.which("foot"):
            procs.append(subprocess.Popen(["foot", "--app-id=sidelit-live-b", "--title=Live B", "sleep", "120"]))
        spin(10, lambda: window(c, "sidelit-live-a") and (len(procs) < 2 or window(c, "sidelit-live-b")))
        a = window(c, "sidelit-live-a")
        check("a new window is reported", a is not None, sorted(t.app_id for t in c.toplevels.values()))
        if a is None:
            return 1
        check("with its title, pid and workspace", a.title == "Live A" and a.pid == procs[0].pid
              and c.workspace_of(a) is w1, f"{a.title!r} pid {a.pid} on {c.workspace_of(a)}")
        check("and its screen", c.output_of(a) is not None and a.output in c.output_names(), a.output)
        check("toplevels_on lists it", a in c.toplevels_on(w1))
        spin(6, lambda: a.title.endswith("(renamed)"))
        check("a title change comes through on its own", a.title == "Live A (renamed)", a.title)

        once = gnome.GnomeClient(primary=False)
        once.wait(1)
        check("a one-shot client sees it too", window(once, "sidelit-live-a") is not None
              and once.workspace_of(window(once, "sidelit-live-a")).id == w1.id)
        once.close()

        check("move_toplevel", c.move_toplevel(a, w2))
        spin(3, lambda: c.workspace_of(a) is w2)
        check("the window moved to the kept workspace", c.workspace_of(a) is w2, a.workspace)
        others = sorted(c.output_names() - {a.output})
        if others:  # (a headless GNOME Shell has two with two --virtual-monitor)
            home = a.output
            c.move_toplevel(a, w2, others[0])
            spin(3, lambda: a.output == others[0])
            check("move_toplevel to another screen", a.output == others[0], f"{a.output!r}, wanted {others[0]!r}")
            check("where (GNOME's default) a window is on every workspace", c.workspace_of(a) is None, a.workspace)
            c.move_toplevel(a, w2, home)
            spin(3, lambda: a.output == home and c.workspace_of(a) is w2)
            check("and back, onto its workspace", a.output == home and c.workspace_of(a) is w2, (a.output, a.workspace))
        else:
            print("skip moving to another screen: there is one")
        c.activate(w2)
        spin(2, lambda: w2.active)
        check("activate switches workspaces", w2.active and c.active() is w2)
        c.focus(a)
        spin(3, lambda: a.active)
        check("focus gives it the keyboard", a.focused)

        if len(procs) > 1:
            b = window(c, "sidelit-live-b")
            check("foot's window too", b is not None)
            if b is not None:
                c.focus(b)
                spin(3, lambda: b.active)
                c.close_toplevel(b)
                spin(5, lambda: b.identifier not in c.toplevels)
                check("close_toplevel closes it", b.identifier not in c.toplevels and b.closed)
                check("and remembers it had focus", b.identifier in c.closed_focus)

        # a kept workspace stays when it is emptied; GNOME drops one that is not kept
        c.activate(w1)
        c.move_toplevel(a, made)
        spin(3, lambda: c.workspace_of(a) is made)
        c.move_toplevel(a, w2)
        spin(3, lambda: c.workspace_of(a) is w2)
        spin(1)
        check("an emptied kept workspace stays", any(w is made for w in c.list()) and not made.removed,
              (made.id[:8], made.removed, [(w.id[:8], w.name, w.pinned, w.active, len(c.toplevels_on(w)))
                                           for w in c.list()]))
        count = len(c.list())
        c.pin(made, False)
        spin(3, lambda: len(c.list()) < count)
        empty = [w for w in c.list() if not c.toplevels_on(w) and not w.pinned and not w.active]
        # (GNOME keeps the first empty one after the last busy one: that may be it, or the next)
        check("unpinned and empty, GNOME lets it go: one empty workspace is left at the end",
              len(c.list()) == count - 1 and empty == [c.list()[-1]], [(w.name, w.pinned) for w in c.list()])

        extra = c.create("Live extra")
        spin(1)  # (GNOME adds the new empty one at the end a moment later)
        order = [w.id for w in c.list()]
        c.move(extra, w1, before=True)
        spin(1)
        check("move puts a workspace before another", c.list()[0] is extra, [w.name for w in c.list()])
        check("and the rest keep their order", [w.id for w in c.list()][1:] == [i for i in order if i != extra.id])
        c.rename(extra, "Renamed")
        check("rename and resolve by name", c.resolve("Renamed") is extra)
        c.remove(extra)
        spin(1)
        check("remove drops it", extra.removed and extra not in c.list())
        label = c.shortcut_label(c.list()[0], len(c.list()))
        check("the first tab's chip shows GNOME's Super+Home", label == "⊞ Home", repr(label))

        # turned off and on (an update, or by hand): same workspaces, the same ids
        ids = [w.id for w in c.list()]
        toggle_extension(False)
        spin(2, lambda: not c.online)
        check("turned off, the client keeps what it had", not c.online and a.identifier in c.toplevels)
        toggle_extension(True)
        spin(5, lambda: c.online)
        check("turned on again, it is back", c.online)
        check("every workspace has the id it had", [w.id for w in c.list()] == ids, ([w.id[:8] for w in c.list()], [i[:8] for i in ids]))
        check("the window kept its id and workspace", c.toplevels.get(a.identifier) is a and c.workspace_of(a) is w2)
        check("and is moved with the old id", c.move_toplevel(a, w1))
        spin(3, lambda: c.workspace_of(a) is w1)
        check("which reaches GNOME", c.workspace_of(a) is w1, a.workspace)

        if "--sidebar" in sys.argv:
            _sidebar_checks(c)

        for p in procs:
            p.terminate()
        spin(3, lambda: window(c, "sidelit-live-a") is None)
        check("a closed window leaves the list", window(c, "sidelit-live-a") is None)
        check("on_change was called", len(changes) > 3, len(changes))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        c.close()
    print(f"{'FAILED: ' + ', '.join(failed) if failed else 'all passed'}")
    return 1 if failed else 0


def _sidebar_checks(c):
    """The extension runs the sidebar while the daemon's bus name is held (it is: by `c`). On
    the left of the leftmost screen first (config.json's defaults, whatever it says now), then
    on the right; config.json is left as it was."""
    from sidelit import config
    state = lambda: json.loads(c._bus_call(gnome.SHELL, gnome.PATH, gnome.IFACE, "GetState")[0])["sidebar"]  # noqa: E731
    before = config.load()
    config.put("side", "left")
    config.put("monitor", "leftmost")
    try:
        spin(25, lambda: state().get("shown"))
        if (before["side"], before["monitor"]) != ("left", "leftmost"):
            _restart_sidebar(state)  # (it read config.json before the lines above)
        _left_side_checks(c, state)
        _right_side_checks(state)
    finally:
        for key in ("side", "monitor"):
            config.put(key, before[key])
        _restart_sidebar(state)


def _left_side_checks(c, state):
    check("the extension runs the sidebar", state().get("running") and state().get("shown"), state())
    info = None
    end = time.monotonic() + 10
    while time.monotonic() < end:  # (the strip reaches the work area on the next frame)
        info = _sidebar_window()
        if isinstance(info, dict) and info["area"]["x"] == info["x"] + info["width"]:
            break
        spin(0.5)
    check("its window is found", isinstance(info, dict), info)
    if not isinstance(info, dict):
        return
    print(f"     sidebar {info}", flush=True)
    check("as a dock", info["dock"], info["type"])
    check("on every workspace", info["sticky"])
    check("out of the window lists", info["skip"])
    # (Ubuntu's session has its dock on the left already: the sidebar starts right of it)
    base = info["x"] - info["monitor"]["x"]
    check("at the left edge (or beside Ubuntu's dock), below the top bar",
          0 <= base < 120 and info["y"] == info["area"]["y"], info)
    check("as tall as the work area", info["height"] == info["area"]["height"], info)
    check("windows get the rest: the work area starts where it ends",
          info["area"]["x"] == info["x"] + info["width"], info)
    check("the sidebar is not in Sidelit's window list", all(t.app_id != "sidelit" or t.title != "Sidelit"
                                                             for t in c.toplevels.values()))
    # a (wider) dock already on the left, as Ubuntu's is: the sidebar goes beside it
    dock = base + 64
    shell_eval(f"""(() => {{
        const St = imports.gi.St, a = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
        globalThis._fakeDock = new St.Widget({{x: 0, y: a.y, width: {dock}, height: a.height, reactive: false}});
        Main.layoutManager.addChrome(globalThis._fakeDock, {{affectsStruts: true}});
    }})()""")
    beside = lambda i: isinstance(i, dict) and i["x"] == dock and i["area"]["x"] == dock + i["width"]  # noqa: E731
    end = time.monotonic() + 5
    while time.monotonic() < end and not beside(info := _sidebar_window()):
        spin(0.3)
    check("beside a dock already on the left, and windows beside both", beside(info), info)
    shell_eval("(() => { Main.layoutManager.removeChrome(globalThis._fakeDock); globalThis._fakeDock.destroy(); })()")
    back = lambda i: isinstance(i, dict) and i["x"] == base and i["area"]["x"] == base + i["width"]  # noqa: E731
    end = time.monotonic() + 5
    while time.monotonic() < end and not back(info := _sidebar_window()):
        spin(0.3)
    check("and back where it was when that dock goes", back(info), info)
    # the sidebar crashes: started again, and the strip is kept meanwhile
    a = window(c, "sidelit-live-a")
    if a is not None:
        c.focus(a)
        spin(3, lambda: a.active)
    pid = state()["pid"]
    os.kill(pid, 9)
    again = lambda: (s := state()).get("shown") and s.get("pid") not in (0, pid)  # noqa: E731
    spin(10, again)
    check("a sidebar that died is started again", again(), state())
    spin(1)
    check("and does not take the keyboard from your window", a is None or a.active)
    placed = lambda i: isinstance(i, dict) and i["x"] == i["monitor"]["x"] + base and i["width"] > 0 \
        and i["area"]["x"] == i["x"] + i["width"]  # noqa: E731
    end = time.monotonic() + 5
    while time.monotonic() < end and not placed(info := _sidebar_window()):
        spin(0.3)
    check("in the same place", placed(info), info)


def _restart_sidebar(state):
    """Kill the sidebar, which the extension starts again (and which reads config.json anew):
    after a pause that grows while it keeps stopping, up to a minute (sidebar.js RESTART_MS)."""
    pid = state()["pid"]
    if pid > 0:  # (0 is none: os.kill(0) would kill this test's own process group)
        os.kill(pid, 9)
    spin(70, lambda: (s := state()).get("shown") and s.get("pid") not in (0, pid))


def _until(ok, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not ok(info := _sidebar_window()):
        spin(0.3)
    return info


def _right_of(gap):
    """Is the sidebar `gap` from its screen's right edge, with windows (the work area) left of it?"""
    def ok(i):
        if not isinstance(i, dict) or i["width"] <= 0:
            return False
        right = i["monitor"]["x"] + i["monitor"]["width"]
        return i["x"] + i["width"] == right - gap and i["area"]["x"] + i["area"]["width"] == i["x"]
    return ok


def _right_side_checks(state):
    """config.json's "side": "right" (the settings' Side): on the right edge, beside a dock there,
    and with two screens on the rightmost one ("monitor": "rightmost")."""
    from sidelit import config
    config.put("side", "right")
    _restart_sidebar(state)
    info = _until(_right_of(0))
    check("on the right side: flush with the screen's right edge, windows left of it", _right_of(0)(info), info)
    if not isinstance(info, dict):
        return
    check("below the top bar, as tall as the work area",
          info["y"] == info["area"]["y"] and info["height"] == info["area"]["height"], info)
    # a dock already on the right (Dash to Dock can be there): the sidebar goes beside it
    m, a = info["monitor"], info["area"]
    shell_eval(f"""(() => {{
        const St = imports.gi.St;
        globalThis._fakeDock = new St.Widget({{x: {m["x"] + m["width"] - 64}, y: {a["y"]}, width: 64,
            height: {a["height"]}, reactive: false}});
        Main.layoutManager.addChrome(globalThis._fakeDock, {{affectsStruts: true}});
    }})()""")
    info = _until(_right_of(64), 5)
    check("beside a dock already on the right, and windows beside both", _right_of(64)(info), info)
    shell_eval("(() => { Main.layoutManager.removeChrome(globalThis._fakeDock); globalThis._fakeDock.destroy(); })()")
    info = _until(_right_of(0), 5)
    check("and back on the edge when that dock goes", _right_of(0)(info), info)
    if not isinstance(info, dict) or len(info["monitors"]) < 2:
        print("skip the rightmost screen: there is one", flush=True)
        return
    # (a headless GNOME Shell has two with two --virtual-monitor)
    edge = max(o["x"] + o["width"] for o in info["monitors"])
    config.put("monitor", "rightmost")
    _restart_sidebar(state)
    on_rightmost = lambda i: _right_of(0)(i) and i["monitor"]["x"] + i["monitor"]["width"] == edge  # noqa: E731
    info = _until(on_rightmost)
    check("on the rightmost screen's right edge", on_rightmost(info), info)
    config.put("side", "left")
    _restart_sidebar(state)
    left = lambda i: isinstance(i, dict) and i["x"] == i["monitor"]["x"] \
        and i["monitor"]["x"] + i["monitor"]["width"] == edge and i["area"]["x"] == i["x"] + i["width"]  # noqa: E731
    info = _until(left)
    check("and on its left edge, between the two screens, windows still beside it", left(info), info)


def _sidebar_window():
    return shell_eval("""(() => {
        const w = global.display.list_all_windows().find(w => w.get_title() === 'Sidelit');
        if (!w) return null;
        // (its own screen, and every screen's geometry)
        const r = w.get_frame_rect(), m = Main.layoutManager.monitors[w.get_monitor()];
        const area = Main.layoutManager.getWorkAreaForMonitor(w.get_monitor());
        return JSON.stringify({x: r.x, y: r.y, width: r.width, height: r.height, type: w.get_window_type(),
            monitors: Main.layoutManager.monitors.map(o => ({x: o.x, y: o.y, width: o.width, height: o.height})),
            dock: w.get_window_type() === Meta.WindowType.DOCK, sticky: w.is_on_all_workspaces(),
            skip: w.is_skip_taskbar(), area: {x: area.x, y: area.y, width: area.width, height: area.height},
            monitor: {x: m.x, y: m.y, width: m.width, height: m.height}, panel: Main.panel.height});
    })()""")


def _second_daemon_refused() -> bool:
    addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    own = Gio.DBusConnection.new_for_address_sync(
        addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    try:
        gnome.GnomeClient(primary=True, bus=own, wait_s=2).close()
    except RuntimeError:
        return True
    finally:
        own.close_sync(None)
    return False


if __name__ == "__main__":
    sys.exit(main())
