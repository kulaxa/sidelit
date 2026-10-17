"""What a tab says about its Claude, shared by the sidebar that draws it and the report
(sources.py) that sends it elsewhere, so both say the same: the note under its title, and
whether it calls you back (yellow, breathing).
"""
from __future__ import annotations

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
    """Which tabs call you back. A Claude does when it hands back to you (busy -> anything else is
    Claude done; shell -> done is the shell it left running finishing), an other source's item
    when it starts waiting for you; either only while you are on another workspace, and until
    you go there. A turn that ends is checked first: yellow only once the model says Claude needs
    you, and a Claude that only waits (external) is not yellow any more."""

    def __init__(self):
        self.prev: dict[str, tuple[bool, str]] = {}  # tab key -> (an item's state?, the state)
        self.keys: set[str] = set()

    def update(self, key: str, state: str, active: bool, item: bool = False) -> bool:
        """The tab's state now; whether it calls you back."""
        prev = self.prev.get(key)
        if item:
            calls = prev is not None and prev[0] and prev[1] != "waiting" and state == "waiting"
        else:
            calls = prev is not None and not prev[0] and prev[1] in HANDS_BACK and state != prev[1] \
                and state not in QUIET
        if calls and not active:
            self.keys.add(key)
        if active or state == "external":
            self.keys.discard(key)
        self.prev[key] = (item, state)
        return key in self.keys

    def seen(self, key: str):
        self.keys.discard(key)
