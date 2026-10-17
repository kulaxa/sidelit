"""What Claude Code tells us about its sessions, from the files it already writes.

~/.claude/sessions/<pid>.json (live sessions: pid, cwd, sessionId, name, status),
~/.claude/statusline-state/<sid>.json (recent status-line snapshots: model, cwd), and the
transcripts under ~/.claude/projects/ (the title Claude shows). Pure file reading.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

from sidelit.core.titles import bare_title, looks_like_claude, title_state

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS = CLAUDE_DIR / "sessions"
PROJECTS = CLAUDE_DIR / "projects"
STATUSLINE_STATE = CLAUDE_DIR / "statusline-state"
SNAPSHOT_MAX_AGE_S = 12 * 3600

# ~/.claude/sessions/<pid>.json status -> tab state. Claude Code writes busy | shell | idle |
# waiting; "shell" is what its own UI shows as "1 shell still running" (the turn is over, a
# background shell is not), "waiting" a permission prompt or question. Everything else is done.
SESSION_STATE = {"busy": "busy", "shell": "shell", "waiting": "waiting"}


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def proc_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, comm) for all processes."""
    table = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            comm = stat[stat.index("(") + 1: stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            table[int(pid)] = (ppid, comm)
        except (OSError, ValueError):
            continue
    return table


def descendants(roots: list[int], table: dict[int, tuple[int, str]]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    out, stack = set(), [r for r in roots if r in table]
    while stack:
        p = stack.pop()
        for c in children.get(p, ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def in_terminal(pid: int) -> bool:
    """Whether Claude `pid` runs in a terminal: an interactive one has it on stdin; `claude -p`,
    and an app driving Claude through the SDK, a pipe or a socket."""
    try:
        return os.readlink(f"/proc/{pid}/fd/0").startswith(("/dev/pts/", "/dev/tty"))
    except OSError:
        return True  # cannot tell


def outer_claude(pid: int, table: dict[int, tuple[int, str]]) -> int:
    """The Claude that started Claude `pid` (one running `claude -p`, say), or 0."""
    pid = table.get(pid, (0, ""))[0]
    for _ in range(40):
        if pid <= 1 or pid not in table:
            return 0
        ppid, comm = table[pid]
        if comm == "claude":
            return pid
        pid = ppid
    return 0


def claude_sessions(table: dict[int, tuple[int, str]]) -> list[dict]:
    """Claude sessions that may be on screen: ~/.claude/sessions/<pid>.json for live pids,
    plus recent status-line snapshots (Claude Code does not always write a sessions file,
    the status line always runs)."""
    out: dict[str, dict] = {}
    if SESSIONS.is_dir():
        for f in SESSIONS.glob("*.json"):
            d = load_json(f, None)
            if not isinstance(d, dict) or not d.get("pid") or not d.get("sessionId"):
                continue
            ppid, comm = table.get(int(d["pid"]), (0, ""))
            if comm != "claude":
                continue  # stale file
            if ppid == os.getpid():
                continue  # our own `claude -p` (turncheck.py), not a session on screen
            out[d["sessionId"]] = d
    if STATUSLINE_STATE.is_dir():
        now_ms = datetime.now().timestamp() * 1000
        for f in STATUSLINE_STATE.glob("*.json"):
            d = load_json(f, None)
            if not isinstance(d, dict) or now_ms - d.get("written_at_ms", 0) > SNAPSHOT_MAX_AGE_S * 1000:
                continue
            p = d.get("payload") or {}
            sid = p.get("session_id")
            if not sid or sid in out:
                continue
            out[sid] = {"sessionId": sid, "cwd": p.get("cwd") or (p.get("workspace") or {}).get("current_dir", ""),
                        "pid": 0, "name": None, "status": None, "snapshot": True}
    return list(out.values())


def live_session_ids(table=None) -> set[str]:
    """Sessions with a running claude process."""
    return {s["sessionId"] for s in claude_sessions(table or proc_table()) if s.get("pid")}


_TITLE_CACHE: dict[str, tuple[float, int, str | None, float]] = {}
_AI_TITLE_RE = re.compile(r'"type"\s*:\s*"ai-title"\s*,\s*"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')


def project_slug(cwd: str) -> str:
    """~/.claude/projects/<slug>: the cwd with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def newest_transcript(cwd: str) -> str | None:
    """Session id of the most recently written transcript for a directory, if any."""
    try:
        files = [f for f in (PROJECTS / project_slug(cwd)).glob("*.jsonl") if f.stat().st_size > 0]
    except OSError:
        return None
    return max(files, key=lambda f: f.stat().st_mtime).stem if files else None


def has_transcript(session_id: str) -> bool:
    return bool(list(PROJECTS.glob(f"*/{session_id}.jsonl")))


_ELSEWHERE: dict[tuple[str, str], tuple[Path | None, float]] = {}
ELSEWHERE_RETRY_S = 30


def transcript_file(session_id: str, cwd: str) -> Path | None:
    """A session's transcript. It stays in the project folder of the directory the session
    started in, so a session that moved on (EnterWorktree into a session clone) reports a cwd
    whose folder does not have it: then every folder is looked in (the hit remembered, a miss
    looked for again after ELSEWHERE_RETRY_S)."""
    if not session_id:
        return None
    path = PROJECTS / project_slug(cwd) / f"{session_id}.jsonl"
    if path.exists():
        return path
    key, now = (str(PROJECTS), session_id), datetime.now().timestamp()
    hit = _ELSEWHERE.get(key)
    if hit and (hit[0].exists() if hit[0] is not None else now - hit[1] < ELSEWHERE_RETRY_S):
        return hit[0]
    found = next(iter(PROJECTS.glob(f"*/{session_id}.jsonl")), None)
    _ELSEWHERE[key] = (found, now)
    return found


def transcript_title(session_id: str, cwd: str) -> str | None:
    """The title Claude shows for a session: the custom title if set, else the latest
    'ai-title' record in the transcript. Cached by file mtime/size (tail read, cheap)."""
    path = transcript_file(session_id, cwd)
    folder = path.parent if path is not None else PROJECTS / project_slug(cwd)
    custom = load_json(folder / session_id / "custom-title.json", None)
    if isinstance(custom, dict) and custom.get("customTitle"):
        return custom["customTitle"]
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _TITLE_CACHE.get(key)
    now = datetime.now().timestamp()
    if cached and (cached[0] == st.st_mtime and cached[1] == st.st_size or now - cached[3] < 10):
        return cached[2]
    title = cached[2] if cached else None
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, st.st_size - 262144))
            found = _AI_TITLE_RE.findall(handle.read().decode("utf-8", "replace"))
        if not found and (not cached or now - cached[3] > 60):  # full scan at most once a minute
            with open(path, "rb") as handle:
                found = _AI_TITLE_RE.findall(handle.read().decode("utf-8", "replace"))
        if found:
            title = json.loads('"' + found[-1] + '"')
    except OSError:
        pass
    _TITLE_CACHE[key] = (st.st_mtime, st.st_size, title, now)
    return title


def session_title_index(sessions: list[dict]) -> dict[str, dict]:
    """bare title -> live session, for every title a session may be showing."""
    index: dict[str, dict] = {}
    for sess in sessions:
        for t in (sess.get("name"), transcript_title(sess.get("sessionId", ""), sess.get("cwd", ""))):
            if t:
                index.setdefault(t.strip(), sess)
    return index


def pretty_model(model_id: str) -> str:
    """'claude-opus-4-5-20251101' -> 'Opus 4.5'"""
    trimmed = re.sub(r"-\d{8}$", "", model_id.replace("claude-", ""))
    parts = trimmed.split("-")
    return (parts[0].capitalize() + " " + ".".join(parts[1:])).strip()


def session_model(session_id: str | None) -> str | None:
    """Model a session is on: the status-line snapshot first, else the transcript tail."""
    if not session_id:
        return None
    snap = load_json(STATUSLINE_STATE / f"{session_id}.json", None)
    try:
        name = snap["payload"]["model"]["display_name"]
        if name:
            return name
    except (KeyError, TypeError):
        pass
    for path in glob.glob(str(PROJECTS / "*" / f"{session_id}.jsonl")):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        models = re.findall(r'"model"\s*:\s*"(claude-[a-z0-9.-]+)"', tail)
        if models:
            return pretty_model(models[-1])
    return None


def login_shell() -> str:
    if os.environ.get("SHELL"):
        return os.environ["SHELL"]
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    except Exception:
        return "/bin/bash"


def shell_command(inner: str) -> str:
    """`inner` in an interactive login shell that stays open afterwards."""
    shell = login_shell()
    return f"{shell} -i -c '{inner}; exec {shell} -i'"


def resume_command(session_id: str, cwd: str) -> str:
    """Resume a session; one that never got a message has no transcript, so resume the
    newest session of its directory instead, or start a fresh Claude there."""
    if has_transcript(session_id):
        return shell_command(f"claude --resume {session_id}")
    fallback = newest_transcript(cwd) if cwd else None
    if fallback:
        return shell_command(f"claude --resume {fallback}")
    return shell_command("claude")


# ---- which window shows which Claude --------------------------------------------------
# Evidence, strongest first. A mapping holds until stronger evidence (or equally strong,
# newer evidence) says otherwise, or its window or session goes.
BY_LAUNCH = 3   # the hook's process ancestry contains the terminal we started for that window
BY_PID = 3      # ...or the window's own pid (compositors that report it)
BY_FOCUS = 2    # you submitted a prompt while that terminal had the keyboard
BY_TITLE = 1    # the terminal's title is the session's title
FOCUS_LAG_S = 0.6  # a focus change reaches us a little after it happens
WOKEN_MAX_S = 300  # Claude's session file alone says it works again for at most this long
AGENT_QUIET_S = 1800  # a subagent not heard from for this long is taken for gone (its stop lost)


class Detector:
    """Which Claude each window shows, and what it is doing.

    Two sources. The Claude Code hook (hook.py, `sidelit hooks install`) reports
    every session's events as they happen, with the process ancestry the hook ran in:
    status straight from the events, and the window from launch ancestry or from which
    window had the keyboard when you submitted a prompt. Without hooks (or for a session
    started before they were installed), the title Claude puts on its terminal is matched
    against the live sessions in ~/.claude/sessions and the status read from there.

    A tab shows its main thread: subagents, and Claudes another Claude started (its own
    `claude -p`, a test harness), are only counted while they work, and never take its window.
    Nor does a Claude with no terminal (an app driving it), whose prompts are no one typing."""

    def __init__(self, hook_dir: Path | None = None, ttl: float = 1.0):
        from sidelit import config
        self.hook_dir = Path(hook_dir) if hook_dir else config.RUNTIME_DIR / "claude"
        self.ttl = ttl
        self._at = 0.0
        self.table: dict = {}
        self.sessions: list[dict] = []
        self.index: dict = {}
        self.hooked: dict[str, dict] = {}              # live sessions the hook reports
        self.inner: dict[int, int] = {}                # claude pid -> Claudes it started, working now
        self.window_of: dict[str, tuple] = {}          # session -> (window id, strength, evidence time)
        self.focus: list[tuple[float, str]] = []       # (when, window id) the keyboard moved
        self.focus_since = 0.0
        self._by_window: dict[str, str] = {}
        self._load_windows()

    # ---- inputs
    def note_focus(self, now: float, wid: str | None):
        if not self.focus_since:
            self.focus_since = now
        if wid and (not self.focus or self.focus[-1][1] != wid):
            self.focus.append((now, wid))
            del self.focus[:-200]

    def focus_at(self, t: float) -> str | None:
        """The window that had the keyboard at `t`, if we were watching then."""
        if not self.focus_since or t < self.focus_since:
            return None
        hit = None
        for when, wid in self.focus:
            if when <= t:
                hit = wid
            else:
                break
        return hit

    def refresh(self, now: float, force: bool = False, table: dict | None = None):
        if not force and now - self._at < self.ttl:
            return
        self._at = now
        self.table = table if table is not None else proc_table()
        self.sessions = claude_sessions(self.table)
        self.index = session_title_index(self.sessions)
        self.hooked = self._read_hooks(now)

    def _read_hooks(self, now: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        try:
            files = list(self.hook_dir.glob("*.json"))
        except OSError:
            return out
        for f in files:
            if f.name == "windows.json":
                continue
            rec = load_json(f, None)
            if not isinstance(rec, dict) or not rec.get("session"):
                continue
            pid = int(rec.get("pid") or 0)
            alive = pid and self.table.get(pid, (0, ""))[1] == "claude"
            if rec.get("state") == "ended" or not alive:
                if now - float(rec.get("t") or 0) > 60:  # long gone: tidy up
                    for p in (f, f.with_suffix(".prompt")):
                        try:
                            p.unlink()
                        except OSError:
                            pass
                continue
            try:
                rec["prompt_t"] = float(f.with_suffix(".prompt").read_text())
            except (OSError, ValueError):
                pass
            rec["outer"] = outer_claude(pid, self.table)
            rec["windowless"] = bool(rec["outer"]) or not in_terminal(pid)
            out[rec["session"]] = rec
        self.inner = {}
        for rec in out.values():
            if rec["outer"] and rec.get("state") in ("busy", "waiting"):
                self.inner[rec["outer"]] = self.inner.get(rec["outer"], 0) + 1
        # /clear, /resume and friends start a new session in the same claude process: the
        # newest one is the live one, and it inherits the window
        by_pid: dict[int, list[dict]] = {}
        for rec in out.values():
            by_pid.setdefault(int(rec.get("pid") or 0), []).append(rec)
        for pid, recs in by_pid.items():
            if len(recs) < 2:
                continue
            recs.sort(key=lambda r: (float(r.get("started") or 0), float(r.get("t") or 0)))
            newest = recs[-1]
            for old in recs[:-1]:
                out.pop(old["session"], None)
                if old["session"] in self.window_of and newest["session"] not in self.window_of:
                    self.window_of[newest["session"]] = self.window_of[old["session"]]
        return out

    # ---- the mapping
    def assign(self, windows: dict, launched_pid: dict[str, int] | None = None):
        """Work out which window shows which Claude. `windows`: id -> Win."""
        from sidelit.core.titles import TERMINAL_APP_IDS
        launched_pid = launched_pid or {}
        live = {sid for sid, rec in self.hooked.items() if not rec["windowless"]} \
            | {s.get("sessionId") for s in self.sessions if s.get("pid") and not self._windowless(s)}
        before = dict(self.window_of)
        for sid in list(self.window_of):
            if sid not in live or self.window_of[sid][0] not in windows:
                del self.window_of[sid]
        by_title = {}
        for wid, w in windows.items():
            if w.app in TERMINAL_APP_IDS and looks_like_claude(w.title) and title_state(w.title) != "other":
                by_title.setdefault(bare_title(w.title) or "", []).append(wid)
        for sid, rec in self.hooked.items():
            if rec["windowless"]:
                continue  # its terminal, if any, is another Claude's
            anc = set(rec.get("ancestors") or [])
            found = []
            for wid, pid in launched_pid.items():
                if pid in anc and wid in windows:
                    found.append((BY_LAUNCH, float(rec.get("started") or rec.get("t") or 0), wid))
            for wid, w in windows.items():
                if w.pid and w.pid in anc:
                    found.append((BY_PID, float(rec.get("t") or 0), wid))
            title = transcript_title(sid, rec.get("cwd") or "")
            prompt_t = rec.get("prompt_t")
            if prompt_t:
                wid = self.focus_at(float(prompt_t) + FOCUS_LAG_S)
                w = windows.get(wid) if wid else None
                # A prompt is submitted when you press Enter, or, typed while Claude worked,
                # when its turn ends, by which time you may be on another Claude: the
                # terminal that had the keyboard counts only if it shows this Claude's title
                if w is not None and w.app in TERMINAL_APP_IDS and looks_like_claude(w.title) \
                        and (not title or bare_title(w.title) == title):
                    found.append((BY_FOCUS, float(prompt_t), wid))
            hits = by_title.get(title or "\0", [])
            if len(hits) == 1:
                found.append((BY_TITLE, float(rec.get("t") or 0), hits[0]))
            if found:
                self._offer(sid, max(found))
        # sessions the hook does not know (started before it was installed): by title only
        for title, wids in by_title.items():
            sess = self.index.get(title)
            if sess and len(wids) == 1 and sess.get("sessionId") not in self.hooked and not self._windowless(sess):
                self._offer(sess["sessionId"], (BY_TITLE, 0.0, wids[0]))
        # one Claude per window: the best claim wins, the newest among equals
        claims: dict[str, list[tuple]] = {}
        for sid, (wid, strength, t) in self.window_of.items():
            claims.setdefault(wid, []).append((strength, t, sid))
        self._by_window = {}
        for wid, cs in claims.items():
            cs.sort(reverse=True)
            self._by_window[wid] = cs[0][2]
            for _, _, loser in cs[1:]:
                self.window_of.pop(loser, None)
        if self.window_of != before:
            self._save_windows()

    def _windowless(self, sess: dict) -> bool:
        """A session another Claude started, or one with no terminal."""
        pid = int(sess.get("pid") or 0)
        return bool(outer_claude(pid, self.table)) or not in_terminal(pid)

    def _offer(self, sid: str, evidence: tuple):
        strength, t, wid = evidence
        cur = self.window_of.get(sid)
        if cur is None or strength > cur[1] or (strength == cur[1] and t > cur[2]):
            self.window_of[sid] = (wid, strength, t)

    def _save_windows(self):
        try:
            self.hook_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.hook_dir / "windows.json.tmp"
            tmp.write_text(json.dumps({s: list(v) for s, v in self.window_of.items()}))
            tmp.replace(self.hook_dir / "windows.json")
        except OSError:
            pass

    def _load_windows(self):
        """A daemon restart keeps what it had worked out (window ids live as long as the
        compositor session)."""
        data = load_json(self.hook_dir / "windows.json", {})
        if isinstance(data, dict):
            for sid, v in data.items():
                if isinstance(v, list) and len(v) == 3:
                    self.window_of[sid] = (str(v[0]), int(v[1]), float(v[2]))

    # ---- outputs
    def status(self, sid: str) -> str:
        """busy | waiting | shell | done: a Claude whose subagents work is busy too"""
        state = self._status(sid)
        return "busy" if state in ("done", "shell") and self.agents(sid) else state

    def _status(self, sid: str) -> str:
        rec = self.hooked.get(sid)
        sess = next((s for s in self.sessions if s.get("sessionId") == sid and s.get("pid")), None)
        file_state = SESSION_STATE.get(str(sess.get("status", "")).lower(), "done") if sess else None
        if rec is not None:
            state = rec.get("state") or "done"
            # the hook cannot see background shells; Claude's own session file can
            if state == "done" and file_state == "shell":
                return "shell"
            # nor a background task waking Claude up again (no prompt, and no event until it
            # uses a tool or stops): the session file says busy after the hook's last word.
            # A Claude that works sends events, so after WOKEN_MAX_S without one the file is stale.
            woke = float(sess.get("statusUpdatedAt") or 0) / 1000 if sess else 0.0
            if state == "done" and file_state == "busy" and woke > float(rec.get("t") or 0) \
                    and self._at - woke < WOKEN_MAX_S:
                return "busy"
            return state if state in ("busy", "waiting", "done") else "done"
        return file_state or "done"

    def agents(self, sid: str) -> int:
        """Subagents of this session working now, and Claudes it started that work."""
        rec = self.hooked.get(sid) or {}
        sess = next((s for s in self.sessions if s.get("sessionId") == sid and s.get("pid")), None)
        pid = int(rec.get("pid") or (sess or {}).get("pid") or 0)
        heard = (rec.get("agents") or {}).values()
        return sum(1 for t in heard if self._at - float(t) < AGENT_QUIET_S) + self.inner.get(pid, 0)

    def for_window(self, title: str, pid: int = 0, wid: str | None = None) -> dict | None:
        """{"session", "title", "status", "agents", "cwd", "how"} for the Claude this window shows."""
        sid = self._by_window.get(wid) if wid else None
        how = self.window_of.get(sid, (None, 0, 0))[1] if sid else 0
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None) if sid else None
        rec = self.hooked.get(sid) if sid else None
        if sid is None:
            # no mapping yet (first look, no hooks): the title, then the process tree
            if looks_like_claude(title) and title_state(title) != "other":
                sess = self.index.get(bare_title(title) or "")
            if sess is None and pid:
                kids = descendants([pid], self.table)
                sess = next((s for s in self.sessions if s.get("pid") and int(s["pid"]) in kids
                             and not self._windowless(s)), None)
            if sess is None or sess.get("sessionId") in self.window_of:
                return None  # (a session shown in a window we know is not also in this one)
            sid, how = sess.get("sessionId"), BY_TITLE
        cwd = (rec or {}).get("cwd") or (sess or {}).get("cwd") or ""
        shown = bare_title(title) if looks_like_claude(title) else None
        return {"session": sid, "title": shown or transcript_title(sid, cwd) or (sess or {}).get("name") or "",
                "status": self.status(sid), "agents": self.agents(sid), "cwd": cwd, "pid": int((rec or sess or {}).get("pid") or 0),
                "how": {BY_LAUNCH: "launch", BY_FOCUS: "focus", BY_TITLE: "title"}.get(how, "pid"),
                "hooked": rec is not None}
