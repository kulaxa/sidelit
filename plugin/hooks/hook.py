#!/usr/bin/env python3
"""Claude Code hook: tells Sidelit what a session is doing, as it happens.

Sidelit's Claude Code plugin runs it (hooks.json beside it; `sidelit hooks install` installs
the plugin) for SessionStart, UserPromptSubmit, PreToolUse, PostToolUse (these two async: they
fire on every tool call), PermissionRequest, Notification, Stop, StopFailure, SubagentStart,
SubagentStop and SessionEnd. Claude runs it with the event as JSON on stdin; it writes the
session's state to $XDG_RUNTIME_DIR/sidelit/claude/<session>.json (so a daemon restart loses
nothing) and nudges the daemon over its socket. With the state go the process ancestry and,
where the terminal says it, the pane Claude runs in (PANE_VARS): several Claudes can share one
terminal window.

The state is the main thread's: a subagent's events (they carry its agent_id) only keep
the list of subagents running, with when each was last heard from, except a permission it asks
for, which waits for you all the same. A turn that ends with background shells still running
is done, and keeps how many (`shells`) until the next event: the daemon shows it as "shell".

It must never slow Claude down or get in its way: standard library only, no output,
always exit 0, and the nudge gives up after a fraction of a second.
"""
import json
import os
import socket
import sys
import time

# event -> what the session is doing afterwards (None: unchanged)
STATE = {"SessionStart": "done", "UserPromptSubmit": "busy", "PreToolUse": "busy", "PostToolUse": "busy",
         "PermissionRequest": "waiting", "Stop": "done", "StopFailure": "done"}
# Notification types: a question for you, or just idle
WAITING = {"permission_prompt", "elicitation_dialog"}
# prompts nobody typed: a background task (a shell, a monitor) or another session waking
# Claude up. They come as UserPromptSubmit too, while the keyboard may be anywhere.
NOT_TYPED = ("<task-notification>", "Another Claude session sent a message")
SAID_MAX = 6000  # characters of Claude's last message kept with the turn's end
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


def ancestry() -> tuple[int, list[int]]:
    """(pid of the claude process running us, every ancestor pid up to init)."""
    chain, claude_pid, pid = [], 0, os.getppid()
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
        if comm == "claude" and not claude_pid:
            claude_pid = pid
        pid = int(stat[stat.rindex(")") + 2:].split()[1])
    return claude_pid, chain


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    sid = event.get("session_id")
    name = event.get("hook_event_name") or ""
    if not sid or not isinstance(sid, str) or "/" in sid:
        return 0
    now = time.time()
    folder = os.path.join(runtime_dir(), "claude")
    path = os.path.join(folder, sid + ".json")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    except OSError:
        return 0
    try:
        with open(path) as f:
            rec = json.load(f)
    except Exception:
        rec = {}
    if rec.get("t", 0) > now:
        return 0  # an older event finishing after a newer one (async hooks): it has nothing new
    claude_pid, chain = ancestry()
    rec.update({"session": sid, "event": name, "t": now})
    if claude_pid:
        rec["pid"] = claude_pid
        rec["ancestors"] = chain
    pane = {k: os.environ[k][:200] for k in PANE_VARS if os.environ.get(k)}
    if pane:
        rec["pane"] = pane
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
        return save(rec, folder, path, sid)
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
    # the turn Claude just ended, which the daemon may ask a model about (turncheck.py); any
    # event but the idle reminder means that turn is not what the tab shows any more
    # With it, Claude's last words: the transcript may not have them yet when the daemon asks,
    # and the background shells it left running ("1 shell still running"): one that ends wakes
    # Claude up again, and that is an event too
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
    if name == "UserPromptSubmit" and not str(event.get("prompt") or "").lstrip().startswith(NOT_TYPED):
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
        if kind in WAITING or (not kind and ("permission" in msg or "needs your" in msg)):
            rec["state"] = "waiting"
        elif kind == "idle_prompt":
            rec["state"] = "done"
    elif name == "SessionEnd":
        rec["state"] = "ended"
        rec["reason"] = event.get("reason") or ""
    elif STATE.get(name):
        rec["state"] = STATE[name]
    return save(rec, folder, path, sid)


def save(rec: dict, folder: str, path: str, sid: str) -> int:
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
        s.sendall((json.dumps({"method": "claude_event", "params": {"session": sid}}) + "\n").encode())
        s.close()
    except Exception:
        pass  # the daemon reads the file when it next looks
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
