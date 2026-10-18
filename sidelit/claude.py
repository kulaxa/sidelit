"""What Claude Code tells us about its sessions, the ways it offers.

Its own list of running sessions, `claude agents --json` (pid, cwd, sessionId, name, status),
which its docs call the supported way to read session state from outside Claude Code; and the
transcripts under ~/.claude/projects/, a documented location (the title Claude shows, the
model). The events of each session come from Sidelit's Claude Code plugin (plugin/hooks/hook.py).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from sidelit.core.titles import bare_title, looks_like_claude, title_state

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
PROJECTS = CLAUDE_DIR / "projects"
AGENTS_EVERY_S = 10.0  # how often Claude Code is asked for its sessions (it starts a process)

# `claude agents --json` status -> tab state: "waiting" is a permission prompt or a question,
# "idle" the turn is over. Everything else is done.
LIVE_STATE = {"busy": "busy", "waiting": "waiting"}


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


_BOOT_TIME: list[float] = []


def process_started(pid: int) -> float | None:
    """When process `pid` started, in seconds since the epoch; None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        if not _BOOT_TIME:
            with open("/proc/stat") as f:
                _BOOT_TIME.append(float(next(line.split()[1] for line in f if line.startswith("btime "))))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return _BOOT_TIME[0] + ticks / os.sysconf("SC_CLK_TCK")


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


def claude_binary() -> str | None:
    return shutil.which("claude") or next((str(p) for p in (Path.home() / ".local/bin/claude", CLAUDE_DIR / "local/claude",
                                                            Path.home() / ".claude/local/claude") if p.exists()), None)


def agents_json() -> list[dict] | None:
    """Claude Code's list of its running sessions, `claude agents --json`: each with `sessionId`,
    `pid`, `cwd`, `name`, `kind` (interactive | background) and `status` (busy | waiting | idle).
    None when it cannot be asked (no claude; one too old to have it)."""
    binary = claude_binary()
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "agents", "--json"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else None


class AgentsPoller:
    """Asks `claude agents --json` at most every AGENTS_EVERY_S, on a thread of its own so a
    daemon step never waits for a process to start; only the first time it waits for the answer
    (a daemon that just started would otherwise take every session for gone). poll() hands back
    the latest answer and when it was asked."""

    def __init__(self, fetch=agents_json, every: float = AGENTS_EVERY_S):
        self.fetch, self.every = fetch, every
        self.latest: tuple[list[dict], float] = ([], 0.0)
        self._asked = 0.0
        self._running = False
        self._first = True

    def poll(self, now: float) -> tuple[list[dict], float]:
        if not self._running and now - self._asked >= self.every:
            self._asked, self._running = now, True
            if self._first:
                self._first = False
                self._ask()
            else:
                threading.Thread(target=self._ask, daemon=True).start()
        return self.latest

    def _ask(self):
        try:
            at = time.time()
            entries = self.fetch()
            if entries is not None:
                self.latest = (entries, at)
        finally:
            self._running = False


def live_sessions(entries: list[dict], table: dict[int, tuple[int, str]]) -> list[dict]:
    """The sessions of `claude agents --json` that may be on screen: a claude process still runs
    them, and it is not our own `claude -p` (turncheck.py)."""
    out = []
    for d in entries:
        try:
            pid = int(d.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or not d.get("sessionId"):
            continue
        ppid, comm = table.get(pid, (0, ""))
        if comm != "claude" or ppid == os.getpid():
            continue  # (gone since it was asked; the turn checker's)
        out.append(d)
    return out


def live_session_ids(table=None) -> set[str]:
    """Sessions with a running claude process."""
    return {s["sessionId"] for s in live_sessions(agents_json() or [], table or proc_table())}


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
    """Model a session is on: the last one its transcript names."""
    if not session_id:
        return None
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
# what names a pane (the hook's PANE_VARS), innermost first: tmux in a Terminator pane is tmux's
PANE_KEYS = ("TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID", "TERMINATOR_UUID")
WOKEN_MAX_S = 300  # Claude Code's list alone says it works again for at most this long
AGENT_QUIET_S = 1800  # a subagent not heard from for this long is taken for gone (its stop lost)


def pane_id(pane) -> str:
    """Which pane of its terminal a Claude runs in, from what the hook recorded; "" when the
    terminal does not say."""
    if not isinstance(pane, dict):
        return ""
    return next((f"{k}={pane[k]}" for k in PANE_KEYS if pane.get(k)), "")


class Detector:
    """Which Claudes each window shows, and what they are doing.

    Two sources. The hook of Sidelit's Claude Code plugin (plugin/hooks/hook.py, `sidelit hooks
    install`) reports every session's events as they happen, with the process ancestry the hook
    ran in: status straight from the events, and the window from launch ancestry or from which
    window had the keyboard when you submitted a prompt. Without the plugin (or for a session
    started before it was installed), the title Claude puts on its terminal is matched against
    Claude Code's own list of running sessions (`claude agents --json`) and the status read
    from there.

    A window can show several Claudes: split panes or tabs of one terminal window. Each is shown
    on the strongest kind of evidence any of them has for that window: if some run in the
    window's own process (BY_LAUNCH, BY_PID), those; else those you typed into there (BY_FOCUS);
    else the one whose title it shows. A Claude is shown in one window only, and in one pane
    only the newest (one suspended under another one in the same shell is not on screen).

    A box shows its main thread: subagents, and Claudes another Claude started (its own
    `claude -p`, a test harness), are only counted while they work, and never take a window.
    Nor does a Claude with no terminal (an app driving it), whose prompts are no one typing.

    `agents(now)` hands back Claude Code's list and when it was asked (AgentsPoller.poll);
    `started(pid)` when a process started, which orders the Claudes of a window."""

    def __init__(self, hook_dir: Path | None = None, ttl: float = 1.0, agents=None, started=process_started):
        from sidelit import config
        self.hook_dir = Path(hook_dir) if hook_dir else config.RUNTIME_DIR / "claude"
        self.ttl = ttl
        self.agents_source = agents or AgentsPoller().poll
        self.started_of = started
        self._at = 0.0
        self._live_at = 0.0                            # when Claude Code's list was asked
        self._quiet_at: dict[str, float] = {}          # session -> when the list last said it did not work
        self._busy_since: dict[str, float] = {}        # session -> since when the list says it works
        self.table: dict = {}
        self.sessions: list[dict] = []                 # Claude Code's list: the live ones
        self.index: dict = {}
        self.hooked: dict[str, dict] = {}              # live sessions the hook reports
        self.inner: dict[int, int] = {}                # claude pid -> Claudes it started, working now
        self.window_of: dict[str, tuple] = {}          # session -> (window id, strength, evidence time)
        self.focus: list[tuple[float, str]] = []       # (when, window id) the keyboard moved
        self.focus_since = 0.0
        self._by_window: dict[str, list[str]] = {}     # window id -> the sessions it shows, oldest first
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
        self.hooked = self._read_hooks(now)
        # Claude Code is asked only while a Claude runs (other than the turn checker's)
        ours = os.getpid()
        if any(comm == "claude" and ppid != ours for ppid, comm in self.table.values()):
            entries, at = self.agents_source(now)
        else:
            entries, at = [], self._live_at
        self.sessions = live_sessions(entries, self.table)
        self.index = session_title_index(self.sessions)
        if at != self._live_at:
            self._live_at = at
            self._note_busy(at)

    def _note_busy(self, at: float):
        """When Claude Code's list last said each session did not work, and since when it says
        it works."""
        listed = {s["sessionId"]: str(s.get("status") or "").lower() == "busy" for s in self.sessions}
        for seen in (self._quiet_at, self._busy_since):
            for sid in [s for s in seen if s not in listed]:
                del seen[sid]
        for sid, busy in listed.items():
            if not busy:
                self._quiet_at[sid] = at
                self._busy_since.pop(sid, None)
            else:
                self._busy_since.setdefault(sid, at)

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
        by_pid: dict[int, list[str]] = {}
        for wid, w in windows.items():
            if w.app in TERMINAL_APP_IDS and looks_like_claude(w.title) and title_state(w.title) != "other":
                by_title.setdefault(bare_title(w.title) or "", []).append(wid)
            if w.pid:
                by_pid.setdefault(w.pid, []).append(wid)
        for sid, rec in self.hooked.items():
            if rec["windowless"]:
                continue  # its terminal, if any, is another Claude's
            anc = set(rec.get("ancestors") or [])
            found = []
            for wid, pid in launched_pid.items():
                if pid in anc and wid in windows:
                    found.append((BY_LAUNCH, float(rec.get("started") or rec.get("t") or 0), wid))
            for pid, wids in by_pid.items():
                # (a terminal that keeps all its windows in one process says nothing about which)
                if pid in anc and len(wids) == 1:
                    found.append((BY_PID, float(rec.get("t") or 0), wids[0]))
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
        # each window shows the Claudes with the strongest kind of evidence for it; a title
        # alone names only one, the newest
        claims: dict[str, list[tuple]] = {}
        for sid, (wid, strength, t) in self.window_of.items():
            claims.setdefault(wid, []).append((strength, t, sid))
        self._by_window = {}
        for wid, cs in claims.items():
            top = max(c[0] for c in cs)
            shown = self._one_per_pane([c for c in cs if c[0] == top] if top >= BY_FOCUS else [max(cs)])
            for _, _, sid in cs:
                if sid not in shown:
                    self.window_of.pop(sid, None)
            self._by_window[wid] = sorted(shown, key=lambda s: (self._started(s), s))
        if self.window_of != before:
            self._save_windows()

    def _one_per_pane(self, claims: list[tuple]) -> list[str]:
        """The sessions of these claims, but of two in the same pane only the one heard from
        last: the other is suspended under it (Ctrl+Z, then another claude)."""
        newest: dict[str, tuple[float, str]] = {}
        out = []
        for _, _, sid in claims:
            pane = pane_id(self.hooked.get(sid, {}).get("pane"))
            if not pane:
                out.append(sid)
                continue
            heard = float(self.hooked[sid].get("t") or 0)
            if pane not in newest or heard > newest[pane][0]:
                newest[pane] = (heard, sid)
        return out + [sid for _, sid in newest.values()]

    def _started(self, sid: str) -> float:
        """When this Claude's process started (it keeps its place through /clear), else when
        the session did."""
        rec = self.hooked.get(sid) or {}
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None) or {}
        pid = int(rec.get("pid") or sess.get("pid") or 0)
        return (self.started_of(pid) if pid else None) or float(rec.get("started") or rec.get("t") or 0)

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
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None)
        live_state = LIVE_STATE.get(str(sess.get("status") or "").lower(), "done") if sess else None
        if rec is not None:
            state = rec.get("state") or "done"
            # the hook cannot see a background task waking Claude up again (no prompt, and no
            # event until it uses a tool or stops): Claude Code's list says it did not work after
            # the hook's last word, and then that it works. (Busy all along is the turn that ended,
            # still winding down: its other Stop hooks, say.) A Claude that works sends events,
            # so after WOKEN_MAX_S without one the list's word alone is taken for stale.
            heard, quiet = float(rec.get("t") or 0), self._quiet_at.get(sid, 0.0)
            woke = self._busy_since.get(sid, 0.0)
            if state == "done" and live_state == "busy" and quiet > heard and woke > quiet \
                    and self._at - woke < WOKEN_MAX_S:
                return "busy"
            if state == "done" and rec.get("shells"):
                return "shell"  # (the turn is over; a shell it started is not)
            return state if state in ("busy", "waiting", "done") else "done"
        return live_state or "done"

    def agents(self, sid: str) -> int:
        """Subagents of this session working now, and Claudes it started that work."""
        rec = self.hooked.get(sid) or {}
        sess = next((s for s in self.sessions if s.get("sessionId") == sid and s.get("pid")), None)
        pid = int(rec.get("pid") or (sess or {}).get("pid") or 0)
        heard = (rec.get("agents") or {}).values()
        return sum(1 for t in heard if self._at - float(t) < AGENT_QUIET_S) + self.inner.get(pid, 0)

    def for_window(self, title: str, pid: int = 0, wid: str | None = None) -> dict | None:
        """The first Claude this window shows (claudes_for), or None."""
        return next(iter(self.claudes_for(title, pid, wid)), None)

    def claudes_for(self, title: str, pid: int = 0, wid: str | None = None) -> list[dict]:
        """{"session", "title", "status", "agents", "cwd", "pid", "pane", "started", "how", "hooked"}
        for each Claude this window shows, in the order they started."""
        sids = self._by_window.get(wid) if wid else None
        if sids:
            return [self._info(sid, title if len(sids) == 1 else "", self.window_of.get(sid, (None, 0, 0))[1])
                    for sid in sids]
        # no mapping yet (first look, no hooks): the title, then the process tree
        sess = None
        if looks_like_claude(title) and title_state(title) != "other":
            sess = self.index.get(bare_title(title) or "")
        if sess is None and pid:
            kids = descendants([pid], self.table)
            sess = next((s for s in self.sessions if s.get("pid") and int(s["pid"]) in kids
                         and not self._windowless(s)), None)
        if sess is None or sess.get("sessionId") in self.window_of:
            return []  # (a session shown in a window we know is not also in this one)
        return [self._info(sess["sessionId"], title, BY_TITLE)]

    def _info(self, sid: str, title: str, how: int) -> dict:
        """What a box says of a Claude. `title`: the window's, when the window shows only this
        Claude (with several, it is the title of whichever pane has the keyboard)."""
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None)
        rec = self.hooked.get(sid)
        cwd = (rec or {}).get("cwd") or (sess or {}).get("cwd") or ""
        shown = bare_title(title) if looks_like_claude(title) else None
        return {"session": sid, "title": shown or transcript_title(sid, cwd) or (sess or {}).get("name") or "",
                "status": self.status(sid), "agents": self.agents(sid), "cwd": cwd,
                "pid": int((rec or sess or {}).get("pid") or 0), "pane": dict((rec or {}).get("pane") or {}),
                "started": self._started(sid), "prompt_t": float((rec or {}).get("prompt_t") or 0),
                "how": {BY_LAUNCH: "launch", BY_FOCUS: "focus", BY_TITLE: "title"}.get(how, "pid"),
                "hooked": rec is not None}
