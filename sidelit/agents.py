"""The coding agents Sidelit follows, behind one interface: Claude Code (claude.py) and Codex
(codex.py).

The daemon, the detector (detect.py), the turn checker (turncheck.py), restore, fork and the
sidebar ask an agent's adapter, never the agent itself. Each adapter provides:

1. its process: `comm`, its name in /proc/<pid>/stat, and `binary()`, where its command is
2. its integration: `install()`, `uninstall()`, `status()` and `upgrade()`, always through the
   agent's own plugin system, never by editing its settings
3. its events: Sidelit's plugin runs one hook for every agent (plugin/hooks/hook.py, a table
   per agent) that turns the agent's events into Sidelit's states (working, waiting for you,
   done, ended; the subagents at work; the turn's last words) and writes them, a file per
   session, to the adapter's `hook_dir`. `resolve()` finds the process showing a session when
   the hook could not see it (Codex's shared background server runs its hooks)
4. its live sessions, where the agent lists them itself: `live_sessions()`
5. a session's title and model: `title()`, `model()`; and what its terminal's title says,
   for telling which window shows it: `title_key()`, `window_key()`, `owns_title()`
6. and 7. resume, fork, a new session: `resume_command()`, `fork_command()`, `new_command()`
8. a one-shot "does this turn need you" check through the agent's own headless mode:
   `last_turn()`, `spawn_judge()`, `ask_judge()`
9. the plan's usage limits, where the agent offers them documented: `usage()`

Everything here is a default for an agent that cannot do the thing; an adapter overrides what
its agent can.
"""
from __future__ import annotations

from pathlib import Path


class Agent:
    name = ""          # "claude", "codex": its folder of hook records, its word in the layout
    label = ""         # "Claude Code", "Codex": how Sidelit names it to you
    short = ""         # "Claude", "Codex": in a line ("Codex 'Fix tests'")
    comm = ""          # its process's name in /proc/<pid>/stat
    command = ""       # what starts a new session in a terminal
    marks = ("", "")   # the glyph in front of a box's title: (not working, working)
    window_title = False  # the terminal's title is the session's title (Claude's is; Codex's is the project)

    def __init__(self, hook_dir: Path | None = None):
        self._hook_dir = Path(hook_dir) if hook_dir else None

    # ---- 1. the process
    def binary(self) -> str | None:
        return None

    def installed(self) -> bool:
        return self.binary() is not None

    # ---- 2. the integration (Sidelit's plugin, the agent's own way)
    after_install: tuple[str, ...] = ()  # what to know once it is in: when its sessions start reporting

    def install(self) -> list[str]:
        """Put Sidelit's plugin in: what was done, a line each. Raises RuntimeError."""
        return []

    def uninstall(self) -> list[str]:
        return []

    def status(self) -> tuple[bool, list[str]]:
        """(whether the plugin is in and working, what to say about it)."""
        return False, []

    def upgrade(self) -> list[str]:
        """At `sidelit start`: whatever an older Sidelit left to put right; quiet ([]) when nothing."""
        return []

    # ---- 3. events
    @property
    def hook_dir(self) -> Path:
        if self._hook_dir is None:
            from sidelit import config
            return config.RUNTIME_DIR / self.name
        return self._hook_dir

    def resolve(self, records: list[dict], table: dict) -> None:
        """Give each record of a live session its process ("pid", "ancestors") where the hook
        could not see it. The hook's own word stands by default."""

    # ---- 4. live sessions
    def live_sessions(self, now: float, table: dict) -> tuple[list[dict], float | None]:
        """The agent's own list of its running sessions ({"sessionId", "pid", "cwd", "name",
        "status": busy | waiting | idle}) and when it was asked; None: not asked this time."""
        return [], None

    # ---- 5. titles and the model
    def title(self, session: str, cwd: str, rec: dict | None = None) -> str | None:
        """The title the agent shows for a session."""
        return None

    def title_key(self, session: str, rec: dict) -> str | None:
        """What the terminal's title says while this session runs in it (window_key's terms)."""
        return None

    def window_key(self, title: str) -> str | None:
        """What a terminal's title says, in title_key's terms, if it may be this agent's; None
        when it cannot be."""
        return None

    def unmatched_title(self, title: str) -> bool:
        """With no session matched to a terminal (no plugin yet, say), does its title say this
        agent runs there?"""
        return False

    def focus_key(self, title: str) -> str | None:
        """What the title of the terminal you typed a prompt into says, in title_key's terms, if
        that terminal may be this agent's; None when the prompt cannot have gone there."""
        return self.window_key(title)

    def owns_title(self, title: str, cwd: str = "") -> bool:
        """Does a terminal's title still say this agent runs in it (for the session of `cwd`)?
        When it stops saying so for a while, the agent has left that terminal."""
        return False

    def model(self, session: str, rec: dict | None) -> str | None:
        return None

    def watch(self, sessions: list[str]):
        """The agent's sessions on screen now, each daemon step: an agent that asks for their
        titles off the main loop asks now, when it is time."""

    # ---- 6. and 7. resume, fork, new
    def resume_command(self, session: str, cwd: str) -> str | None:
        return None

    def fork_command(self, session: str, cwd: str) -> str | None:
        return None

    def new_command(self) -> str:
        from sidelit.procs import shell_command
        return shell_command(self.command)

    # ---- 8. the turn check
    def last_turn(self, rec: dict) -> dict | None:
        """{"asked", "events": [(who, text)], "cut", "wakes"} for the turn `rec` ended, else None."""
        return None

    def check_model(self, cfg: dict) -> str | None:
        return None

    def spawn_judge(self, model: str | None, system: str, cwd: str | None):
        """A headless process ready to judge one turn (started ahead: starting takes seconds)."""
        return None

    def ask_judge(self, proc, text: str, timeout: float) -> str:
        """Its answer to `text`. Raises on anything but an answer."""
        raise RuntimeError(f"{self.label} has no headless mode Sidelit knows")

    # ---- 9. usage limits
    # where the sidebar reads them ({"at", "limits": [{"kind", "percentUsed", "resetsAt"}]}),
    # under $XDG_RUNTIME_DIR/sidelit: Sidelit's plugin writes Claude Code's from inside it, the
    # daemon an agent's whose limits it asks for (polls_usage, usage()); "" for an agent with none
    usage_name = ""
    polls_usage = False

    @property
    def usage_file(self) -> Path | None:
        if not self.usage_name:
            return None
        from sidelit import config
        return config.RUNTIME_DIR / self.usage_name

    def usage(self) -> dict | None:
        """The plan's limits now, asked of the agent (polls_usage); None when they cannot be read."""
        return None

    def mark(self, working: bool) -> str:
        return self.marks[1 if working else 0]


_ALL: list[Agent] = []


def every() -> list[Agent]:
    """Every agent Sidelit knows, installed or not."""
    if not _ALL:
        from sidelit import claude, codex
        _ALL.extend([claude.ClaudeCode(), codex.Codex()])
    return _ALL


def get(name: str | None) -> Agent:
    """The agent of that name; Claude Code for none (what a layout from before agents means)."""
    return next((a for a in every() if a.name == name), None) or every()[0]


def of_comm(comm: str) -> Agent | None:
    return next((a for a in every() if a.comm == comm), None)


def comms() -> set[str]:
    return {a.comm for a in every()}


def installed() -> list[Agent]:
    return [a for a in every() if a.installed()]
