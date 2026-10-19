"""OpenCode, behind the agent interface (agents.py).

Two OpenCodes are in use, and `opencode` may be either: OpenCode 1 (1.x, what opencode.ai/install
and npm's opencode-ai install) and OpenCode 2 (2.x, opencode.ai/v2/install, Arch's package).
`opencode --version` tells them apart (major()).

What OpenCode offers, and how Sidelit uses it:
- Plugins: a JavaScript file in its plugin folder (~/.config/opencode/plugins/), which it loads
  by itself. Sidelit's (plugin/opencode/sidelit.js) is one file for both: OpenCode 1 runs it in
  each opencode it starts, a terminal's or `opencode run`'s, OpenCode 2 in its server. It turns
  OpenCode's events into the record Sidelit's hook writes for the other agents, in
  $XDG_RUNTIME_DIR/sidelit/opencode/. `sidelit hooks install` copies the file there, the way
  OpenCode's docs say to install one; `sidelit start` refreshes the copy after an update. Nothing
  of OpenCode's settings is touched.
- OpenCode 2's terminals share one background server (`opencode serve --service`, started by the
  first of them), which runs the plugin: like Codex's, its records cannot say which terminal shows
  a session, and resolve() finds its opencode (agents.TerminalClients). A session opened with
  `opencode -s <id>` says nothing until you type in it: OpenCode 1's plugin reports it at once
  from inside it, for OpenCode 2 resolve() does, from the terminal's command line.
- Its terminal's title is "OC | <the session's title, cut at 40>" ("OpenCode" with none open).
- `opencode -s <id>` resumes a session. OpenCode 1 forks one with `--fork`; OpenCode 2 through
  its server (`opencode api POST /api/session/<id>/fork`), then opens the copy.
- `opencode run --format json` is its headless mode, for the turn check: with every tool off,
  without plugins (OpenCode 1's --pure; OpenCode 2: none but its own, on a server of the check's
  own, --standalone), and the check's session deleted after.
- It has no usage limits of its own (it works with any provider): no bars.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from sidelit import claude, codex
from sidelit.agents import Agent, TerminalClients, turn_from_record
from sidelit.procs import cmdline, cwd_of, in_terminal, process_started, shell_command

PLUGIN_NAME = "sidelit.js"
MARK = "// Sidelit's OpenCode plugin"  # (its first line: a file of that name that is not ours stays)
OFFERED_FILE = "opencode-plugin-offered"  # (in ~/.config/sidelit: sidelit start put the plugin in once)
TITLE_PREFIX = "OC | "
HOME_TITLE = "OpenCode"
TITLE_MAX = 40  # (a longer title is cut to 37 characters and "…" in its terminal's)
# the turn check: nothing it may do, nothing of yours it loads, its session deleted after
JUDGE_CONFIG = {1: {"permission": {"*": "deny"}, "tools": {"*": False}},
                2: {"permissions": [{"action": "*", "resource": "*", "effect": "deny"}], "plugins": ["-*", "opencode.*"]}}
JUDGE_FLAGS = {1: ["--pure"], 2: ["--standalone"]}
JUDGE_TITLE = "Sidelit turn check"
SESSION_ID = re.compile(r"[\w-]{1,100}")
# opencode's subcommands, OpenCode 1's and 2's: with one it is no terminal UI ("mini" is
# OpenCode 2's small one, and `run` a session in the terminal while it runs)
SUBCOMMANDS = {"acp", "agent", "api", "attach", "auth", "console", "db", "debug", "export", "generate", "github",
               "import", "mcp", "models", "plugin", "pair", "pr", "providers", "reload", "serve", "service", "session",
               "stats", "uninstall", "update", "upgrade", "web"}
ON_SCREEN = {"", "mini", "run"}
VALUE_FLAGS = {"-s", "--session", "-m", "--model", "--prompt", "--agent", "--server", "--port", "--hostname",
               "--log-level", "--completions", "--replay-limit", "--title", "--format", "-f", "--file"}
OPENED_AFTER_S = 3.0  # (OpenCode 1's plugin reports a session it was started for by then)

_MAJOR: dict[tuple[str, float], int] = {}


def pretty_model(model: str) -> str:
    """A model as its box shows it: Claude's and GPT's as Claude Code and Codex show them, any
    other's words capitalised ('kimi-k2.5' -> 'Kimi K2.5', 'qwen3-coder' -> 'Qwen3 Coder')."""
    model = model.rsplit("/", 1)[-1]
    if model.startswith("claude-"):
        return claude.pretty_model(model)
    if model.lower().startswith("gpt-"):
        return codex.pretty_model(model)
    return " ".join(w.upper() if w.lower() in ("glm", "oss") else w[:1].upper() + w[1:]
                    for w in model.replace("_", "-").split("-") if w)


def opencode_binary() -> str | None:
    return shutil.which("opencode") or next(
        (str(p) for p in (Path.home() / ".opencode/bin/opencode", Path.home() / ".local/bin/opencode") if p.exists()), None)


def version_of(binary: str | None = None) -> int | None:
    """Which OpenCode `opencode` is, 1 or 2: `opencode --version` says "1.19.3", or "opencode
    v2.0.32"; None when it cannot say. Asked once per binary (as long as it answers)."""
    binary = binary or opencode_binary()
    try:
        key = (binary, os.stat(binary).st_mtime) if binary else None
    except OSError:
        return None
    if key is None:
        return None
    if key not in _MAJOR:
        try:
            said = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10,
                                  stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            said = ""
        found = re.search(r"(\d+)\.\d+\.\d+", said or "")
        if not found:
            return None  # (asked again next time)
        _MAJOR[key] = 2 if int(found.group(1)) >= 2 else 1
    return _MAJOR[key]


def major(binary: str | None = None) -> int:
    """version_of(), 1 (the one most have) when it cannot say."""
    return version_of(binary) or 1


def fork_here(session: str) -> int:
    """`sidelit opencode-fork <id>`: what a fork of an OpenCode session runs, in the terminal it
    opens, so that it is your shell's opencode that is asked which OpenCode it is. OpenCode 1
    forks with `opencode -s <id> --fork`. OpenCode 2's terminal cannot fork: its server does,
    and the copy is opened. A fork that fails says so, and a new session starts there."""
    binary = shutil.which("opencode") or opencode_binary()
    if not binary or not SESSION_ID.fullmatch(session or ""):
        print(f"sidelit: {'opencode not found' if not binary else 'not a session id: ' + repr(session)}", file=sys.stderr)
        return 1
    version = version_of(binary)
    if version == 1:
        os.execv(binary, [binary, "-s", session, "--fork"])
    try:
        r = subprocess.run([binary, "api", "POST", f"/api/session/{session}/fork", "-d", "{}"], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        copy = (json.loads(r.stdout or "{}").get("data") or {}).get("id")
        why = (r.stderr or r.stdout or "").strip()[:300]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as e:
        copy, why = None, str(e)
    if isinstance(copy, str) and SESSION_ID.fullmatch(copy):
        os.execv(binary, [binary, "-s", copy])
    if version is None:  # (it would not say which it is, and is not OpenCode 2: 1's way)
        os.execv(binary, [binary, "-s", session, "--fork"])
    print(f"sidelit: OpenCode could not fork that session ({why or 'no answer'}); a new one instead", file=sys.stderr)
    os.execv(binary, [binary])


def judge_env(version: int) -> dict:
    return {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(JUDGE_CONFIG[version])}


def plugin_source() -> Path:
    """Sidelit's OpenCode plugin: the checkout's, or the package's /usr/share/sidelit/plugin/opencode."""
    return Path(__file__).resolve().parent.parent / "plugin" / "opencode" / PLUGIN_NAME


def plugins_dir() -> Path:
    """OpenCode's global plugin folder, from which it loads every file by itself."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode" / "plugins"


def installed_copy() -> Path:
    return plugins_dir() / PLUGIN_NAME


def ours(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().startswith(MARK)
    except OSError:
        return False


def stale_copy() -> bool:
    """Is our plugin in OpenCode's folder, and not what this Sidelit has?"""
    path = installed_copy()
    if not ours(path):
        return False
    try:
        return path.read_bytes() != plugin_source().read_bytes()
    except OSError:
        return True


def copy_plugin() -> str:
    source, dest = plugin_source(), installed_copy()
    if not source.exists():
        raise RuntimeError(f"{source} is missing: is this Sidelit's checkout or package?")
    if dest.exists() and not ours(dest):
        raise RuntimeError(f"{dest} is there already and is not Sidelit's: not replacing it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".sidelit.tmp")  # (OpenCode loads .js and .ts files only)
    tmp.write_bytes(source.read_bytes())
    tmp.replace(dest)
    return f"put Sidelit's plugin in OpenCode's plugin folder ({dest})"


def command_of(args: list[str]) -> tuple[str, str, str]:
    """What an opencode command line is: (its subcommand, "" for the terminal UI; the folder it
    was given, if any; the session it was started for, if any)."""
    words, session, value_of = [], "", None
    for a in args[1:]:
        if value_of is not None:
            if value_of in ("-s", "--session"):
                session = a
            value_of = None
        elif a in VALUE_FLAGS:
            value_of = a
        elif a.startswith("--session="):
            session = a.split("=", 1)[1]
        elif not a.startswith("-"):
            words.append(a)
    sub = words[0] if words and words[0] in SUBCOMMANDS | {"mini", "run"} else ""
    rest = words[1:] if sub else words
    return sub, (rest[0] if rest and sub in ("", "mini") else ""), session


class OpenCode(Agent):
    """OpenCode (see the module docstring)."""
    name, label, short, comm, command = "opencode", "OpenCode", "OpenCode", "opencode", "opencode"
    marks = ("◎", "◎")
    window_title = False  # (its terminal's title is the session's, after "OC | ": window_key reads it)
    after_install = ("  OpenCode sessions report their status and window to Sidelit from now on (with "
                     "OpenCode 1, one already running once you start it again).",)

    def __init__(self, hook_dir: Path | None = None, started=process_started):
        super().__init__(hook_dir)
        self.terminals = TerminalClients(self, started)  # (OpenCode 2's: the opencode processes in terminals)
        self._opened: set[tuple[int, float]] = set()  # (the terminals whose session resolve() reported)

    def binary(self):
        return opencode_binary()

    # ---- the plugin: a file in OpenCode's plugin folder
    def install(self):
        said = [copy_plugin()]
        try:  # (from now on it is yours to keep or take out: sidelit start does not put it back)
            from sidelit import config
            config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (config.CONFIG_DIR / OFFERED_FILE).write_text("Sidelit's plugin went into OpenCode (sidelit hooks install|uninstall)\n")
        except OSError:
            pass
        return said

    def uninstall(self):
        path = installed_copy()
        if not ours(path):
            return []
        path.unlink()
        return [f"took Sidelit's plugin out of OpenCode's plugin folder ({path})"]

    def status(self):
        path = installed_copy()
        if not ours(path):
            return False, ["Sidelit's OpenCode plugin is not installed (sidelit hooks install)"]
        said = [f"Sidelit's OpenCode plugin is installed ({path})"]
        if stale_copy():
            said.append("! it is an older Sidelit's (sidelit hooks install, or sidelit start, refreshes it)")
        return not stale_copy(), said

    def upgrade(self):
        """`sidelit start`: a Sidelit from before OpenCode was supported puts the plugin in, once
        (taken out again, it stays out); an update refreshes it."""
        from sidelit import config
        if opencode_binary() is None:
            return []
        if not installed_copy().exists() and not (config.CONFIG_DIR / OFFERED_FILE).exists():
            if config.DRY_RUN:
                return ["OpenCode is here: Sidelit's plugin would go in (dry run: nothing is changed)"]
            return ["OpenCode is here: putting Sidelit's plugin in"] + [f"  ✓ {line}" for line in self.install()] \
                + list(self.after_install)
        if not stale_copy():
            return []
        if config.DRY_RUN:
            return ["OpenCode has an older Sidelit's plugin (dry run: not refreshed)"]
        return ["refreshed Sidelit's plugin in OpenCode", f"  ✓ {copy_plugin()}"]

    # ---- which process shows a session (OpenCode 2's server writes the records)
    def clients(self, table: dict) -> list[int]:
        """The opencode processes in terminals that show sessions: its terminal UI, OpenCode 2's
        small one, and `opencode run` while it runs."""
        return [pid for pid, (_, comm) in table.items()
                if comm == self.comm and command_of(cmdline(pid) or ["opencode", "serve"])[0] in ON_SCREEN
                and in_terminal(pid)]

    @staticmethod
    def folder_of(pid: int) -> str:
        """Where an opencode works: the folder it was given, else the one it was started in."""
        here = cwd_of(pid)
        given = command_of(cmdline(pid))[1]
        return os.path.realpath(os.path.join(here, given) if given else here) if here else ""

    def resolve(self, records, table):
        """A record OpenCode 2's server wrote gets its session's opencode (TerminalClients). And
        an opencode started for a session (`opencode -s <id>`, as a restore does) shows it from
        the start: with no record of it yet, one is written, done. Once per opencode: a session
        deleted in it since stays gone."""
        self.terminals.resolve(records, table)
        for pid in self.terminals.live:
            started, folder, args, _ = self.terminals.seen[pid]
            sub, _, session = command_of(args)
            if sub or not session or "--fork" in args or not SESSION_ID.fullmatch(session) \
                    or time.time() - started < OPENED_AFTER_S or (pid, started) in self._opened:
                continue
            self._opened.add((pid, started))
            path = self.hook_dir / f"{session}.json"
            if path.exists() or not ours(installed_copy()):
                continue
            rec = {"session": session, "event": "opened", "server": True, "pid": 0, "t": time.time(),
                   "started": started, "cwd": folder, "state": "done", "agents": {}}
            try:
                tmp = path.with_name(path.name + ".sidelit.tmp")
                tmp.write_text(json.dumps(rec))
                tmp.replace(path)
            except OSError:
                pass

    # ---- titles
    def title(self, session, cwd, rec=None):
        rec = rec or {}
        return rec.get("title") or rec.get("first_prompt") or None

    def title_key(self, session, rec):
        title = rec.get("title")
        if not title:
            return None
        # (cut as OpenCode's JavaScript cuts it: in UTF-16 units, so an emoji counts two)
        units = title.encode("utf-16-le")
        if len(units) <= 2 * TITLE_MAX:
            return title
        return units[:2 * (TITLE_MAX - 3)].decode("utf-16-le", "ignore") + "…"

    def window_key(self, title):
        if title.startswith(TITLE_PREFIX):
            return title[len(TITLE_PREFIX):].strip()
        return "" if title.strip() == HOME_TITLE else None

    def owns_title(self, title, cwd=""):
        return self.window_key(title or "") is not None

    def unmatched_title(self, title):
        return bool(title) and title.startswith(TITLE_PREFIX)

    def model(self, session, rec):
        model = (rec or {}).get("model")
        return pretty_model(model) if isinstance(model, str) and model else None

    # ---- resume, fork
    def resume_command(self, session, cwd):
        """(one OpenCode does not know any more, deleted say, starts a fresh one there)"""
        if not SESSION_ID.fullmatch(session or ""):
            return None
        return shell_command(f"opencode -s {session} || opencode")

    def fork_command(self, session, cwd):
        """Sidelit's own `opencode-fork` (fork_here), in the new terminal: which OpenCode it is
        is asked there, of the opencode your shell finds."""
        if not SESSION_ID.fullmatch(session or ""):
            return None
        root = Path(__file__).resolve().parent.parent  # (the folder the sidelit package is in)
        return shell_command(f'env PYTHONPATH="{root}" "{sys.executable}" -m sidelit opencode-fork {session}')

    # ---- the turn check: `opencode run`, from what the plugin kept of the turn
    def last_turn(self, rec):
        return turn_from_record(rec, "OpenCode")

    def check_model(self, cfg):
        return cfg.get("check_model_opencode") or None  # (none: the model your OpenCode is set to)

    def spawn_judge(self, model, system, cwd):
        binary = opencode_binary()
        if binary is None:
            return None
        version = major(binary)
        title = f"{JUDGE_TITLE} {uuid.uuid4().hex[:8]}"  # (its own: it can be found by it)
        try:
            proc = subprocess.Popen([binary, "run", "--format", "json", *JUDGE_FLAGS[version], "--title", title,
                                     *(["-m", model] if model else [])],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=judge_env(version), cwd=cwd or None, start_new_session=True)
        except OSError:
            return None
        proc.sidelit_system = system
        proc.sidelit_binary = binary
        proc.sidelit_title = title
        proc.sidelit_cwd = cwd
        return proc

    def ask_judge(self, proc, text, timeout):
        """The prompt goes in on stdin; the answer is the turn's last text. The check's session
        is deleted after (_forget), so that it is not among yours."""
        proc.stdin.write((getattr(proc, "sidelit_system", "") + "\n\n" + text).encode())
        proc.stdin.close()
        deadline, buf, fd = time.time() + timeout, b"", proc.stdout.fileno()
        said, session = "", ""
        try:
            while True:
                if not select.select([fd], [], [], max(0.0, deadline - time.time()))[0]:
                    raise TimeoutError(f"no answer in {timeout:.0f} s")
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    session = event.get("sessionID") or session
                    part = event.get("part") if isinstance(event.get("part"), dict) else {}
                    if event.get("type") == "text" and isinstance(part.get("text"), str):
                        said = part["text"]
                    elif event.get("type") == "error":
                        err = event.get("error")
                        raise RuntimeError(str((err.get("data") or {}).get("message") or err.get("message")
                                               or err.get("name") or err)[:80]
                                           if isinstance(err, dict) else str(err or "error")[:80])
        finally:
            # (on a thread of its own: deleting takes seconds, and the answer is waited for)
            proc.sidelit_forgetting = threading.Thread(target=self._forget, args=(proc, session),
                                                       name="opencode-forget", daemon=True)
            proc.sidelit_forgetting.start()
        if not said:
            raise EOFError("opencode run said nothing")
        return said

    @staticmethod
    def _forget(proc, session: str):
        """The check's session goes once its opencode has quit: by its id, or, when it never said
        it (it gave no answer in time), by its own title. As the check ran: without plugins, so
        that Sidelit's does not hear of it, and OpenCode 2's on a server of its own, so that
        Sidelit never starts your background one."""
        binary = getattr(proc, "sidelit_binary", None) or "opencode"
        version = major(binary)

        def session_cmd(sub, *args):
            return subprocess.run([binary, "session", sub, *JUDGE_FLAGS[version], *args], env=judge_env(version),
                                  cwd=getattr(proc, "sidelit_cwd", None) or None, capture_output=True, text=True,
                                  timeout=30, stdin=subprocess.DEVNULL)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        try:
            if not session:
                listed = json.loads(session_cmd("list", "--format", "json").stdout or "[]")
                title = getattr(proc, "sidelit_title", None)
                session = next((s.get("id") for s in listed if isinstance(s, dict) and title
                                and s.get("title") == title), "")
            if session:
                session_cmd("delete", session)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            pass
