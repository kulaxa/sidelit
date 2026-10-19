"""The GTK 4 sidebar's window (see sidebar4.py, which loads gtk4-layer-shell first).

Only drawing and input live here; what the sidebar does is sidebar_common.SidebarLogic.
On GNOME there is no layer shell: the window is a plain one, which Sidelit's GNOME Shell
extension runs and docks on the left (gnome.py).
"""
from __future__ import annotations

import signal
import sys

import gi

from sidelit import gnome

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
if not gnome.hosted():
    gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib  # noqa: E402

# our dialogs are windows too; with this app id the daemon knows them and leaves them out
# (it has to be set before GTK initialises)
GLib.set_prgname("sidelit")
from gi.repository import Gdk, GdkPixbuf, Gio, Gtk, Pango  # noqa: E402

if gnome.hosted():
    LayerShell = None
else:
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

from sidelit import ipc  # noqa: E402
from sidelit.sidebar_common import (  # noqa: E402
    FORK_ICON_SVG, SEPARATOR, SOURCE_STATE_CLASS, SidebarLogic, box_classes, close_question, countdown, day_label,
    history_lines, item_sub, matches, tab_classes, tab_snapshot, usage_level,
)

FONT = "'Inter', 'SF Pro Text', 'Fira Sans', 'Cantarell', 'Noto Sans', sans-serif"
MONO = "'JetBrainsMonoNL Nerd Font Mono', 'JetBrains Mono', 'DejaVu Sans Mono', monospace"
# Frosted glass after iOS: the window itself is clear, the sidebar a translucent rounded panel
# floating over the wallpaper, every tab a near-solid card on it. Colours are iOS's dark-mode
# system colours: cyan for where you are, orange working, yellow waiting, red failed.
CSS = f"""
window.cw-window {{ background: none; }}
.cw-root, .cw-root * {{ font-family: {FONT}; }}
scrolledwindow, viewport {{ background: none; border: none; }}
scrollbar {{ background: none; border: none; }}
scrollbar slider {{ background-color: rgba(255,255,255,0.18); border-radius: 4px; border: none;
  min-width: 4px; min-height: 24px; margin: 0; }}
.cw-panel {{ background-color: rgba(24,24,27,0.28); border: 1px solid rgba(255,255,255,0.10); border-radius: 22px;
  margin: 6px; }}

/* A tab is its card. Pointing at one swells it and slides every card below it down: both are
   transforms, which GTK 4 draws on the GPU without laying anything out again, so text stays
   put and nothing shakes. The cards are pickable where they are drawn, so a sweep down the
   list hands the hover from card to card without flicker.
   A transition runs with the timing of the state it goes to: into hover it is quick (110 ms),
   so a card the pointer only crosses still pops; out of it slow (380 ms), so a fast sweep
   leaves a wave behind the pointer. With one even timing a quick pass reversed every card
   before it had grown, and the hover seemed not to happen. */
.cw-tab {{ background-color: rgba(62,62,68,0.84); border: 1px solid rgba(255,255,255,0.10); border-radius: 16px;
  margin: 4px 8px; padding: 10px 5px 9px 7px;
  transition: transform 380ms cubic-bezier(0.25, 1, 0.5, 1), background-color 380ms ease-out,
              border-color 380ms ease-out, box-shadow 380ms ease-out; }}
.cw-tab:hover {{ transform: scale(1.04); background-color: rgba(80,80,88,0.92);
  box-shadow: 0 8px 20px rgba(0,0,0,0.35);
  transition: transform 110ms cubic-bezier(0.2, 0.9, 0.3, 1.2), background-color 110ms ease-out,
              border-color 110ms ease-out, box-shadow 110ms ease-out; }}
.cw-tab:hover ~ .cw-tab {{ transform: translateY(6px); transition: transform 110ms cubic-bezier(0.2, 0.9, 0.3, 1); }}
.cw-tab.active {{ background-color: rgba(40,92,116,0.88); border-color: rgba(100,210,255,0.45); }}
.cw-tab.active:hover {{ background-color: rgba(48,104,130,0.92); }}
.cw-tab.claude.attention {{ border-color: rgba(255,214,10,0.45); }}
.cw-tab.drop-before {{ box-shadow: inset 0 3px #64d2ff; }}
.cw-tab.drop-after {{ box-shadow: inset 0 -3px #64d2ff; }}
.cw-tab.dragging {{ opacity: 0.5; }}

/* Claude's state is the capsule at the card's left edge, not a glyph in the title:
     working   orange, breathing
     shell     orange, faint — Claude's turn is over but a shell it started still runs
     external  orange, faint — Claude's turn is over, it waits on CI, a deploy, a job
     waiting   yellow, still — a permission prompt or a question is up
     done      grey, still — nothing wants you
     attention yellow, breathing — it needs you, and you were not typing in it
   (a workspace with Claudes gives each its own box and capsule, below) */
@keyframes cw-working {{
  0%   {{ background-color: rgba(255,159,10,0.30); }}
  50%  {{ background-color: #ff9f0a; }}
  100% {{ background-color: rgba(255,159,10,0.30); }}
}}
@keyframes cw-attention {{
  0%   {{ background-color: rgba(255,214,10,0.30); }}
  50%  {{ background-color: #ffd60a; }}
  100% {{ background-color: rgba(255,214,10,0.30); }}
}}
.cw-pill {{ min-width: 4px; border-radius: 2px; margin: 2px 6px 2px 0; background-color: transparent; }}
.cw-tab.claude .cw-pill {{ background-color: rgba(174,174,178,0.55); }}
.cw-tab.claude.working .cw-pill {{ background-color: #ff9f0a; animation: cw-working 1.8s ease-in-out infinite; }}
.cw-tab.claude.idle .cw-pill {{ background-color: rgba(174,174,178,0.55); }}
.cw-tab.claude.shell .cw-pill {{ background-color: rgba(255,159,10,0.55); }}
.cw-tab.claude.waiting .cw-pill {{ background-color: #ffd60a; }}
.cw-tab.claude.attention .cw-pill {{ background-color: #ffd60a; animation: cw-attention 1.4s ease-in-out infinite; }}
.cw-tab.claude.failed .cw-pill {{ background-color: #ff453a; }}

/* A workspace is a card: a box per Claude in the order they started, under a header with its
   name (two lines at most) and close button when you named it or it has no Claude (else the
   first Claude's title names it, and its box has the close button). Its folder and shortcut
   show on hover. Its Claudes share one recessed well, a thin line between them; each has its
   own state stripe, a working one is lit from its stripe, one that needs you lit yellow and its
   title too, and the card around it glows softly. Subagents at work are small blocks right
   after the model, in the box's colour. The other windows are a line of small grey icons under
   the well, a browser's tab count and what is unread beside them in dim grey; they take their
   colour while the pointer is on the card, so at a glance only a Claude's colour stands out.
   An icon's title shows on hover. */
.cw-tab.card {{ padding: 6px; }}
.cw-tab.card:hover {{ transform: scale(1.02); }}
.cw-tab.card.attention {{ border-color: rgba(255,214,10,0.35); box-shadow: 0 0 14px -2px rgba(255,214,10,0.22); }}
.cw-tab.card.attention:hover {{ box-shadow: 0 0 14px -2px rgba(255,214,10,0.22), 0 8px 20px rgba(0,0,0,0.35); }}
.cw-head {{ padding: 1px 2px 6px 6px; }}
.cw-card-name {{ color: rgba(255,255,255,0.94); font-size: 14px; font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-tab.missing .cw-card-name {{ color: rgba(235,235,245,0.45); }}
.cw-card-note {{ color: rgba(235,235,245,0.45); font-size: 12px; padding: 0 6px 4px; }}
.cw-apps {{ padding: 5px 4px 0; }}
.cw-app {{ border-radius: 6px; padding: 2px 3px; filter: grayscale(1); opacity: 0.45;
  transition: opacity 160ms ease-out, filter 160ms ease-out, background-color 160ms ease-out; }}
.cw-tab:hover .cw-app, .cw-tab.active .cw-app {{ filter: none; opacity: 0.9; }}
.cw-app:hover {{ background-color: rgba(255,255,255,0.12); opacity: 1; }}
.cw-tab .cw-app.missing {{ opacity: 0.25; }}
.cw-app-tabs, .cw-app-unread {{ color: rgba(235,235,245,0.75); font-size: 10.5px; font-weight: 600; padding-left: 3px; }}
.cw-well {{ border-radius: 10px; background-color: rgba(22,22,26,0.5); box-shadow: inset 0 1px 2px rgba(0,0,0,0.25); }}
.cw-sep {{ min-height: 1px; background-color: rgba(255,255,255,0.07); margin: 0 8px 0 14px; }}
.cw-box {{ border-radius: 0; padding: 8px 8px 7px 6px;
  transition: background-color 160ms ease-out, background-image 160ms ease-out; }}
.cw-box:first-child {{ border-top-left-radius: 10px; border-top-right-radius: 10px; }}
.cw-box:last-child {{ border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }}
/* on the darker well a stripe breathes from .55, or at its faintest it reads as switched off */
@keyframes cw-box-working {{
  0%   {{ background-color: rgba(255,159,10,0.55); }}
  50%  {{ background-color: #ff9f0a; }}
  100% {{ background-color: rgba(255,159,10,0.55); }}
}}
@keyframes cw-box-attention {{
  0%   {{ background-color: rgba(255,214,10,0.55); }}
  50%  {{ background-color: #ffd60a; }}
  100% {{ background-color: rgba(255,214,10,0.55); }}
}}
.cw-box:hover {{ background-color: rgba(255,255,255,0.04); }}
.cw-box.working {{ background-image: linear-gradient(90deg, rgba(255,159,10,0.10), transparent 45%); }}
.cw-box.attention {{ background-image: linear-gradient(90deg, rgba(255,214,10,0.12), transparent 45%); }}
.cw-box .cw-pill {{ margin: 0; background-color: rgba(174,174,178,0.55); }}
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
.cw-tab.card .cw-box button.cw-icon {{ opacity: 0.35; }}
.cw-tab.card .cw-box:hover button.cw-icon {{ opacity: 0.9; }}

.cw-src-head {{ color: rgba(235,235,245,0.45); font-size: 78%; font-weight: 600; padding: 14px 18px 6px; }}
.cw-src-note {{ color: rgba(235,235,245,0.45); font-size: 82%; padding: 2px 18px 8px; }}
.cw-src-name {{ color: rgba(255,255,255,0.92); font-size: 10.5pt; font-weight: 500; }}
.cw-src.waiting .cw-src-name {{ color: #ffd60a; }}
.cw-name {{ color: rgba(255,255,255,0.96); font-size: 10.5pt; font-weight: 600; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }}
.cw-tab.attention .cw-name {{ color: #ffd60a; }}
.cw-tab.missing .cw-name, .cw-tab.missing .cw-model {{ color: rgba(235,235,245,0.32); }}
.cw-model {{ color: rgba(235,235,245,0.60); font-size: 85%; margin-top: 2px; }}
.cw-path {{ color: rgba(235,235,245,0.32); font-family: {MONO}; font-size: 70%; margin-top: 1px; }}
/* fork and close stay quiet until the pointer is on their tab */
button.cw-icon {{ padding: 2px 4px; min-width: 16px; min-height: 16px; border: none; background: none;
  box-shadow: none; opacity: 0.35; transition: opacity 160ms ease-out; }}
.cw-tab:hover button.cw-icon, .cw-tab.active button.cw-icon {{ opacity: 1; }}
button.cw-icon:hover {{ background-color: rgba(255,255,255,0.12); border-radius: 8px; }}
button.cw-close {{ color: rgba(235,235,245,0.6); font-size: 10pt; }}
button.cw-close:hover {{ color: #ffffff; }}

.cw-usage {{ background-color: rgba(62,62,68,0.84); border: 1px solid rgba(255,255,255,0.10); border-radius: 16px;
  padding: 12px 14px 10px; margin: 8px 8px 3px; }}
.usage-agent {{ color: rgba(235,235,245,0.45); font-size: 72%; font-weight: 600; letter-spacing: 0.5px; }}
.usage-agent.usage-next {{ margin-top: 10px; }}
.usage-label {{ color: rgba(235,235,245,0.62); font-size: 82%; }}
.usage-value {{ color: rgba(255,255,255,0.92); font-size: 82%; font-weight: 600; }}
.usage-reset {{ color: rgba(235,235,245,0.32); font-size: 72%; }}
progressbar.usage-bar > trough {{ min-height: 6px; border-radius: 3px; border: none; background-color: rgba(255,255,255,0.10); }}
progressbar.usage-bar > trough > progress {{ min-height: 6px; border-radius: 3px; border: none; background-color: #64d2ff; }}
/* a limit running out turns its bar and number orange, then red */
progressbar.usage-bar.warn > trough > progress {{ background-color: #ff9f0a; }}
progressbar.usage-bar.high > trough > progress {{ background-color: #ff453a; }}
.usage-row.warn .usage-value {{ color: #ff9f0a; }}
.usage-row.high .usage-value {{ color: #ff453a; }}
button.cw-update {{ color: #64d2ff; font-size: 85%; font-weight: 600; background: rgba(100,210,255,0.14);
  border: none; border-radius: 14px; padding: 6px 10px; box-shadow: none; text-shadow: none; margin: 6px 8px 3px; }}
button.cw-update:hover {{ background: rgba(100,210,255,0.24); }}
button.cw-new {{ color: rgba(255,255,255,0.92); background: rgba(70,70,76,0.84); box-shadow: none; text-shadow: none;
  padding: 8px; border: none; border-radius: 16px; margin: 6px 8px 8px;
  transition: transform 200ms cubic-bezier(0.2, 0.9, 0.25, 1.15), background-color 160ms ease-out; }}
button.cw-new:hover {{ background: rgba(86,86,94,0.90); transform: scale(1.03); }}
button.cw-new:active {{ background: rgba(100,100,108,0.94); transform: scale(0.98); }}
.cw-new-label {{ font-size: 88%; font-weight: 600; }}
.cw-footer {{ color: rgba(235,235,245,0.5); font-size: 80%; padding: 6px 18px; }}
.cw-restore {{ background-color: rgba(22,48,78,0.88); border-radius: 16px; padding: 10px 12px; margin: 3px 8px; }}
.cw-restore-title {{ color: #64d2ff; font-size: 85%; font-weight: 600; }}
.cw-restore-sub {{ color: rgba(235,235,245,0.6); font-size: 78%; }}
.cw-restore.done {{ background-color: rgba(24,60,36,0.88); }}
.cw-restore.done .cw-restore-title {{ color: #30d158; }}
.cw-header-title {{ color: rgba(255,255,255,0.96); font-size: 15pt; font-weight: 700; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }}
button.cw-history {{ color: rgba(235,235,245,0.6); border: none; background: none; box-shadow: none; padding: 6px;
  border-radius: 12px; }}
button.cw-history:hover {{ color: #ffffff; background-color: rgba(255,255,255,0.10); }}

popover.cw-pop > contents {{ background-color: #2c2c2e; border: 1px solid rgba(255,255,255,0.10); border-radius: 14px;
  color: rgba(255,255,255,0.92); }}
popover.cw-pop > arrow {{ background-color: #2c2c2e; border-color: rgba(255,255,255,0.10); }}
.cw-history-title {{ color: rgba(255,255,255,0.92); font-weight: 600; }}
.cw-history-sub {{ color: rgba(235,235,245,0.6); font-size: 85%; }}
.cw-history-pop entry {{ margin: 8px 8px 4px 8px; border-radius: 10px; }}
.cw-history-pop list {{ background: none; }}
.cw-history-row {{ padding: 6px 10px; border-radius: 10px; }}
.cw-history-row:hover {{ background-color: rgba(255,255,255,0.08); }}
.cw-history-day {{ color: #64d2ff; font-size: 80%; font-weight: 700; padding: 10px 10px 2px 10px; }}
.cw-history-empty {{ color: rgba(235,235,245,0.5); padding: 14px 10px; }}
"""

_FORK_TEXTURE = None
MAX_DOTS = 8  # subagent dots a box has room for


def fork_icon(size: int = 16) -> Gtk.Image:
    # decoding the SVG on every tab rebuild adds up: keep the texture
    global _FORK_TEXTURE
    if _FORK_TEXTURE is None:
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(FORK_ICON_SVG)
            loader.close()
            _FORK_TEXTURE = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
        except Exception:
            return Gtk.Image.new_from_icon_name("edit-copy-symbolic")
    image = Gtk.Image.new_from_paintable(_FORK_TEXTURE)
    image.set_pixel_size(size)
    return image


_APPS: dict[str, tuple] = {}  # app id -> (its icon, its name), from its desktop entry


def app_entry(app: str) -> tuple:
    """An app's icon and name from its desktop entry (Wayland's app id is the entry's name),
    (None, "") when it has none."""
    if app not in _APPS:
        found = (None, "")
        for name in dict.fromkeys((app, app.lower())):
            info = Gio.DesktopAppInfo.new(f"{name}.desktop") if name else None
            if info is not None:
                found = (info.get_icon(), info.get_display_name() or "")
                break
        _APPS[app] = found
    return _APPS[app]


def app_icon(app: str, size: int = 16) -> Gtk.Image:
    icon = app_entry(app)[0]
    image = Gtk.Image.new_from_gicon(icon) if icon is not None else \
        Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
    image.set_pixel_size(size)
    return image


def label(text: str, *classes: str, xalign: float = 0.0) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign)
    for cls in classes:
        lbl.add_css_class(cls)
    return lbl


def children(box: Gtk.Widget) -> list[Gtk.Widget]:
    out, child = [], box.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def clear(box: Gtk.Box):
    for child in children(box):
        box.remove(child)


def set_classes(widget: Gtk.Widget, pairs):
    for cls, on in pairs:
        (widget.add_css_class if on else widget.remove_css_class)(cls)


def translate(widget: Gtk.Widget, target: Gtk.Widget, x: float, y: float) -> tuple[float, float] | None:
    """A point in `widget` in `target`'s coordinates (None when they share no root)."""
    res = widget.translate_coordinates(target, x, y)
    if not res or (isinstance(res[0], bool) and not res[0]):
        return None
    return res[-2], res[-1]


def bounds(widget: Gtk.Widget, target: Gtk.Widget) -> tuple[float, float] | None:
    """(top, height) of `widget` in `target`'s coordinates, as laid out (transforms aside)."""
    ok, rect = widget.compute_bounds(target)
    return (rect.get_y(), rect.get_height()) if ok else None


class Menu:
    """A popover menu from the shared menu entries (sidebar_common.SEPARATOR and friends),
    pointing at a spot of `parent`. Each menu has actions of its own, gone with the menu."""

    _serial = 0

    def __init__(self, parent: Gtk.Widget, entries: list):
        Menu._serial += 1
        self.prefix = f"cwm{Menu._serial}"
        self.group = Gio.SimpleActionGroup()
        self.count = 0
        model = self._model(entries)
        self.popover = Gtk.PopoverMenu.new_from_model(model)
        self.popover.add_css_class("cw-pop")
        self.popover.set_has_arrow(False)
        self.popover.insert_action_group(self.prefix, self.group)
        self.popover.set_parent(parent)
        self.popover.connect("closed", lambda p: GLib.idle_add(self._dispose))

    def _model(self, entries: list) -> Gio.Menu:
        menu, section = Gio.Menu(), Gio.Menu()
        for entry in entries:
            if entry is SEPARATOR:
                menu.append_section(None, section)
                section = Gio.Menu()
                continue
            text, action = entry
            if isinstance(action, list):
                section.append_submenu(text, self._model(action))
                continue
            self.count += 1
            name = f"a{self.count}"
            act = Gio.SimpleAction.new(name, None)
            if action is None:
                act.set_enabled(False)
            else:
                act.connect("activate", lambda _a, _p, cb=action: cb())
            self.group.add_action(act)
            section.append(text, f"{self.prefix}.{name}")
        menu.append_section(None, section)
        return menu

    def popup_at(self, x: float, y: float):
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.popover.set_pointing_to(rect)
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        self.popover.popup()

    def popup_above(self):
        self.popover.set_position(Gtk.PositionType.TOP)
        self.popover.popup()

    def _dispose(self):
        if self.popover.get_parent() is not None:
            self.popover.unparent()
        return False


class EntryDialog(Gtk.Window):
    """A small window asking for one line of text: OK (or Enter) hands it to `done`."""

    def __init__(self, title: str, ok: str, text: str, placeholder: str, hint: str, done):
        super().__init__(title=title)
        self.set_default_size(460, -1)
        self.set_resizable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(14)
        self.entry = Gtk.Entry(text=text, placeholder_text=placeholder, hexpand=True)
        self.entry.connect("activate", lambda *_: self._finish(True))
        box.append(self.entry)
        hint_label = Gtk.Label(label=hint, xalign=0)
        hint_label.set_wrap(True)
        box.append(hint_label)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._finish(False))
        confirm = Gtk.Button(label=ok)
        confirm.add_css_class("suggested-action")
        confirm.connect("clicked", lambda *_: self._finish(True))
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)
        self.set_child(box)
        self.done = done
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, *_: keyval == Gdk.KEY_Escape and (self._finish(False), True)[1])
        self.add_controller(keys)

    def _finish(self, ok: bool):
        text = self.entry.get_text().strip()
        self.destroy()
        if ok:
            self.done(text)


class HistoryPanel(Gtk.Box):
    """Closed workspaces, newest first under a heading per day, with a search over their
    titles, what was open and their directories. It scrolls: as a menu they grew taller than
    the screen. `pick(id)` brings one back."""

    def __init__(self, pick, max_height: int):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.pick = pick
        self.search = Gtk.SearchEntry(placeholder_text="Search closed workspaces")
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search.connect("activate", lambda *_: self._pick_first())  # Enter: the first match
        self.append(self.search)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_activate_on_single_click(True)
        self.list.set_filter_func(self._visible)
        self.list.set_header_func(self._header)
        self.list.connect("row-activated", lambda _list, row: self.pick(row.entry["id"]))
        self.empty = label("Loading…", "cw-history-empty")
        self.list.set_placeholder(self.empty)
        self.max_height = max_height
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_propagate_natural_height(True)
        self.scroller.set_max_content_height(max_height)
        self.scroller.set_child(self.list)
        self.scroller.set_vexpand(True)
        self.append(self.scroller)
        self.rows: list[Gtk.ListBoxRow] = []

    def set_items(self, items: list[dict]):
        self.list.remove_all()
        self.rows = []
        for h in items:
            title, sub, needle, tooltip = history_lines(h)
            row = Gtk.ListBoxRow()
            row.entry, row.day, row.needle = h, day_label(h.get("closed") or 0), needle
            row.add_css_class("cw-history-row")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            for text, cls in ((title, "cw-history-title"), (sub, "cw-history-sub")):
                line = label(text, cls)
                line.set_ellipsize(Pango.EllipsizeMode.END)
                box.append(line)
            row.set_tooltip_text(tooltip)
            row.set_child(box)
            self.list.append(row)
            self.rows.append(row)
        self.empty.set_text("No closed workspaces yet")
        # as tall as the list, up to max_height: the list arrives after the panel is up
        natural = self.list.measure(Gtk.Orientation.VERTICAL, -1)[1]
        self.scroller.set_min_content_height(min(natural, self.max_height))

    def _visible(self, row) -> bool:
        return matches(row.needle, self.search.get_text())

    def _refilter(self):
        self.empty.set_text("Nothing matches" if self.search.get_text().strip() else "No closed workspaces yet")
        self.list.invalidate_filter()
        self.list.invalidate_headers()

    def _header(self, row, before):
        if before is not None and before.day == row.day:
            row.set_header(None)
            return
        row.set_header(label(row.day, "cw-history-day"))

    def _pick_first(self):
        row = next((r for r in self.rows if self._visible(r)), None)
        if row is not None:
            self.pick(row.entry["id"])


# --------------------------------------------------------------------------- ui
class Sidebar(Gtk.Window, SidebarLogic):
    def __init__(self, layer: bool = True):
        super().__init__(title="Sidelit")
        self._init_logic()

        width = int(self.cfg["width"])
        self._monitor = self._pick_monitor(str(self.cfg.get("monitor", "leftmost")))
        if not layer:
            self.set_decorated(False)  # GNOME's extension docks it: no title bar
        if layer:
            LayerShell.init_for_window(self)
            LayerShell.set_layer(self, LayerShell.Layer.TOP)
            LayerShell.set_namespace(self, "sidelit")
            for edge in (LayerShell.Edge.LEFT, LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM):
                LayerShell.set_anchor(self, edge, True)
            LayerShell.set_exclusive_zone(self, width)
            if self._monitor is not None:
                LayerShell.set_monitor(self, self._monitor)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.ON_DEMAND)
        monitors = Gdk.Display.get_default().get_monitors()
        monitors.connect("items-changed", lambda *_: self._screens_changed("screens added or removed"))
        for i in range(monitors.get_n_items()):
            monitors.get_item(i).connect("notify::geometry", self._on_geometry_changed)
        self.connect("close-request", self._on_close_request)
        self.set_default_size(width, 600)
        self.set_size_request(width, -1)
        self.add_css_class("cw-window")

        settings = Gtk.Settings.get_default()
        if settings is not None:  # menus, entries and dialogs in the dark variant
            settings.set_property("gtk-application-prefer-dark-theme", True)
        css = Gtk.CssProvider()
        if hasattr(css, "load_from_string"):
            css.load_from_string(CSS)
        else:
            css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("cw-root")
        root.add_css_class("cw-panel")
        self.root_box = root

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("cw-header")
        header.set_margin_bottom(2)
        title = label("Workspaces", "cw-header-title")
        title.set_hexpand(True)
        title.set_margin_start(16)
        title.set_margin_top(14)
        title.set_margin_bottom(10)
        header.append(title)
        history_btn = Gtk.Button(icon_name="document-open-recent-symbolic")
        history_btn.set_has_frame(False)
        history_btn.set_tooltip_text("Closed workspaces — click one to bring it back")
        history_btn.add_css_class("cw-history")
        history_btn.set_valign(Gtk.Align.CENTER)
        history_btn.set_margin_end(10)
        history_btn.connect("clicked", self._on_history_clicked)
        header.append(history_btn)
        press = Gtk.GestureClick(button=3)
        press.connect("pressed", lambda _g, _n, x, y: self._popup(header, x, y, self.header_menu()))
        header.add_controller(press)
        root.append(header)

        # stays up for as long as a restore runs: windows come back one at a time, with waits
        self.restore_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.restore_box.add_css_class("cw-restore")
        self.restore_spinner = Gtk.Spinner()
        self.restore_spinner.set_valign(Gtk.Align.START)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.restore_title = label("", "cw-restore-title")
        # unwrapped, a long title ("Reopening windows that closed unexpectedly — …") widens the bar
        self.restore_title.set_wrap(True)
        self.restore_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.restore_sub = label("", "cw-restore-sub")
        self.restore_sub.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(self.restore_title)
        texts.append(self.restore_sub)
        self.restore_box.append(self.restore_spinner)
        self.restore_box.append(texts)
        self.restore_box.set_visible(False)
        root.append(self.restore_box)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # other sources (sources.py): sessions running elsewhere, one group each, under the tabs
        self.sources_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(self.list_box)
        content.append(self.sources_box)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(content)
        root.append(scroller)

        self.footer = label("", "cw-footer")
        self.footer.set_wrap(True)
        self.footer.set_visible(False)
        root.append(self.footer)

        self.usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.usage_box.add_css_class("cw-usage")
        self.usage_box.set_visible(False)
        root.append(self.usage_box)

        # "Update to X.Y.Z" appears under the usage bars when a newer release is out
        self.update_btn = Gtk.Button(label="")
        self.update_btn.add_css_class("cw-update")
        self.update_btn.set_visible(False)
        self.update_btn.connect("clicked", self.on_update_clicked)
        root.append(self.update_btn)

        new_btn = Gtk.Button()
        new_label = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
        new_label.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        new_label.append(label("New workspace", "cw-new-label"))
        new_btn.set_child(new_label)
        new_btn.set_tooltip_text("New workspace — pick a template")
        new_btn.add_css_class("cw-new")
        new_btn.connect("clicked", self.on_new_clicked)
        self.new_btn = new_btn
        root.append(new_btn)

        self.set_child(root)

        self._start_timers()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, lambda *_: (self._screens_changed("SIGUSR2"), True)[1])

        self.daemon = ipc.Client(self._on_state, self._on_connection)
        if not self.daemon.connected:
            self._footer("waiting for the daemon… (sidelit status)", 3600)

    # ---- screens --------------------------------------------------------------------------
    @staticmethod
    def _pick_monitor(rule: str):
        model = Gdk.Display.get_default().get_monitors()
        mons = [model.get_item(i) for i in range(model.get_n_items())]
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

    def _place_on_gnome(self):
        """Tell GNOME's extension which screen the sidebar goes on."""
        self._monitor = self._pick_monitor(str(self.cfg.get("monitor", "leftmost")))
        connector = (self._monitor.get_connector() or "") if self._monitor is not None else ""
        gnome.place_sidebar(connector)

    def _on_close_request(self, *_):
        """The compositor closes a layer surface whose output vanished."""
        self._screens_changed("surface closed")
        return True

    # ---- menus ------------------------------------------------------------------------------
    def _popup(self, widget: Gtk.Widget, x: float, y: float, entries: list):
        """A menu at a point of `widget`. It hangs off the panel, not the widget, so a redraw
        that replaces the tab under it does not take it down."""
        point = translate(widget, self.root_box, x, y) or (x, y)
        Menu(self.root_box, entries).popup_at(*point)

    # ---- history ----------------------------------------------------------------------------
    def _on_history_clicked(self, button):
        """Closed workspaces, newest first, in a panel that scrolls and searches (HistoryPanel);
        picking one brings it back with its windows. The list is asked for now, not pushed."""
        if gnome.hosted():  # GNOME's dock gets the keyboard only when it asks: the search needs it
            gnome.focus_sidebar()
        pop = Gtk.Popover()
        pop.add_css_class("cw-pop")
        pop.add_css_class("cw-history-pop")
        pop.set_position(Gtk.PositionType.BOTTOM)
        pop.set_parent(button)

        def pick(entry_id):
            pop.popdown()
            self.send("history_restore", id=entry_id)

        panel = HistoryPanel(pick, max(240, self.get_height() - 170))
        panel.set_size_request(int(self.cfg["width"]) - 24, -1)
        pop.set_child(panel)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.get_parent() and p.unparent(), False)[1]))
        pop.popup()
        panel.search.grab_focus()

        def done(result, error):
            if error:
                panel.empty.set_text(f"Could not load the history: {error}")
            else:
                panel.set_items(result or [])
        self.daemon.request("history", done)

    def _show_restore(self, restore: dict):
        banner = self._restore_texts(restore)
        if banner is None:
            self.restore_box.set_visible(False)
            self._restore_hide_at = 0.0
            return
        set_classes(self.restore_box, [("done", not banner["running"])])
        self.restore_title.set_text(banner["title"])
        self.restore_sub.set_text(banner["sub"])
        if banner["running"]:
            self.restore_spinner.start()
        else:
            self.restore_spinner.stop()
        self.restore_spinner.set_visible(banner["running"])
        self.restore_box.set_visible(True)
        self._restore_hide_at = banner["hide_at"]

    # ---- updates ----------------------------------------------------------
    def _offer_update(self, latest: str):
        self.update_btn.set_label(f"Update to {latest}")
        self.update_btn.set_tooltip_text(self.update_tooltip())
        self.update_btn.set_visible(True)
        return False

    def _hide_update(self):
        self.update_btn.set_visible(False)

    # ---- usage ------------------------------------------------------------
    def _render_usage(self, groups):
        """The bars of each agent that has usage limits, under its name."""
        clear(self.usage_box)
        self._countdowns = []
        for i, (agent, rows) in enumerate(groups):
            head = label(agent.upper(), "usage-agent")  # (the sidebar's small headings: cw-src-head)
            if i:
                head.add_css_class("usage-next")
            self.usage_box.append(head)
            self._render_usage_rows(rows)
        self.usage_box.set_visible(bool(groups))
        return False

    def _render_usage_rows(self, rows):
        for caption, percent, resets in rows:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = label(caption, "usage-label")
            name.set_hexpand(True)
            head.append(name)
            head.append(label("%d%%" % percent, "usage-value", xalign=1))
            bar = Gtk.ProgressBar()
            bar.set_fraction(max(0.0, min(percent, 100)) / 100.0)
            bar.add_css_class("usage-bar")
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            row.add_css_class("usage-row")
            level = usage_level(percent)
            if level:
                bar.add_css_class(level)
                row.add_css_class(level)
            row.append(head)
            row.append(bar)
            left = countdown(resets)
            if left:
                cd = label("resets in %s" % left, "usage-reset", xalign=1)
                row.append(cd)
                self._countdowns.append((cd, resets))
            self.usage_box.append(row)

    # ---- other sources (sources.py) ----------------------------------------------------
    def _draw_sources(self):
        clear(self.sources_box)
        for src, listed in self._source_groups():
            self.sources_box.append(label(src["name"].upper(), "cw-src-head"))
            if src["error"]:
                err = label(src["error"], "cw-src-note")
                err.set_wrap(True)
                self.sources_box.append(err)
            if not listed and not src["error"]:
                self.sources_box.append(label("nothing else right now" if src["items"] else "nothing right now",
                                              "cw-src-note"))
            for item in listed:
                self.sources_box.append(self._make_source_item(src["key"], item))

    def _make_source_item(self, source: str, item: dict) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for cls in ("cw-tab", "claude", "cw-src", SOURCE_STATE_CLASS.get(item["state"], "idle")):
            w.add_css_class(cls)
        w.append(self._pill())
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        col.set_margin_end(8)
        title = label(item["title"], "cw-src-name")
        title.set_wrap(True)
        title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_lines(2)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        sub = label(item_sub(item), "cw-model")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        col.append(title)
        col.append(sub)
        w.append(col)
        if item["url"]:
            w.set_tooltip_text(f"{item['url']}\nright-click: open on its own workspace")
        click = Gtk.GestureClick(button=0)

        def pressed(gesture, _n, x, y):
            if gesture.get_current_button() == 3 and item["url"]:
                self._popup(w, x, y, self.source_item_menu(source, item))

        def released(gesture, _n, _x, _y):
            if gesture.get_current_button() == 1:
                self.source_item_clicked(source, item)
        click.connect("pressed", pressed)
        click.connect("released", released)
        w.add_controller(click)
        return w

    # ---- rendering --------------------------------------------------------
    def render(self):
        rows = self._tab_rows()
        snapshot = tab_snapshot(rows)
        if snapshot == self._snapshot:
            return
        order = [(r[0], r[1]) for r in snapshot]
        old_order = [(r[0], r[1]) for r in self._snapshot] if self._snapshot else None
        self._snapshot = snapshot
        tabs = children(self.list_box)
        if order != old_order or len(tabs) != len(rows):
            clear(self.list_box)
            for tab, info, attention in rows:
                self.list_box.append(self._make_tab(tab, info, attention))
        else:  # same tabs: just update text and state classes, no widget churn
            for child, (tab, info, attention) in zip(tabs, rows):
                self._update_tab(child, tab, info, attention)

    def _update_tab(self, w, tab, info, attention):
        if w.cw_box_keys != [b["key"] for b in info["boxes"]] or w.cw_app_keys != [a["key"] for a in info["apps"]] \
                or w.cw_card != (not info["item"]) or (info["item"] and w.cw_session_id != info["session_id"]) \
                or w.cw_head != info["head"]:
            # a Claude or a window came or went, or the fork button appears/disappears: rebuild this tab only
            new = self._make_tab(tab, info, attention)
            self.list_box.insert_child_after(new, w)
            self.list_box.remove(w)
            return
        w.cw_tab = tab
        set_classes(w, tab_classes(tab, info, attention))
        if w.cw_card:
            w.set_tooltip_text(info["tip"] or None)
            if w.cw_head and w.cw_name.get_text() != info["title"]:
                w.cw_name.set_text(info["title"])
            if w.cw_note.get_text() != info["sub"]:
                w.cw_note.set_text(info["sub"])
                w.cw_note.set_visible(bool(info["sub"]))
            for bw, box in zip(w.cw_boxes, info["boxes"]):
                self._update_box(bw, box)
            for aw, app in zip(w.cw_apps, info["apps"]):
                self._update_app(aw, app)
            return
        if w.cw_title.get_text() != info["title"]:
            w.cw_title.set_text(info["title"])
        if w.cw_sub.get_text() != info["sub"]:
            w.cw_sub.set_text(info["sub"])
        if w.cw_path.get_text() != info["path"]:
            w.cw_path.set_text(info["path"])
            w.cw_path.set_visible(bool(info["path"]))

    @staticmethod
    def _pill() -> Gtk.Widget:
        """The capsule at a card's left edge that the tab's state classes colour."""
        pill = Gtk.Box()
        pill.add_css_class("cw-pill")
        return pill

    def _close_button(self, w) -> Gtk.Button:
        close = Gtk.Button(label="×")
        close.set_has_frame(False)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Close workspace: close all its windows and stop keeping it")
        close.add_css_class("cw-icon")
        close.add_css_class("cw-close")
        close.connect("clicked", lambda *_: self.close_tab(w.cw_tab))
        return close

    def _make_tab(self, tab: dict, info: dict, attention: bool) -> Gtk.Widget:
        if info["item"]:
            w = self._make_plain_tab(tab, info, attention)
        else:
            w = self._make_card(tab, info, attention)
        w.cw_card = not info["item"]
        w.cw_tab = tab
        w.cw_session_id = info["session_id"]
        w.cw_pressed = w.cw_dragging = w.cw_double = False
        w.cw_drop = (None, True)

        # right-click: the menu; a second click of a double-click: rename (the first switched)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_tab_pressed, w)
        w.add_controller(click)
        # left button: a click switches (on a Claude's box: to its terminal), a drag (past a
        # few pixels) reorders
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._on_drag_begin, w)
        drag.connect("drag-update", self._on_drag_update, w)
        drag.connect("drag-end", self._on_drag_end, w)
        w.add_controller(drag)
        return w

    def _make_card(self, tab: dict, info: dict, attention: bool) -> Gtk.Widget:
        """A workspace: its header (name, close) if it has one, a box per Claude (without a
        header the first has the close button), an icon per other window; its folder and shortcut
        show on hover."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        w.add_css_class("cw-tab")
        set_classes(w, tab_classes(tab, info, attention))
        w.set_tooltip_text(info["tip"] or None)
        w.cw_head, w.cw_name = info["head"], None
        if info["head"]:
            w.cw_name = self._card_head(w, info)
        note = label(info["sub"], "cw-card-note")
        note.set_visible(bool(info["sub"]))
        w.append(note)
        w.cw_boxes, w.cw_apps = [], []
        well = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        well.add_css_class("cw-well")
        for i, box in enumerate(info["boxes"]):
            if i:
                sep = Gtk.Box()
                sep.add_css_class("cw-sep")
                well.append(sep)
            bw = self._make_box(w, box, closes=i == 0 and not info["head"])
            well.append(bw)
            w.cw_boxes.append(bw)
        well.set_visible(bool(info["boxes"]))
        w.append(well)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.add_css_class("cw-apps")
        for app in info["apps"]:
            aw = self._make_app(app)
            row.append(aw)
            w.cw_apps.append(aw)
        row.set_visible(bool(info["apps"]))
        w.append(row)
        w.cw_note = note
        w.cw_box_keys, w.cw_app_keys = [b["key"] for b in info["boxes"]], [a["key"] for a in info["apps"]]
        return w

    def _card_head(self, w, info: dict) -> Gtk.Label:
        """A card's header: its name (two lines at most) and close button."""
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.add_css_class("cw-head")
        name = label(info["title"], "cw-card-name")
        name.set_hexpand(True)
        name.set_wrap(True)
        name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        name.set_lines(2)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_valign(Gtk.Align.CENTER)
        head.append(name)
        close = self._close_button(w)
        close.set_valign(Gtk.Align.START)
        head.append(close)
        w.append(head)
        return name

    @staticmethod
    def _app_tip(app: dict) -> str:
        name = app_entry(app["app"])[1] or app["name"]
        notes = [f"{app['tabs']} tabs" if app["tabs"] else "", f"{app['unread']} unread" if app["unread"] else "",
                 "missing: it comes back with a restore" if app["missing"] else ""]
        notes = ", ".join(filter(None, notes))
        return f"{name}: {app['title']}" + (f" ({notes})" if notes else "")

    def _make_app(self, app: dict) -> Gtk.Widget:
        """An app's window: its icon, then its tab count (a browser's) and what is unread in it;
        its title on hover."""
        aw = Gtk.Box()
        aw.add_css_class("cw-app")
        icon = app_icon(app["app"], 14)
        icon.set_valign(Gtk.Align.CENTER)
        aw.append(icon)
        aw.cw_tabs = label("", "cw-app-tabs")
        aw.cw_tabs.set_valign(Gtk.Align.CENTER)
        aw.append(aw.cw_tabs)
        aw.cw_unread = label("", "cw-app-unread")
        aw.cw_unread.set_valign(Gtk.Align.CENTER)
        aw.append(aw.cw_unread)
        aw.cw_app = None
        self._update_app(aw, app)
        return aw

    def _update_app(self, aw, app: dict):
        set_classes(aw, [("missing", app["missing"])])
        if aw.cw_app is None or aw.cw_app["title"] != app["title"]:
            aw.set_tooltip_text(self._app_tip(app))
        for lbl, n in ((aw.cw_tabs, app["tabs"]), (aw.cw_unread, app["unread"])):
            text = (f"{n}" if n < 1000 else "999+") if n else ""
            if lbl.get_text() != text:
                lbl.set_text(text)
            lbl.set_visible(bool(n))
        aw.cw_app = app

    def _make_box(self, w, box: dict, closes: bool = False) -> Gtk.Widget:
        """One Claude: its stripe, glyph and title, the model with a block per subagent, fork."""
        bw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bw.add_css_class("cw-box")
        set_classes(bw, box_classes(box))
        bw.append(self._pill())
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        name = label(f"{box['glyph']} {box['title']}", "cw-box-name")
        name.set_wrap(True)
        name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        name.set_lines(2)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
        sub = label(box["sub"], "cw-box-sub")  # (the blocks follow the model, not the box's edge)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        dots.set_valign(Gtk.Align.CENTER)
        self._fill_dots(dots, box["agents"])
        meta.append(sub)
        meta.append(dots)
        col.append(name)
        col.append(meta)
        bw.append(col)
        if box["session_id"]:
            fork = Gtk.Button()
            fork.set_child(fork_icon(14))
            fork.set_has_frame(False)
            fork.set_focus_on_click(False)
            fork.set_valign(Gtk.Align.CENTER)
            fork.set_tooltip_text("Fork this session into a new workspace")
            fork.add_css_class("cw-icon")
            fork.connect("clicked", lambda *_: self.fork(w.cw_tab, box["session_id"]))
            bw.append(fork)
        if closes:  # (the card has no header to hold it)
            close = self._close_button(w)
            close.set_valign(Gtk.Align.START)
            bw.append(close)
        bw.cw_name, bw.cw_sub, bw.cw_dots, bw.cw_box = name, sub, dots, box
        return bw

    def _update_box(self, bw, box: dict):
        set_classes(bw, box_classes(box))
        text = f"{box['glyph']} {box['title']}"
        if bw.cw_name.get_text() != text:
            bw.cw_name.set_text(text)
        if bw.cw_sub.get_text() != box["sub"]:
            bw.cw_sub.set_text(box["sub"])
        if len(children(bw.cw_dots)) != min(box["agents"], MAX_DOTS):
            self._fill_dots(bw.cw_dots, box["agents"])
        bw.cw_box = box

    @staticmethod
    def _fill_dots(dots: Gtk.Box, n: int):
        """A block per subagent at work (a box has room for MAX_DOTS)."""
        clear(dots)
        for _ in range(min(n, MAX_DOTS)):
            dot = Gtk.Box()
            dot.add_css_class("cw-dot")
            dot.set_valign(Gtk.Align.CENTER)
            dots.append(dot)
        dots.set_visible(n > 0)

    @staticmethod
    def _box_at(w, y: float) -> dict | None:
        """The Claude whose box is at height `y` of card `w`, if any."""
        for bw in getattr(w, "cw_boxes", []):
            b = bounds(bw, w)
            if b is not None and b[0] <= y < b[0] + b[1]:
                return bw.cw_box
        return None

    @staticmethod
    def _app_at(w, x: float, y: float) -> dict | None:
        """The app whose icon is at (`x`, `y`) of card `w`, if any."""
        for aw in getattr(w, "cw_apps", []):
            ok, rect = aw.compute_bounds(w)
            if ok and rect.get_x() <= x < rect.get_x() + rect.get_width() \
                    and rect.get_y() <= y < rect.get_y() + rect.get_height():
                return aw.cw_app
        return None

    def _make_plain_tab(self, tab: dict, info: dict, attention: bool) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        w.add_css_class("cw-tab")
        set_classes(w, tab_classes(tab, info, attention))
        w.append(self._pill())

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        lbl = label(info["title"], "cw-name")
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_lines(2)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_valign(Gtk.Align.START)
        lbl.set_yalign(0.0)
        sub = label(info["sub"], "cw-model")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        path = label(info["path"], "cw-path")
        path.set_ellipsize(Pango.EllipsizeMode.START)
        path.set_visible(bool(info["path"]))
        col.append(lbl)
        col.append(sub)
        col.append(path)
        w.append(col)

        # the icons take the column's spare height; vexpand set here keeps that from spreading up
        # (in GTK 4 a child's expand makes its parents expand: every card would stretch)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, vexpand=False)
        icons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, vexpand=True)
        icons.set_halign(Gtk.Align.END)
        icons.set_valign(Gtk.Align.CENTER)
        if info["session_id"]:
            fork = Gtk.Button()
            fork.set_child(fork_icon())
            fork.set_has_frame(False)
            fork.set_focus_on_click(False)
            fork.set_tooltip_text("Fork this session into a new workspace")
            fork.add_css_class("cw-icon")
            fork.connect("clicked", lambda *_: self.fork(w.cw_tab, info["session_id"]))
            icons.append(fork)
        right.append(icons)
        close = self._close_button(w)
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.END)
        right.append(close)  # bottom-right, level with the path line
        w.append(right)

        if info["closed"]:
            w.set_tooltip_text("Closed: click to reopen it")
        w.cw_title, w.cw_sub, w.cw_path, w.cw_box_keys, w.cw_app_keys, w.cw_head = lbl, sub, path, [], [], False
        return w

    # ---- click, drag to reorder ------------------------------------------------------------
    def _on_tab_pressed(self, gesture, n_press, x, y, w):
        button = gesture.get_current_button()
        if button == 3:
            self._popup(w, x, y, self.tab_menu(w.cw_tab, self._box_at(w, y)))
        elif button == 1 and n_press == 2:
            w.cw_double = True
            self.rename_dialog(w.cw_tab)

    def _on_drag_begin(self, gesture, x, y, w):
        w.cw_pressed, w.cw_dragging, w.cw_drop = True, False, (None, True)
        w.cw_press = (x, y)

    def _on_drag_update(self, gesture, dx, dy, w):
        if not w.cw_pressed or not self.can_reorder() or w.cw_tab.get("space") is None:
            return
        if not w.cw_dragging:
            if abs(dy) < self.DRAG_THRESHOLD:
                return
            w.cw_dragging = True
            w.add_css_class("dragging")
        point = translate(w, self.list_box, w.cw_press[0] + dx, w.cw_press[1] + dy)
        target, before = self._drop_target(point[1]) if point else (None, True)
        self._clear_drop_marks()
        if target is not None and target is not w:
            target.add_css_class("drop-before" if before else "drop-after")
        w.cw_drop = (target, before)

    def _on_drag_end(self, gesture, dx, dy, w):
        pressed, dragging, double = w.cw_pressed, w.cw_dragging, w.cw_double
        w.cw_pressed = w.cw_dragging = w.cw_double = False
        if dragging:
            w.remove_css_class("dragging")
            self._clear_drop_marks()
            target, before = w.cw_drop
            w.cw_drop = (None, True)
            if target is not None and target is not w:
                self.finish_drag(w.cw_tab, target.cw_tab, before)
        elif pressed and not double:
            box, app = self._box_at(w, w.cw_press[1]), self._app_at(w, *w.cw_press)
            if box is not None:
                self.on_box_clicked(w.cw_tab, box)
            elif app is not None:
                self.on_app_clicked(w.cw_tab, app)
            else:
                self.on_tab_clicked(w.cw_tab)

    def _drop_target(self, ly: float):
        tabs = [c for c in children(self.list_box) if c.cw_tab.get("space") is not None]
        placed = [(c, bounds(c, self.list_box)) for c in tabs]
        placed = [(c, b) for c, b in placed if b is not None]
        for child, (top, height) in placed:
            if top <= ly < top + height:
                return child, ly < top + height / 2
        if not placed:
            return None, True
        return (placed[0][0], True) if ly < placed[0][1][0] else (placed[-1][0], False)

    def _clear_drop_marks(self):
        for child in children(self.list_box):
            child.remove_css_class("drop-before")
            child.remove_css_class("drop-after")

    # ---- actions ----------------------------------------------------------
    def on_new_clicked(self, *_):
        entries = self.new_menu()
        if entries is not None:
            Menu(self.new_btn, entries).popup_above()

    def web_app_dialog(self, tab: dict):
        EntryDialog("Open web app here", "Open", "", "https://…",
                    "Opens as a browser app window (no tabs) on this workspace, in your default browser.",
                    lambda url: url and self.send("webapp", url=url, **self._where(tab))).present()

    def rename_dialog(self, tab: dict):
        EntryDialog("Name workspace", "OK", tab.get("name") or "", "empty = no name",
                    "A name heads the card, above its sessions. Without one, its first session's title says what it is.",
                    lambda name: self.send("rename", name=name, **self._where(tab))).present()

    def close_tab(self, tab: dict):
        """Close the whole workspace: every window on it gets a normal close request from the
        compositor (apps may still ask about unsaved work), and it is no longer kept."""
        question = close_question(tab)
        if question is None:
            self.send("close", **self._where(tab))
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message(question[0])
        dialog.set_detail(question[1])
        dialog.set_buttons(["Cancel", "Close"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)

        def answered(d, result):
            try:
                choice = d.choose_finish(result)
            except GLib.Error:  # dismissed
                return
            if choice == 1:
                self.send("close", **self._where(tab))
        dialog.choose(None, None, answered)

    def _error(self, msg: str):
        dialog = Gtk.AlertDialog()
        dialog.set_message(msg)
        dialog.show(None)


def main(fall_back):
    """Run the sidebar; `fall_back(why)` restarts as the GTK 3 one when there is no layer shell."""
    hosted = gnome.hosted()
    if not hosted and not LayerShell.is_supported():
        fall_back("the compositor has no layer shell for GTK 4")
    loop = GLib.MainLoop()
    win = Sidebar(layer=not hosted)
    win.present()
    if hosted:
        win._place_on_gnome()
    renderer = win.get_renderer()  # `sidelit log` says which sidebar runs, and how it draws
    print(f"sidebar: GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
          f"{type(renderer).__name__ if renderer else 'no renderer yet'}", file=sys.stderr, flush=True)

    def _quit(*_):
        loop.quit()
        signal.alarm(3)  # a shutdown that wedges must not make us unkillable
        return True

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _quit)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _quit)
    loop.run()
