#!/usr/bin/env python3
"""The hook of Sidelit's plugin: tells Sidelit what a coding agent session is doing, as it happens.

One script for every agent, called with the agent's name (`hook.py codex`; none is Claude Code).
The agent runs it for each event, with the event as JSON on stdin; it writes the session's state
to $XDG_RUNTIME_DIR/sidelit/<agent>/<session>.json (so a daemon restart loses nothing) and
nudges the daemon over its socket. With the state go the process ancestry and, where the
terminal says it, the pane the agent runs in (PANE_VARS): several sessions can share one
terminal window.

What each agent's events mean is in AGENTS, its table, and nowhere else:
- Claude Code (hooks.json beside this file) runs it for SessionStart, UserPromptSubmit,
  PreToolUse, PostToolUse (these two async: they fire on every tool call), PermissionRequest,
  Notification, Stop, StopFailure, SubagentStart, SubagentStop and SessionEnd.
- Codex (codex.json, which .codex-plugin/plugin.json names) has the same events but no
  Notification (so nothing says "a question for you") and adds Interrupt. Its interactive
  sessions usually run in its shared background server, which also runs their hooks: the
  ancestry then leads to the server, not to the terminal, so the record leaves the process to
  Sidelit's daemon (pid 0; sidelit/codex.py finds it). It keeps what the turn checker reads
  from it (Codex's transcript "isn't a stable interface"): what you asked, the tools the turn
  ran, what Codex said last; and the first thing you asked, a title until Codex has one.

The state is the main thread's: a subagent's events (they carry its agent_id) only keep
the list of subagents running, with when each was last heard from, except a permission it asks
for, which waits for you all the same. A turn that ends with background shells still running
is done, and keeps how many (`shells`) until the next event: the daemon shows it as "shell".

It must never slow the agent down or get in its way: standard library only, no output,
always exit 0, and the nudge gives up after a fraction of a second.
"""
import json
import os
import socket
import sys
import time

# Per agent, what its events mean:
#   states    event -> what the session is doing afterwards (missing: unchanged)
#   waiting   Notification types that are a question for you; idle: the one that only reminds
#   not_typed prompts nobody typed: a background task (a shell, a monitor) or another session
#             waking the agent up. They come as UserPromptSubmit too, while the keyboard may be anywhere.
#   server    words in its process's arguments that make it a shared server, not a session's own
#   keeps     what of the turn the record keeps for the turn checker (asked, tools) and the title
AGENTS = {
    "claude": {"comm": "claude",
               "states": {"SessionStart": "done", "UserPromptSubmit": "busy", "PreToolUse": "busy",
                          "PostToolUse": "busy", "PermissionRequest": "waiting", "Stop": "done", "StopFailure": "done"},
               "waiting": {"permission_prompt", "elicitation_dialog"}, "idle": "idle_prompt",
               "not_typed": ("<task-notification>", "Another Claude session sent a message"),
               "server": (), "keeps": False},
    "codex": {"comm": "codex",
              "states": {"SessionStart": "done", "UserPromptSubmit": "busy", "PreToolUse": "busy",
                         "PostToolUse": "busy", "PermissionRequest": "waiting", "Stop": "done", "Interrupt": "done"},
              "waiting": set(), "idle": None, "not_typed": (),
              "server": ("app-server",), "keeps": True},
}
SAID_MAX = 6000  # characters of the agent's last message kept with the turn's end
ASKED_MAX = 1500  # characters of your request (Codex)
TOOLS_MAX = 8     # tool calls of the turn kept (Codex)
TOOL_MAX = 160    # characters of each
LOCK_WAIT_S = 0.5  # how long an event waits for the one before it to be written
# what tells one pane of a terminal window from another (split panes, tabs), where the terminal
# says it, and where to reach the terminal that can focus that pane from outside
PANE_VARS = ("TERMINATOR_UUID", "KITTY_WINDOW_ID", "KITTY_LISTEN_ON", "WEZTERM_PANE", "WEZTERM_UNIX_SOCKET",
             "TMUX_PANE", "TMUX")


def runtime_dir() -> str:
    home = os.environ.get("SIDELIT_HOME")
    if home:
        return home
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "sidelit")


def socket_path() -> str:
    home = os.environ.get("SIDELIT_HOME")
    if home:  # same rule as config.SOCKET
        import hashlib
        return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                            f"sidelit-{hashlib.sha1(home.encode()).hexdigest()[:8]}.sock")
    return os.path.join(runtime_dir(), "daemon.sock")


def ancestry(comm_name: str, server: tuple = ()) -> tuple[int, list[int], bool]:
    """(pid of the agent process running us, every ancestor pid up to init, whether that process
    is the agent's shared server rather than a session's own)."""
    chain, agent_pid, pid = [], 0, os.getppid()
    for _ in range(40):
        if pid <= 1:
            break
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            break
        comm = stat[stat.index("(") + 1: stat.rindex(")")]
        chain.append(pid)
        if comm == comm_name and not agent_pid:
            agent_pid = pid
            if server:
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        args = f.read().decode("utf-8", "replace").split("\0")
                except OSError:
                    args = []
                if any(word in args for word in server):
                    return pid, chain, True
        pid = int(stat[stat.rindex(")") + 2:].split()[1])
    return agent_pid, chain, False


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tool_line(event: dict) -> str:
    """A tool call in a line: its name and what it was given ("Bash: npm test")."""
    name = str(event.get("tool_name") or "tool")
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    what = next((inp[k] for k in ("command", "description", "file_path", "path", "url", "query", "pattern")
                 if isinstance(inp.get(k), str) and inp[k].strip()), "")
    return clip(f"{name}: {what}" if what else name, TOOL_MAX)


def main() -> int:
    agent_name = sys.argv[1] if len(sys.argv) > 1 else "claude"
    spec = AGENTS.get(agent_name)
    if spec is None:
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    sid = event.get("session_id")
    name = event.get("hook_event_name") or ""
    if not sid or not isinstance(sid, str) or "/" in sid:
        return 0
    now = time.time()
    folder = os.path.join(runtime_dir(), agent_name)
    path = os.path.join(folder, sid + ".json")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    except OSError:
        return 0
    lock = hold(os.path.join(folder, sid + ".lock"))
    try:
        return record(event, sid, name, now, spec, agent_name, folder, path)
    finally:
        if lock is not None:
            lock.close()


def hold(path: str):
    """The session's lock, so that two events (an async one finishing as the next one comes)
    never both read the record before either has written it; given up on after LOCK_WAIT_S
    (None: go on without it)."""
    import fcntl
    try:
        f = open(path, "w")
    except OSError:
        return None
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() > deadline:
                f.close()
                return None
            time.sleep(0.01)


def record(event: dict, sid: str, name: str, now: float, spec: dict, agent_name: str, folder: str, path: str) -> int:
    """The session's record with this event in it, saved."""
    try:
        with open(path) as f:
            rec = json.load(f)
    except Exception:
        rec = {}
    if rec.get("t", 0) > now:
        return 0  # an older event finishing after a newer one (async hooks): it has nothing new
    turn = event.get("turn_id")
    if turn and name in ("PreToolUse", "PostToolUse") and turn == rec.get("ended_turn"):
        return 0  # (the same, where the agent numbers its turns: an async hook of a turn already over)
    agent_pid, chain, shared = ancestry(spec["comm"], spec["server"])
    rec.update({"session": sid, "event": name, "t": now})
    if shared:
        # the agent's shared server runs this, for whichever terminal: which one is the
        # daemon's to find (its process and its environment are the server's, not the session's)
        rec.update(pid=0, server=agent_pid)
        rec.pop("ancestors", None)
        rec.pop("pane", None)
    else:
        if agent_pid:
            rec["pid"] = agent_pid
            rec["ancestors"] = chain
        pane = {k: os.environ[k][:200] for k in PANE_VARS if os.environ.get(k)}
        if pane:
            rec["pane"] = pane
    if spec["keeps"] and isinstance(event.get("model"), str) and event["model"]:
        rec["model"] = event["model"][:80]
    agents = rec.get("agents") if isinstance(rec.get("agents"), dict) else {}
    agent = event.get("agent_id")
    if agent or name in ("SubagentStart", "SubagentStop"):
        if name == "SubagentStop":
            agents.pop(str(agent), None)
        elif agent:
            agents[str(agent)] = now
        if name == "PermissionRequest":
            if rec.get("state") != "waiting":
                rec["resume"] = rec.get("state") or "busy"
            rec["state"] = "waiting"
        elif name != "SubagentStop" and rec.get("state") == "waiting" and "resume" in rec:
            rec["state"] = rec.pop("resume")  # its permission was answered: back to what the main thread did
        rec["agents"] = agents
        return save(rec, folder, path, sid, agent_name)
    rec.update({"cwd": event.get("cwd") or rec.get("cwd") or "",
                "transcript": event.get("transcript_path") or rec.get("transcript") or ""})
    if name != "Notification":  # (one comes with a subagent's permission prompt too)
        rec.pop("resume", None)
    if name == "SessionStart":
        rec["started"] = now
        rec["source"] = event.get("reason") or event.get("source") or ""
        agents = {}
    shells = 0
    if name == "Stop" and isinstance(event.get("background_tasks"), list):
        # what Claude says still runs: corrects a SubagentStop we missed (an interrupted agent)
        tasks = [b for b in event["background_tasks"] if isinstance(b, dict) and b.get("status") == "running"]
        running = {str(b.get("id")) for b in tasks if b.get("type") == "subagent"}
        shells = sum(1 for b in tasks if b.get("type") == "shell")
        agents = {a: t for a, t in agents.items() if a in running}
        for a in running - set(agents):
            agents[a] = now
    rec["agents"] = agents
    # the turn the agent just ended, which the daemon may ask a model about (turncheck.py); any
    # event but the idle reminder means that turn is not what the tab shows any more
    # With it, the agent's last words: the transcript may not have them yet when the daemon asks,
    # and the background shells it left running ("1 shell still running"): one that ends wakes
    # Claude up again, and that is an event too
    if name in ("Stop", "Interrupt") and turn:
        rec["ended_turn"] = turn
    elif name == "UserPromptSubmit":
        rec.pop("ended_turn", None)
    if name == "Stop":
        rec["turn_end"] = now
        said = event.get("last_assistant_message")
        if isinstance(said, str) and said.strip():
            rec["said"] = said[-SAID_MAX:]
        else:
            rec.pop("said", None)
        if shells:
            rec["shells"] = shells
        else:
            rec.pop("shells", None)
    elif name != "Notification":
        rec.pop("turn_end", None)
        rec.pop("said", None)
        rec.pop("shells", None)
    prompt = str(event.get("prompt") or "")
    if name == "UserPromptSubmit" and spec["keeps"]:
        rec["asked"] = prompt.strip()[:ASKED_MAX]
        rec["tools"] = []
        if not rec.get("first_prompt") and prompt.strip():
            rec["first_prompt"] = clip(prompt, 120)
    if name == "PreToolUse" and spec["keeps"]:
        rec["tools"] = (rec.get("tools") if isinstance(rec.get("tools"), list) else [])[-(TOOLS_MAX - 1):] \
            + [tool_line(event)]
    if name == "UserPromptSubmit" and not (spec["not_typed"] and prompt.lstrip().startswith(spec["not_typed"])):
        # you just typed into this session's terminal (kept apart: an async tool event
        # writing the record at the same moment must not lose it)
        try:
            with open(os.path.join(folder, sid + ".prompt"), "w") as f:
                f.write(repr(now))
        except OSError:
            pass
    if name == "Notification":
        kind = event.get("notification_type") or ""
        msg = (event.get("message") or "").lower()
        if kind in spec["waiting"] or (not kind and ("permission" in msg or "needs your" in msg)):
            rec["state"] = "waiting"
        elif kind and kind == spec["idle"]:
            rec["state"] = "done"
    elif name == "SessionEnd":
        rec["state"] = "ended"
        rec["reason"] = event.get("reason") or ""
    elif spec["states"].get(name):
        rec["state"] = spec["states"][name]
    return save(rec, folder, path, sid, agent_name)


def save(rec: dict, folder: str, path: str, sid: str, agent: str) -> int:
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(socket_path())
        s.sendall((json.dumps({"method": "agent_event", "params": {"agent": agent, "session": sid}}) + "\n").encode())
        s.close()
    except Exception:
        pass  # the daemon reads the file when it next looks
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
