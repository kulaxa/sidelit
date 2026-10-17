"""Does a Claude that ended its turn need you, or is it only waiting for a pipeline?

A tab turns yellow when its Claude finishes on a workspace you are not looking at. Often it
has only pushed and is waiting for CI, a deploy or a background job, and there is nothing
for you to do yet. So before the tab turns yellow, Sonnet reads the end of the turn (what
you asked, the last tool calls, what Claude said) and answers "input" (it needs you: it is
finished, stuck or asking) or "waiting" (and on what). A check that fails or takes too long
counts as "input": the tab turns yellow, as it always did.

The model runs through `claude -p` on the subscription, no API key, the way the spell checker
in ~/.claude/wpm does: no tools, no settings, no session saved. Starting claude takes 5-20 s,
so one process is started ahead while a Claude is working; each check takes it and the next
one starts. One check per process: no check sees another session's conversation. The spare
quits after IDLE_QUIT seconds with nothing working.
"""
from __future__ import annotations

import json
import os
import queue
import re
import select
import subprocess
import threading
import time
from pathlib import Path

from sidelit.claude import claude_binary

MODEL = "claude-sonnet-5-5"
CHECK_TIMEOUT = 45.0  # a check that has to start claude first waits for that too
CHECKING_MAX_S = CHECK_TIMEOUT + 15  # an answer that has not come by then is not coming
IDLE_QUIT = 600.0     # no Claude working for this long: stop the spare
TURN_FRESH_S = 900    # a turn that ended longer ago is not checked (the daemon just started)
SHELL_GRACE_S = 15    # a background shell that ends wakes Claude up: this long to show it is busy
FLUSH_WAIT_S = 1.0    # without the hook's copy of Claude's last words, give the transcript this long
STRANDED_S = 1200     # waiting with nothing that could wake Claude: yellow after this long
PARALLEL = 4          # checks at once
# tools that wake Claude up later by themselves (a background shell or agent says so in its line)
WAKERS = ("Monitor", "ScheduleWakeup", "CronCreate")
TAIL = 1 << 20        # bytes of the transcript read, from its end
ASKED_MAX = 1500      # characters of your request
TURN_MAX = 6000       # characters of the turn's end
TOOL_MAX = 160        # characters per tool call
LEAKED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_WPM_FILE")

PROMPT = """A software engineer runs several Claude Code sessions side by side, one per workspace. When a session ends its turn, its tab turns yellow to call them back. Before it does, you read the end of that turn and decide whether the session really needs them now.

Rules, in order:
1. "input" when the assistant needs something from the user: it asks a question, wants a decision, approval or information, or asks them to do something (run a command, log in, test, review). An optional offer it does not need an answer to ("want me to also...?") does not count as asking.
2. "waiting" when the work is not settled yet because something automated it started or depends on is still running and its outcome is not in: a CI pipeline, build, deploy, test run, a background shell, monitor, agent or job, a scheduled check-back. Also when it only reports that such a thing is running, or says it will check or report back once it finishes. Waiting for other people (a colleague's review) is not this: that is "input".
3. "input" otherwise: the work is finished, it answered, it failed or it is stuck.
When unsure, answer "input".

Reply with only a JSON object: {"verdict": "input"} or {"verdict": "waiting", "on": "<what it waits for, at most 4 words, like CI pipeline or prod deploy>"}.
Every message is a new, independent session: ignore earlier messages."""


# ---- the end of a turn, from the transcript ----------------------------------------------------
def _entries(path: str) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.split("\n")
    if size > TAIL:
        lines = lines[1:]  # (cut mid-line)
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and not e.get("isSidechain"):
            out.append(e)
    return out


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head].rstrip() + " … " + text[-(limit - head):].lstrip()


def _tool(block: dict) -> str:
    name = str(block.get("name") or "tool")
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    what = next((inp[k] for k in ("command", "description", "reason", "prompt", "file_path", "url", "query", "pattern")
                 if isinstance(inp.get(k), str) and inp[k].strip()), "")
    if name == "ScheduleWakeup" and inp.get("delaySeconds"):
        what = f"in {inp['delaySeconds']} s: {what}"
    background = " (in the background)" if inp.get("run_in_background") else ""
    return f"{name}{background}: {_clip(' '.join(what.split()), TOOL_MAX)}" if what else name + background


def _notice(text: str) -> str:
    """What a background task (a shell, a monitor, an agent) told Claude when it woke it up"""
    status = re.search(r"<status>(.*?)</status>", text, re.S)
    summary = re.search(r"<summary>(.*?)</summary>", text, re.S)
    return " ".join(filter(None, ((status.group(1).strip() if status else ""),
                                  _clip(summary.group(1), 300) if summary else "")))


def _prompt_text(content) -> str | None:
    """What you typed, when this user entry is a prompt (not a tool result, not Claude Code's own)"""
    if isinstance(content, str):
        return None if content.startswith(("<task-notification>", "<local-command")) else content
    if not isinstance(content, list) or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    texts = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(t for t in texts if not t.startswith("[Request interrupted"))
    if any(isinstance(b, dict) and b.get("type") == "image" for b in content):
        text += " [image]"
    return text.strip() or None


def _norm(text: str) -> str:
    return " ".join(text.split())


def last_turn(path: str, said: str | None = None) -> dict | None:
    """{"asked": your request, "events": [(who, text)] oldest first} for the transcript's last
    turn; None when there is nothing to judge. `said`: Claude's last message as the Stop hook
    had it. Claude Code may not have written it to the transcript yet when the check reads it,
    and without it the turn looks as it did halfway (a Claude waiting for a deploy that had
    just said "the configs are pushed" was taken for done)."""
    events: list[tuple[str, str]] = []
    asked = ""
    for e in reversed(_entries(path)):
        kind = e.get("type")
        content = (e.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    events.append(("Claude", block["text"].strip()))
                elif block.get("type") == "tool_use":
                    events.append(("tool", _tool(block)))
        elif kind == "attachment":
            att = e.get("attachment") or {}
            queued = att.get("prompt") if att.get("type") == "queued_command" else None
            if isinstance(queued, str) and queued.startswith("<task-notification>"):
                events.append(("background task", _notice(queued)))
            elif isinstance(queued, str) and queued.strip() and (att.get("humanTurn") or (att.get("origin") or {}).get("kind") == "human"):
                events.append(("you, during the turn", queued.strip()))
        elif kind == "user" and not e.get("isMeta"):
            if isinstance(content, str) and content.startswith("<task-notification>"):
                events.append(("background task", _notice(content)))
                continue
            text = _prompt_text(content)
            if text:
                asked = text
                break
    events.reverse()
    if said and said.strip():
        tail: list[str] = []  # what Claude said since its last tool call
        for who, text in reversed(events):
            if who != "Claude":
                break
            tail.insert(0, text)
        if _norm(said)[:300] != _norm(" ".join(tail))[:300]:
            events.append(("Claude", said.strip()))
    if not any(who in ("Claude", "tool") for who, _ in events):
        return None
    wakes = any(who == "background task" or (who == "tool" and (text.startswith(WAKERS) or "(in the background)" in text))
                for who, text in events)
    kept, size = [], 0
    for who, text in reversed(events):  # the end matters most: keep that
        text = _clip(text, TURN_MAX if who == "Claude" else 600)
        if kept and size + len(text) > TURN_MAX:
            break
        kept.append((who, text))
        size += len(text)
    kept.reverse()
    return {"asked": _clip(asked, ASKED_MAX), "events": kept, "cut": len(kept) < len(events), "wakes": wakes}


def describe(turn: dict, shell: bool = False) -> str:
    """The message the model reads."""
    parts = [f"The request:\n{turn['asked'] or '(not in view)'}", "",
             "The end of the turn, oldest first" + (" (earlier steps left out)" if turn.get("cut") else "") + ":"]
    parts += [f"[{who}] {text}" for who, text in turn["events"]]
    if shell:
        parts += ["", "Now: a background shell the assistant started is still running."]
    return "\n".join(parts)


def parse(reply: str) -> tuple[str, str] | None:
    """(verdict, on) from the model's reply; None when it is not one."""
    found = re.search(r"\{.*\}", reply or "", re.S)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("input", "waiting"):
        return None
    on = data.get("on") if isinstance(data.get("on"), str) else ""
    return data["verdict"], " ".join(on.split())[:40] if data["verdict"] == "waiting" else ""


# ---- which tab turns yellow ---------------------------------------------------------------------
class Turns:
    """The verdict on each session's last turn, and what it makes the tab show.

    A turn that ends while you are not looking would turn the tab yellow; one that leaves a
    background shell running could be waiting for it, looking or not. Such a turn shows
    "checking" (still working, to you) until the model has answered, then "external" (still
    orange, "waiting on CI pipeline") when Claude only waits, else "done". Verdicts are kept in
    `path`, so a daemon restart neither asks again nor forgets. `journal(kind, **fields)` hears
    of every tab that turns yellow without the model saying so."""

    def __init__(self, checker, path: Path | None = None, journal=None):
        self.checker = checker
        self.journal = journal or (lambda kind, **fields: None)
        self.path = Path(path) if path else None
        self.verdicts: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text()) if self.path else {}
            self.verdicts = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("verdict") != "checking"}
        except (OSError, ValueError, AttributeError):
            pass

    def state(self, sid: str, rec: dict | None, state: str, active: bool, now: float) -> tuple[str, str]:
        """(state, what it waits on) for a Claude's tab; `state` is what the hook and Claude
        Code's list of sessions say (claude.Detector), `active` whether you are looking at its
        workspace."""
        if state == "busy":
            self.checker.warm()
        turn = float((rec or {}).get("turn_end") or 0)
        if not turn or rec.get("state") != "done" or state not in ("done", "shell"):
            return state, ""
        v = self.verdicts.get(sid)
        shell = state == "shell"
        if v is None or v.get("turn") != turn or (v["verdict"] == "seen" and shell):
            # a turn that would not turn yellow needs no asking, unless something it started
            # still runs: then only the model can say whether Claude is waiting for it
            if not shell and (active or now - turn > TURN_FRESH_S):
                v = {"turn": turn, "verdict": "seen"}
            else:
                v = {"turn": turn, "verdict": "checking", "since": now, "shell": state == "shell"}
                self.checker.check((sid, turn), rec.get("transcript") or "", shell=shell, said=rec.get("said"))
            self.verdicts[sid] = v
        if v["verdict"] == "checking":
            if now - v["since"] < CHECKING_MAX_S:
                return "checking", ""
            v["verdict"] = "input"  # the answer got lost: yellow, as before
            self.journal("turn_checked", session=sid, verdict="input", on="",
                         error=f"no answer in {CHECKING_MAX_S:.0f} s", secs=round(now - v.pop("since"), 1))
        if v["verdict"] == "waiting":
            # it waits on a background shell: once that is gone, Claude wakes up (busy again)
            # or it had nothing to wake it, and then it needs you
            if v.get("shell") and not shell:
                v.setdefault("shell_gone", now)
                if now - v["shell_gone"] > SHELL_GRACE_S:
                    return self._stranded(sid, v, "the shell it waited on ended and nothing woke it")
            # nothing it started can wake it ("I'll check the pipeline", with no watcher):
            # it waits for you to come back to it, sooner or later
            elif not v.get("shell") and not v.get("wakes", True) and now - turn > STRANDED_S:
                return self._stranded(sid, v, f"waited {STRANDED_S // 60} min with nothing to wake it")
            return "external", v.get("on") or ""
        # it needs you, or you saw it end: a shell it left running (a dev server) is not
        # something it waits for
        return "done", ""

    def _stranded(self, sid: str, v: dict, why: str) -> tuple[str, str]:
        if not v.get("stranded"):
            v["stranded"] = True
            self.journal("turn_checked", session=sid, verdict="input", on=v.get("on") or "", error=why, secs=0)
        return "done", ""

    def collect(self) -> list[dict]:
        """The answers that came in since the last call, stored."""
        got = []
        while True:
            try:
                (sid, turn), verdict, on, error, secs, wakes = self.checker.results.get_nowait()
            except queue.Empty:
                break
            v = self.verdicts.get(sid)
            if v is not None and v.get("turn") == turn and v["verdict"] == "checking":
                v.update(verdict=verdict, on=on, wakes=wakes)
                v.pop("since", None)
            got.append({"session": sid, "verdict": verdict, "on": on, "error": error, "secs": secs})
        if got:
            self.save()
        return got

    def forget(self, live: set[str]):
        """Drop the verdicts of sessions that are gone."""
        for sid in [s for s in self.verdicts if s not in live]:
            del self.verdicts[sid]

    def save(self):
        if self.path is None:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.verdicts))
            tmp.replace(self.path)
        except OSError:
            pass


# ---- the model ----------------------------------------------------------------------------------
class Checker:
    """Checks turns, each on a thread of its own with a claude of its own, up to PARALLEL at
    once: when several Claudes stop together, the ones after the first do not wait in line
    while claude starts for each (5-20 s a time) and run out the clock. check() queues one;
    the answer comes back on self.results as (key, verdict, on, error, seconds, wakes) and
    notify() is called (from a worker: hand it on to the main loop). warm() says a Claude is
    working, so a check may come soon: one process is started ahead for it."""

    def __init__(self, notify, model: str = MODEL, cwd: str | None = None):
        self.notify = notify
        self.model = model
        self.cwd = cwd
        self.results: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.spare = None
        self.wanted = 0.0     # last time a Claude was working
        self._warming = False
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(PARALLEL)
        threading.Thread(target=self._work, name="turncheck", daemon=True).start()

    def warm(self):
        self.wanted = time.time()
        if not self._warming:
            self._warming = True
            self.jobs.put(None)

    def check(self, key, transcript: str, shell: bool = False, said: str | None = None):
        self.wanted = time.time()
        self.jobs.put((key, transcript, shell, said))

    def stop(self):
        self.jobs.put(False)

    def _work(self):
        """Hands out checks and keeps the spare: one ready while a Claude works, none after."""
        while True:
            try:
                job = self.jobs.get(timeout=30)
            except queue.Empty:
                job = None
            if job is False:
                with self._lock:
                    self._kill_spare()
                return
            if job is not None:
                self._slots.acquire()
                threading.Thread(target=self._run, args=(job,), name="turncheck-job", daemon=True).start()
            with self._lock:
                if time.time() - self.wanted > IDLE_QUIT:
                    self._kill_spare()
                    self._warming = False
                elif self.spare is None or self.spare.poll() is not None:
                    self.spare = self._spawn()

    def _run(self, job):
        key, transcript, shell, said = job
        started = time.time()
        try:
            verdict, on, error, wakes = self._check(transcript, shell, said)
        except Exception as exc:  # noqa: BLE001
            verdict, on, error, wakes = "input", "", f"{type(exc).__name__}: {exc}", False
        finally:
            self._slots.release()
        self.results.put((key, verdict, on, error, round(time.time() - started, 1), wakes))
        try:
            self.notify()
        except Exception:  # noqa: BLE001
            pass
        self.jobs.put(None)  # (the spare may have gone to this check: start another)

    def _take(self):
        """The spare if it is up, else a new claude."""
        with self._lock:
            proc, self.spare = self.spare, None
        return proc if proc is not None and proc.poll() is None else self._spawn()

    def _check(self, transcript: str, shell: bool, said: str | None = None) -> tuple[str, str, str | None, bool]:
        if not said:
            time.sleep(FLUSH_WAIT_S)  # (a Claude Code that does not tell the hook)
        turn = last_turn(transcript, said)
        if turn is None:
            return "input", "", "nothing to read in the transcript", False
        proc = self._take()
        if proc is None:
            return "input", "", "claude not found", turn["wakes"]
        try:
            reply = self._ask(proc, describe(turn, shell))
            got = parse(reply)
            if got is None:
                return "input", "", f"unexpected reply: {reply[:80]!r}", turn["wakes"]
            return got[0], got[1], None, turn["wakes"]
        except Exception as exc:  # noqa: BLE001 - any failure is a yellow tab, as before
            return "input", "", f"{type(exc).__name__}: {exc}", turn["wakes"]
        finally:
            proc.kill()
            proc.wait()

    def _spawn(self):
        binary = claude_binary()
        if binary is None:
            return None
        env = {k: v for k, v in os.environ.items() if k not in LEAKED_ENV}
        env["MAX_THINKING_TOKENS"] = "0"
        try:
            return subprocess.Popen(
                [binary, "-p", "--model", self.model, "--system-prompt", PROMPT,
                 "--settings", json.dumps({"alwaysThinkingEnabled": False}), "--setting-sources", "",
                 "--tools", "", "--strict-mcp-config", "--no-chrome", "--disable-slash-commands",
                 "--no-session-persistence", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, cwd=self.cwd or None, start_new_session=True)
        except OSError:
            return None  # (the check says so: "claude not found")

    def _kill_spare(self):
        if self.spare is not None:
            self.spare.kill()
            self.spare.wait()
            self.spare = None

    @staticmethod
    def _ask(proc, text: str) -> str:
        message = {"type": "user", "message": {"role": "user", "content": text}}
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
        deadline, buf, fd = time.time() + CHECK_TIMEOUT, b"", proc.stdout.fileno()
        while True:
            while b"\n" not in buf:
                if not select.select([fd], [], [], max(0.0, deadline - time.time()))[0]:
                    raise TimeoutError(f"no answer in {CHECK_TIMEOUT:.0f} s")
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
