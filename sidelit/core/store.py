"""layout.json on disk: atomic saves, rotating backups, and the move from registry.json.

A save writes a temp file, fsyncs it and renames it over the old one, so the file is
always either the old layout or the new one. Backups (layout.json.1 .. .5) are rotated at
most once an hour, going by the newest one's age on disk, and when the daemon starts unless
the newest is under ten minutes old: a burst of restarts (deploys, a crash loop, which systemd
allows ten of in two minutes) used to push every good copy out. A layout that does not parse is never
overwritten: it is set aside as layout.json.broken-<time> and the newest good backup is
used instead.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from sidelit.core import adapters as ad
from sidelit.core import titles
from sidelit.core.model import Layout, Slot, Workspace, new_id

BACKUPS = 5
BACKUP_EVERY_S = 3600
START_BACKUP_S = 600  # a daemon start makes a backup unless the newest is younger than this


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._last_text = None

    def _backup_path(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def load(self) -> Layout | None:
        """The saved layout, or the newest backup that parses, or None if there is none."""
        for p in [self.path] + [self._backup_path(n) for n in range(1, BACKUPS + 1)]:
            if not p.exists():
                continue
            try:
                layout = Layout.from_json(json.loads(p.read_text()))
            except Exception as e:
                print(f"layout: {p} is unusable ({e})", file=sys.stderr, flush=True)
                if p == self.path:
                    broken = p.with_name(f"{p.name}.broken-{int(time.time())}")
                    try:
                        p.replace(broken)
                        print(f"layout: set aside as {broken}", file=sys.stderr, flush=True)
                    except OSError:
                        pass
                continue
            if p != self.path:
                print(f"layout: using backup {p}", file=sys.stderr, flush=True)
            return layout
        return None

    def _newest_backup_age(self) -> float:
        try:
            return time.time() - self._backup_path(1).stat().st_mtime
        except OSError:
            return float("inf")

    def rotate_on_start(self) -> bool:
        """A copy of the layout as it was before this daemon touched it, unless one was made
        minutes ago (then that one is as good, and the older ones are kept)."""
        return self.rotate(min_age=START_BACKUP_S)

    def rotate(self, min_age: float = 0.0) -> bool:
        """layout.json -> .1, .1 -> .2, ...; nothing when .1 is younger than min_age."""
        if not self.path.exists() or self._newest_backup_age() < min_age:
            return False
        for n in range(BACKUPS, 1, -1):
            older = self._backup_path(n - 1)
            if older.exists():
                older.replace(self._backup_path(n))
        try:
            data = self.path.read_bytes()
            tmp = self._backup_path(1).with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(self._backup_path(1))
        except OSError:
            pass
        return True

    def save(self, layout: Layout):
        text = json.dumps(layout.to_json(), indent=2, ensure_ascii=False)
        if text == self._last_text:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate(min_age=BACKUP_EVERY_S)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        self._last_text = text


def migrate_registry(registry: dict, live_sessions: set, fresh: bool, now: float, terminal_app: str = "terminator") -> Layout:
    """registry.json (0.3.x) -> Layout.

    The old registry kept a workspace's Claude session forever, even after you closed its
    terminal, which is why closed Claudes kept coming back at login. Migrating in the middle
    of a session we can tell: a remembered session that is not running any more was closed,
    and goes to the recent list instead of into the layout. Right after a login nothing runs
    yet, so every remembered session is kept."""
    lay = Layout()
    active_id = registry.get("active_ws")
    for e in registry.get("claudes") or []:
        if not isinstance(e, dict):
            continue
        lw = Workspace(id=new_id("w"), ids=[e["ws_id"]] if e.get("ws_id") else [],
                       name=e.get("custom_name") or "", cwd=e.get("cwd") or "", created=now)
        sid = e.get("last_session")
        if sid:
            slot = Slot(id=new_id("s"), kind="terminal", app=terminal_app, title=e.get("last_title") or "",
                        output=e.get("output") or "", created=now, updated=now,
                        state={"session": sid, "claude_title": e.get("last_title") or "", "cwd": e.get("cwd") or ""})
            if fresh or sid in live_sessions:
                lw.slots.append(slot)
            else:
                lay.remember_recent(slot, lw, now)
        for rec in e.get("windows") or []:
            app = rec.get("app") or ""
            if not app:
                continue
            kind = ad.kind_for_app(app)
            slot = Slot(id=new_id("s"), kind=kind, app=app, title=rec.get("title") or "",
                        output=rec.get("output") or "", window=rec.get("id") or "", created=now, updated=now)
            if kind == "terminal":
                cwd = titles.cwd_from_prompt_title(slot.title)
                if cwd:
                    slot.state["cwd"] = cwd
            elif kind == "files":
                folder = titles.folder_from_title(app, slot.title)
                if folder:
                    slot.state["folder"] = folder
                    path = titles.resolve_folder(folder, [e.get("cwd") or ""])
                    if path:
                        slot.state["path"] = path
            elif kind == "webapp":
                parsed = titles.chrome_app_url(app)
                if parsed:
                    slot.state["url"], slot.state["profile"] = parsed
            lw.slots.append(slot)
        lay.workspaces.append(lw)
        if active_id and active_id == e.get("ws_id"):
            lay.active = lw.id
    lay.validate()
    return lay
