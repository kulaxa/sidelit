"""Other sources: sessions that run somewhere else, shown as their own group in the sidebar,
and this desktop's sessions reported back to them.

A source is any program that serves a small JSON feed (a web app's agent jobs, a CI board,
a remote machine's agents). Nothing here knows any particular source; each one is a file
of yours in ~/.config/sidelit/sources/, so what you plug in stays on your machine:

    {
      "name": "My jobs",                       # the group's heading
      "url": "http://127.0.0.1:8080/feed",     # GET, answers with the feed below
      "token_file": "~/.local/share/x/token",  # optional: sent as "Authorization: Bearer <contents>"
      "every": 10,                             # optional: seconds between fetches (min 3, default 10)
      "report": "http://127.0.0.1:8080/report", # optional: POST this desktop's sessions here
      "only": {"here": true},                  # optional: keep only items whose fields have these values
      "workspaces": true,                      # optional: each item gets a workspace of its own, no list
      "terminal": "my-tool open {id}"          # optional: an item's workspace opens this, not its page
                                               # ({url}: its link, e.g. "… || sidelit open --app {url}")
    }

The feed, and a report (the same items, without "url"; "folder", a directory under home for
the item's workspace, is optional and feed-only; any other field is only for "only"):

    {"items": [{"id": "…", "title": "…", "state": "working|waiting|idle|done|failed",
                "detail": "…", "url": "https://…", "updated": 1791016000, "folder": "src/app"}]}

Only http(s) URLs are fetched, posted to and opened. A source that fails shows its error under
its heading instead of items, and is tried again on its next turn. A report that fails is
logged and sent again `every` seconds later.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

from sidelit import config
from sidelit.tabstate import HANDS_BACK, Attention, note

SOURCES_DIR = config.CONFIG_DIR / "sources"
STATES = ("working", "waiting", "idle", "done", "failed")
MAX_ITEMS = 12
TIMEOUT = 4
REPORT_DEBOUNCE_S = 2.0  # a change is reported once it has stood this long (titles come in bursts)
ACTIVE = ("working", "waiting")  # an item in one of these gets its workspace ("workspaces": true)
PLAIN_ID = re.compile(r"[A-Za-z0-9._:-]{1,120}")  # the only ids that go into a "terminal" command
PLAIN_URL = re.compile(r"https?://[A-Za-z0-9.:/_~%-]{1,500}")  # and urls: nothing a shell reads


def _str(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def _http(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def load_sources(folder: Path = SOURCES_DIR) -> list[dict]:
    """The configured sources, in file-name order; broken files are skipped and reported."""
    out = []
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix == ".json")
    except OSError:
        return out
    for path in files:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            out.append({"key": path.stem, "name": path.stem, "url": None, "error": f"unreadable config: {e}"})
            continue
        if not isinstance(raw, dict) or not _http(raw.get("url")):
            out.append({"key": path.stem, "name": path.stem, "url": None, "error": "config needs an http(s) \"url\""})
            continue
        every = raw.get("every", 10)
        out.append({
            "key": path.stem,
            "name": _str(raw.get("name"), 40) or path.stem,
            "url": raw["url"],
            "token_file": _str(raw.get("token_file"), 500) or None,
            "every": max(3, int(every)) if isinstance(every, (int, float)) else 10,
            "report": raw["report"] if _http(raw.get("report")) else None,
            "only": _only(raw.get("only")),
            "workspaces": raw.get("workspaces") is True,
            # (it runs inside '…' in a login shell: a quote of its own would break out of that)
            "terminal": t if (t := _str(raw.get("terminal"), 300)) and "'" not in t else None,
            "error": None,
        })
    return out


def _only(v) -> dict:
    """"only": field -> the value an item must have (a string, number, true/false or null)."""
    if not isinstance(v, dict):
        return {}
    return {str(k): x for k, x in v.items() if x is None or isinstance(x, (str, bool, int, float))}


def parse_feed(data, only: dict | None = None) -> list[dict]:
    """Keep only well-formed items (and, with `only`, those whose fields have those values), with a
    known state and safe types; newest first."""
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or any(it.get(k) != v for k, v in (only or {}).items()):
            continue
        title = _str(it.get("title"), 200)
        if not title:
            continue
        state = it.get("state") if it.get("state") in STATES else "idle"
        updated = it.get("updated")
        out.append({
            "id": _str(it.get("id"), 120) or title,
            "title": title,
            "state": state,
            "detail": _str(it.get("detail"), 200),
            "url": it["url"] if _http(it.get("url")) else None,
            "updated": float(updated) if isinstance(updated, (int, float)) else 0.0,
            "folder": _str(it.get("folder"), 300) or None,
        })
    # what needs you first, then what is working, then the rest by time
    rank = {"waiting": 0, "working": 1}
    out.sort(key=lambda i: (rank.get(i["state"], 2), -i["updated"]))
    return out[:MAX_ITEMS]


def _headers(source: dict, **extra) -> dict:
    """The token file is read every time: a new token works without a restart."""
    headers = {"Accept": "application/json", "User-Agent": "sidelit", **extra}
    if source.get("token_file"):
        token = Path(source["token_file"]).expanduser().read_text().strip()
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch(source: dict) -> list[dict]:
    """One fetch of one source. Raises on any failure; the caller shows the message."""
    req = urllib.request.Request(source["url"], headers=_headers(source))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (http(s) only, checked in load)
        return parse_feed(json.loads(resp.read(512 * 1024)), source.get("only"))


def post(source: dict, items: list[dict]):
    """One report to one source. Raises on any failure; the caller logs it."""
    req = urllib.request.Request(source["report"], data=json.dumps({"items": items}).encode(), method="POST",
                                 headers=_headers(source, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (http(s) only, checked in load)
        resp.read(64 * 1024)


class Poller:
    """Fetches every due source; call poll() from a worker thread every few seconds."""

    def __init__(self, folder: Path = SOURCES_DIR, fetcher=fetch, clock=time.time):
        self.folder, self.fetcher, self.clock = folder, fetcher, clock
        self.results: dict[str, dict] = {}   # key -> {"name", "items", "error"}
        self._next: dict[str, float] = {}

    def poll(self) -> list[dict]:
        """Refresh what is due and return every source's latest result, in config order."""
        sources = load_sources(self.folder)
        now = self.clock()
        for s in sources:
            key = s["key"]
            if s["error"]:
                self.results[key] = {"key": key, "name": s["name"], "items": [], "error": s["error"],
                                     "workspaces": False}
                continue
            if now < self._next.get(key, 0):
                continue
            self._next[key] = now + s["every"]
            try:
                items, error = self.fetcher(s), None
            except Exception as e:  # noqa: BLE001 (any failure is shown, never raised)
                items, error = (self.results.get(key) or {}).get("items", []), _short_error(e)
            self.results[key] = {"key": key, "name": s["name"], "items": items, "error": error,
                                 "workspaces": s["workspaces"]}
        keys = [s["key"] for s in sources]
        self.results = {k: v for k, v in self.results.items() if k in keys}
        return [self.results[k] for k in keys if k in self.results]


def item_command(source: dict | None, item_id: str, url: str | None = None) -> str | None:
    """What an item's workspace opens in a terminal: its source's "terminal" command with {id}
    as the item's id and {url} as its link, or None (its page opens instead). The command is
    yours, from your file; the feed only supplies the id and the link, and only plain ones."""
    template = (source or {}).get("terminal")
    if not template or not PLAIN_ID.fullmatch(item_id or ""):
        return None
    if "{url}" in template and not PLAIN_URL.fullmatch(url or ""):
        return None
    return template.replace("{id}", item_id).replace("{url}", url or "")


def wanting_workspaces(results: list[dict], bound: set, asked: set) -> list[tuple[str, dict]]:
    """(source key, item) for each item that should get its workspace now: from a source with
    "workspaces": true, working or waiting, without one, and not asked for already."""
    return [(src["key"], it) for src in results if src.get("workspaces") for it in src["items"]
            if it["state"] in ACTIVE and (src["key"], it["id"]) not in bound | asked]


def _short_error(e: Exception) -> str:
    reason = getattr(e, "reason", None) or getattr(e, "code", None) or e
    return f"can't reach it ({reason})"[:120]


# ---- report out ------------------------------------------------------------------------------
def claude_items(tabs: list[dict], attention: Attention) -> list[dict]:
    """This desktop's session tabs (the daemon's view) as report items, without "updated": one per
    tab with a Claude, except on a workspace opened for a source's item. `waiting` while the tab
    is yellow, `working` while it is orange, `done` otherwise."""
    out = []
    for tab in tabs:
        c = tab.get("claude")
        if not c:
            continue
        state = c.get("state") or "done"
        yellow = attention.update(tab["key"], state, bool(tab.get("active")))
        if tab.get("item"):
            continue  # its source has it already
        out.append({"id": c.get("session") or tab["key"],
                    "title": c.get("title") or tab.get("last_title") or "Session",
                    "state": "waiting" if state == "waiting" or yellow else "working" if state in HANDS_BACK else "done",
                    "detail": note(c) or ""})
    return out


def _log(msg: str):
    print(msg, file=sys.stderr, flush=True)


class Reporter:
    """Report out: this desktop's session tabs, POSTed to every source with a "report" URL soon
    after one changes, and every `every` seconds anyway (a receiver may drop a list it has not
    heard again for a while). Call update() with each new view and tick() every second, both
    on the daemon's loop; each POST runs on a thread of its own, one per source at a time, so a
    slow or dead receiver never holds anything up."""

    def __init__(self, folder: Path = SOURCES_DIR, poster=post, clock=time.time, spawn=None, log=_log):
        self.folder, self.poster, self.clock, self.log = folder, poster, clock, log
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.attention = Attention()
        self.items: list[dict] = []
        self.changed = 0.0                        # when the items last changed
        self._since: dict[str, tuple] = {}        # item id -> (what it showed, since when)
        self._sources: list[dict] = []
        self._loaded = float("-inf")
        self._sent: dict[str, dict] = {}          # source key -> {"items", "next", "ok", "error"}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def update(self, tabs: list[dict]):
        """The tabs the daemon shows now."""
        now = self.clock()
        items, since = [], {}
        for it in claude_items(tabs, self.attention):
            shown = (it["title"], it["state"], it["detail"])
            last = self._since.get(it["id"])
            since[it["id"]] = (shown, last[1] if last and last[0] == shown else now)
            items.append({**it, "updated": int(since[it["id"]][1])})
        self._since = since
        if items != self.items:
            self.items, self.changed = items, now

    def tick(self):
        """Send to every source that is due: a change that has settled, or its regular report."""
        now = self.clock()
        if now - self._loaded >= 3:
            self._sources = [s for s in load_sources(self.folder) if s.get("report")]
            self._loaded = now
        for source in self._sources:
            key = source["key"]
            with self._lock:
                last = self._sent.get(key) or {}
                fresh = last.get("ok", True) and last.get("items") != self.items \
                    and now - self.changed >= REPORT_DEBOUNCE_S
                if key in self._busy or (now < last.get("next", 0) and not fresh):
                    continue
                self._busy.add(key)
            self.spawn(lambda s=source, items=list(self.items): self._send(s, items, now))

    def _send(self, source: dict, items: list[dict], now: float):
        try:
            self.poster(source, items)
            error = None
        except Exception as e:  # noqa: BLE001 (a receiver that fails is logged, never raised)
            error = str(e)[:160] or type(e).__name__
        with self._lock:
            self._busy.discard(source["key"])
            last = self._sent.get(source["key"]) or {}
            self._sent[source["key"]] = {"items": items if error is None else last.get("items"),
                                         "next": now + source["every"], "ok": error is None, "error": error}
        # a receiver that stays down is said once, not every few seconds
        if error is not None and error != last.get("error"):
            self.log(f"report to {source['name']} failed: {error}; trying again every {source['every']} s")
        elif error is None and last.get("error"):
            self.log(f"report to {source['name']} works again")
