"""Why things happened: every decision the engine takes, one JSON line each.

~/.local/state/sidelit/journal.jsonl, rotated at 4 MB. `sidelit why
TEXT` reads it back for the slots and windows matching TEXT.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAX_BYTES = 4_000_000

# kinds that only say something happened, printed without their fields
QUIET = {"calm"}


class Journal:
    def __init__(self, path: Path, echo: bool = True):
        self.path = Path(path)
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, **fields):
        rec = {"t": round(time.time(), 2), "kind": kind, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"journal: {e}", file=sys.stderr, flush=True)
        if self.echo:
            print(format_record(rec), file=sys.stderr, flush=True)


def format_record(rec: dict) -> str:
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(rec.get("t", 0)))
    kind = rec.get("kind", "?")
    if kind in QUIET:
        return f"{when} {kind}"
    what = rec.get("what")
    why = rec.get("why")
    rest = {k: v for k, v in rec.items() if k not in ("t", "kind", "what", "why")}
    parts = [f"{when} {kind:<14}"]
    if what:
        parts.append(str(what))
    if why:
        parts.append(f"— {why}")
    if rest:
        parts.append(" ".join(f"{k}={v}" for k, v in rest.items()))
    return " ".join(parts)


def read(path: Path, needle: str = "", limit: int = 40, also: list[str] | None = None) -> list[dict]:
    """The last `limit` records mentioning `needle` or any of `also` (case-insensitive),
    oldest first. A record that names a slot pulls in the other records about that slot
    and its windows."""
    recs = []
    for p in (Path(str(path) + ".1"), Path(path)):
        try:
            with open(p) as f:
                for line in f:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    needles = [n.lower() for n in [needle] + list(also or []) if n]
    if needles:
        hits = [r for r in recs if any(n in json.dumps(r, ensure_ascii=False).lower() for n in needles)]
        slots = {r.get("slot") for r in hits if r.get("slot")}
        windows = {r.get("window") for r in hits if r.get("window")}
        recs = [r for r in recs if r in hits or r.get("slot") in slots or r.get("window") in windows]
    return recs[-limit:]


if __name__ == "__main__":  # python3 -m sidelit.core.journal [TEXT]
    default = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sidelit" / "journal.jsonl"
    for r in read(default, " ".join(sys.argv[1:])):
        print(format_record(r))
