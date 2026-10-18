"""What a box says about its Claude, shared by the daemon (which decides who calls you), the
sidebar that draws it and the report (sources.py) that sends it elsewhere, so all say the same:
the note under its title, and whether it calls you back (yellow, breathing).
"""
from __future__ import annotations

import time

# the note under a Claude's title, by state ("external": the turn check says it only waits on
# something, turncheck.py)
STATE_NOTE = {"shell": "shell still running", "waiting": "waiting for you", "external": "waiting on something"}
QUIET = ("busy", "checking", "external")  # states that do not call you back
HANDS_BACK = ("busy", "shell", "checking", "external")  # leaving one of these hands the turn to you


def note(claude: dict) -> str | None:
    """The note under a Claude's title, if its state has one."""
    state = claude.get("state") or "done"
    return f"waiting on {claude['on']}" if state == "external" and claude.get("on") else STATE_NOTE.get(state)


class Attention:
    """Who calls you back, and since when. A Claude does when it hands back to you (busy ->
    anything else is Claude done; shell -> done is the shell it left running finishing), while
    you are not typing in its own terminal, and until you do (or click its box); an other
    source's item when it starts waiting for you, while you are not on its workspace, and until
    you go there. A turn that ends is checked first: yellow only once the model says Claude
    needs you, and a Claude that only waits (external) is not yellow any more. `active`: you are
    typing in its terminal (a Claude), on its workspace (an item)."""

    def __init__(self):
        self.prev: dict[str, tuple[bool, str]] = {}  # key -> (an item's state?, the state)
        self.keys: dict[str, float] = {}             # key -> since when it calls

    def update(self, key: str, state: str, active: bool, item: bool = False, now: float | None = None) -> bool:
        """The state now; whether it calls you back."""
        prev = self.prev.get(key)
        if item:
            calls = prev is not None and prev[0] and prev[1] != "waiting" and state == "waiting"
        else:
            calls = prev is not None and not prev[0] and prev[1] in HANDS_BACK and state != prev[1] \
                and state not in QUIET
        if calls and not active:
            self.keys.setdefault(key, time.time() if now is None else now)
        if active or state == "external":
            self.keys.pop(key, None)
        self.prev[key] = (item, state)
        return key in self.keys

    def since(self, key: str) -> float:
        """When it started calling you; 0 if it does not."""
        return self.keys.get(key, 0.0)

    def seen(self, key: str):
        self.keys.pop(key, None)

    def forget(self, live: set[str]):
        """Drop what it knows of keys that are gone."""
        for key in [k for k in self.prev if k not in live]:
            del self.prev[key]
            self.keys.pop(key, None)
