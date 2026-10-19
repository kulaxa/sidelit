"""Sidelit's OpenCode adapter against a real OpenCode, by hand: not part of the unit tests.

Needs `opencode` on PATH (or SIDELIT_OPENCODE=/path/to/opencode), OpenCode 1 or 2, nothing else
of yours: it runs OpenCode with a throwaway home (its config, data, state and cache; OpenCode 2's
background server on a port of its own) whose model is a stand-in served on localhost (no
account, nothing billed; OpenCode fetches its OpenAI-compatible provider package once), and
Sidelit's records go to a throwaway SIDELIT_HOME. It puts Sidelit's plugin in OpenCode's plugin
folder, starts OpenCode in a pseudo-terminal the way you would, and has it run turns: one that
answers, one that runs a command for a while, one that asks your permission. Sidelit's detector
must find the session in its own opencode, its state as it changes, its title (also in the
terminal's), model and last words; the turn check must answer through `opencode run` and leave
no session behind; `opencode -s` must be on screen before you type (also with its record gone,
as after a reboot), and a fork (Sidelit's own fork command) a session of its own. Prints each
check; exits 1 if any failed.

    PYTHONPATH=. python3 tests/opencode_live.py
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
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.environ.get("SIDELIT_OPENCODE") or shutil.which("opencode")
if not BINARY:
    sys.exit("opencode not found: put it on PATH, or SIDELIT_OPENCODE=/path/to/opencode")
BASE = tempfile.mkdtemp(prefix="sidelit-opencode-live-")
WORK = os.path.join(BASE, "project")
os.makedirs(WORK)
os.makedirs(os.path.join(BASE, "home"))  # (OpenCode 2's background server starts in it)
os.environ.update(HOME=os.path.join(BASE, "home"), XDG_CONFIG_HOME=os.path.join(BASE, "config"),
                  XDG_DATA_HOME=os.path.join(BASE, "data"), XDG_STATE_HOME=os.path.join(BASE, "state"),
                  XDG_CACHE_HOME=os.path.join(BASE, "cache"), SIDELIT_HOME=os.path.join(BASE, "sidelit"),
                  OPENCODE_DISABLE_AUTOUPDATE="1", PYTHONDONTWRITEBYTECODE="1",
                  PATH=os.path.dirname(os.path.abspath(BINARY)) + os.pathsep + os.environ.get("PATH", ""))
sys.path.insert(0, ROOT)

from sidelit import detect, opencode, turncheck  # noqa: E402  (after the environment: it names the folders)

MAJOR = opencode.major(BINARY)
SHELL_TOOL = "bash" if MAJOR == 1 else "shell"
print(f"OpenCode {MAJOR} ({BINARY})", flush=True)
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


# ---- a stand-in for the model: an OpenAI-compatible chat API, streamed --------------------------
class Model(BaseHTTPRequestHandler):
    """"RUN <cmd>": runs it with OpenCode's bash tool, then says "ran it"; Sidelit's turn check:
    says it waits on CI; a title request: "Fix the pricing test"; anything else: "Hello"."""
    n = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"object": "list", "data": [{"id": "fake-model", "object": "model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        msgs = body.get("messages") or []
        text_of = lambda m: m.get("content") if isinstance(m.get("content"), str) else " ".join(  # noqa: E731
            p.get("text", "") for p in m.get("content") or [] if isinstance(p, dict))
        last = msgs[-1] if msgs else {}
        text = text_of(last) if last.get("role") == "user" else ""
        system = " ".join(text_of(m) for m in msgs if m.get("role") == "system")
        Model.n += 1
        n = Model.n
        if last.get("role") == "tool":
            deltas = [{"content": "ran it"}]
        elif "title" in system.lower() and "generat" in system.lower():
            deltas = [{"content": "Fix the pricing test"}]
        elif "Reply with only a JSON object" in text:
            deltas = [{"content": '{"verdict": "waiting", "on": "CI pipeline"}'}]
        elif "RUN " in text:
            cmd = text.split("RUN ", 1)[1].strip()
            deltas = [{"tool_calls": [{"index": 0, "id": f"call_{n}", "type": "function",
                                       "function": {"name": SHELL_TOOL, "arguments": json.dumps(
                                           {"command": cmd, **({"description": "run it"} if MAJOR == 1 else {})})}}]}]
        else:
            deltas = [{"content": "Hello"}]
        finish = "tool_calls" if "tool_calls" in deltas[0] else "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {"id": f"c{n}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "fake-model"}
        chunks = [{"role": "assistant"}, *deltas]
        for d in chunks:
            self.wfile.write(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': d}]})}\n\n".encode())
        end = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.makedirs(os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode"), exist_ok=True)  # (--version may make it)
with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode", "opencode.json"), "w") as f:
    json.dump({"autoupdate": False, "model": "fake/fake-model", "small_model": "fake/fake-model",
               **({"share": "disabled", "permission": {"bash": {"*": "allow", "rm *": "ask"}}} if MAJOR == 1 else
                  {"permissions": [{"action": "shell", "resource": "*", "effect": "allow"},
                                   {"action": "shell", "resource": "rm *", "effect": "ask"}]}),
               "provider": {"fake": {"npm": "@ai-sdk/openai-compatible", "name": "Fake",
                                     "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "x"},
                                     "models": {"fake-model": {"name": "Fake model"}}}}}, f)
if MAJOR == 2:  # (its background server on a port of its own: never yours, nor another test's)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode", "service.json"), "w") as f:
        json.dump({"port": port}, f)


# ---- OpenCode in a pseudo-terminal ------------------------------------------------------------------
class Tui:
    """`opencode` in a terminal of its own: what it writes is kept (escapes stripped), keys are typed."""

    def __init__(self, *args, shell=None):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.chdir(WORK)
            if shell:  # (a command line Sidelit would run in a terminal)
                os.execv("/bin/sh", ["/bin/sh", "-c", shell])
            os.execv(BINARY, [BINARY, *args])
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
            self.out += re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]", "", text)

    def sees(self, text, secs=20):
        want = "".join(text.split())
        return until(lambda: want in "".join(self.out.split()), secs)

    def settle(self, quiet=2.0, secs=30):
        """Until it has drawn nothing new for `quiet` seconds."""
        end, size, since = time.time() + secs, -1, time.time()
        while time.time() < end:
            if len(self.out) != size:
                size, since = len(self.out), time.time()
            elif time.time() - since >= quiet:
                return
            time.sleep(0.2)

    def type(self, text):
        for ch in text:
            os.write(self.fd, ch.encode())
            time.sleep(0.01)
        time.sleep(0.3)
        os.write(self.fd, b"\r")

    def stop(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
            time.sleep(1)
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


agent = opencode.OpenCode(hook_dir=os.path.join(os.environ["SIDELIT_HOME"], "opencode"))
d = detect.Detector(agents=[agent], windows=os.path.join(BASE, "windows.json"))


def shown(tui):
    """The session the detector sees in this opencode (or one it started), and its record."""
    d.refresh(time.time(), force=True)
    return next(((sid, rec) for sid, rec in d.hooked.items()
                 if rec.get("pid") == tui.pid or tui.pid in (rec.get("ancestors") or [])), None)


def state_is(tui, sid, state, **want):
    got = shown(tui)
    return got and got[0] == sid and d.status(sid) == state and all(got[1].get(k) == v for k, v in want.items())


def sessions():
    """OpenCode's sessions of the folder the turn check runs in."""
    out = subprocess.run([BINARY, "session", "list", "--format", "json"], capture_output=True, text=True, timeout=60,
                         cwd=BASE).stdout
    try:
        listed = json.loads(out or "[]")
    except ValueError:
        return None
    listed = listed.get("data", listed) if isinstance(listed, dict) else listed
    return sorted(s.get("id") for s in listed if isinstance(s, dict)) if isinstance(listed, list) else None


tuis = []
try:
    said = opencode.OpenCode().install()
    check("the plugin goes into OpenCode's plugin folder", opencode.installed_copy().exists() and
          opencode.OpenCode().status()[0], str(said))
    tui = Tui()
    tuis.append(tui)
    check("OpenCode starts", tui.sees("Ask anything", 60), tui.out[-400:])
    tui.type("say hi")
    found = until(lambda: shown(tui), 30)
    check("the detector finds the session in its own opencode", bool(found), str(d.hooked))
    if found:
        sid, rec = found
        check("it is in a terminal, on its own", not rec["windowless"])
        check("the turn ends: done, with its last words", until(lambda: state_is(tui, sid, "done", said="Hello"), 20),
              str(d.hooked.get(sid)))
        check("the title OpenCode gave it", until(lambda: agent.title(sid, WORK, (shown(tui) or (0, {}))[1])
                                                  == "Fix the pricing test", 20), str(d.hooked.get(sid)))
        check("the model", agent.model(sid, d.hooked[sid]) == "Fake Model", str(d.hooked.get(sid)))
        check("the terminal's title is the session's", until(lambda: tui.titles and agent.window_key(tui.titles[-1])
                                                             == agent.title_key(sid, d.hooked[sid]), 10), str(tui.titles[-3:]))
        tui.type("RUN sleep 6")
        check("running a command: busy", until(lambda: state_is(tui, sid, "busy"), 15), d.status(sid))
        check("then done again", until(lambda: state_is(tui, sid, "done", said="ran it"), 25), str(d.hooked.get(sid)))
        turn = agent.last_turn(d.hooked[sid])
        check("the turn's end, from the plugin", turn and turn["asked"] == "RUN sleep 6"
              and ("tool", f"{SHELL_TOOL}: sleep 6") in turn["events"] and turn["events"][-1] == ("OpenCode", "ran it"),
              str(turn))
        tui.type(f"RUN rm -rf {WORK}/nothing")
        check("a permission it asks for waits for you", until(lambda: state_is(tui, sid, "waiting"), 20), d.status(sid))
        time.sleep(1)
        os.write(tui.fd, b"\r")  # (allow once)
        check("answered, it goes on and ends", until(lambda: state_is(tui, sid, "done", said="ran it"), 25), d.status(sid))
        judged = []
        forget = agent._forget
        agent._forget = lambda proc, session: (judged.append(session), forget(proc, session))
        proc = agent.spawn_judge(None, turncheck.prompt_for(agent), BASE)
        try:
            reply = agent.ask_judge(proc, turncheck.describe(turn), 90)
        except Exception as e:  # noqa: BLE001
            reply = f"{type(e).__name__}: {e}"
        finally:
            proc.kill()
            proc.wait()
            if hasattr(proc, "sidelit_forgetting"):  # (its session is deleted on a thread of its own)
                proc.sidelit_forgetting.join(90)
            agent._forget = forget
        check("the turn check answers through opencode run", turncheck.parse(reply) == ("waiting", "CI pipeline"), reply)
        left = sessions()
        check("and leaves no session of its own", judged and left is not None and judged[0] not in left,
              f"{judged} in {left}")
        check("nor any record of its own", not any(f.startswith(tuple(judged)) for f in os.listdir(agent.hook_dir)),
              str(os.listdir(agent.hook_dir)))
        tui.stop()
        check("once it quits, the session is gone", until(lambda: sid not in (d.refresh(time.time(), force=True)
                                                                             or d.hooked), 15))
        again = Tui("-s", sid)
        tuis.append(again)
        check("resumed (opencode -s), it is on screen before you type", until(lambda: state_is(again, sid, "done"), 30),
              str(d.hooked))
        again.stop()
        until(lambda: sid not in (d.refresh(time.time(), force=True) or d.hooked), 15)
        os.remove(os.path.join(agent.hook_dir, f"{sid}.json"))  # (as after a reboot: the runtime folder is new)
        again = Tui("-s", sid)
        tuis.append(again)
        check("so it is with no record of it left", until(lambda: state_is(again, sid, "done"), 30), str(d.hooked))
        again.stop()
        until(lambda: sid not in (d.refresh(time.time(), force=True) or d.hooked), 15)
        command = agent.fork_command(sid, WORK)
        fork = Tui(shell=command.split(" -c '", 1)[1].rsplit("; exec ", 1)[0])  # (what the terminal would run)
        tuis.append(fork)
        fork.sees("Ask anything", 30)
        got = until(lambda: (shown(fork) or ("", {}))[0] not in ("", sid) and shown(fork), 30)
        check("forked (Sidelit's fork command), a session of its own, on screen", bool(got), f"{command} {d.hooked}")
        fork.settle()  # (it draws the conversation so far first)
        fork.type("say hi")
        check("what you type there is its own", bool(got) and until(
            lambda: (shown(fork) or ("", {}))[0] == got[0] and shown(fork)[1].get("asked") == "say hi", 30), str(d.hooked))
        fork.stop()
    said = opencode.OpenCode().uninstall()
    check("the plugin comes out again", not opencode.installed_copy().exists(), str(said))
finally:
    for t in tuis:
        t.stop()
    if MAJOR == 2:  # (the background server the first terminal started)
        subprocess.run([BINARY, "service", "stop"], capture_output=True, timeout=60)
    server.shutdown()
    shutil.rmtree(BASE, ignore_errors=True)
print("all good" if not failed else f"{len(failed)} failed: {failed}")
sys.exit(1 if failed else 0)
