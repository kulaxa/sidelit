"""Codex CLI, behind the agent interface (agents.py).

What Codex offers, and how Sidelit uses it:
- Plugins with hooks. Sidelit's plugin (plugin/) has a Codex manifest beside Claude Code's
  (.codex-plugin/plugin.json), whose hooks (hooks/codex.json) run the same hook script as Claude
  Code's (`hook.py codex`). Codex reads Sidelit's marketplace (.claude-plugin/marketplace.json,
  which it takes as a "legacy-compatible" one) and installs with its own commands: `codex
  plugin marketplace add <folder>`, `codex plugin add sidelit@sidelit`. It copies the plugin
  into its cache, and runs a plugin's hooks only once you have trusted them (it asks when it
  next starts, or in /hooks). Trust is kept by each hook's command, which names the cached
  copy's folder, and that folder its version: so the Codex manifest's version stays the same
  until codex.json changes, and `sidelit start` refreshes the copy when Sidelit's changed.
- Its shared background server. An interactive `codex` hands its sessions to one server
  process unless started with --no-daemon, and that server runs their hooks: the hook then
  cannot tell which terminal a session is in (pid 0 in its record). resolve() finds the
  session's own `codex` among the running ones: the one started for that session (`codex
  resume <id>`), else the one in the session's folder that started last before it did.
- Its app-server (`codex app-server`, JSON-RPC over stdio, experimental in Codex's own words):
  the plan's usage limits (account/rateLimits/read), the name you gave a session
  (thread/read), and whether you trusted Sidelit's hooks (hooks/list).
- Headless runs: `codex exec --ephemeral --json`, the turn check.
- `codex resume <id>`, `codex fork <id>`.
Codex has no Notification event: a Codex session never says it waits for an answer to a
question, only for a permission (PermissionRequest).
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sidelit.agents import Agent, TerminalClients, turn_from_record
from sidelit.core.titles import bare_title
from sidelit.procs import cmdline, cwd_of, in_terminal, load_json, process_started, shell_command

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
MARKETPLACE = "sidelit"
PLUGIN = f"sidelit@{MARKETPLACE}"
# the plugin's files Codex copies into its cache (sidelit start refreshes a copy that differs)
PLUGIN_FILES = (".codex-plugin/plugin.json", "hooks/codex.json", "hooks/hook.py")
# arguments that make a codex process something other than a session in a terminal
NOT_A_SESSION = {"app-server", "exec", "e", "review", "mcp-server", "exec-server", "proxy"}
OFFERED_FILE = "codex-plugin-offered"  # (in ~/.config/sidelit: sidelit start put the plugin in once)
NAMES_EVERY_S = 60.0      # how often the names of live sessions are asked for
RPC_TIMEOUT_S = 15.0
EXEC_FLAGS = [            # the turn check: no tools, no hooks or plugins, no MCP servers, nothing saved
    "exec", "--ephemeral", "--json", "--skip-git-repo-check", "-s", "read-only",
    "--disable", "hooks", "--disable", "plugins", "--disable", "apps", "--disable", "shell_tool",
    "--disable", "unified_exec", "--disable", "multi_agent", "-c", "mcp_servers={}",
    "-c", 'model_reasoning_effort="low"']
# windowDurationMins -> the kind the bars know (sidebar_common.USAGE_NAMES)
WINDOWS = {300: "five_hour", 10080: "seven_day"}
TRUST_HINT = ("Codex asks you once, the next time it starts, to review Sidelit's hooks: trust them "
              "(there, or later with /hooks), or Codex sessions do not report to Sidelit")


def codex_binary() -> str | None:
    return shutil.which("codex") or next((str(p) for p in (Path.home() / ".local/bin/codex",) if p.exists()), None)


def plugin_dir() -> Path:
    """Sidelit's plugin folder: the checkout's, or the package's /usr/share/sidelit/plugin."""
    return Path(__file__).resolve().parent.parent / "plugin"


def plugin_version() -> str:
    return str((load_json(plugin_dir() / ".codex-plugin" / "plugin.json", {}) or {}).get("version") or "")


def cached_plugin() -> Path:
    """Where Codex keeps its copy of the plugin."""
    return CODEX_HOME / "plugins" / "cache" / MARKETPLACE / "sidelit" / plugin_version()


def project_name(cwd: str) -> str:
    """The project Codex names in its terminal's title: its git checkout's folder, else the folder."""
    path = Path(cwd or "/")
    for p in [path, *path.parents]:
        if (p / ".git").exists():
            return p.name or str(p)
    return path.name or str(path)


def pretty_model(model: str) -> str:
    """'gpt-5.5-codex' -> 'GPT-5.5 Codex', 'gpt-5.5-codex-mini' -> 'GPT-5.5 Codex mini'"""
    if not model.lower().startswith("gpt-"):
        return model
    version, *rest = model[4:].split("-")
    return " ".join([f"GPT-{version}", *(w.capitalize() if w == "codex" else w for w in rest)])


# ---- the plugin, through Codex's own commands ---------------------------------------------------
def run_codex(*args: str, json_out: bool = True) -> tuple[int, dict, str]:
    """`codex plugin <args> --json`: (exit code, what it answered, what it said otherwise)."""
    binary = codex_binary()
    if binary is None:
        raise RuntimeError("Codex's codex command is not on PATH")
    try:
        r = subprocess.run([binary, "plugin", *args, *(["--json"] if json_out else [])], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"codex plugin {args[0]}: {e}") from e
    try:
        result = json.loads(r.stdout)
    except ValueError:
        result = {}
    said = "\n".join(line for line in (r.stderr or "").splitlines()
                     if line.strip() and not line.startswith("WARNING:")).strip()
    return r.returncode, result if isinstance(result, dict) else {}, said.removeprefix("Error: ")


def _check(what: str, code: int, result: dict, said: str) -> dict:
    if code != 0:
        raise RuntimeError(f"codex plugin {what}: {said or f'exit {code}'}")
    return result


def marketplace_root() -> str | None:
    """The folder Codex's marketplace "sidelit" was added from, if it has one."""
    code, result, _ = run_codex("marketplace", "list")
    found = next((m for m in result.get("marketplaces") or [] if isinstance(m, dict) and m.get("name") == MARKETPLACE), None)
    return str(found.get("root") or "") if found else None


def installed_plugin() -> dict | None:
    """The plugin as `codex plugin list` shows it ({"enabled", "version", ...}), or None."""
    try:
        code, result, _ = run_codex("list")
    except RuntimeError:
        return None
    return next((p for p in result.get("installed") or [] if isinstance(p, dict) and p.get("pluginId") == PLUGIN), None)


def install_plugin() -> list[str]:
    folder = plugin_dir().parent
    if not (folder / ".claude-plugin" / "marketplace.json").exists() or not (plugin_dir() / ".codex-plugin").exists():
        raise RuntimeError(f"{folder} has no Codex plugin: is this Sidelit's checkout or package?")
    said = []
    root = marketplace_root()
    if root is not None and Path(root).resolve() != folder.resolve():
        # (an older checkout, or the package after a checkout): the marketplace comes from here now
        _check("marketplace remove", *run_codex("marketplace", "remove", MARKETPLACE))
        root = None
    if root is None:
        _check("marketplace add", *run_codex("marketplace", "add", str(folder)))
        said.append(f"added Sidelit's marketplace to Codex ({folder})")
    result = _check("add", *run_codex("add", PLUGIN))
    said.append(f"installed {PLUGIN} in Codex" + (f" ({result['installedPath']})" if result.get("installedPath") else ""))
    return said


def uninstall_plugin() -> list[str]:
    said = []
    if installed_plugin() is not None:
        _check("remove", *run_codex("remove", PLUGIN))
        said.append(f"removed {PLUGIN} from Codex")
    if marketplace_root() is not None:
        _check("marketplace remove", *run_codex("marketplace", "remove", MARKETPLACE))
        said.append("removed Sidelit's marketplace from Codex")
    return said


def stale_copy() -> bool:
    """Is Codex's copy of the plugin there, and not what this Sidelit has?"""
    cache = cached_plugin()
    if not cache.is_dir():
        return False
    for name in PLUGIN_FILES:
        try:
            if (cache / name).read_bytes() != (plugin_dir() / name).read_bytes():
                return True
        except OSError:
            return True
    return False


# ---- the app-server ------------------------------------------------------------------------------
class AppServer:
    """`codex app-server` over stdio: a JSON message a line, JSON-RPC. One per visit: started,
    asked, stopped (`with AppServer() as s: s.call(...)`)."""

    def __init__(self, binary: str | None = None, timeout: float = RPC_TIMEOUT_S):
        binary = binary or codex_binary()
        if binary is None:
            raise RuntimeError("codex not found")
        self.timeout = timeout
        self.proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=str(Path.home()), start_new_session=True)
        self._buf = b""
        self._id = 0
        try:
            from sidelit import version
            self.call("initialize", {"clientInfo": {"name": "sidelit", "title": "Sidelit", "version": version()}})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, method: str, params: dict | None = None):
        """Its result; RuntimeError with its message for an error, TimeoutError for no answer."""
        self._id += 1
        self._send({"id": self._id, "method": method, **({"params": params} if params is not None else {})})
        deadline, fd = time.time() + self.timeout, self.proc.stdout.fileno()
        while True:
            while b"\n" not in self._buf:
                if not select.select([fd], [], [], max(0.0, deadline - time.time()))[0]:
                    raise TimeoutError(f"codex app-server: no answer to {method}")
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError("codex app-server exited")
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == self._id and "method" not in msg:
                if msg.get("error"):
                    raise RuntimeError(str((msg["error"] or {}).get("message") or msg["error"])[:200])
                return msg.get("result")

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass


def usage_from(result: dict, now: float | None = None) -> dict | None:
    """account/rateLimits/read's answer as the bars' file: each window of the plan's limit (the
    5-hour, the week) with how much is used and when it resets."""
    snap = (result or {}).get("rateLimits") if isinstance(result, dict) else None
    limits = []
    for window in ((snap or {}).get("primary"), (snap or {}).get("secondary")):
        if not isinstance(window, dict) or not isinstance(window.get("usedPercent"), (int, float)):
            continue
        mins = window.get("windowDurationMins")
        kind = WINDOWS.get(mins) or (f"{mins // 60}_hour" if isinstance(mins, int) and mins % 60 == 0 and mins < 1440
                                     else f"{mins // 1440}_day" if isinstance(mins, int) and mins % 1440 == 0
                                     else "limit")
        resets = window.get("resetsAt")
        limits.append({"kind": kind, "percentUsed": float(window["usedPercent"]),
                       "resetsAt": datetime.fromtimestamp(resets, timezone.utc).isoformat().replace("+00:00", "Z")
                       if isinstance(resets, (int, float)) else None})
    if not limits:
        return None
    return {"at": datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(), "limits": limits}


# ---- the adapter ---------------------------------------------------------------------------------
class Codex(Agent):
    """Codex CLI (see the module docstring)."""
    name, label, short, comm, command = "codex", "Codex", "Codex", "codex", "codex"
    marks = ("◆", "◆")
    window_title = False  # (its terminal's title is the project, not the session)
    usage_name = "usage-codex.json"
    polls_usage = True
    after_install = (f"  {TRUST_HINT}.",)

    def __init__(self, hook_dir: Path | None = None, server=AppServer, started=process_started):
        super().__init__(hook_dir)
        self.server = server
        self._names: dict[str, tuple[str | None, str | None]] = {}  # session -> (name, first prompt) as Codex has them
        self._asked_at: dict[str, float] = {}  # session -> when Codex was last asked for its name
        self._asking = False
        self.terminals = TerminalClients(self, started)  # (the codex processes in terminals)

    def binary(self):
        return codex_binary()

    # ---- the plugin
    def install(self):
        said = install_plugin()
        try:  # (from now on it is yours to keep or take out: sidelit start does not put it back)
            from sidelit import config
            config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (config.CONFIG_DIR / OFFERED_FILE).write_text("Sidelit's plugin went into Codex (sidelit hooks install|uninstall)\n")
        except OSError:
            pass
        return said

    def uninstall(self):
        if codex_binary() is None:
            return []
        return uninstall_plugin()

    def status(self):
        plugin = installed_plugin()
        if plugin is None:
            return False, ["Sidelit's Codex plugin is not installed (sidelit hooks install)"]
        said = [f"Sidelit's Codex plugin is installed, {'enabled' if plugin.get('enabled') else 'disabled'}"
                f" ({cached_plugin()})"]
        trust = self.hooks_trusted()
        if trust is False:
            said.append(f"! {TRUST_HINT}")
        elif trust is None:
            said.append("  (whether you trusted its hooks in Codex could not be asked)")
        if stale_copy():
            said.append("! Codex has an older copy of it (sidelit hooks install, or sidelit start, refreshes it)")
        return bool(plugin.get("enabled")) and trust is not False and not stale_copy(), said

    def hooks_trusted(self) -> bool | None:
        """Has Codex been told to trust every hook of Sidelit's plugin? None: could not ask."""
        try:
            with self.server() as s:
                data = s.call("hooks/list", {"cwds": [str(Path.home())]}) or {}
        except Exception:  # noqa: BLE001
            return None
        ours = [h for entry in data.get("data") or [] for h in entry.get("hooks") or []
                if isinstance(h, dict) and h.get("pluginId") == PLUGIN]
        return bool(ours) and all(h.get("trustStatus") in ("trusted", "managed") for h in ours)

    def upgrade(self):
        """`sidelit start`: a Sidelit from before Codex was supported puts the plugin in, once
        (taken out again, it stays out); and an update changes the hook, while Codex runs its own
        copy, so that is refreshed."""
        from sidelit import config
        if codex_binary() is None:
            return []
        if not cached_plugin().parent.is_dir() and not (config.CONFIG_DIR / OFFERED_FILE).exists():
            if config.DRY_RUN:
                return ["Codex is here: Sidelit's plugin would go in (dry run: nothing is changed)"]
            return ["Codex is here: putting Sidelit's plugin in"] + [f"  ✓ {line}" for line in self.install()] \
                + list(self.after_install)
        if not stale_copy():
            return []
        if config.DRY_RUN:
            return ["Codex has an older copy of Sidelit's plugin (dry run: not refreshed)"]
        return ["refreshed Codex's copy of Sidelit's plugin"] + [f"  ✓ {line}" for line in install_plugin()]

    # ---- which process shows a session
    def clients(self, table: dict) -> list[int]:
        """The codex processes that are a session in a terminal (not its server, not `codex exec`)."""
        out = []
        for pid, (_, comm) in table.items():
            if comm != self.comm:
                continue
            args = cmdline(pid)
            if args and not NOT_A_SESSION & set(args[1:]) and in_terminal(pid):
                out.append(pid)
        return out

    @staticmethod
    def folder_of(pid: int) -> str:
        """Where a codex process works: its -C/--cd, else the folder it was started in."""
        args, here = cmdline(pid), cwd_of(pid)
        for i, a in enumerate(args):
            if a in ("-C", "--cd") and i + 1 < len(args):
                return os.path.realpath(os.path.join(here, args[i + 1]))
            if a.startswith("--cd="):
                return os.path.realpath(os.path.join(here, a[5:]))
        return os.path.realpath(here) if here else ""

    def resolve(self, records, table):
        """A record the server wrote (pid 0) gets its session's codex (TerminalClients): the one
        started for it (resume, fork), else the one in its folder that started last before it did."""
        self.terminals.resolve(records, table)

    # ---- titles
    def title(self, session, cwd, rec=None):
        """The name you gave the session (/rename), else what you first asked in it."""
        name, preview = self._names.get(session, (None, None))
        return name or " ".join((preview or "").split())[:120] or (rec or {}).get("first_prompt") or None

    def title_key(self, session, rec):
        return project_name(rec.get("cwd") or "")

    def window_key(self, title):
        return bare_title(title) if title else None

    def owns_title(self, title, cwd=""):
        return bool(title) and bare_title(title) == project_name(cwd)

    def unmatched_title(self, title):
        """The title of a codex running in a terminal: its project's name (with a spinner while
        it works), for one of the codex processes running now."""
        seen = self.terminals.seen or {}
        live = {project_name(seen[p][1]) for p in self.terminals.live if p in seen}
        return bool(title) and bare_title(title) in live

    def watch(self, sessions: list[str]):
        """Ask Codex for these sessions' names, on a thread: each at once when it is new, then at
        most every NAMES_EVERY_S, whether Codex could say or not (one with nothing saved yet,
        an app-server that does not start)."""
        now = time.time()
        for sid in [s for s in self._asked_at if s not in sessions]:
            del self._asked_at[sid]
        due = [s for s in sessions if now - self._asked_at.get(s, 0.0) >= NAMES_EVERY_S]
        if self._asking or not due:
            return
        self._asking = True
        for sid in due:
            self._asked_at[sid] = now

        def ask():
            try:
                with self.server() as s:
                    for sid in due:
                        try:
                            thread = (s.call("thread/read", {"threadId": sid}) or {}).get("thread") or {}
                        except RuntimeError:
                            continue  # (one it does not know yet)
                        self._names[sid] = (thread.get("name") or None, thread.get("preview") or None)
            except Exception:  # noqa: BLE001 (no title is no reason to stop anything)
                pass
            finally:
                self._asking = False
        threading.Thread(target=ask, daemon=True).start()

    def model(self, session, rec):
        model = (rec or {}).get("model")
        return pretty_model(model) if isinstance(model, str) and model else None

    # ---- resume, fork
    def resume_command(self, session, cwd):
        """Resume a session; one Codex has no saved copy of (it never got a message) starts a
        fresh Codex there instead."""
        if any(CODEX_HOME.glob(f"*sessions/*/*/*/rollout-*-{session}.jsonl")):
            return shell_command(f"codex resume {session}")
        return shell_command(f"codex resume {session} || codex")

    def fork_command(self, session, cwd):
        return shell_command(f"codex fork {session}")

    # ---- the turn check: `codex exec`, from what the hook kept of the turn
    def last_turn(self, rec):
        return turn_from_record(rec, "Codex")

    def check_model(self, cfg):
        return cfg.get("check_model_codex") or None  # (none: the model your Codex is set to)

    def spawn_judge(self, model, system, cwd):
        binary = codex_binary()
        if binary is None:
            return None
        try:
            proc = subprocess.Popen([binary, *EXEC_FLAGS, *(["-m", model] if model else []), "-C", cwd or str(Path.home())],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    cwd=cwd or None, start_new_session=True)
        except OSError:
            return None
        proc.sidelit_system = system
        return proc

    def ask_judge(self, proc, text, timeout):
        """The prompt goes in on stdin (Codex reads it all, then runs one turn); the answer is the
        turn's last agent message."""
        proc.stdin.write((getattr(proc, "sidelit_system", "") + "\n\n" + text).encode())
        proc.stdin.close()
        deadline, buf, fd, said = time.time() + timeout, b"", proc.stdout.fileno(), ""
        while True:
            while b"\n" not in buf:
                if not select.select([fd], [], [], max(0.0, deadline - time.time()))[0]:
                    raise TimeoutError(f"no answer in {timeout:.0f} s")
                chunk = os.read(fd, 65536)
                if not chunk:
                    if said:
                        return said
                    raise EOFError("codex exec exited")
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type") if isinstance(event, dict) else None
            item = event.get("item") if kind == "item.completed" else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                said = str(item.get("text") or "")
            elif kind == "turn.completed":
                return said
            elif kind in ("turn.failed", "error"):
                err = event.get("error") if kind == "turn.failed" else event
                raise RuntimeError(str((err or {}).get("message") or kind)[:80])

    # ---- usage limits
    def usage(self):
        try:
            with self.server() as s:
                return usage_from(s.call("account/rateLimits/read", {"excludeResetCreditDetails": True}))
        except Exception:  # noqa: BLE001 (not logged in, no Codex, an older one: no bars)
            return None
