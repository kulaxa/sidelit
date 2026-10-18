"""The GTK 3 sidebar on X11, where there is no layer shell: a dock window on the left of its
screen, on every desktop, with the strip it stands on reserved (_NET_WM_STRUT_PARTIAL), so
that maximised windows stop beside it. Any window manager that keeps the EWMH hints does this
for a panel (Cinnamon, Xfce, MATE, Openbox). The GTK 4 sidebar stays Wayland-only.
"""
from __future__ import annotations

import os
import sys

ALL_DESKTOPS = 0xFFFFFFFF


def on_x11() -> bool:
    """Does GTK draw on X11 here? As GDK_BACKEND says, else: an X display and no Wayland one."""
    first = (os.environ.get("GDK_BACKEND") or "").split(",")[0].strip()
    if first in ("x11", "wayland"):
        return first == "x11"
    return bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")


CHECK_S = 2  # how often the dock looks whether the desktop's panels moved


def span(geometry: tuple, workarea: tuple) -> tuple:
    """Where the sidebar goes on a screen (x, y, width, height of it, and of its work area):
    (x, y, height). At its left edge, as high as the work area, which leaves out the desktop's
    own panels (Cinnamon's at the bottom, with Mint's menu in it; Xfce's and MATE's at the top);
    the whole height where the work area makes no sense for this screen."""
    x, y, _w, h = geometry
    _ax, ay, _aw, ah = workarea
    top, bottom = max(y, ay), min(y + h, ay + ah)
    if bottom - top < h // 3:
        return x, y, h
    return x, top, bottom - top


def strut(monitor: tuple, others: list, width: int, scale: int = 1) -> list | None:
    """_NET_WM_STRUT_PARTIAL for a strip `width` wide on the left of `monitor` (x, y, width,
    height: the sidebar's stretch of its screen, in GTK's units; the strut is in the X server's
    pixels, `scale` of them each). None when another screen lies to its left: a left strut is
    measured from the left edge of the whole X screen and would take that screen's strip too."""
    x, y, _w, h = monitor
    if any(ox < x and oy < y + h and oy + oh > y for ox, oy, _ow, oh in others):
        return None
    return [(x + width) * scale, 0, 0, 0, y * scale, (y + h) * scale - 1, 0, 0, 0, 0, 0, 0]


class _Dock:
    """The sidebar's window as a dock on `monitor`, kept beside the desktop's own panels."""

    def __init__(self, window, monitor, width: int):
        self.window, self.monitor, self.width = window, monitor, width
        self.placed: tuple | None = None
        self.warned = False

    def where(self) -> tuple:
        geo, area = self.monitor.get_geometry(), self.monitor.get_workarea()
        return span((geo.x, geo.y, geo.width, geo.height), (area.x, area.y, area.width, area.height))

    def place(self):
        x, y, height = self.placed = self.where()
        self.window.move(x, y)
        self.window.set_size_request(self.width, height)
        self.window.resize(self.width, height)
        if self.window.get_realized():
            self.reserve()

    def reserve(self):
        x, y, height = self.placed
        display = self.monitor.get_display()
        geo = self.monitor.get_geometry()
        others = [(o.x, o.y, o.width, o.height) for o in
                  (display.get_monitor(i).get_geometry() for i in range(display.get_n_monitors()))]
        others = [o for o in others if o != (geo.x, geo.y, geo.width, geo.height)]
        values = strut((x, y, geo.width, height), others, self.width, self.monitor.get_scale_factor())
        if values is None and not self.warned:
            self.warned = True
            print("sidebar: another screen is left of the sidebar's: its strip is not reserved (put the "
                  "sidebar on the leftmost screen: \"monitor\": \"leftmost\" in config.json)", file=sys.stderr, flush=True)
        _reserve(self.window, values)

    def check(self) -> bool:
        """A panel came, went or moved (at login Cinnamon's comes up after the sidebar): follow."""
        if self.where() != self.placed:
            print(f"sidebar: the desktop's panels moved: now {self.where()}", file=sys.stderr, flush=True)
            self.place()
        return True


def dock(window, monitor, width: int):
    """Make `window` (a Gtk.Window, not shown yet) the sidebar's dock on the left of `monitor`
    (a Gdk.Monitor; None: wherever GTK puts it)."""
    import gi
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib
    window.set_type_hint(Gdk.WindowTypeHint.DOCK)
    window.set_decorated(False)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_keep_above(True)
    window.stick()  # on every desktop
    if monitor is None:
        return
    d = _Dock(window, monitor, width)
    d.place()
    window.set_default_size(width, d.placed[2])
    # the strut once it has an X window, before it is mapped (and after the sidebar picked its visual)
    window.connect("realize", lambda w: d.reserve())
    GLib.timeout_add_seconds(CHECK_S, d.check)


def _reserve(window, values: list | None):
    """Put the strut (and the sticky desktop, for window managers that read it at map time) on
    the window, through python-xlib: GDK's own property calls do not take CARDINAL lists."""
    try:
        import gi
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11  # noqa: F401 (gives Gdk windows their get_xid)
        from Xlib import Xatom, display
    except (ImportError, ValueError) as exc:
        print(f"sidebar: cannot reserve the sidebar's strip on X11 ({exc}); windows may cover it",
              file=sys.stderr, flush=True)
        return
    xid = window.get_window().get_xid()
    d = display.Display()
    try:
        w = d.create_resource_object("window", xid)
        w.change_property(d.intern_atom("_NET_WM_DESKTOP"), Xatom.CARDINAL, 32, [ALL_DESKTOPS])
        if values is not None:
            w.change_property(d.intern_atom("_NET_WM_STRUT_PARTIAL"), Xatom.CARDINAL, 32, values)
            w.change_property(d.intern_atom("_NET_WM_STRUT"), Xatom.CARDINAL, 32, values[:4])
        d.sync()
    finally:
        d.close()


def focus(window):
    """Ask for the keyboard: a dock gets it only when it asks (the history's search needs it)."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    gdk_window = window.get_window()
    if gdk_window is not None:
        gdk_window.focus(Gtk.get_current_event_time())
