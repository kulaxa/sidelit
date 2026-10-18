"""Claude Code, behind the agent interface (agents.py): what it tells us about its sessions, the
ways it offers.

Its own list of running sessions, `claude agents --json` (pid, cwd, sessionId, name, status),
which its docs call the supported way to read session state from outside Claude Code; and the
transcripts under ~/.claude/projects/, a documented location (the title Claude shows, the
model). The events of each session come from Sidelit's plugin (plugin/hooks/hook.py), which
Claude Code installs with its own `claude plugin` commands (hooks.py); the plan's usage limits
from the same plugin's mod (plugin/hooks/usage.js). The turn check runs `claude -p`.
"""
from __future__ import annotations

import glob
import json
import os
import re
import select
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from sidelit.agents import Agent
from sidelit.core.titles import bare_title, looks_like_claude, title_state
from sidelit.procs import load_json, proc_table, shell_command  # noqa: F401 (load_json: for importers)

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
PROJECTS = CLAUDE_DIR / "projects"
AGENTS_EVERY_S = 10.0  # how often Claude Code is asked for its sessions (it starts a process)
FLUSH_WAIT_S = 1.0     # without the hook's copy of Claude's last words, give the transcript this long
# what a judging `claude -p` must not see from the session that started Sidelit
LEAKED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_WPM_FILE")


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


def resume_command(session_id: str, cwd: str) -> str:
    """Resume a session; one that never got a message has no transcript, so resume the
    newest session of its directory instead, or start a fresh Claude there."""
    if has_transcript(session_id):
        return shell_command(f"claude --resume {session_id}")
    fallback = newest_transcript(cwd) if cwd else None
    if fallback:
        return shell_command(f"claude --resume {fallback}")
    return shell_command("claude")


class ClaudeCode(Agent):
    """Claude Code (see the module docstring)."""
    name, label, short, comm, command = "claude", "Claude Code", "Claude", "claude", "claude"
    marks = ("✳", "◐")  # the glyphs Claude Code itself puts in its terminal's title
    window_title = True
    usage_name = "usage.json"  # (plugin/hooks/usage.js writes it)
    after_install = ("  Claude Code sessions started from now on report their status and window to Sidelit",
                     "  (one already running: type /reload-plugins in it); until then they are followed by their",
                     "  terminal title.")

    def __init__(self, hook_dir: Path | None = None, poll=None):
        super().__init__(hook_dir)
        self._poll = poll

    def binary(self) -> str | None:
        return claude_binary()

    # ---- the plugin (hooks.py: Claude Code's `claude plugin` commands)
    def install(self) -> list[str]:
        from sidelit import hooks
        return hooks.install()

    def uninstall(self) -> list[str]:
        from sidelit import hooks
        return hooks.uninstall()

    def status(self) -> tuple[bool, list[str]]:
        from sidelit import hooks
        return hooks.status()

    def upgrade(self) -> list[str]:
        from sidelit import hooks
        return hooks.upgrade()

    # ---- sessions
    def live_sessions(self, now: float, table: dict) -> tuple[list[dict], float | None]:
        """`claude agents --json`, asked only while a Claude runs (other than the turn checker's)."""
        ours = os.getpid()
        if not any(comm == "claude" and ppid != ours for ppid, comm in table.values()):
            return [], None
        if self._poll is None:
            self._poll = AgentsPoller().poll
        entries, at = self._poll(now)
        return live_sessions(entries, table), at

    def title(self, session, cwd, rec=None):
        return transcript_title(session, cwd)

    def title_key(self, session, rec):
        return transcript_title(session, rec.get("cwd") or "")

    def window_key(self, title):
        return (bare_title(title) or "") if looks_like_claude(title) and title_state(title) != "other" else None

    def focus_key(self, title):
        return bare_title(title) if looks_like_claude(title) else None

    def unmatched_title(self, title):
        return looks_like_claude(title)

    def owns_title(self, title, cwd=""):
        return looks_like_claude(title)

    def model(self, session, rec):
        return session_model(session)

    def resume_command(self, session, cwd):
        return resume_command(session, cwd)

    def fork_command(self, session, cwd):
        return shell_command(f"claude --resume {session} --fork-session")

    # ---- the turn check: `claude -p` on the subscription, the way the spell checker in
    # ~/.claude/wpm does: no tools, no settings, no session saved
    def last_turn(self, rec):
        from sidelit.turncheck import last_turn
        if not rec.get("said"):
            time.sleep(FLUSH_WAIT_S)  # (a Claude Code that does not tell the hook)
        return last_turn(rec.get("transcript") or "", rec.get("said"))

    def check_model(self, cfg):
        from sidelit import turncheck
        return cfg.get("check_model") or turncheck.MODEL

    def spawn_judge(self, model, system, cwd):
        binary = claude_binary()
        if binary is None:
            return None
        env = {k: v for k, v in os.environ.items() if k not in LEAKED_ENV}
        env["MAX_THINKING_TOKENS"] = "0"
        try:
            return subprocess.Popen(
                [binary, "-p", "--model", model, "--system-prompt", system,
                 "--settings", json.dumps({"alwaysThinkingEnabled": False}), "--setting-sources", "",
                 "--tools", "", "--strict-mcp-config", "--no-chrome", "--disable-slash-commands",
                 "--no-session-persistence", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, cwd=cwd or None, start_new_session=True)
        except OSError:
            return None  # (the check says so: "claude not found")

    def ask_judge(self, proc, text, timeout):
        message = {"type": "user", "message": {"role": "user", "content": text}}
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
        deadline, buf, fd = time.time() + timeout, b"", proc.stdout.fileno()
        while True:
            while b"\n" not in buf:
                if not select.select([fd], [], [], max(0.0, deadline - time.time()))[0]:
                    raise TimeoutError(f"no answer in {timeout:.0f} s")
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError("claude -p exited")
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise RuntimeError(str(event.get("result"))[:80])
                return str(event.get("result") or "")
