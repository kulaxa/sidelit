"""What the sidebar does, apart from how it draws: shared by the GTK 4 sidebar (sidebar4.py) and
the GTK 3 one it falls back to (sidebar.py).

Nothing here imports GTK, so either toolkit can load it (a process can only hold one GTK).
`SidebarLogic` is mixed into both windows; it talks to the daemon, polls usage, releases and
other sources, and builds the menus as plain (label, action) lists that each toolkit turns into
its own kind of menu. The window supplies the drawing: `render()`, `_draw_sources()`,
`_render_usage(rows)`, `_offer_update(v)`, `_hide_update()`, `_error(msg)` and the dialogs.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from gi.repository import GLib

from sidelit import agents, config, gnome, sources, version as local_version
from sidelit.procs import load_json, login_shell
from sidelit.tabstate import Attention, note

# The plans' usage limits, a file per agent that has them (agents.py: usage_file): Claude
# Code's as it measures them after each turn, written by Sidelit's plugin from inside it
# (plugin/hooks/usage.js); one the daemon asks the agent for (Codex's). The last ones seen are
# kept for after a login.
USAGE_FILE = config.RUNTIME_DIR / "usage.json"
USAGE_CACHE = config.CONFIG_DIR / "usage-cache.json"
USAGE_EVERY_S = 15
USAGE_NAMES = {"five_hour": "Session", "seven_day": "Week", "spend_limit": "Spend limit"}
# Where releases are published (GitHub releases, see .github/workflows/ci.yml). The version file says what the latest
# one is, so an update is only ever offered when there is one.
RELEASES_URL = "https://github.com/kulaxa/sidelit/releases/latest/download"
LATEST_VERSION_URL = RELEASES_URL + "/sidelit_latest_version.txt"
LATEST_DEB = "sidelit_latest_all.deb"
LATEST_ARCH = "sidelit-latest-any.pkg.tar.zst"

# Claude's state -> tab style class (the note on the model line: tabstate.py)
# "checking": the turn is over and a model is deciding whether Claude needs you; "external": it
# does not, Claude only waits on something (CI, a deploy) and the tab says what (turncheck.py)
STATE_CLASS = {"busy": "working", "checking": "working", "shell": "shell", "external": "shell",
               "waiting": "waiting", "done": "idle"}
# an other-source item's state (sources.py) -> the same stripe colours as a session tab
SOURCE_STATE_CLASS = {"working": "working", "waiting": "waiting", "idle": "idle", "done": "idle", "failed": "failed"}
SOURCE_STATE_NOTE = {"working": "working", "waiting": "waiting for you", "done": "done", "failed": "failed"}
STATE_CLASSES = sorted(set(STATE_CLASS.values()) | set(SOURCE_STATE_CLASS.values()))

FORK_ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
 <g fill="none" stroke="#9a9a9a" stroke-width="1.5">
  <circle cx="4" cy="3.5" r="1.75"/>
  <circle cx="12" cy="3.5" r="1.75"/>
  <circle cx="8" cy="12.5" r="1.75"/>
  <path d="M4 5.25 V6.5 a2 2 0 0 0 2 2 h4 a2 2 0 0 0 2 -2 V5.25 M8 8.5 v2.25"/>
 </g>
</svg>"""


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ---- usage limits (what Claude Code measured, through Sidelit's plugin) ------------------------
def usage_rows(data, now: datetime | None = None) -> list[tuple[str, float, str | None]]:
    """(label, percent used, when it resets) for each window in what the plugin wrote: the
    session's five hours, the week, a gateway's spend limit. A window whose reset has passed is
    left out (Claude Code drops it too); nothing known, no rows."""
    now = now or datetime.now(timezone.utc)
    rows = []
    limits = data.get("limits") if isinstance(data, dict) else None
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict) or not isinstance(limit.get("percentUsed"), (int, float)):
            continue
        kind, resets = str(limit.get("kind") or ""), limit.get("resetsAt")
        moment = _parse_stamp(resets)
        if moment is not None and moment <= now:
            continue
        rows.append((USAGE_NAMES.get(kind) or kind.replace("_", " ").capitalize() or "Limit",
                     float(limit["percentUsed"]), resets if moment is not None else None))
    order = list(USAGE_NAMES.values())
    return sorted(rows, key=lambda r: order.index(r[0]) if r[0] in order else len(order))


def usage_sources() -> list[tuple[str, Path, Path]]:
    """(agent's name to show, the file its limits are in, the copy kept of the last ones) for
    every agent that has usage limits."""
    out = []
    for agent in agents.every():
        if agent.usage_file is None:
            continue
        if agent.name == "claude":
            out.append((agent.label, USAGE_FILE, USAGE_CACHE))
        else:
            out.append((agent.label, agent.usage_file, config.CONFIG_DIR / f"usage-cache-{agent.name}.json"))
    return out


def usage_level(percent: float) -> str | None:
    """A limit running out: its bar and number turn orange at 70%, red at 90%."""
    return "high" if percent >= 90 else "warn" if percent >= 70 else None


def _parse_stamp(stamp):
    if not stamp or not isinstance(stamp, str):
        return None
    try:  # (a "Z" for UTC is new to Python 3.11)
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def countdown(stamp) -> str | None:
    moment = _parse_stamp(stamp)
    if not moment:
        return None
    seconds = int((moment - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "any moment"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, seconds)
    return "%ds" % seconds


def version_tuple(text: str | None):
    """'0.2.0' -> (0, 2, 0), or None for anything that is not a version."""
    if not text or not re.fullmatch(r"\d+(\.\d+)*", text.strip()):
        return None
    return tuple(int(part) for part in text.strip().split("."))


def checkout_root() -> str | None:
    """The git checkout this package runs from, if it is one (install.sh symlinks a clone)."""
    root = Path(__file__).resolve().parent.parent
    return str(root) if (root / ".git").exists() and (root / "bin" / "sidelit").exists() else None


def package_tool() -> str:
    """How this machine installs a package: "pacman" on Arch, else "apt"."""
    return "pacman" if shutil.which("pacman") and not shutil.which("apt") else "apt"


def update_command() -> str:
    """Command that updates this install, in a terminal of its own so git, apt or pacman can talk
    to you (they ask for the sudo password there). A checkout is pulled, a package install gets the
    latest .deb (or Arch package); both restart the services afterwards."""
    shell = login_shell()
    root = checkout_root()
    if root:
        inner = (f'git -C {shlex.quote(root)} pull --ff-only && {shlex.quote(root + "/bin/sidelit")} restart'
                 f' && echo && echo "Updated to $(cat {shlex.quote(root + "/VERSION")}); restarted."')
    else:
        pkg, install = ((LATEST_ARCH, "sudo pacman -U") if package_tool() == "pacman"
                        else (LATEST_DEB, "sudo apt install"))
        inner = (f'cd "$(mktemp -d)" && curl -fLO {RELEASES_URL}/{pkg} && {install} ./{pkg}'
                 f' && sidelit restart && echo && echo "Updated; restarted."')
    return f"{shell} -i -c '{inner}; exec {shell} -i'"


def ago(t: float | None) -> str:
    if not t:
        return ""
    s = max(0, datetime.now().timestamp() - float(t))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    days = int(s // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def home_path(path: str | None) -> str:
    home = str(Path.home())
    return ("~" + path[len(home):] if path and path.startswith(home) else path) or ""


def day_label(t: float, today=None) -> str:
    """'Today', 'Yesterday', the weekday within a week, else 'Mon 22 Sep'."""
    day = datetime.fromtimestamp(float(t)).date()
    days = ((today or datetime.now().date()) - day).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return day.strftime("%A") if days < 7 else day.strftime("%a %-d %b")


def history_lines(h: dict) -> tuple[str, str, str, str]:
    """A closed workspace in the history list: (title, the line under it, the search text, the
    tooltip). Under the title: when, which folder, and what else was open ("Claude 'title'"
    only says the title again)."""
    title = h.get("title") or "Workspace"
    what = ", ".join(h.get("what") or [])
    sessions = tuple(f"{a.short} " for a in agents.every())  # ("Claude 'Fix tests'": the title again)
    others = [w for w in h.get("what") or [] if w != title and not (w.startswith(sessions) and title in w)]
    folder = os.path.basename((h.get("cwd") or "").rstrip("/")) if h.get("cwd") != str(Path.home()) else "~"
    closed = h.get("closed") or 0
    sub = " · ".join(filter(None, (datetime.fromtimestamp(closed).strftime("%H:%M") if closed else "",
                                   folder, ", ".join(others))))
    needle = " ".join([title, what, h.get("cwd") or ""]).lower()
    tooltip = "\n".join([home_path(h.get("cwd")), ago(closed)] + (h.get("what") or [])).strip()
    return title, sub, needle, tooltip


def matches(needle: str, query: str) -> bool:
    """Every word of the search is somewhere in the entry."""
    return all(w in needle for w in query.lower().split())


def item_sub(item: dict) -> str:
    """The line under an other source's item: its state and what it says."""
    return " · ".join(filter(None, (SOURCE_STATE_NOTE.get(item["state"]), item.get("detail")))) or " "


def bound_items(tabs: list[dict]) -> set[tuple]:
    """(source key, item id) of every item that has a workspace of its own."""
    return {(t["item"]["source"], t["item"]["id"]) for t in tabs if t.get("item")}


def group_items(src: dict, bound: set) -> list[dict]:
    """What a source's group lists: its items, but not those on a workspace of their own (their
    tab shows them)."""
    return [it for it in src["items"] if (src["key"], it["id"]) not in bound]


def box_info(c: dict, last_title: str = "") -> dict:
    """What a session's box shows: its agent's mark (for Claude Code the glyph it puts in its
    terminal's title, working or not: agents.py), its title, the model and a note, one dot per
    subagent at work, and whether it calls you (the daemon decides: tabstate.Attention)."""
    state = c.get("state") or "done"
    cls = STATE_CLASS.get(state, "idle")
    agent = agents.get(c.get("agent"))
    return {"key": c.get("key") or c.get("session") or "", "session_id": c.get("session"), "agent": agent.name,
            "title": c.get("title") or last_title or "Session", "glyph": agent.mark(cls == "working"),
            "sub": " · ".join(filter(None, (c.get("model"), note(c)))) or " ", "state": state, "cls": cls,
            "agents": int(c.get("agents") or 0), "calls": bool(c.get("calls")), "cwd": c.get("cwd") or ""}


def folder_name(path: str | None) -> str:
    """A directory as a card's header names it: its last part, ~ for home."""
    if not path:
        return ""
    return "~" if path.rstrip("/") == str(Path.home()) else os.path.basename(path.rstrip("/")) or path


def tab_info(tab: dict, item: dict | None = None) -> dict:
    """Everything a tab shows, from what the daemon sent for it; a workspace opened for an other
    source's item is drawn from that `item` while its source still lists it. A workspace with
    Claudes is a card holding a box per Claude (`boxes`), under a header naming the workspace
    (`head`: its name, else its folder; `head_tip` the whole path)."""
    claudes = tab.get("claudes") or []
    c = claudes[0] if claudes else None
    closed = tab.get("space") is None
    boxes, head, head_tip = [], "", ""
    if item:
        title, state, sub, cwd = item["title"], item["state"], item_sub(item), None
        cls = SOURCE_STATE_CLASS.get(state, "idle")
    elif c:
        boxes = [box_info(b, tab.get("last_title") or "") for b in claudes]
        title, state, sub, cls = boxes[0]["title"], boxes[0]["state"], boxes[0]["sub"], None
        cwd = tab.get("cwd") or c.get("cwd")
        head = tab.get("name") or folder_name(cwd) or (f"Workspace {tab['space']}" if not closed else "Workspace")
        head_tip = home_path(cwd)
    else:
        title = tab.get("name") or (tab.get("last_title") if closed else "") \
            or (f"Workspace {tab['space']}" if not closed else "Workspace")
        state = "other"
        if closed:
            n = len(tab.get("missing") or [])
            sub = f"{n} window{'s' if n != 1 else ''} to reopen" if n else "empty"
        else:
            names = []
            for w in tab.get("windows") or []:
                app = (w.get("app") or "?").rsplit(".", 1)[-1].replace("google-", "").lower()
                if app not in names:
                    names.append(app)
            sub = " · ".join(names[:4]) if names else "empty"
        cwd = tab.get("cwd") if tab.get("ws") else None
        cls = None
    if closed:  # the title stays the title; the line under it says so
        sub = " · ".join(filter(None, ("closed", sub.strip() if sub.strip() != "empty" else None)))
    return {"title": title, "sub": sub, "state": state, "cls": cls, "has_claude": bool(c or item),
            "item": bool(item), "session_id": (c or {}).get("session") if len(claudes) == 1 else None,
            "path": home_path(cwd), "closed": closed, "boxes": boxes, "head": head, "head_tip": head_tip}


def tab_classes(tab: dict, info: dict, attention: bool) -> list[tuple[str, bool]]:
    """Every style class a tab can carry, and whether it carries it now. A card of boxes is
    "boxes", not "claude": each box carries its own state."""
    boxes = bool(info["boxes"])
    return [("active", bool(tab.get("active"))), ("attention", attention),
            ("claude", info["has_claude"] and not boxes), ("boxes", boxes), ("plain", not info["has_claude"]),
            ("missing", info["closed"]), *((c, c == info["cls"]) for c in STATE_CLASSES)]


def box_classes(box: dict) -> list[tuple[str, bool]]:
    """Every style class a Claude's box can carry, and whether it carries it now."""
    return [("attention", box["calls"]), *((c, c == box["cls"]) for c in STATE_CLASSES)]


def tab_snapshot(rows) -> list[tuple]:
    """What a redraw depends on: an unchanged snapshot needs none."""
    return [(t["key"], t.get("space"), t.get("active"), i["title"], i["sub"], i["session_id"], att,
             i["has_claude"], i["path"], i["state"], i["cls"], t.get("shortcut"), i["head"], i["head_tip"],
             tuple((b["key"], b["title"], b["sub"], b["cls"], b["calls"], b["agents"]) for b in i["boxes"]))
            for t, i, att in rows]


def close_question(tab: dict) -> tuple[str, str] | None:
    """Closing a workspace with windows asks first: (question, what it closes); None closes
    straight away."""
    windows = tab.get("windows") or []
    if not windows:
        return None
    claudes = [c for c in tab.get("claudes") or [] if c.get("title")]
    name = tab.get("name") or (claudes[0]["title"] if len(claudes) == 1 else folder_name(tab.get("cwd")))
    apps = sorted({(w.get("app") or "?").rsplit(".", 1)[-1].replace("google-", "") for w in windows})
    labels = {agents.get(c.get("agent")).label for c in tab.get("claudes") or []} or {agents.get(None).label}
    kind = f"{labels.pop()} session" if len(labels) == 1 else "session"
    kept = f"Its {kind}s stay" if len(tab.get("claudes") or []) > 1 else f"A {kind} stays"
    return (f"Close workspace{' – ' + name if name else ''}?",
            f"Closes {len(windows)} window{'s' if len(windows) != 1 else ''} ({', '.join(apps)}). {kept}"
            f" in Recent sessions (right-click the header).")


# A menu, for either toolkit: entries are (label, callback), (label, None) for a line that only
# informs, SEPARATOR, or (label, [entries]) for a submenu.
SEPARATOR = ("", None, "separator")


class SidebarLogic:
    """The sidebar's behaviour, shared by both toolkits (see the module docstring)."""

    DRAG_THRESHOLD = 8

    def _init_logic(self):
        self.cfg = config.load()
        self.state: dict = {}
        self._snapshot = None
        self._attention = Attention()
        self._countdowns: list = []  # (label, reset stamp)
        self._usage_at: dict = {}    # agent -> when the figures its bars show were measured
        self._usage_rows = None
        self._footer_until = 0.0
        self._restart_pending = False
        self._restore_hide_at = 0.0
        self._sources = sources.Poller()
        self._sources_shown = None
        self._sources_busy = False
        self._items: dict[tuple, dict] = {}  # (source key, item id) -> the item, as last fetched
        self._bound: set[tuple] = set()      # the items with a workspace of their own, as the group shows
        self._asked: set[tuple] = set()      # items whose workspace was asked for on their own

    def _start_timers(self):
        GLib.timeout_add_seconds(10, self._tick_countdowns)
        GLib.timeout_add_seconds(3, self._refresh_usage_once)
        GLib.timeout_add_seconds(USAGE_EVERY_S, self._refresh_usage)
        GLib.timeout_add_seconds(15, lambda: (self._check_update(), False)[1])
        GLib.timeout_add_seconds(3600, lambda: (self._check_update(), True)[1])
        GLib.timeout_add(1000, self._tick_footer)
        GLib.timeout_add_seconds(3, self._refresh_sources)

    # ---- daemon ------------------------------------------------------------------------
    def _on_connection(self, up: bool):
        if up:
            self._footer("", 0)
        else:
            self._footer("the daemon is not running — reconnecting… (sidelit status)", 3600)

    def _on_state(self, state: dict):
        self.state = state
        self.render()

    def send(self, method: str, _tries: int = 16, **params):
        """A command to the daemon; errors come back as a dialog. While the daemon is being
        restarted (systemd brings it back within seconds) the command waits for it."""
        def done(result, error):
            if error == "the daemon is not running" and _tries > 1:
                GLib.timeout_add(500, lambda: self.send(method, _tries=_tries - 1, **params) and False)
            elif error:
                self._error(f"{method}: {error}")
        self.daemon.request(method, done, **params)

    # ---- screens --------------------------------------------------------------------------
    def _screens_changed(self, reason: str):
        """Start over on the screen the config picks. Re-mapping a live layer surface onto a
        changing output hung GTK inside a compositor round-trip; a fresh process maps its
        sidebar through the ordinary startup path. Nothing is lost: the daemon has it all.

        On GNOME the extension places the window: it is only told the screen again."""
        if gnome.hosted() and reason != "menu":
            if not self._restart_pending:
                self._restart_pending = True
                GLib.timeout_add_seconds(1, self._replace_on_gnome)
            return
        if self._restart_pending:
            return
        self._restart_pending = True
        print(f"sidebar: screens changed ({reason}); restarting in 3 s", file=sys.stderr, flush=True)
        GLib.timeout_add_seconds(3, self._restart_self)

    def _replace_on_gnome(self):
        self._restart_pending = False
        self._place_on_gnome()
        return False

    def _restart_self(self):
        sys.stderr.flush()
        if gnome.hosted():  # GNOME Shell started this process and starts the next one
            os.kill(os.getpid(), signal.SIGTERM)
            return False
        os.execv(sys.executable, [sys.executable, "-m", "sidelit", "sidebar"])

    # ---- footer -------------------------------------------------------------------------
    def _footer(self, text: str, seconds: float):
        self.footer.set_text(text)
        self.footer.set_visible(bool(text))
        self._footer_until = datetime.now().timestamp() + seconds if text else 0.0

    def _tick_footer(self):
        now = datetime.now().timestamp()
        if self.footer.get_visible() and now > self._footer_until:
            self.footer.set_visible(False)
        if self.restore_box.get_visible() and now > self._restore_hide_at:
            self.restore_box.set_visible(False)
        return True

    def _restore_texts(self, restore: dict) -> dict | None:
        """The restore banner: up from the moment a restore starts until it is done, then the
        result for a while. None: no banner."""
        total, back = restore.get("total") or 0, restore.get("back") or 0
        what = "Reopening windows that closed unexpectedly" if restore.get("reason") == "relaunch" \
            else "Restoring windows"
        if restore.get("running"):
            return {"running": True, "hide_at": float("inf"),
                    "title": f"{what} — {back} of {total} back" if restore.get("planned") else f"{what}…",
                    "sub": (restore.get("log") or ["getting ready"])[-1]}
        finished = restore.get("finished") or 0
        if finished and datetime.now().timestamp() - finished < 20:
            return {"running": False, "hide_at": finished + 20,
                    "title": f"{'Reopened' if restore.get('reason') == 'relaunch' else 'Restore finished'}"
                             f" — {back} of {total} windows back",
                    "sub": f"{total - back} could not be reopened (shown as missing on their tabs)"
                           if total > back else "everything is back"}
        return None

    # ---- updates ----------------------------------------------------------
    def _check_update(self):
        def worker():
            try:
                with urllib.request.urlopen(LATEST_VERSION_URL, timeout=15) as response:
                    latest = response.read(64).decode("utf-8", "replace").strip()
            except Exception as e:
                print("update check failed:", e, file=sys.stderr)
                return
            ours, theirs = version_tuple(local_version()), version_tuple(latest)
            if ours and theirs and theirs > ours:
                GLib.idle_add(self._offer_update, latest)
            else:  # up to date (again): drop an offer made earlier
                GLib.idle_add(lambda: (self._hide_update(), False)[1])
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def update_tooltip() -> str:
        how = "pulls the checkout and restarts" if checkout_root() else \
            (f"downloads the package and runs sudo {package_tool()} (the password prompt happens there), "
             f"then restarts")
        return f"A newer Sidelit is out. Opens a terminal that {how}."

    def on_update_clicked(self, *_):
        active = next((t for t in self.state.get("tabs") or [] if t.get("active")), None)
        if active is not None:
            self.send("terminal", space=active["space"], cwd=str(Path.home()), command=update_command())

    # ---- usage ------------------------------------------------------------
    def _refresh_usage_once(self):
        self._refresh_usage()
        return False

    def _refresh_usage(self):
        """The bars, under each agent's name: from the figures last written for it (after the
        last turn of any of its sessions); until some are since a login, the last ones seen (its
        cache). Bars only for what is known."""
        groups = []
        for name, live, cache in usage_sources():
            data = load_json(live, None)  # (None too while it is half way through being written)
            if isinstance(data, dict) and data.get("limits"):
                if data.get("at") != self._usage_at.get(name):
                    self._usage_at[name] = data.get("at")
                    try:
                        save_json(cache, data)
                    except OSError:
                        pass
            else:
                data = load_json(cache, None)
            rows = usage_rows(data)
            if rows:
                groups.append((name, rows))
        if groups != self._usage_rows:
            self._usage_rows = groups
            self._render_usage(groups)
        return True

    def _tick_countdowns(self):
        for label, stamp in self._countdowns:
            left = countdown(stamp)
            if left:
                label.set_text("resets in %s" % left)
        return True

    # ---- other sources (sources.py) ----------------------------------------------------
    def _refresh_sources(self):
        """Every 3 s: fetch whatever source is due, off the main loop."""
        if self._sources_busy:
            return True
        self._sources_busy = True

        def worker():
            try:
                results = self._sources.poll()
            except Exception as e:  # noqa: BLE001 (a broken source must never take the sidebar down)
                print("sources failed:", e, file=sys.stderr)
                results = None
            GLib.idle_add(self._render_sources, results)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _render_sources(self, results):
        self._sources_busy = False
        if results is None or results == self._sources_shown:
            return False
        self._sources_shown = results
        self._items = {(src["key"], it["id"]): it for src in results for it in src["items"]}
        self.render()  # a tab opened for an item is drawn from it
        self._draw_sources()
        return False

    def _source_groups(self) -> list[tuple[dict, list[dict]]]:
        """The groups under the tabs: (source, the items it lists). A source whose items are tabs
        of their own lists nothing, and shows no group unless it has an error to say."""
        self._bound = bound_items(self.state.get("tabs") or [])
        groups = []
        for src in self._sources_shown or []:
            if src.get("workspaces") and not src["error"]:
                continue
            groups.append((src, [] if src.get("workspaces") else group_items(src, self._bound)))
        return groups

    def _open_wanted(self):
        """A "workspaces" source's items that are working or waiting get their workspace: made
        without showing it, so nothing takes your screen; its page opens when you go there."""
        if not self.state or not self.daemon.connected:
            return
        for source, item in sources.wanting_workspaces(self._sources_shown or [],
                                                       bound_items(self.state.get("tabs") or []), self._asked):
            key = (source, item["id"])
            self._asked.add(key)

            def done(result, error, key=key):
                if error:  # tried again with the next state
                    print(f"sources: no workspace for {key[0]} item {key[1]}: {error}", file=sys.stderr)
                    self._asked.discard(key)
            self.daemon.request("item_workspace", done, source=source, item=item["id"], title=item["title"],
                                url=item["url"], folder=item.get("folder"), auto=True)

    def _item_tab(self, source: str, item_id: str) -> dict | None:
        """The tab of the workspace opened for this item, if it has one."""
        return next((t for t in self.state.get("tabs") or []
                     if t.get("item") == {"source": source, "id": item_id}), None)

    def source_item_clicked(self, source: str, item: dict):
        """An item with a workspace of its own goes there (one just given it is still listed
        until the daemon says so); any other opens on this workspace."""
        tab = self._item_tab(source, item["id"])
        if tab is not None:
            self.on_tab_clicked(tab)
        elif item["url"]:
            self._open_url(item["url"])

    def source_item_menu(self, source: str, item: dict) -> list:
        return [("Open on its own workspace", lambda: self.send(
            "item_workspace", source=source, item=item["id"], title=item["title"], url=item["url"],
            folder=item.get("folder")))]

    @staticmethod
    def _open_url(url: str):
        """Open on the current workspace, the way `sidelit open` does."""
        if not url.startswith(("http://", "https://")):
            return
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", "")}
        env.pop("LD_PRELOAD", None)  # the GTK 4 sidebar's preload is its own business
        subprocess.Popen([sys.executable, "-m", "sidelit", "open", url], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    # ---- tabs ---------------------------------------------------------------
    def _tab_rows(self) -> list[tuple[dict, dict, bool]]:
        """(tab, what it shows, whether it wants you) for every tab the daemon sent, and the
        housekeeping a new state brings: source groups, wanted workspaces, the restore banner."""
        rows = []
        for tab in self.state.get("tabs") or []:
            bound = tab.get("item")
            info = tab_info(tab, self._items.get((bound["source"], bound["id"])) if bound else None)
            if info["item"]:  # an item starting to wait for you (tabstate.py)
                attention = self._attention.update(tab["key"], info["state"], bool(tab.get("active")), item=True)
            else:  # a Claude handing back to you: the daemon says which
                attention = any(b["calls"] for b in info["boxes"])
            rows.append((tab, info, attention))
        if bound_items(self.state.get("tabs") or []) != self._bound:
            self._draw_sources()  # an item got its own workspace, or lost it
        self._open_wanted()
        restore = self.state.get("restore") or {}
        self._show_restore(restore)
        if not restore.get("running") and self.state.get("disturbed"):
            self._footer(f"putting windows back ({self.state['disturbed']})…", 3)
        return rows

    @staticmethod
    def _where(tab: dict) -> dict:
        """How to name this tab's workspace to the daemon."""
        return {"ws": tab["ws"]} if tab.get("ws") else {"space": tab.get("space")}

    def finish_drag(self, tab: dict, target_tab: dict | None, before: bool):
        """A tab dropped before or after another: the daemon moves its workspace there."""
        if target_tab is None or target_tab is tab:
            return
        src, dst = tab.get("space"), target_tab.get("space")
        if src is None or dst is None or src == dst:
            return
        self.send("reorder", space=src, other=dst, before=before)

    def can_reorder(self) -> bool:
        return "move" in (self.state.get("caps") or [])  # a desktop that cannot reorder: no drop indicator

    def tab_menu(self, tab: dict, box: dict | None = None) -> list:
        """A card's menu; on one of its Claudes' boxes, its Fork is that Claude's."""
        info = tab_info(tab)
        where = self._where(tab)
        cwd = (box or {}).get("cwd") or tab.get("cwd") or next(
            (c.get("cwd") for c in tab.get("claudes") or [] if c.get("cwd")), "") or str(Path.home())
        session = (box or {}).get("session_id") or info["session_id"]
        items = []
        if not info["closed"]:
            items.append(("Open terminal here", lambda: self.send("terminal", **where)))
        # a new session of each agent installed (Claude Code's with none: its terminal says so)
        found = agents.installed() or [agents.get(None)]
        for agent in found:
            items.append((f"New {agent.label} session in this directory" if len(found) > 1 else
                          "New session in this directory",
                          lambda a=agent: self.send("new", cwd=cwd, after=tab.get("ws"), agent=a.name)))
        if session:
            items.append(("Fork session", lambda: self.fork(tab, session)))
        if not info["closed"]:
            for app in self.cfg.get("web_apps") or []:
                if isinstance(app, dict) and app.get("url"):
                    items.append((f"Open {app.get('name') or app['url']} here",
                                  lambda u=app["url"]: self.send("webapp", url=u, **where)))
            items.append(("Open web app here…", lambda: self.web_app_dialog(tab)))
        items.append(("Rename…", lambda: self.rename_dialog(tab)))
        missing = tab.get("missing") or []
        if missing and not info["closed"]:
            items.append((f"Forget {len(missing)} missing window{'s' if len(missing) != 1 else ''}",
                          lambda: [self.send("forget_slot", slot=m["slot"]) for m in missing]))
        if tab.get("ws"):
            items.append(("Forget (stop keeping it, leave its windows)", lambda: self.send("forget", ws=tab["ws"])))
        items.append(("Close workspace (all windows)", lambda: self.close_tab(tab)))
        if missing:
            items.append(SEPARATOR)
            items.extend((f"missing: {m['what']}", None) for m in missing[:8])
        return items

    def header_menu(self) -> list:
        items = [("Put windows back on their workspaces", lambda: self.send("reapply")),
                 ("Reopen missing windows (restore)", lambda: self.send("restore"))]
        recent = self.state.get("recent") or []
        if recent:
            sub = []
            for r in recent:
                name = (r.get("title") or r.get("session", "")[:8])[:48]
                if r.get("agent"):  # (another agent's than Claude Code: its mark says whose)
                    name = f"{agents.get(r['agent']).mark(False)} {name}"
                where = home_path(r.get("cwd"))
                sub.append((f"{name}  —  {where}" if where else name,
                            lambda s=r["session"]: self.send("resume", session=s)))
            items.append(("Recent sessions", sub))
        items.append(("Restart sidebar", lambda: self._screens_changed("menu")))
        return items

    def new_menu(self) -> list | None:
        """'+' asks which template to use; with only one configured it just opens it (None)."""
        tpls = config.templates(self.cfg)
        if len(tpls) < 2:  # (never none: config.templates falls back to the defaults)
            self.send("new", template=tpls[0].get("name"))
            return None
        return [(tpl.get("name") or "unnamed", lambda t=tpl: self.send("new", template=t.get("name"))) for tpl in tpls]

    def on_tab_clicked(self, tab: dict):
        self._attention.seen(tab["key"])
        bound = tab.get("item")
        item = self._items.get((bound["source"], bound["id"])) if bound else None
        if item is not None and not tab.get("windows") and not tab.get("missing"):
            # an item's workspace that opened by itself, empty so far: its page opens now
            self.send("item_workspace", source=bound["source"], item=bound["id"], title=item["title"],
                      url=item["url"], folder=item.get("folder"))
            return
        self.send("activate", **self._where(tab))

    def on_box_clicked(self, tab: dict, box: dict):
        """A Claude's box: its terminal (and pane, where the terminal lets us) gets the keyboard."""
        if tab.get("space") is None:
            self.on_tab_clicked(tab)
            return
        self.send("focus", key=box["key"])

    def fork(self, tab: dict, session_id: str):
        self.send("fork", session=session_id, **({"ws": tab["ws"]} if tab.get("ws") else {}))
