"""The KDE backend against a real KWin (Plasma 6), by hand: not part of the unit tests.

Run it inside a KDE session you can spare, or a headless one (`kwin_wayland --virtual` with a
private session bus): it adds and removes desktops and opens, moves and closes two windows of its
own (GTK 3 and foot). Prints each check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/kwin_live.py
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

from sidelit import backend, kwin  # noqa: E402  (first: it asks gi for the versions it needs)
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
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail else ""), flush=True)
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


def main() -> int:
    check("XDG_CURRENT_DESKTOP=KDE picks the kde backend", backend.chosen({"XDG_CURRENT_DESKTOP": "KDE"}) == "kde")
    tmp = Path(tempfile.mkdtemp(prefix="sidelit-live-"))
    changes = []
    daemon = kwin.KWinClient(on_change=lambda: changes.append(time.monotonic()), primary=True, script_dir=tmp / "d")
    try:
        check("a second daemon is refused the bus name", _second_daemon_refused(tmp))
        while len(daemon.list()) < 3:
            daemon.create(f"Live {len(daemon.list()) + 1}")
        d1, d2, d3 = daemon.list()[:3]
        check("three desktops, numbered from 1", [d.index for d in (d1, d2, d3)] == [1, 2, 3])
        daemon.activate(d1)
        spin(1, lambda: d1.active)
        check("activate switches desktops", d1.active and daemon.active() is d1)

        procs = [subprocess.Popen([sys.executable, "-c", WINDOW, "sidelit-live-a", "Live A"])]
        if shutil.which("foot"):
            procs.append(subprocess.Popen(["foot", "--app-id=sidelit-live-b", "--title=Live B", "sleep", "60"]))
        spin(8, lambda: window(daemon, "sidelit-live-a") and (len(procs) < 2 or window(daemon, "sidelit-live-b")))
        a = window(daemon, "sidelit-live-a")
        check("the daemon's script reports a new window", a is not None, sorted(t.app_id for t in daemon.toplevels.values()))
        if a is None:
            return 1
        check("with its title, pid and desktop", a.title == "Live A" and a.pid == procs[0].pid
              and daemon.workspace_of(a) is d1, f"{a.title!r} pid {a.pid} on {daemon.workspace_of(a)}")
        check("and its screen", daemon.output_of(a) is not None and daemon.output_of(a).name in daemon.output_names(),
              f"{a.output!r} of {daemon.output_names()}")
        check("toplevels_on lists it", a in daemon.toplevels_on(d1))
        spin(6, lambda: a.title.endswith("(renamed)"))
        check("a title change comes through on its own", a.title == "Live A (renamed)", a.title)

        once = kwin.KWinClient(primary=False, script_dir=tmp / "o")
        once.wait(2)
        b_once = window(once, "sidelit-live-a")
        check("a one-shot client gets one report", b_once is not None and once.workspace_of(b_once).id == d1.id)
        once.close()
        spin(1)
        check("and unloads its script", not _loaded(daemon, once._windows_script) and not list((tmp / "o").glob("*.js")))

        check("move_toplevel asks", daemon.move_toplevel(a, d2))
        spin(3, lambda: daemon.workspace_of(a) is d2)
        check("the window moved to desktop 2", daemon.workspace_of(a) is d2, a.desktops)
        check("the move's script is gone", not daemon._pending and not list((tmp / "d").glob("sidelit-*-*.js")),
              list(daemon._pending))
        others = sorted(daemon.output_names() - {a.output})
        if others:  # (a headless KWin has two with --output-count 2)
            daemon.move_toplevel(a, d2, others[0])
            spin(3, lambda: a.output == others[0])
            check("and to another screen", a.output == others[0], f"{a.output!r}, wanted {others[0]!r}")
        else:
            print("skip moving to another screen: there is one")

        daemon.focus(a)
        spin(3, lambda: a.active)
        check("focus gives it the keyboard", a.focused)
        spin(2, lambda: d2.active)
        check("on its own desktop", d2.active)

        daemon.rename(d3, "Renamed")
        spin(1)
        check("rename", d3.name == "Renamed" and daemon.resolve("Renamed") is d3)
        check("resolve by number", daemon.resolve("2") is d2)
        label = daemon.shortcut_label(d1, len(daemon.list()))
        check("the shortcut chip shows KDE's binding", label.startswith("Ctrl F1"), repr(label))

        if len(procs) > 1:
            b = window(daemon, "sidelit-live-b")
            check("foot's window too", b is not None)
            if b is not None:
                daemon.focus(b)
                spin(3, lambda: b.active)
                daemon.close_toplevel(b)
                spin(5, lambda: b.identifier not in daemon.toplevels)
                check("close_toplevel closes it", b.identifier not in daemon.toplevels and b.closed)
                check("and remembers it had focus", b.identifier in daemon.closed_focus)

        extra = daemon.create("Live extra")
        check("create adds a desktop at the end", extra is not None and extra.index == len(daemon.list()))
        daemon.remove(extra)
        spin(1)
        check("remove drops it", extra.removed and extra not in daemon.list())

        check("only KWin may report", _stranger_refused(daemon))
        for p in procs:
            p.terminate()
        spin(3, lambda: window(daemon, "sidelit-live-a") is None)
        check("a closed window leaves the list", window(daemon, "sidelit-live-a") is None)
        check("on_change was called", len(changes) > 3, len(changes))
    finally:
        daemon.close()
    spin(1)
    probe = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    loaded = probe.call_sync(kwin.KWIN, kwin.SCRIPTING_PATH, kwin.SCRIPTING_IFACE, "isScriptLoaded",
                             GLib.Variant("(s)", (daemon._windows_script,)), None, 0, 2000, None).unpack()[0]
    check("closing unloads the daemon's script", not loaded)
    print(f"{'FAILED: ' + ', '.join(failed) if failed else 'all passed'}")
    return 1 if failed else 0


def _loaded(client, name: str) -> bool:
    return client._kwin(kwin.SCRIPTING_PATH, kwin.SCRIPTING_IFACE, "isScriptLoaded", GLib.Variant("(s)", (name,)))[0]


def _second_daemon_refused(tmp: Path) -> bool:
    # a connection of its own, as a second process would have
    addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    own = Gio.DBusConnection.new_for_address_sync(
        addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    try:
        kwin.KWinClient(primary=True, bus=own, script_dir=tmp / "second")
    except RuntimeError:
        return True
    finally:
        own.close_sync(None)
    return False


def _stranger_refused(client) -> bool:
    addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    other = Gio.DBusConnection.new_for_address_sync(
        addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    result = []
    other.call(*client.to, kwin.OUR_IFACE, "Windows", GLib.Variant("(s)", ('{"full": true}',)),
               None, 0, 2000, None, lambda c, r: result.append(_finish(c, r)))
    spin(2, lambda: result)
    other.close_sync(None)
    return bool(result) and result[0] == "AccessDenied" and any(not t.closed for t in client.toplevels.values())


def _finish(conn, res):
    try:
        conn.call_finish(res)
        return "accepted"
    except GLib.Error as exc:
        return "AccessDenied" if "AccessDenied" in exc.message or "AccessDenied" in str(exc) else exc.message


if __name__ == "__main__":
    sys.exit(main())
