"""The GTK 3 sidebar on X11, where there is no layer shell: a dock window on the left or the
right of its screen (config.json's "side"), on every desktop, with the strip it stands on
reserved (_NET_WM_STRUT_PARTIAL), so that maximised windows stop beside it. Any window manager
that keeps the EWMH hints does this for a panel (Cinnamon, Xfce, MATE, Openbox). The GTK 4
sidebar stays Wayland-only.
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


def span(geometry: tuple, workarea: tuple, width: int = 0, side: str = "left") -> tuple:
    """Where the sidebar, `width` wide, goes on a screen (x, y, width, height of it, and of its
    work area): (x, y, height). At its left edge, or its right one, as high as the work area,
    which leaves out the desktop's own panels (Cinnamon's at the bottom, with Mint's menu in it;
    Xfce's and MATE's at the top); the whole height where the work area makes no sense for this
    screen."""
    x, y, w, h = geometry
    _ax, ay, _aw, ah = workarea
    if side == "right":
        x += w - width
    top, bottom = max(y, ay), min(y + h, ay + ah)
    if bottom - top < h // 3:
        return x, y, h
    return x, top, bottom - top


def strut(monitor: tuple, others: list, width: int, scale: int = 1, side: str = "left") -> list | None:
    """_NET_WM_STRUT_PARTIAL for a strip `width` wide on the left (or right) of `monitor` (x, y,
    width, height: its screen's x and width, and the sidebar's stretch of it from top to bottom,
    in GTK's units; the strut is in the X server's pixels, `scale` of them each). A left strut
    is measured from the left edge of the whole X screen, a right one from its right edge (the
    right edge of the screen furthest right), so each takes every screen's strip in between too:
    None when another screen lies to that side of this one."""
    x, y, w, h = monitor
    beside = [(ox, ow) for ox, oy, ow, oh in others if oy < y + h and oy + oh > y]
    if side == "right":
        if any(ox + ow > x + w for ox, ow in beside):
            return None
        edge = max([x + w] + [ox + ow for ox, _oy, ow, _oh in others])
        return [0, (edge - (x + w) + width) * scale, 0, 0, 0, 0, y * scale, (y + h) * scale - 1, 0, 0, 0, 0]
    if any(ox < x for ox, _ow in beside):
        return None
    return [(x + width) * scale, 0, 0, 0, y * scale, (y + h) * scale - 1, 0, 0, 0, 0, 0, 0]


class _Dock:
    """The sidebar's window as a dock on the `side` of `monitor`, kept beside the desktop's own
    panels. `unreserved()` is called when its strip cannot be reserved there."""

    def __init__(self, window, monitor, width: int, side: str, unreserved=None):
        self.window, self.monitor, self.width, self.side = window, monitor, width, side
        self.unreserved = unreserved
        self.placed: tuple | None = None
        self.warned = False

    def where(self) -> tuple:
        geo, area = self.monitor.get_geometry(), self.monitor.get_workarea()
        return span((geo.x, geo.y, geo.width, geo.height), (area.x, area.y, area.width, area.height),
                    self.width, self.side)

    def place(self):
        x, y, height = self.placed = self.where()
        self.window.move(x, y)
        self.window.set_size_request(self.width, height)
        self.window.resize(self.width, height)
        if self.window.get_realized():
            self.reserve()

    def reserve(self):
        _x, y, height = self.placed
        display = self.monitor.get_display()
        geo = self.monitor.get_geometry()
        others = [(o.x, o.y, o.width, o.height) for o in
                  (display.get_monitor(i).get_geometry() for i in range(display.get_n_monitors()))]
        others = [o for o in others if o != (geo.x, geo.y, geo.width, geo.height)]
        values = strut((geo.x, y, geo.width, height), others, self.width, self.monitor.get_scale_factor(), self.side)
        if values is None and not self.warned:
            self.warned = True
            outer = "leftmost" if self.side == "left" else "rightmost"
            print(f"sidebar: another screen is {self.side} of the sidebar's: its strip is not reserved (put the "
                  f"sidebar on the {outer} screen, in the settings or with \"monitor\": \"{outer}\" in config.json)",
                  file=sys.stderr, flush=True)
            if self.unreserved is not None:
                self.unreserved()
        _reserve(self.window, values)

    def check(self) -> bool:
        """A panel came, went or moved (at login Cinnamon's comes up after the sidebar): follow."""
        if self.where() != self.placed:
            print(f"sidebar: the desktop's panels moved: now {self.where()}", file=sys.stderr, flush=True)
            self.place()
        return True


def dock(window, monitor, width: int, side: str = "left", unreserved=None):
    """Make `window` (a Gtk.Window, not shown yet) the sidebar's dock on the `side` (left or
    right) of `monitor` (a Gdk.Monitor; None: wherever GTK puts it). `unreserved()` is called
    once if its strip cannot be reserved there (another screen lies beyond that edge). The dock
    it returns (None without a monitor) places it again after it was hidden."""
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
        return None
    d = _Dock(window, monitor, width, side, unreserved)
    d.place()
    window.set_default_size(width, d.placed[2])
    # the strut once it has an X window, before it is mapped (and after the sidebar picked its visual)
    window.connect("realize", lambda w: d.reserve())
    GLib.timeout_add_seconds(CHECK_S, d.check)
    return d


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
        else:  # (the screens moved under it: a strip it reserved before would be the wrong one now)
            w.delete_property(d.intern_atom("_NET_WM_STRUT_PARTIAL"))
            w.delete_property(d.intern_atom("_NET_WM_STRUT"))
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
