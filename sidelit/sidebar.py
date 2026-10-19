#!/usr/bin/env python3
"""Sidelit's sidebar, GTK 3: the fallback when the GTK 4 one (sidebar4.py) cannot run.

A view of the daemon (daemon.py): it draws the tabs the daemon pushes and sends it a
command for everything you do. It holds no state of its own worth keeping, so it can
crash, restart or be re-mapped on another screen without anything being lost.

- click a tab .......... switch to that workspace (a closed one is reopened)
- '+' at the bottom .... new pinned workspace from a template
- fork icon ............ new workspace running `claude --resume <id> --fork-session`
- x .................... close every window on the workspace and stop keeping it
- other sources ........ their items under the tabs (sources.py); right-click one to give it
                         a workspace of its own, whose tab is then drawn from the item
"""
from __future__ import annotations

import functools
import signal
import sys
import time

import gi

from sidelit import gnome, themes, xdock

# on X11 the sidebar is a dock window (xdock.py); GNOME's extension docks it on either
X11_DOCK = not gnome.hosted() and xdock.on_x11()

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("GdkPixbuf", "2.0")
if not gnome.hosted() and not X11_DOCK:
    gi.require_version("GtkLayerShell", "0.1")
from gi.repository import GLib  # noqa: E402

# our dialogs are windows too; with this app id the daemon knows them and leaves them out
# (it has to be set before GTK initialises, which importing Gtk does)
GLib.set_prgname("sidelit")
from gi.repository import Gdk, GdkPixbuf, Gtk, Pango  # noqa: E402

# on GNOME there is no layer shell: Sidelit's GNOME Shell extension docks the plain window
if gnome.hosted() or X11_DOCK:
    GtkLayerShell = None
else:
    from gi.repository import GtkLayerShell  # noqa: E402

from sidelit import ipc  # noqa: E402
from sidelit.sidebar_common import (  # noqa: E402,F401 (names the tests and older callers use)
    FORK_ICON_SVG, LOGO_SVG, SEPARATOR, app_entry, SOURCE_STATE_CLASS, STATE_CLASSES, SidebarLogic, bound_items, box_classes,
    box_room, close_question, countdown, day_label, group_items, history_lines, item_sub, matches, tab_classes, tab_info,
    tab_snapshot, usage_level,
)

MAX_DOTS = 8  # subagent dots a box has room for

FONT = "'Inter', 'SF Pro Text', 'Fira Sans', 'Cantarell', 'Noto Sans', sans-serif"
MONO = "'JetBrainsMonoNL Nerd Font Mono', 'JetBrains Mono', 'DejaVu Sans Mono', monospace"
# Frosted glass after iOS: the window itself is clear, the sidebar a translucent rounded panel
# floating over the wallpaper, every tab a near-solid card on it. Colours are iOS's dark-mode
# system colours: cyan for where you are, orange working, yellow waiting, red failed.
CSS = f"""
window {{ background-color: #1c1c1e; }}
window.cw-window {{ background-color: transparent; }}
* {{ font-family: {FONT}; }}
scrolledwindow, viewport, scrolledwindow > viewport {{ background-color: transparent; border: none; }}
scrollbar {{ background-color: transparent; border: none; }}
scrollbar slider {{ background-color: rgba(255,255,255,0.18); border-radius: 4px; min-width: 4px; border: none; }}
.cw-panel {{ background-color: rgba(24,24,27,0.28); border: 1px solid rgba(255,255,255,0.10); border-radius: 22px; }}
/* A tab is an event box (the hit area) holding its card. Pointing at a tab swells the card
   and pushes the cards below a little; it settles back as the pointer moves on, so a sweep
   down the list ripples. The swell is drawn, not laid out: a ring of the card's own colour
   (box-shadow spread) grows around it, which costs a repaint and moves no text. The push is
   the one layout change, a single value (the gap under the card), so nothing rounds against
   anything else and only the cards below move. */
.cw-card {{ background-color: rgba(62,62,68,0.84); border: 1px solid rgba(255,255,255,0.10); border-radius: 16px;
  margin: 4px 8px; padding: 10px 5px 9px 7px;
  box-shadow: 0 0 0 0 rgba(62,62,68,0), 0 0 0 0 rgba(0,0,0,0);
  transition: margin-bottom 200ms cubic-bezier(0.22, 1, 0.36, 1), box-shadow 200ms cubic-bezier(0.22, 1, 0.36, 1),
              background-color 160ms ease-out, border-color 160ms ease-out; }}
.cw-tab:hover > .cw-card {{ background-color: rgba(78,78,86,0.92); border-color: rgba(78,78,86,0.92); margin-bottom: 7px;
  box-shadow: 0 0 0 3px rgba(78,78,86,0.92), 0 8px 18px rgba(0,0,0,0.35); }}
.cw-tab.active > .cw-card {{ background-color: rgba(40,92,116,0.88); border-color: rgba(100,210,255,0.45); }}
/* Claude's state is the capsule at the card's left edge, not a glyph in the title:
     working   orange, breathing — Claude is on its turn
     shell     orange, faint — Claude's turn is over but a shell it started still runs
     external  orange, faint — Claude's turn is over, it waits on CI, a deploy, a job
     waiting   yellow, still — a permission prompt or a question is up
     done      grey, still — nothing wants you
     attention yellow, breathing — it needs you, and you were not typing in it
   (a workspace with Claudes gives each its own box and capsule, below) */
@keyframes cw-working {{ 0% {{ background-color: rgba(255,159,10,0.30); }} 50% {{ background-color: #ff9f0a; }}
  100% {{ background-color: rgba(255,159,10,0.30); }} }}
@keyframes cw-attention {{ 0% {{ background-color: rgba(255,214,10,0.30); }} 50% {{ background-color: #ffd60a; }}
  100% {{ background-color: rgba(255,214,10,0.30); }} }}
.cw-pill {{ min-width: 4px; border-radius: 2px; background-color: transparent; }}
.cw-tab.claude .cw-pill {{ background-color: rgba(174,174,178,0.55); }}
.cw-tab.claude.working .cw-pill {{ background-color: #ff9f0a; animation: cw-working 1.8s ease-in-out infinite; }}
.cw-tab.claude.idle .cw-pill {{ background-color: rgba(174,174,178,0.55); }}
.cw-tab.claude.shell .cw-pill {{ background-color: rgba(255,159,10,0.55); }}
.cw-tab.claude.waiting .cw-pill {{ background-color: #ffd60a; }}
.cw-tab.claude.attention .cw-pill {{ background-color: #ffd60a; animation: cw-attention 1.4s ease-in-out infinite; }}
.cw-tab.claude.failed .cw-pill {{ background-color: #ff453a; }}
.cw-tab.claude.attention > .cw-card {{ border-color: rgba(255,214,10,0.45); }}
/* A workspace is a card: a box per Claude in the order they started, under a header with its
   name (two lines at most) and close button when you named it or it has no Claude (else the
   first Claude's title names it, and its box has the close button). Its folder and shortcut
   show on hover. Its Claudes share one recessed well, a thin line between them; each has its
   own state stripe, a working one is lit from its stripe, one that needs you lit yellow and its
   title too, and the card around it glows softly. Subagents at work are small blocks right
   after the model, in the box's colour. The other windows are a line of small dimmed icons
   under the well, a browser's tab count and what is unread beside them in dim grey; they come
   up while the pointer is on the card, so at a glance only a Claude's colour stands out. An
   icon's title shows on hover. A box has room for a two-line title whatever its own (in_room),
   and a card with a Claude keeps the icons' line without icons, so every card with one Claude
   is as tall as the next. A card without a Claude is as small as its header. */
.cw-tab.card > .cw-card {{ padding: 6px; }}
.cw-tab.card.attention > .cw-card {{ border-color: rgba(255,214,10,0.35);
  box-shadow: 0 0 14px -2px rgba(255,214,10,0.22); }}
.cw-head {{ padding: 1px 2px 6px 6px; }}
.cw-card-name {{ color: rgba(255,255,255,0.94); font-size: 14px; font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-tab.missing .cw-card-name {{ color: rgba(235,235,245,0.45); }}
.cw-card-note {{ color: rgba(235,235,245,0.45); font-size: 12px; padding: 0 6px 4px; }}
.cw-apps {{ padding: 5px 4px 0; min-height: 18px; }}
.cw-app {{ border-radius: 6px; padding: 2px 3px; opacity: 0.45; -gtk-icon-effect: dim; }}
.cw-tab:hover .cw-app, .cw-tab.active .cw-app {{ opacity: 0.9; -gtk-icon-effect: none; }}
.cw-app-hit:hover > .cw-app {{ background-color: rgba(255,255,255,0.12); opacity: 1; }}
.cw-tab .cw-app.missing {{ opacity: 0.25; }}
.cw-app-tabs, .cw-app-unread {{ color: rgba(235,235,245,0.75); font-size: 10.5px; font-weight: 600; padding-left: 3px; }}
.cw-well {{ border-radius: 10px; background-color: rgba(22,22,26,0.5); box-shadow: inset 0 1px 2px rgba(0,0,0,0.25); }}
.cw-sep {{ min-height: 1px; background-color: rgba(255,255,255,0.07); margin: 0 8px 0 14px; }}
.cw-box {{ border-radius: 0; padding: 8px 8px 7px 6px; }}
.cw-box-hit:first-child > .cw-box {{ border-top-left-radius: 10px; border-top-right-radius: 10px; }}
.cw-box-hit:last-child > .cw-box {{ border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }}
.cw-box-hit:hover > .cw-box {{ background-color: rgba(255,255,255,0.04); }}
.cw-box.working {{ background-image: linear-gradient(90deg, rgba(255,159,10,0.10), transparent 45%); }}
.cw-box.attention {{ background-image: linear-gradient(90deg, rgba(255,214,10,0.12), transparent 45%); }}
.cw-box .cw-pill {{ background-color: rgba(174,174,178,0.55); }}
/* on the darker well a stripe breathes from .55, or at its faintest it reads as switched off */
@keyframes cw-box-working {{ 0% {{ background-color: rgba(255,159,10,0.55); }} 50% {{ background-color: #ff9f0a; }}
  100% {{ background-color: rgba(255,159,10,0.55); }} }}
@keyframes cw-box-attention {{ 0% {{ background-color: rgba(255,214,10,0.55); }} 50% {{ background-color: #ffd60a; }}
  100% {{ background-color: rgba(255,214,10,0.55); }} }}
.cw-box.working .cw-pill {{ background-color: #ff9f0a; animation: cw-box-working 1.8s ease-in-out infinite; }}
.cw-box.shell .cw-pill {{ background-color: rgba(255,159,10,0.55); }}
.cw-box.waiting .cw-pill {{ background-color: #ffd60a; }}
.cw-box.attention .cw-pill {{ background-color: #ffd60a; box-shadow: 0 0 8px rgba(255,214,10,0.6);
  animation: cw-box-attention 1.4s ease-in-out infinite; }}
.cw-box-name {{ color: rgba(255,255,255,0.96); font-size: 13.5px; font-weight: 600; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-box.attention .cw-box-name {{ color: #ffd60a; font-weight: 700; }}
.cw-box-sub {{ color: rgba(235,235,245,0.60); font-size: 12px; }}
.cw-dot {{ min-width: 6px; min-height: 6px; border-radius: 1.5px; background-color: rgba(235,235,245,0.55); }}
.cw-box.working .cw-dot {{ background-color: rgba(255,159,10,0.8); box-shadow: 0 0 4px rgba(255,159,10,0.5); }}
.cw-tab.card .cw-box .cw-icon {{ opacity: 0.35; }}
.cw-tab.card .cw-box-hit:hover .cw-box .cw-icon {{ opacity: 0.9; }}
.cw-src-head {{ color: rgba(235,235,245,0.45); font-size: 78%; font-weight: 600; padding: 14px 18px 6px; }}
.cw-src-note {{ color: rgba(235,235,245,0.45); font-size: 82%; padding: 2px 18px 8px; }}
.cw-src-name {{ color: rgba(255,255,255,0.92); font-size: 10.5pt; font-weight: 500; }}
.cw-src.waiting .cw-src-name {{ color: #ffd60a; }}
.cw-tab.missing .cw-name, .cw-tab.missing .cw-model {{ color: rgba(235,235,245,0.32); }}
.cw-tab.drop-before > .cw-card {{ box-shadow: inset 0 3px #64d2ff; }}
.cw-tab.drop-after > .cw-card {{ box-shadow: inset 0 -3px #64d2ff; }}
.cw-tab.dragging > .cw-card {{ opacity: 0.5; }}
.cw-name {{ color: rgba(255,255,255,0.96); font-size: 10.5pt; font-weight: 600; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-tab.attention .cw-name {{ color: #ffd60a; }}
.cw-model {{ color: rgba(235,235,245,0.60); font-size: 85%; margin-top: 2px; }}
.cw-path {{ color: rgba(235,235,245,0.32); font-family: {MONO}; font-size: 70%; margin-top: 1px; }}
/* fork and close stay quiet until the pointer is on their tab */
.cw-icon {{ padding: 2px 4px; min-width: 16px; min-height: 16px; border: none; background: none; box-shadow: none;
  opacity: 0.35; }}
.cw-tab:hover .cw-icon, .cw-tab.active .cw-icon {{ opacity: 1; }}
.cw-icon:hover {{ background-color: rgba(255,255,255,0.12); border-radius: 8px; }}
.cw-close {{ color: rgba(235,235,245,0.6); font-size: 10pt; }}
.cw-close:hover {{ color: #ffffff; }}
.cw-usage {{ background-color: rgba(62,62,68,0.84); border: 1px solid rgba(255,255,255,0.10); border-radius: 16px;
  padding: 12px 14px 10px; }}
.usage-agent {{ color: rgba(235,235,245,0.45); font-size: 72%; font-weight: 600; letter-spacing: 0.5px; }}
.usage-agent.usage-next {{ margin-top: 10px; }}
.usage-label {{ color: rgba(235,235,245,0.62); font-size: 82%; }}
.usage-value {{ color: rgba(255,255,255,0.92); font-size: 82%; font-weight: 600; }}
.usage-reset {{ color: rgba(235,235,245,0.32); font-size: 72%; }}
.usage-bar trough {{ min-height: 6px; border-radius: 3px; border: none; background-color: rgba(255,255,255,0.10); }}
.usage-bar progress {{ min-height: 6px; border-radius: 3px; border: none; background-color: #64d2ff; }}
/* a limit running out turns its bar and number orange, then red */
.usage-bar.warn progress {{ background-color: #ff9f0a; }}
.usage-bar.high progress {{ background-color: #ff453a; }}
.usage-row.warn .usage-value {{ color: #ff9f0a; }}
.usage-row.high .usage-value {{ color: #ff453a; }}
.cw-update {{ color: #64d2ff; font-size: 85%; font-weight: 600; background-color: rgba(100,210,255,0.14);
  background-image: none; border: none; border-radius: 14px; padding: 6px 10px; box-shadow: none; text-shadow: none; }}
.cw-update:hover {{ background-color: rgba(100,210,255,0.24); }}
.cw-update:active {{ background-color: rgba(100,210,255,0.32); }}
.cw-new {{ color: rgba(255,255,255,0.92); background-color: rgba(70,70,76,0.84); background-image: none;
  box-shadow: none; text-shadow: none; padding: 8px; border: none; border-radius: 16px; }}
.cw-new:hover {{ background-color: rgba(86,86,94,0.90); }}
.cw-new:active {{ background-color: rgba(100,100,108,0.94); }}
.cw-new-label {{ font-size: 88%; font-weight: 600; }}
.cw-footer {{ color: rgba(235,235,245,0.5); font-size: 80%; padding: 6px 18px; }}
.cw-restore {{ background-color: rgba(22,48,78,0.88); border-radius: 16px; padding: 10px 12px; }}
.cw-restore-title {{ color: #64d2ff; font-size: 85%; font-weight: 600; }}
.cw-restore-sub {{ color: rgba(235,235,245,0.6); font-size: 78%; }}
.cw-restore.done {{ background-color: rgba(24,60,36,0.88); }}
.cw-restore.done .cw-restore-title {{ color: #30d158; }}
.cw-header {{ background-color: transparent; }}
.cw-header-title {{ color: rgba(255,255,255,0.96); font-size: 15pt; font-weight: 700; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }}
.cw-header-count {{ color: rgba(235,235,245,0.5); font-size: 80%; }}
.cw-history {{ color: rgba(235,235,245,0.6); border: none; background: none; box-shadow: none; padding: 6px;
  border-radius: 12px; }}
.cw-history:hover {{ color: #ffffff; background-color: rgba(255,255,255,0.10); }}
/* The history is a view of the sidebar itself: the clock swaps the cards for it and the
   header for "← History". A recessed search field, day headings like the usage section's,
   and every closed workspace a card, as wide as the workspaces' cards. */
.cw-history-view entry {{ margin: 2px 8px 6px; padding: 6px 10px; border-radius: 12px; border: none;
  background-color: rgba(22,22,26,0.5); box-shadow: inset 0 1px 2px rgba(0,0,0,0.25); color: rgba(255,255,255,0.92); }}
.cw-history-view list {{ background-color: transparent; }}
.cw-history-view list row.cw-history-row {{ background-color: rgba(62,62,68,0.84); border: 1px solid rgba(255,255,255,0.10);
  border-radius: 16px; margin: 4px 8px; padding: 8px 12px; }}
.cw-history-view list row.cw-history-row:hover {{ background-color: rgba(80,80,88,0.92); }}
.cw-history-title {{ color: rgba(255,255,255,0.96); font-size: 13.5px; font-weight: 600; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-history-sub {{ color: rgba(235,235,245,0.60); font-size: 12px; }}
.cw-history-day {{ color: rgba(235,235,245,0.45); font-size: 72%; font-weight: 600; letter-spacing: 0.5px;
  padding: 12px 18px 2px; }}
.cw-history-empty {{ color: rgba(235,235,245,0.5); font-size: 12px; padding: 14px 18px; }}
.cw-older {{ color: rgba(235,235,245,0.7); font-size: 12px; font-weight: 600; background: rgba(255,255,255,0.06);
  border: none; box-shadow: none; border-radius: 12px; padding: 6px 10px; margin: 6px 8px 10px; }}
.cw-older:hover {{ background: rgba(255,255,255,0.12); color: #ffffff; }}
""".encode()

@functools.lru_cache(maxsize=1)
def _fork_pixbuf():
    """The fork icon, decoded once: parsing the SVG costs ~1.6ms per tab on a full rebuild.
    None where GdkPixbuf cannot read SVG."""
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(FORK_ICON_SVG)
        loader.close()
    except Exception:
        return None
    return loader.get_pixbuf()


def logo(size: int = 22) -> Gtk.Widget:
    """Sidelit's mark; nothing where GdkPixbuf cannot read SVG (the name alone)."""
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.set_size(size, size)
        loader.write(LOGO_SVG)
        loader.close()
    except Exception:
        return Gtk.Box()
    return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())


def fork_icon() -> Gtk.Image:
    pixbuf = _fork_pixbuf()
    if pixbuf is None:
        return Gtk.Image.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.MENU)
    return Gtk.Image.new_from_pixbuf(pixbuf)


def app_icon(app: str, size: int = 18) -> Gtk.Image:
    icon = app_entry(app)[0]
    image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.MENU) if icon is not None else \
        Gtk.Image.new_from_icon_name("application-x-executable-symbolic", Gtk.IconSize.MENU)
    image.set_pixel_size(size)
    return image


def state_pill() -> Gtk.Widget:
    """The capsule at a card's left edge that the tab's state classes colour."""
    pill = Gtk.Box()
    pill.get_style_context().add_class("cw-pill")
    pill.set_margin_top(2)
    pill.set_margin_bottom(2)
    pill.set_margin_end(6)
    return pill


def _on_enter(w, _ev):
    w.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
    return False


def _on_leave(w, ev):
    if ev.detail != Gdk.NotifyType.INFERIOR:  # onto its own fork or close button is still on the tab
        w.unset_state_flags(Gtk.StateFlags.PRELIGHT)
    return False


def hoverable(w: Gtk.EventBox) -> Gtk.EventBox:
    """Let a tab's :hover rules (the card swelling, its buttons lighting up) see the pointer: an
    event box hears no crossings unless it asks for them, and so never turns prelight itself."""
    w.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
    w.connect("enter-notify-event", _on_enter)
    w.connect("leave-notify-event", _on_leave)
    return w


def in_room(content: Gtk.Widget) -> Gtk.Stack:
    """`content` in a Claude's box's room (box_room): a stack is as tall as its tallest page,
    and its other page is that much text, never shown."""
    title, sub = box_room()
    ghost = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    for text, cls in ((title, "cw-box-name"), (sub, "cw-box-sub")):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class(cls)
        ghost.pack_start(lbl, False, False, 0)
    stack = Gtk.Stack(vhomogeneous=True, hhomogeneous=False)
    stack.add(ghost)
    stack.add(content)
    stack.show_all()  # (a page becomes the shown one only once it is visible itself)
    stack.set_visible_child(content)
    return stack


def card(w: Gtk.Widget) -> Gtk.Widget:
    """The gap around a card that is not a tab (GTK 3 ignores CSS margins on an event box or a
    button; a tab's card is a box inside its event box, with its margins in the CSS)."""
    w.set_margin_start(8)
    w.set_margin_end(8)
    w.set_margin_top(3)
    w.set_margin_bottom(3)
    return w


class HistoryPanel(Gtk.Box):
    """Closed workspaces, newest first under a heading per day, with a search over their
    titles, what was open and their directories: a view of the sidebar, in place of the
    workspaces. The last RECENT_DAYS days show; "Show older" (or a search) reaches the rest.
    `pick(id)` brings one back, `back()` returns to the workspaces (Esc too)."""

    RECENT_DAYS = 3

    def __init__(self, pick, back):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("cw-history-view")
        self.pick = pick
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search closed workspaces")
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search.connect("activate", lambda *_: self._pick_first())  # Enter: the first match
        self.search.connect("stop-search", lambda *_: back())  # Esc
        self.pack_start(self.search, False, False, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_activate_on_single_click(True)
        self.list.set_filter_func(self._visible)
        self.list.set_header_func(self._header)
        self.list.connect("row-activated", lambda _list, row: self.pick(row.entry["id"]))
        self.empty = Gtk.Label(label="Loading…", xalign=0)
        self.empty.get_style_context().add_class("cw-history-empty")
        self.empty.show()
        self.list.set_placeholder(self.empty)
        self.older = Gtk.Button(label="Show older")
        self.older.get_style_context().add_class("cw-older")
        self.older.connect("clicked", lambda *_: self._show_older())
        self.older.set_no_show_all(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.pack_start(self.list, False, False, 0)
        content.pack_start(self.older, False, False, 0)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(content)
        self.pack_start(self.scroller, True, True, 0)
        self.all = False

    def reset(self):
        """Opened again: an empty search, the recent days, at the top, while the list loads."""
        self.search.set_text("")
        self.all = False
        for row in self.list.get_children():
            self.list.remove(row)
        self.older.hide()
        self.empty.set_text("Loading…")
        self.scroller.get_vadjustment().set_value(0)

    def set_items(self, items: list[dict]):
        for row in self.list.get_children():
            self.list.remove(row)
        since = time.time() - self.RECENT_DAYS * 86400
        for h in items:
            title, sub, needle, tooltip = history_lines(h)
            row = Gtk.ListBoxRow()
            row.entry, row.day = h, day_label(h.get("closed") or 0)
            row.needle = needle
            row.recent = (h.get("closed") or 0) >= since
            row.get_style_context().add_class("cw-history-row")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            for text, cls in ((title, "cw-history-title"), (sub, "cw-history-sub")):
                label = Gtk.Label(label=text, xalign=0)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.get_style_context().add_class(cls)
                box.pack_start(label, False, False, 0)
            row.set_tooltip_text(tooltip)
            row.add(box)
            self.list.add(row)
        self.list.show_all()
        self._refilter()

    def _searching(self) -> bool:
        return bool(self.search.get_text().strip())

    def _visible(self, row) -> bool:
        return (self.all or row.recent or self._searching()) and matches(row.needle, self.search.get_text())

    def _refilter(self):
        rows = self.list.get_children()
        older = sum(1 for r in rows if not r.recent)
        self.older.set_label(f"Show {older} older")
        self.older.set_visible(bool(older) and not self.all and not self._searching())
        if self._searching():
            self.empty.set_text("Nothing matches")
        else:
            self.empty.set_text("Nothing closed in the last few days" if rows else "No closed workspaces yet")
        self.list.invalidate_filter()
        self.list.invalidate_headers()

    def _show_older(self):
        self.all = True
        self._refilter()

    def _header(self, row, before):
        if before is not None and before.day == row.day:
            row.set_header(None)
            return
        label = Gtk.Label(label=row.day.upper(), xalign=0)
        label.get_style_context().add_class("cw-history-day")
        row.set_header(label)

    def _pick_first(self):
        row = next((r for r in self.list.get_children() if self._visible(r)), None)
        if row is not None:
            self.pick(row.entry["id"])


def build_menu(entries: list) -> Gtk.Menu:
    """A GTK 3 menu from the shared menu entries (sidebar_common.SEPARATOR and friends)."""
    menu = Gtk.Menu()
    for entry in entries:
        if entry is SEPARATOR:
            menu.append(Gtk.SeparatorMenuItem())
            continue
        label, action = entry
        item = Gtk.MenuItem(label=label)
        if isinstance(action, list):
            item.set_submenu(build_menu(action))
        elif action is None:
            item.set_sensitive(False)
        else:
            item.connect("activate", lambda _i, cb=action: cb())
        menu.append(item)
    menu.show_all()
    return menu


# --------------------------------------------------------------------------- ui
class Sidebar(Gtk.Window, SidebarLogic):
    def __init__(self):
        super().__init__(title="Sidelit")
        self._init_logic()

        width = int(self.cfg["width"])
        self._monitor = self._pick_monitor(str(self.cfg.get("monitor", "leftmost")))
        if GtkLayerShell is not None:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
            GtkLayerShell.set_namespace(self, "sidelit")
            for edge in (GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM):
                GtkLayerShell.set_anchor(self, edge, True)
            GtkLayerShell.set_exclusive_zone(self, width)
            if self._monitor is not None:
                GtkLayerShell.set_monitor(self, self._monitor)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        elif not X11_DOCK:
            self.set_decorated(False)  # GNOME's extension docks it: no title bar
        disp = Gdk.Display.get_default()
        disp.connect("monitor-added", lambda *_: self._screens_changed("monitor added"))
        disp.connect("monitor-removed", lambda *_: self._screens_changed("monitor removed"))
        for i in range(disp.get_n_monitors()):
            disp.get_monitor(i).connect("notify::geometry", self._on_geometry_changed)
        self.connect("delete-event", self._on_delete)
        self.set_default_size(width, 600)
        self.set_size_request(width, -1)
        if X11_DOCK:  # a dock window, as tall as its screen (after the sizes above)
            xdock.dock(self, self._monitor, width)

        self.get_style_context().add_class("cw-window")
        visual = self.get_screen().get_rgba_visual()
        # without one (no compositor) the clear window is just dark; X11 has one either way,
        # but it is clear only while a compositor runs
        if visual is not None and (not X11_DOCK or self.get_screen().is_composited()):
            self.set_visual(visual)

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # the theme's CSS over it (themes.py): a rule of the theme wins over the same one above
        self._theme_css, self._theme = Gtk.CssProvider(), None
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self._theme_css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("cw-root")
        root.get_style_context().add_class("cw-panel")
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.get_style_context().add_class("cw-header")
        header.set_margin_bottom(2)
        self.back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.MENU)
        self.back_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.back_btn.set_tooltip_text("Back to the workspaces")
        self.back_btn.get_style_context().add_class("cw-history")
        self.back_btn.set_valign(Gtk.Align.CENTER)
        self.back_btn.set_margin_start(10)
        self.back_btn.connect("clicked", lambda *_: self._show_history(False))
        self.back_btn.set_no_show_all(True)
        header.pack_start(self.back_btn, False, False, 0)
        mark = logo()
        mark.set_margin_start(16)
        mark.set_valign(Gtk.Align.CENTER)
        header.pack_start(mark, False, False, 0)
        title = Gtk.Label(label="Sidelit", xalign=0)
        title.get_style_context().add_class("cw-header-title")
        title.set_margin_start(8)
        title.set_margin_top(14)
        title.set_margin_bottom(10)
        header.pack_start(title, True, True, 0)
        history_btn = Gtk.Button.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.MENU)
        history_btn.set_relief(Gtk.ReliefStyle.NONE)
        history_btn.set_tooltip_text("Closed workspaces — click one to bring it back")
        history_btn.get_style_context().add_class("cw-history")
        history_btn.set_valign(Gtk.Align.CENTER)
        history_btn.set_margin_end(10)
        history_btn.connect("clicked", lambda *_: self._show_history(True))
        header.pack_end(history_btn, False, False, 0)
        # the gear: the header's menu (also on a right click) and the themes
        menu_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU)
        menu_btn.set_relief(Gtk.ReliefStyle.NONE)
        menu_btn.set_tooltip_text("Settings: theme, and putting windows back")
        menu_btn.get_style_context().add_class("cw-history")
        menu_btn.set_valign(Gtk.Align.CENTER)
        menu_btn.connect("clicked", lambda b: build_menu(self.header_menu() + [("Theme", self.theme_menu())]).popup_at_widget(
            b, Gdk.Gravity.SOUTH, Gdk.Gravity.NORTH, None))
        header.pack_end(menu_btn, False, False, 0)
        self.header_mark, self.header_title, self.history_btn, self.menu_btn = mark, title, history_btn, menu_btn
        self._apply_theme()
        GLib.timeout_add_seconds(3600, lambda: (self._apply_theme(), True)[1])  # "auto" turns with the season
        header_box = Gtk.EventBox()
        header_box.add(header)
        header_box.connect("button-press-event", self._on_header_press)
        root.pack_start(header_box, False, False, 0)

        # stays up for as long as a restore runs: windows come back one at a time, with waits
        self.restore_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.restore_box.get_style_context().add_class("cw-restore")
        self.restore_spinner = Gtk.Spinner()
        self.restore_spinner.set_valign(Gtk.Align.START)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.restore_title = Gtk.Label(label="", xalign=0)
        self.restore_title.get_style_context().add_class("cw-restore-title")
        # unwrapped, a long title ("Reopening windows that closed unexpectedly — …") widens the bar
        self.restore_title.set_line_wrap(True)
        self.restore_title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.restore_sub = Gtk.Label(label="", xalign=0)
        self.restore_sub.get_style_context().add_class("cw-restore-sub")
        self.restore_sub.set_ellipsize(Pango.EllipsizeMode.END)
        texts.pack_start(self.restore_title, False, False, 0)
        texts.pack_start(self.restore_sub, False, False, 0)
        self.restore_box.pack_start(self.restore_spinner, False, False, 0)
        self.restore_box.pack_start(texts, True, True, 0)
        self.restore_box.set_no_show_all(True)
        for w in (self.restore_spinner, texts, self.restore_title, self.restore_sub):
            w.show()
        card(self.restore_box)
        root.pack_start(self.restore_box, False, False, 0)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # other sources (sources.py): sessions running elsewhere, one group each, under the tabs
        self.sources_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.pack_start(self.list_box, False, False, 0)
        content.pack_start(self.sources_box, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(content)
        # the clock swaps the workspaces for the history, in place
        self.views = Gtk.Stack()
        self.views.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.views.set_transition_duration(180)
        self.views.add_named(scroller, "workspaces")
        self.history = HistoryPanel(self._pick_history, lambda: self._show_history(False))
        self.views.add_named(self.history, "history")
        root.pack_start(self.views, True, True, 0)

        self.footer = Gtk.Label(label="", xalign=0)
        self.footer.get_style_context().add_class("cw-footer")
        self.footer.set_line_wrap(True)
        self.footer.set_no_show_all(True)
        root.pack_start(self.footer, False, False, 0)

        self.usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.usage_box.get_style_context().add_class("cw-usage")
        card(self.usage_box)
        self.usage_box.set_margin_top(8)
        self.usage_box.set_no_show_all(True)
        root.pack_start(self.usage_box, False, False, 0)

        # "Update to X.Y.Z" appears under the usage bars when a newer release is out
        self.update_btn = Gtk.Button(label="")
        self.update_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.update_btn.get_style_context().add_class("cw-update")
        card(self.update_btn)
        self.update_btn.set_margin_top(6)
        self.update_btn.set_no_show_all(True)
        self.update_btn.hide()
        self.update_btn.connect("clicked", self.on_update_clicked)
        root.pack_start(self.update_btn, False, False, 0)

        self.new_btn = new_btn = Gtk.Button(tooltip_text="New workspace — pick a template",
                                            relief=Gtk.ReliefStyle.NONE)
        new_label = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
        new_label.pack_start(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU), False, False, 0)
        text = Gtk.Label(label="New workspace")
        text.get_style_context().add_class("cw-new-label")
        new_label.pack_start(text, False, False, 0)
        new_btn.add(new_label)
        card(new_btn)
        new_btn.set_margin_top(6)
        new_btn.set_margin_bottom(8)
        new_btn.get_style_context().add_class("cw-new")
        new_btn.connect("clicked", self.on_new_clicked)
        root.pack_start(new_btn, False, False, 0)

        self.add(root)
        self.connect("destroy", lambda *_: Gtk.main_quit())

        self._start_timers()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, lambda *_: (self._screens_changed("SIGUSR2"), True)[1])

        self.daemon = ipc.Client(self._on_state, self._on_connection)
        if not self.daemon.connected:
            self._footer("waiting for the daemon… (sidelit status)", 3600)

    # ---- screens --------------------------------------------------------------------------
    @staticmethod
    def _pick_monitor(rule: str):
        disp = Gdk.Display.get_default()
        mons = [disp.get_monitor(i) for i in range(disp.get_n_monitors())]
        if not mons:
            return None
        if rule == "largest":
            return max(mons, key=lambda m: m.get_geometry().width * m.get_geometry().height)
        if rule == "leftmost":
            return min(mons, key=lambda m: (m.get_geometry().x, m.get_geometry().y))
        for m in mons:
            if rule.lower() in (m.get_model() or "").lower() or rule.lower() in (m.get_manufacturer() or "").lower():
                return m
        return None

    def _on_geometry_changed(self, *_):
        """Screens moved or resized in the display settings: a different one may now be the
        leftmost or largest. Only a change of pick restarts; a mode change alone does not."""
        if self._pick_monitor(str(self.cfg.get("monitor", "leftmost"))) is not self._monitor:
            self._screens_changed("screens rearranged")

    def _on_delete(self, *_):
        """The compositor closes a layer surface whose output vanished."""
        self._screens_changed("surface closed")
        return True

    def _place_on_gnome(self):
        """Tell GNOME's extension the sidebar is there. GTK 3 does not know a screen's
        connector name, so it goes on the leftmost screen."""
        self._monitor = self._pick_monitor(str(self.cfg.get("monitor", "leftmost")))
        gnome.place_sidebar("")

    # ---- theme ------------------------------------------------------------------------------
    def _apply_theme(self):
        """The theme config.json picks (themes.py): its CSS over the sidebar's."""
        name = themes.wanted(self.cfg)
        if name == self._theme:
            return
        self._theme = name
        try:
            self._theme_css.load_from_data(themes.css(themes.load(name)).encode())
        except GLib.Error as e:  # (GTK 3 takes no rule GTK 4 alone knows: the sidebar as it is)
            print(f"sidelit: theme {name}: {e}", file=sys.stderr)
            self._theme_css.load_from_data(b"")

    # ---- history ----------------------------------------------------------------------------
    def _show_history(self, show: bool):
        """The history in place of the workspaces (HistoryPanel), or back; the list is asked
        for each time it opens, not pushed. Picking one brings it back with its windows."""
        self.back_btn.set_visible(show)
        self.header_mark.set_visible(not show)
        self.history_btn.set_visible(not show)
        self.menu_btn.set_visible(not show)
        self.header_title.set_text("History" if show else "Sidelit")
        self.header_title.set_margin_start(2 if show else 8)
        self.views.set_visible_child_name("history" if show else "workspaces")
        if not show:
            return
        if gnome.hosted():  # GNOME's dock gets the keyboard only when it asks: the search needs it
            gnome.focus_sidebar()
        elif X11_DOCK:  # (so does a dock on X11)
            xdock.focus(self)
        self.history.reset()
        self.history.search.grab_focus()

        def done(result, error):
            if error:
                self.history.empty.set_text(f"Could not load the history: {error}")
            else:
                self.history.set_items(result or [])
        self.daemon.request("history", done)

    def _pick_history(self, entry_id):
        self._show_history(False)
        self.send("history_restore", id=entry_id)

    # ---- header menu ------------------------------------------------------------------------
    def _on_header_press(self, widget, ev):
        if ev.button != 3:
            return False
        build_menu(self.header_menu()).popup_at_pointer(ev)
        return True

    def _show_restore(self, restore: dict):
        banner = self._restore_texts(restore)
        if banner is None:
            self.restore_box.hide()
            self._restore_hide_at = 0.0
            return
        ctx = self.restore_box.get_style_context()
        (ctx.remove_class if banner["running"] else ctx.add_class)("done")
        self.restore_title.set_text(banner["title"])
        self.restore_sub.set_text(banner["sub"])
        if banner["running"]:
            self.restore_spinner.start()
            self.restore_spinner.show()
        else:
            self.restore_spinner.stop()
            self.restore_spinner.hide()
        self.restore_box.show()
        self._restore_hide_at = banner["hide_at"]

    # ---- updates ----------------------------------------------------------
    def _offer_update(self, latest: str):
        self.update_btn.set_label(f"Update to {latest}")
        self.update_btn.set_tooltip_text(self.update_tooltip())
        self.update_btn.show()
        return False

    def _hide_update(self):
        self.update_btn.hide()

    # ---- usage ------------------------------------------------------------
    def _render_usage(self, groups):
        """The bars of each agent that has usage limits, under its name."""
        for child in self.usage_box.get_children():
            self.usage_box.remove(child)
        self._countdowns = []
        for i, (agent, rows) in enumerate(groups):
            head = Gtk.Label(label=agent.upper(), xalign=0)  # (the sidebar's small headings: cw-src-head)
            head.get_style_context().add_class("usage-agent")
            if i:
                head.get_style_context().add_class("usage-next")
            self.usage_box.pack_start(head, False, False, 0)
            self._render_usage_rows(rows)
        for child in self.usage_box.get_children():
            child.show_all()
        self.usage_box.set_visible(bool(groups))
        return False

    def _render_usage_rows(self, rows):
        for caption, percent, resets in rows:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = Gtk.Label(label=caption, xalign=0)
            name.get_style_context().add_class("usage-label")
            value = Gtk.Label(label="%d%%" % percent, xalign=1)
            value.get_style_context().add_class("usage-value")
            level = usage_level(percent)
            head.pack_start(name, True, True, 0)
            head.pack_end(value, False, False, 0)
            bar = Gtk.ProgressBar()
            bar.set_fraction(max(0.0, min(percent, 100)) / 100.0)
            bar.get_style_context().add_class("usage-bar")
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            row.get_style_context().add_class("usage-row")
            if level:
                bar.get_style_context().add_class(level)
                row.get_style_context().add_class(level)
            row.pack_start(head, False, False, 0)
            row.pack_start(bar, False, False, 0)
            left = countdown(resets)
            if left:
                cd = Gtk.Label(label="resets in %s" % left, xalign=1)
                cd.get_style_context().add_class("usage-reset")
                row.pack_start(cd, False, False, 0)
                self._countdowns.append((cd, resets))
            self.usage_box.pack_start(row, False, False, 0)

    # ---- other sources (sources.py) ----------------------------------------------------
    def _draw_sources(self):
        for child in self.sources_box.get_children():
            self.sources_box.remove(child)
        for src, listed in self._source_groups():
            head = Gtk.Label(label=src["name"].upper(), xalign=0)
            head.get_style_context().add_class("cw-src-head")
            self.sources_box.pack_start(head, False, False, 0)
            if src["error"]:
                err = Gtk.Label(label=src["error"], xalign=0)
                err.set_line_wrap(True)
                err.get_style_context().add_class("cw-src-note")
                self.sources_box.pack_start(err, False, False, 0)
            if not listed and not src["error"]:
                none = Gtk.Label(label="nothing else right now" if src["items"] else "nothing right now", xalign=0)
                none.get_style_context().add_class("cw-src-note")
                self.sources_box.pack_start(none, False, False, 0)
            for item in listed:
                self.sources_box.pack_start(self._make_source_item(src["key"], item), False, False, 0)
        self.sources_box.show_all()

    def _make_source_item(self, source: str, item: dict) -> Gtk.Widget:
        w = Gtk.EventBox()
        ctx = w.get_style_context()
        for cls in ("cw-tab", "claude", "cw-src", SOURCE_STATE_CLASS.get(item["state"], "idle")):
            ctx.add_class(cls)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.set_margin_end(8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.get_style_context().add_class("cw-card")
        row.pack_start(state_pill(), False, False, 0)
        row.pack_start(col, True, True, 0)
        title = Gtk.Label(label=item["title"], xalign=0)
        title.set_line_wrap(True)
        title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_lines(2)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("cw-src-name")
        sub = Gtk.Label(label=item_sub(item), xalign=0)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.get_style_context().add_class("cw-model")
        col.pack_start(title, False, False, 0)
        col.pack_start(sub, False, False, 0)
        w.add(row)
        if item["url"]:
            w.set_tooltip_text(f"{item['url']}\nright-click: open on its own workspace")
        w.connect("button-press-event", lambda _w, ev: self._on_source_item_press(ev, source, item))
        w.connect("button-release-event", lambda _w, ev: self._on_source_item_click(ev, source, item))
        hoverable(w)
        return w

    def _on_source_item_click(self, ev, source: str, item: dict) -> bool:
        if ev.button != 1:
            return False
        self.source_item_clicked(source, item)
        return True

    def _on_source_item_press(self, ev, source: str, item: dict) -> bool:
        if ev.button != 3 or not item["url"]:
            return False
        build_menu(self.source_item_menu(source, item)).popup_at_pointer(ev)
        return True

    # ---- rendering --------------------------------------------------------
    def render(self):
        rows = self._tab_rows()
        snapshot = tab_snapshot(rows)
        if snapshot == self._snapshot:
            return
        order = [(r[0], r[1]) for r in snapshot]
        old_order = [(r[0], r[1]) for r in self._snapshot] if self._snapshot else None
        self._snapshot = snapshot
        if order != old_order or len(self.list_box.get_children()) != len(rows):
            for child in self.list_box.get_children():
                self.list_box.remove(child)
            for tab, info, attention in rows:
                self.list_box.pack_start(self._make_tab(tab, info, attention), False, False, 0)
            self.list_box.show_all()
        else:  # same tabs: just update text and state classes, no widget churn
            for child, (tab, info, attention) in zip(self.list_box.get_children(), rows):
                self._update_tab(child, tab, info, attention)

    def _classes(self, ctx, tab, info, attention):
        for cls, on in tab_classes(tab, info, attention):
            (ctx.add_class if on else ctx.remove_class)(cls)
    def _update_tab(self, w, tab, info, attention):
        if w.cw_box_keys != [b["key"] for b in info["boxes"]] or w.cw_app_keys != [a["key"] for a in info["apps"]] \
                or w.cw_card != (not info["item"]) or (info["item"] and w.cw_session_id != info["session_id"]) \
                or w.cw_head != info["head"]:
            # a Claude or a window came or went, or the fork button appears/disappears: rebuild this tab only
            new = self._make_tab(tab, info, attention)
            pos = self.list_box.get_children().index(w)
            self.list_box.remove(w)
            self.list_box.pack_start(new, False, False, 0)
            self.list_box.reorder_child(new, pos)
            new.show_all()
            return
        w.cw_tab = tab
        self._classes(w.get_style_context(), tab, info, attention)
        if w.cw_card:
            w.set_tooltip_text(info["tip"] or None)
            if w.cw_head and w.cw_name.get_text() != info["title"]:
                w.cw_name.set_text(info["title"])
            if w.cw_note.get_text() != info["sub"]:
                w.cw_note.set_text(info["sub"])
                w.cw_note.set_no_show_all(not info["sub"])
                w.cw_note.set_visible(bool(info["sub"]))
            for bw, box in zip(w.cw_boxes, info["boxes"]):
                self._update_box(bw, box)
            for hit, app in zip(w.cw_apps, info["apps"]):
                self._update_app(hit, app)
            return
        if w.cw_title.get_text() != info["title"]:
            w.cw_title.set_text(info["title"])
        if w.cw_sub.get_text() != info["sub"]:
            w.cw_sub.set_text(info["sub"])
        if w.cw_path.get_text() != info["path"]:
            w.cw_path.set_text(info["path"])
            w.cw_path.set_no_show_all(not info["path"])
            w.cw_path.set_visible(bool(info["path"]))

    def _close_button(self, w) -> Gtk.Button:
        close = Gtk.Button(label="×")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Close workspace: close all its windows and stop keeping it")
        close.get_style_context().add_class("cw-icon")
        close.get_style_context().add_class("cw-close")
        close.connect("clicked", lambda *_: self.close_tab(w.cw_tab))
        return close

    def _make_tab(self, tab: dict, info: dict, attention: bool) -> Gtk.Widget:
        w = Gtk.EventBox()
        ctx = w.get_style_context()
        ctx.add_class("cw-tab")
        self._classes(ctx, tab, info, attention)
        w.cw_card, w.cw_head = not info["item"], info["head"]
        w.add(self._plain_card(w, tab, info) if info["item"] else self._card(w, tab, info))
        if info["closed"] and info["item"]:  # (a card's tip says so itself)
            w.set_tooltip_text("Closed: click to reopen it")
        w.cw_tab = tab
        w.cw_session_id = info["session_id"]
        w.cw_pressed = False
        w.cw_dragging = False
        w.connect("button-press-event", self._on_tab_button_press)
        w.connect("button-release-event", self._on_tab_button_release)
        hoverable(w)
        if tab.get("space") is not None:
            w.add_events(Gdk.EventMask.BUTTON_MOTION_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
            w.connect("motion-notify-event", self._on_tab_motion)
        return w

    def _card(self, w, tab: dict, info: dict) -> Gtk.Widget:
        """A workspace: its header (name, close) if it has one, a box per Claude (without a
        header the first has the close button), an icon per other window; its folder and shortcut
        show on hover."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.get_style_context().add_class("cw-card")
        w.set_tooltip_text(info["tip"] or None)
        w.cw_name = None
        if info["head"]:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            head.get_style_context().add_class("cw-head")
            name = Gtk.Label(label=info["title"], xalign=0)
            name.set_line_wrap(True)
            name.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            name.set_lines(2)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.get_style_context().add_class("cw-card-name")
            head.pack_start(name, True, True, 0)
            close = self._close_button(w)
            close.set_valign(Gtk.Align.START)
            head.pack_end(close, False, False, 0)
            card.pack_start(head, False, False, 0)
            w.cw_name = name
        note = Gtk.Label(label=info["sub"], xalign=0)
        note.get_style_context().add_class("cw-card-note")
        note.set_no_show_all(not info["sub"])
        card.pack_start(note, False, False, 0)
        w.cw_boxes, w.cw_apps = [], []
        well = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        well.get_style_context().add_class("cw-well")
        for i, box in enumerate(info["boxes"]):
            if i:
                sep = Gtk.Box()
                sep.get_style_context().add_class("cw-sep")
                well.pack_start(sep, False, False, 0)
            bw = self._make_box(w, box, closes=i == 0 and not info["head"])
            well.pack_start(bw, False, False, 0)
            w.cw_boxes.append(bw)
        well.set_no_show_all(not info["boxes"])
        card.pack_start(well, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.get_style_context().add_class("cw-apps")
        for app in info["apps"]:
            hit = self._make_app(w, app)
            row.pack_start(hit, False, False, 0)
            w.cw_apps.append(hit)
        row.set_no_show_all(not (info["apps"] or info["boxes"]))  # (a card with a Claude keeps the line)
        card.pack_start(row, False, False, 0)
        w.cw_note = note
        w.cw_box_keys, w.cw_app_keys = [b["key"] for b in info["boxes"]], [a["key"] for a in info["apps"]]
        return card

    @staticmethod
    def _app_tip(app: dict) -> str:
        name = app_entry(app["app"])[1] or app["name"]
        notes = [f"{app['tabs']} tabs" if app["tabs"] else "", f"{app['unread']} unread" if app["unread"] else "",
                 "missing: it comes back with a restore" if app["missing"] else ""]
        notes = ", ".join(filter(None, notes))
        return f"{name}: {app['title']}" + (f" ({notes})" if notes else "")

    def _make_app(self, w, app: dict) -> Gtk.Widget:
        """An app's window: its icon, then its tab count (a browser's) and what is unread in it;
        its title on hover. Its own event box, as a Claude's box: a click brings that
        window forward, a drag still moves the card."""
        hit = Gtk.EventBox()
        hit.get_style_context().add_class("cw-app-hit")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.get_style_context().add_class("cw-app")
        row.pack_start(app_icon(app["app"], 14), False, False, 0)
        hit.cw_tabs = Gtk.Label()
        hit.cw_tabs.get_style_context().add_class("cw-app-tabs")
        hit.cw_unread = Gtk.Label()
        hit.cw_unread.get_style_context().add_class("cw-app-unread")
        hit.cw_unread.set_valign(Gtk.Align.CENTER)
        row.pack_start(hit.cw_tabs, False, False, 0)
        row.pack_start(hit.cw_unread, False, False, 0)
        hit.add(row)
        hit.cw_row, hit.cw_app, hit.cw_box = row, None, None  # (right-click: the card's own menu)
        self._update_app(hit, app)
        hit.connect("button-press-event", self._on_box_press, w)
        hit.connect("button-release-event", self._on_app_release, w)
        hoverable(hit)
        return hit

    def _update_app(self, hit, app: dict):
        ctx = hit.cw_row.get_style_context()
        (ctx.add_class if app["missing"] else ctx.remove_class)("missing")
        if hit.cw_app is None or hit.cw_app["title"] != app["title"]:
            hit.set_tooltip_text(self._app_tip(app))
        for lbl, n in ((hit.cw_tabs, app["tabs"]), (hit.cw_unread, app["unread"])):
            text = (f"{n}" if n < 1000 else "999+") if n else ""
            if lbl.get_text() != text:
                lbl.set_text(text)
            lbl.set_no_show_all(not n)
            lbl.set_visible(bool(n))
        hit.cw_app = app

    def _on_app_release(self, hit, ev, w) -> bool:
        if ev.button != 1 or w.cw_dragging or not w.cw_pressed:
            return False  # (the card ends its drag)
        w.cw_pressed = False
        self.on_app_clicked(w.cw_tab, hit.cw_app)
        return True

    def _make_box(self, w, box: dict, closes: bool = False) -> Gtk.Widget:
        """One Claude: its stripe, glyph and title, the model with a block per subagent, fork
        (and the card's close button when the card has no header). Its
        own event box: a click on it goes to that Claude's terminal, a drag still moves the card."""
        hit = Gtk.EventBox()
        hit.get_style_context().add_class("cw-box-hit")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.get_style_context().add_class("cw-box")
        for cls, on in box_classes(box):
            (row.get_style_context().add_class if on else row.get_style_context().remove_class)(cls)
        pill = Gtk.Box()
        pill.get_style_context().add_class("cw-pill")
        row.pack_start(pill, False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
        name = Gtk.Label(label=f"{box['glyph']} {box['title']}", xalign=0)
        name.set_line_wrap(True)
        name.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        name.set_lines(2)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("cw-box-name")
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sub = Gtk.Label(label=box["sub"], xalign=0)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.get_style_context().add_class("cw-box-sub")
        dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        dots.set_valign(Gtk.Align.CENTER)
        self._fill_dots(dots, box["agents"])
        meta.pack_start(sub, False, True, 0)  # (the blocks follow the model, not the box's edge)
        meta.pack_start(dots, False, False, 0)
        col.pack_start(name, False, False, 0)
        col.pack_start(meta, False, False, 0)
        row.pack_start(in_room(col), True, True, 0)  # (a one-line title takes as much height as two)
        if box["session_id"]:
            fork = Gtk.Button()
            fork.set_image(fork_icon())
            fork.set_relief(Gtk.ReliefStyle.NONE)
            fork.set_focus_on_click(False)
            fork.set_valign(Gtk.Align.CENTER)
            fork.set_tooltip_text("Fork this session into a new workspace")
            fork.get_style_context().add_class("cw-icon")
            fork.connect("clicked", lambda *_: self.fork(w.cw_tab, box["session_id"]))
        if closes:  # (pack_end: the last packed sits rightmost)
            close = self._close_button(w)
            close.set_valign(Gtk.Align.START)
            row.pack_end(close, False, False, 0)
        if box["session_id"]:
            row.pack_end(fork, False, False, 0)
        hit.add(row)
        hit.cw_row, hit.cw_name, hit.cw_sub, hit.cw_dots, hit.cw_box = row, name, sub, dots, box
        hit.connect("button-press-event", self._on_box_press, w)
        hit.connect("button-release-event", self._on_box_release, w)
        hoverable(hit)
        return hit

    def _update_box(self, hit, box: dict):
        ctx = hit.cw_row.get_style_context()
        for cls, on in box_classes(box):
            (ctx.add_class if on else ctx.remove_class)(cls)
        text = f"{box['glyph']} {box['title']}"
        if hit.cw_name.get_text() != text:
            hit.cw_name.set_text(text)
        if hit.cw_sub.get_text() != box["sub"]:
            hit.cw_sub.set_text(box["sub"])
        if len(hit.cw_dots.get_children()) != min(box["agents"], MAX_DOTS):
            self._fill_dots(hit.cw_dots, box["agents"])
            hit.cw_dots.show_all()
        hit.cw_box = box

    @staticmethod
    def _fill_dots(dots: Gtk.Box, n: int):
        """A block per subagent at work (a box has room for MAX_DOTS)."""
        for child in dots.get_children():
            dots.remove(child)
        for _ in range(min(n, MAX_DOTS)):
            dot = Gtk.Box()
            dot.get_style_context().add_class("cw-dot")
            dot.set_valign(Gtk.Align.CENTER)
            dots.pack_start(dot, False, False, 0)

    def _on_box_press(self, hit, ev, w) -> bool:
        if ev.button == 3:  # the card's menu, with this Claude's Fork
            build_menu(self.tab_menu(w.cw_tab, hit.cw_box)).popup_at_pointer(ev)
            return True
        return False  # (the card hears the press: a drag from a box moves the card)

    def _on_box_release(self, hit, ev, w) -> bool:
        if ev.button != 1 or w.cw_dragging or not w.cw_pressed:
            return False  # (the card ends its drag)
        w.cw_pressed = False
        self.on_box_clicked(w.cw_tab, hit.cw_box)
        return True

    def _plain_card(self, w, tab: dict, info: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.get_style_context().add_class("cw-card")
        row.pack_start(state_pill(), False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl = Gtk.Label(label=info["title"], xalign=0)
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_lines(2)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.get_style_context().add_class("cw-name")
        lbl.set_valign(Gtk.Align.START)
        lbl.set_yalign(0.0)
        sub = Gtk.Label(label=info["sub"], xalign=0)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.get_style_context().add_class("cw-model")
        path = Gtk.Label(label=info["path"], xalign=0)
        path.set_ellipsize(Pango.EllipsizeMode.START)
        path.get_style_context().add_class("cw-path")
        path.set_no_show_all(not info["path"])
        col.pack_start(lbl, False, False, 0)
        col.pack_start(sub, False, False, 0)
        col.pack_start(path, False, False, 0)
        row.pack_start(col, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right.set_valign(Gtk.Align.FILL)
        icons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        icons.set_halign(Gtk.Align.END)
        icons.set_valign(Gtk.Align.CENTER)
        if info["session_id"]:
            fork = Gtk.Button()
            fork.set_image(fork_icon())
            fork.set_relief(Gtk.ReliefStyle.NONE)
            fork.set_focus_on_click(False)
            fork.set_tooltip_text("Fork this session into a new workspace")
            fork.get_style_context().add_class("cw-icon")
            fork.connect("clicked", lambda *_: self.fork(w.cw_tab, info["session_id"]))
            icons.pack_start(fork, False, False, 0)
        close = self._close_button(w)
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.END)
        right.pack_end(close, False, False, 0)  # bottom-right, level with the path line
        right.pack_start(icons, True, True, 0)
        row.pack_end(right, False, False, 0)
        w.cw_title, w.cw_sub, w.cw_path, w.cw_box_keys, w.cw_app_keys = lbl, sub, path, [], []
        return row

    # ---- drag to reorder: manual pointer tracking (GTK DnD is flaky on layer-shell) --------
    def _drop_target(self, w, ev):
        res = w.translate_coordinates(self.list_box, int(ev.x), int(ev.y))
        if not res:
            return None, True
        ly = res[-1]
        children = [c for c in self.list_box.get_children() if c.cw_tab.get("space") is not None]
        for child in children:
            a = child.get_allocation()
            if a.y <= ly < a.y + a.height:
                return child, ly < a.y + a.height / 2
        if not children:
            return None, True
        return (children[0], True) if ly < children[0].get_allocation().y else (children[-1], False)

    def _clear_drop_marks(self):
        for child in self.list_box.get_children():
            ctx = child.get_style_context()
            ctx.remove_class("drop-before")
            ctx.remove_class("drop-after")

    def _on_tab_motion(self, w, ev):
        if not w.cw_pressed or not (ev.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        if not self.can_reorder():
            return False
        if not w.cw_dragging:
            if abs(ev.y - w.cw_press_y) < self.DRAG_THRESHOLD:
                return False
            w.cw_dragging = True
            w.get_style_context().add_class("dragging")
        target, before = self._drop_target(w, ev)
        self._clear_drop_marks()
        if target is not None and target is not w:
            target.get_style_context().add_class("drop-before" if before else "drop-after")
        w.cw_drop = (target, before)
        return True

    def _finish_drag(self, w):
        w.cw_dragging = False
        w.get_style_context().remove_class("dragging")
        self._clear_drop_marks()
        target, before = getattr(w, "cw_drop", (None, True))
        w.cw_drop = (None, True)
        if target is not None and target is not w:
            self.finish_drag(w.cw_tab, target.cw_tab, before)

    def _on_tab_button_press(self, w, ev):
        if ev.button == 1:
            if ev.type == Gdk.EventType._2BUTTON_PRESS:
                self.rename_dialog(w.cw_tab)
                return True
            w.cw_pressed = True
            w.cw_press_y = ev.y
            w.cw_drop = (None, True)
            return True
        if ev.button == 3:
            build_menu(self.tab_menu(w.cw_tab)).popup_at_pointer(ev)
            return True
        return False

    def _on_tab_button_release(self, w, ev):
        if ev.button != 1:
            return False
        pressed, dragging = w.cw_pressed, w.cw_dragging
        w.cw_pressed = False
        if dragging:
            self._finish_drag(w)
            return True
        if pressed:
            self.on_tab_clicked(w.cw_tab)
            return True
        return False

    # ---- actions ----------------------------------------------------------
    def on_new_clicked(self, button: Gtk.Widget | None = None, *_):
        """'+': the template menu, opening from the button (or straight away, see new_menu)."""
        entries = self.new_menu()
        if entries is not None:
            build_menu(entries).popup_at_widget(button or self.new_btn, Gdk.Gravity.NORTH_EAST,
                                                Gdk.Gravity.SOUTH_EAST, None)

    def web_app_dialog(self, tab: dict):
        dlg = Gtk.Dialog(title="Open web app here", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        e = Gtk.Entry(activates_default=True, placeholder_text="https://…", width_chars=48)
        box.pack_start(e, False, False, 0)
        box.pack_start(Gtk.Label(label="Opens as a browser app window (no tabs) on this workspace, in your default browser.", xalign=0), False, False, 0)
        dlg.get_content_area().add(box)
        dlg.show_all()
        url = e.get_text().strip() if dlg.run() == Gtk.ResponseType.OK else ""
        dlg.destroy()
        if url:
            self.send("webapp", url=url, **self._where(tab))

    def rename_dialog(self, tab: dict):
        dlg = Gtk.Dialog(title="Name workspace", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        e = Gtk.Entry(text=tab.get("name") or "", activates_default=True, placeholder_text="empty = no name")
        box.pack_start(e, False, False, 0)
        box.pack_start(Gtk.Label(label="A name heads the card, above its sessions. Without one, its first session's title says what it is.", xalign=0), False, False, 0)
        dlg.get_content_area().add(box)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            self.send("rename", name=e.get_text().strip(), **self._where(tab))
        dlg.destroy()

    def close_tab(self, tab: dict):
        """Close the whole workspace: every window on it gets a normal close request from the
        compositor (apps may still ask about unsaved work), and it is no longer kept."""
        question = close_question(tab)
        if question:
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.OK_CANCEL, text=question[0], secondary_text=question[1])
            resp = d.run()
            d.destroy()
            if resp != Gtk.ResponseType.OK:
                return
        self.send("close", **self._where(tab))

    def _error(self, msg: str):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.OK, text=msg)
        d.run()
        d.destroy()


def main():
    win = Sidebar()
    win.show_all()
    if gnome.hosted():
        win._place_on_gnome()

    def stop(*_):
        """SIGTERM (systemd) or Ctrl+C: leave the main loop, and die anyway 3 s later, so a
        shutdown that wedges never leaves us unkillable."""
        signal.alarm(3)
        Gtk.main_quit()
        return GLib.SOURCE_CONTINUE

    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, stop)
    Gtk.main()


if __name__ == "__main__":
    main()
