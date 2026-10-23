"""The coding agents Sidelit follows, behind one interface: Claude Code (claude.py), Codex
(codex.py) and OpenCode (opencode.py).

The daemon, the detector (detect.py), the turn checker (turncheck.py), restore, fork and the
sidebar ask an agent's adapter, never the agent itself. Each adapter provides:

1. its process: `comm`, its name in /proc/<pid>/stat, and `binary()`, where its command is
2. its integration: `install()`, `uninstall()`, `status()` and `upgrade()`, always through the
   agent's own plugin system, never by editing its settings
3. its events: Sidelit's plugin runs one hook for every agent (plugin/hooks/hook.py, a table
   per agent) that turns the agent's events into Sidelit's states (working, waiting for you,
   done, ended; the subagents at work; the turn's last words) and writes them, a file per
   session, to the adapter's `hook_dir`. `resolve()` finds the process showing a session when
   the hook could not see it (Codex's and OpenCode 2's shared servers run their hooks:
   TerminalClients)
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

import json
import os
import time
from pathlib import Path

from sidelit.procs import ancestors, cmdline, load_json, process_started


class Agent:
    name = ""          # "claude", "codex": its folder of hook records, its word in the layout
    label = ""         # "Claude Code", "Codex": how Sidelit names it to you
    short = ""         # "Claude", "Codex": in a line ("Codex 'Fix tests'")
    comm = ""          # its process's name in /proc/<pid>/stat
    command = ""       # what starts a new session in a terminal
    marks = ("", "")   # the glyph in front of a box's title: (not working, working)
    window_title = False  # the terminal's title is the session's title (Claude's is; Codex's is the project)
    untitled = ""      # what that title says while the session has no name yet ("Claude Code")

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

    def listed_name(self, name: str | None, cwd: str = "") -> str | None:
        """The `name` the agent's own list gives a session, if it is one to show."""
        return name

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

    def subagent_model(self, session: str, rec: dict | None, agent_id: str) -> str | None:
        """The model a subagent of this session runs on (its id from the hook), once known."""
        return None

    def keeps_subagent(self, session: str, rec: dict | None, agent_id: str) -> bool:
        """Does a subagent the hook heard of leave a trace of its own (a transcript)? One that does
        not may be the agent's own helper, which counts only while it is heard from."""
        return True

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


class TerminalClients:
    """For an agent whose sessions run in a server of its own, which also runs the hook (Codex's
    shared background server, OpenCode 2's): the hook cannot tell which terminal shows a session
    and writes pid 0 and "server". resolve() gives such a record the agent's process in a terminal
    that shows it: the one started for that session (its id on the command line, as a resume
    has it), else the one in the session's folder that started last before the session did. That
    one may have quit since (the server keeps a session a while after you quit it, without a
    word): the processes seen are remembered, also across a daemon restart (a file in the hook
    folder), and a session whose own has quit is left without one, so it is gone.

    The agent says which of its processes are such clients: `clients(table)`, and where each
    works: `folder_of(pid)`."""
    FILE = "processes.seen"
    STARTED_SLACK_S = 5.0     # a client may be seen to start this long after its first session
    MEMORY_S = 7 * 86400      # one that quit is remembered this long after (its sessions are gone)

    def __init__(self, agent: Agent, started=process_started):
        self.agent = agent
        self.started_of = started
        # clients seen -> (started, folder, args, last seen running)
        self.seen: dict[int, tuple[float, str, list[str], float]] | None = None
        self.live: set[int] = set()  # (those running at the last look)

    def resolve(self, records: list[dict], table: dict):
        pending = [r for r in records if not r.get("pid") and r.get("server")]
        now = time.time()
        if self.seen is None:  # (what an earlier daemon saw: one that quit before this one started)
            seen = load_json(self.agent.hook_dir / self.FILE, {})
            self.seen = {int(p): (float(v[0]), str(v[1]), list(v[2]), float(v[3])) for p, v in seen.items()
                         if isinstance(v, list) and len(v) == 4} if isinstance(seen, dict) else {}
        live = self.live = set(self.agent.clients(table))
        changed = False
        for p in live:
            known = self.seen.get(p)
            started = self.started_of(p) or 0.0
            if known is None or abs(known[0] - started) > 1:  # (a pid used again is another process)
                self.seen[p] = (started, self.agent.folder_of(p), cmdline(p), now)
                changed = True
            else:
                self.seen[p] = (*known[:3], now)
        for p in [p for p, v in self.seen.items() if p not in live and now - v[3] > self.MEMORY_S]:
            del self.seen[p]
            changed = True
        if changed:
            try:
                self.agent.hook_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.agent.hook_dir / (self.FILE + ".tmp")
                tmp.write_text(json.dumps({str(p): list(v) for p, v in self.seen.items()}))
                tmp.replace(self.agent.hook_dir / self.FILE)
            except OSError:
                pass
        for rec in pending:
            sid = rec["session"]
            pid = next((p for p in live if sid in self.seen[p][2]), 0)
            if not pid:
                # (two in one folder: a session belongs to the one that started last before it
                # did; one started later is newer than the session)
                where = os.path.realpath(rec.get("cwd") or "/")
                when = float(rec.get("started") or rec.get("t") or 0)
                before = [p for p, (start, folder, _, _) in self.seen.items()
                          if folder == where and start <= when + self.STARTED_SLACK_S]
                pid = max(before, key=lambda p: self.seen[p][0], default=0)
            if pid in live:
                rec["pid"] = pid
                rec["ancestors"] = ancestors(pid, table)
                pane = pane_of(pid)
                if pane:
                    rec["pane"] = pane


def pane_of(pid: int) -> dict:
    """The pane a process runs in, from its own environment (a server's hook only has the server's)."""
    from sidelit.detect import PANE_KEYS  # (detect.py imports this module)
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = dict(v.split(b"=", 1) for v in f.read().split(b"\0") if b"=" in v)
    except OSError:
        return {}
    keys = set(PANE_KEYS) | {"KITTY_LISTEN_ON", "WEZTERM_UNIX_SOCKET", "TMUX"}
    return {k: env[k.encode()].decode("utf-8", "replace")[:200] for k in sorted(keys) if env.get(k.encode())}


def turn_from_record(rec: dict, who: str) -> dict | None:
    """last_turn() from what the plugin kept of the turn in the session's record (what you asked,
    the tool calls, the agent's last message), for an agent whose transcript Sidelit does not
    read. `who`: how the judge reads the agent's own words ("Codex")."""
    events = [("tool", t) for t in rec.get("tools") or [] if isinstance(t, str)]
    if isinstance(rec.get("said"), str) and rec["said"].strip():
        events.append((who, rec["said"].strip()))
    if not events:
        return None
    return {"asked": rec.get("asked") or "", "events": events, "cut": False, "wakes": False}


_ALL: list[Agent] = []


def every() -> list[Agent]:
    """Every agent Sidelit knows, installed or not."""
    if not _ALL:
        from sidelit import claude, codex, opencode
        _ALL.extend([claude.ClaudeCode(), codex.Codex(), opencode.OpenCode()])
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
