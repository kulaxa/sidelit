"""Sidelit's Codex adapter against a real Codex CLI, by hand: not part of the unit tests.

Needs `codex` on PATH, nothing else of yours: it runs Codex in a throwaway CODEX_HOME whose
model is a stand-in served on localhost (no account, no network, nothing billed), and Sidelit's
records go to a throwaway SIDELIT_HOME. It installs Sidelit's plugin with Codex's own commands,
starts Codex in a pseudo-terminal the way you would (its shared background server and all),
trusts the plugin's hooks when Codex asks, and has it run turns: one that answers, one that
runs a command for a while. Meanwhile Sidelit's detector must find the session, its own codex
process (the server runs the hooks, so the hook cannot say which), its state as it changes,
title and model; the turn checker must get an answer through `codex exec`; resume and fork
must name the session. Then again with --no-daemon. Prints each check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/codex_live.py
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = tempfile.mkdtemp(prefix="sidelit-codex-live-")
CODEX_HOME = os.path.join(BASE, "codex-home")
WORK = os.path.join(BASE, "project")
os.makedirs(CODEX_HOME)
os.makedirs(WORK)
os.environ.update(SIDELIT_HOME=os.path.join(BASE, "sidelit"), CODEX_HOME=CODEX_HOME, FAKE_API_KEY="x",
                  PYTHONDONTWRITEBYTECODE="1")
sys.path.insert(0, ROOT)

from sidelit import codex, detect, turncheck  # noqa: E402  (after CODEX_HOME and SIDELIT_HOME)

failed = []


def check(what, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failed.append(what)


def until(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.5)
    return fn()


# ---- a stand-in for the model: the Responses API, streamed --------------------------------------
class Model(BaseHTTPRequestHandler):
    """"RUN <cmd>": runs the command with Codex's exec_command tool, then says "ran it"; Sidelit's
    turn check: says it waits on CI; a title request: a title; anything else: "Hello"."""
    n = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"models": [], "data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        last = (body.get("input") or [{}])[-1]
        text = ""
        if last.get("role") == "user":
            content = last.get("content")
            text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content or [])
        Model.n += 1
        n = Model.n
        if last.get("type") == "function_call_output":
            item = self.message(n, "ran it")
        elif text.startswith("Generate a concise, single-line task title"):
            item = self.message(n, "Say hello")
        elif "Reply with only a JSON object" in text:
            item = self.message(n, '{"verdict": "waiting", "on": "CI pipeline"}')
        elif "RUN " in text:
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "exec_command",
                    "arguments": json.dumps({"cmd": text.split("RUN ", 1)[1].strip()})}
        else:
            item = self.message(n, "Hello")
        events = [{"type": "response.created", "response": {"id": f"r{n}"}},
                  {"type": "response.output_item.done", "item": item},
                  {"type": "response.completed", "response": {"id": f"r{n}", "usage": {
                      "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for e in events:
            self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())

    @staticmethod
    def message(n, text):
        return {"type": "message", "role": "assistant", "id": f"m{n}",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
with open(os.path.join(CODEX_HOME, "config.toml"), "w") as f:
    f.write(f'model = "fake-model"\nmodel_provider = "fake"\n\n[model_providers.fake]\nname = "Fake"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\nenv_key = "FAKE_API_KEY"\n'
            f'requires_openai_auth = false\nwire_api = "responses"\n')


# ---- Codex in a pseudo-terminal --------------------------------------------------------------------
class Tui:
    """`codex` in a terminal of its own: what it writes is kept (escapes stripped), keys are typed."""

    def __init__(self, *args):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.chdir(WORK)
            os.execvp("codex", ["codex", *args])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.out, self.titles = "", []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
                data = os.read(self.fd, 65536) if r else b""
            except OSError:
                return
            if r and not data:
                return
            text = data.decode("utf-8", "replace")
            if "\x1b[6n" in text:  # where is the cursor: it waits for an answer
                os.write(self.fd, b"\x1b[1;1R")
            self.titles += re.findall(r"\x1b\][02];([^\x07\x1b]*)(?:\x07|\x1b\\)", text)
            self.out += re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]", "", text)

    def sees(self, text, secs=20, since=0):
        """Has it written `text` (after `since` characters of its output)? Spaces do not count:
        a TUI moves its cursor instead of writing many of them."""
        want = "".join(text.split())
        return until(lambda: want in "".join(self.out[since:].split()), secs)

    def type(self, text):
        for ch in text:
            os.write(self.fd, ch.encode())
            time.sleep(0.01)
        time.sleep(0.2)
        os.write(self.fd, b"\r")

    def key(self, seq):
        os.write(self.fd, seq.encode())
        time.sleep(0.3)

    def stop(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
            time.sleep(0.5)
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass


def session_of(d, tui):
    d.refresh(time.time(), force=True)
    return next(((sid, rec) for sid, rec in d.hooked.items() if rec.get("pid") == tui.pid), None)


def run(daemon: bool):
    mode = "with its background server" if daemon else "with --no-daemon"
    agent = codex.Codex(hook_dir=os.path.join(os.environ["SIDELIT_HOME"], "codex"))
    d = detect.Detector(agents=[agent], windows=os.path.join(BASE, "windows.json"))
    tui = Tui("-C", WORK, *([] if daemon else ["--no-daemon"]))
    try:
        check(f"{mode}: Codex starts", tui.sees("Ask Codex", 25), tui.out[-400:])
        if tui.sees("Trust all and continue", 5 if agent.hooks_trusted() else 30):
            mark = len(tui.out)
            tui.key("\x1b[B")
            tui.key("\r")
            tui.sees("Ask Codex", 10, since=mark)
        if os.environ.get("SHOW"):
            print(tui.out[-3000:])
        tui.type("say hi")
        found = until(lambda: session_of(d, tui), 20)
        check(f"{mode}: the detector finds the session in its own codex", bool(found),
              json.dumps({s: {k: r.get(k) for k in ("pid", "server", "state")} for s, r in d.hooked.items()}))
        if not found:
            return
        sid, rec = found
        if daemon:
            check(f"{mode}: the server ran the hook (it could not tell the terminal)", bool(rec.get("server")), str(rec))
        check(f"{mode}: it is in a terminal, on its own", not rec["windowless"], str(rec))
        check(f"{mode}: the turn ends: done", until(lambda: session_of(d, tui) and d.status(sid) == "done"
                                                     and "turn_end" in d.hooked[sid], 20), d.status(sid))
        check(f"{mode}: the model", agent.model(sid, d.hooked[sid]) == "fake-model", agent.model(sid, d.hooked[sid]))
        agent.watch([sid])
        check(f"{mode}: the title is what you asked", until(lambda: agent.title(sid, WORK, d.hooked.get(sid)) == "say hi", 20),
              agent.title(sid, WORK, d.hooked.get(sid)))
        check(f"{mode}: the terminal's title is the project", until(lambda: tui.titles and agent.owns_title(tui.titles[-1], WORK), 5),
              str(tui.titles[-3:]))
        tui.type("RUN sleep 6")
        check(f"{mode}: running a command: busy", until(lambda: session_of(d, tui) and d.status(sid) == "busy", 15), d.status(sid))
        check(f"{mode}: then done again", until(lambda: session_of(d, tui) and d.status(sid) == "done"
                                                and d.hooked[sid].get("said") == "ran it", 25), str(d.hooked.get(sid)))
        turn = agent.last_turn(d.hooked[sid])
        check(f"{mode}: the turn's end, from the hook", turn and turn["asked"] == "RUN sleep 6"
              and ("tool", "Bash: sleep 6") in turn["events"] and turn["events"][-1] == ("Codex", "ran it"), str(turn))
        proc = agent.spawn_judge(None, turncheck.prompt_for(agent), BASE)
        try:
            reply = agent.ask_judge(proc, turncheck.describe(turn), 60)
        except Exception as e:  # noqa: BLE001
            reply = f"{type(e).__name__}: {e}"
        finally:
            proc.kill()
            proc.wait()
        check(f"{mode}: the turn check answers through codex exec", turncheck.parse(reply) == ("waiting", "CI pipeline"), reply)
        check(f"{mode}: resume and fork name the session", f"codex resume {sid}" in agent.resume_command(sid, WORK)
              and f"codex fork {sid}" in agent.fork_command(sid, WORK))
        check(f"{mode}: Codex has a saved copy to resume", "||" not in agent.resume_command(sid, WORK), agent.resume_command(sid, WORK))
        tui.type("/quit")
        check(f"{mode}: once it quits, the session is gone", until(lambda: sid not in (d.refresh(time.time(), force=True)
                                                                                     or d.hooked), 15))
    finally:
        tui.stop()


try:
    said = codex.Codex().install()
    check("the plugin installs with Codex's own commands", any("installed sidelit@sidelit" in s for s in said), str(said))
    check("Codex has the plugin's hooks, not trusted yet", codex.Codex().hooks_trusted() is False)
    check("its copy is Sidelit's", not codex.stale_copy())
    run(daemon=True)
    check("after Codex asked, the hooks are trusted", codex.Codex().hooks_trusted() is True)
    run(daemon=False)
    check("no usage limits without an account (and nothing breaks)", codex.Codex().usage() is None)
    said = codex.Codex().uninstall()
    check("the plugin goes out the same way", codex.installed_plugin() is None and codex.marketplace_root() is None, str(said))
finally:
    subprocess.run(["codex", "app-server", "daemon", "stop"], capture_output=True, timeout=30)
    for pid in [p for p in os.listdir("/proc") if p.isdigit()]:  # (its update loop outlives the stop)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if CODEX_HOME.encode() in f.read():
                    os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    server.shutdown()
    shutil.rmtree(BASE, ignore_errors=True)
print("all good" if not failed else f"{len(failed)} failed: {failed}")
sys.exit(1 if failed else 0)
