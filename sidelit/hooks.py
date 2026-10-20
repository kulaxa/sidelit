"""`sidelit hooks install|uninstall|status`: Sidelit's plugin, in every coding agent installed.

Each agent takes it its own way (agents.py: install, uninstall, status, upgrade); this is the
command over all of them, and Claude Code's way.

For Claude Code the plugin (plugin/, listed by .claude-plugin/marketplace.json in the folder
above this package) carries the hook that reports what each session does (plugin/hooks/hook.py)
and the usage limits for the bars (plugin/hooks/usage.js). It goes in and out the way any Claude
Code plugin does, with Claude Code's own commands: `claude plugin marketplace add <that folder>`,
then `claude plugin install sidelit@sidelit`. Claude Code keeps track of it in its settings
itself, and loads it in place from that folder, so updating Sidelit updates the plugin.

Sidelit 0.5 wrote its hook into ~/.claude/settings.json instead. Once the plugin is in, those
entries go (recognised by their command, which ends in sidelit-hook): every other hook and the
rest of the file stay as they are, and the first change backs the file up next to it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sidelit import claude, terminator

SETTINGS = claude.CLAUDE_DIR / "settings.json"
BACKUP = "settings.json.before-sidelit"  # the file as it was before our first change, next to it
NAME = "sidelit-hook"  # the command of the entries Sidelit 0.5 wrote
MARKETPLACE = "sidelit"
PLUGIN = f"sidelit@{MARKETPLACE}"


def marketplace_dir() -> Path:
    """The folder whose .claude-plugin/marketplace.json lists the plugin: the checkout, or the
    package's /usr/share/sidelit."""
    return Path(__file__).resolve().parent.parent


def hook_command() -> str:
    """The hook's launcher by its old name (bin/sidelit-hook), for what still calls it by that name."""
    here = Path(__file__).resolve().parent.parent / "bin" / NAME
    for cand in (shutil.which(NAME), str(Path.home() / ".local/bin" / NAME) if (Path.home() / ".local/bin" / NAME).exists() else None,
                 str(here) if here.exists() else None):
        if cand:
            return cand
    raise RuntimeError(f"{NAME} not found (run install.sh, or install the package)")


# ---- the plugin, through Claude Code's CLI --------------------------------------------------
def run_claude(*args: str) -> tuple[int, dict, str]:
    """`claude plugin <args> --json`: (exit code, its one-line result, what it said otherwise)."""
    binary = claude.claude_binary()
    if binary is None:
        raise RuntimeError("Claude Code's claude command is not on PATH: install Claude Code first")
    try:
        r = subprocess.run([binary, "plugin", *args, "--json"], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"claude plugin {args[0]}: {e}") from e
    result = {}
    for line in reversed(r.stdout.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            result = data
            break
    return r.returncode, result, (r.stderr or r.stdout).strip()


def _check(what: str, code: int, result: dict, said: str) -> str:
    if code != 0 or result.get("outcome") not in (None, "ok"):
        raise RuntimeError(f"claude plugin {what}: {result.get('message') or said or f'exit {code}'}")
    return result.get("message") or "ok"


def plugin_state() -> dict | None:
    """The plugin as `claude plugin list` shows it ({"enabled": ..., "readFromFolder": ...}), or None."""
    binary = claude.claude_binary()
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "plugin", "list", "--json"], capture_output=True, text=True,
                             timeout=60, stdin=subprocess.DEVNULL).stdout
        plugins = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return next((p for p in plugins if isinstance(p, dict) and p.get("id") == PLUGIN), None) \
        if isinstance(plugins, list) else None


def install_plugin() -> list[str]:
    """Add Sidelit's marketplace (this folder) and install the plugin from it, for your user.
    Both are no-ops when done already; a marketplace added from elsewhere (an older checkout)
    now points here; a plugin you had disabled is enabled again."""
    folder = marketplace_dir()
    if not (folder / ".claude-plugin" / "marketplace.json").exists():
        raise RuntimeError(f"{folder} has no .claude-plugin/marketplace.json: is this Sidelit's checkout or package?")
    said = [_check("marketplace add", *run_claude("marketplace", "add", str(folder), "--scope", "user"))]
    said.append(_check("install", *run_claude("install", PLUGIN, "--scope", "user")))
    return said


def install() -> list[str]:
    """The plugin in, then the hook entries Sidelit 0.5 wrote into settings.json out."""
    said = install_plugin()
    n = retire()
    if n:
        said.append(f"removed the hook Sidelit 0.5 had written into {SETTINGS} "
                    f"({n} entr{'y' if n == 1 else 'ies'}; the file as it was: {BACKUP})")
    return said


def uninstall() -> list[str]:
    said = []
    n = retire()
    if n:
        said.append(f"removed {n} hook entr{'y' if n == 1 else 'ies'} of Sidelit 0.5 from {SETTINGS}")
    if claude.claude_binary() is None:
        return said  # (no Claude Code: no plugin in it either)
    if plugin_state() is not None:
        said.append(_check("uninstall", *run_claude("uninstall", PLUGIN, "--scope", "user")))
    code, result, out = run_claude("marketplace", "remove", MARKETPLACE)
    if result.get("failureCode") != "not_configured":
        said.append(_check("marketplace remove", code, result, out))
    return said


# ---- hook entries in settings.json: Sidelit 0.5's, and the old name's (moveover.py) ------------
def _ours(entry: dict, name: str = NAME) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(
        str(h.get("command", "")).rstrip().endswith(name) for h in hooks if isinstance(h, dict))


def _load() -> dict:
    if not SETTINGS.exists():
        return {}
    data = json.loads(SETTINGS.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"{SETTINGS} is not a JSON object; not touching it")
    return data


def _save(data: dict):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    backup = SETTINGS.with_name(BACKUP)
    if SETTINGS.exists() and not backup.exists():
        shutil.copy2(SETTINGS, backup)
    path = SETTINGS.resolve()  # (a link into your dotfiles stays a link: what it points at changes)
    tmp = path.with_name(path.name + ".sidelit.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _keep_mode(path, tmp)
        os.replace(tmp, path)
    except OSError as e:
        if path == SETTINGS:
            raise
        # (one that Nix's home-manager manages, say: replacing the link would only break that)
        raise RuntimeError(f"{SETTINGS} is a link to {path}, which cannot be written ({e.strerror}): "
                           f"take Sidelit's old hook out where you manage that file") from e


def _keep_mode(path: Path, tmp: Path):
    """A private file stays private."""
    try:
        shutil.copymode(path, tmp)
    except OSError:  # (not there yet; a filesystem without modes)
        pass


def _strip(data: dict, name: str = NAME) -> int:
    """Drop the hooks whose command ends in `name`, and an entry they leave empty; another
    command sharing their entry stays. How many entries changed."""
    removed = 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        keep = []
        for e in entries:
            if not (isinstance(e, dict) and _ours(e, name)):
                keep.append(e)
                continue
            removed += 1
            rest = [h for h in e.get("hooks") or []
                    if not (isinstance(h, dict) and str(h.get("command", "")).rstrip().endswith(name))]
            if rest:
                keep.append({**e, "hooks": rest})
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    return removed


def retire(name: str = NAME) -> int:
    """Take the entries of the hook whose command ends in `name` out of settings.json: how many."""
    data = _load()
    removed = _strip(data, name)
    if removed:
        _save(data)
    return removed


def present(name: str = NAME) -> bool:
    """Has settings.json an entry of the hook whose command ends in `name`?"""
    try:
        data = _load()
    except (ValueError, RuntimeError, OSError):
        return False
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return any(isinstance(x, dict) and _ours(x, name)
               for entries in hooks.values() if isinstance(entries, list) for x in entries)


MODS_SINCE = (2, 1, 287)  # the Claude Code that first runs a plugin's mods (the usage bars' usage.js)


def claude_version() -> tuple[int, ...] | None:
    """`claude --version`'s "2.1.304 (Claude Code)" as (2, 1, 304); None when it cannot be told."""
    binary = claude.claude_binary()
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def status() -> tuple[bool, list[str]]:
    """Claude Code's: is the plugin in and enabled, with nothing of Sidelit 0.5's left over? And
    can this Claude Code run its mod (the usage bars)?"""
    state = plugin_state()
    if state is None:
        said = ["Sidelit's Claude Code plugin is not installed (sidelit hooks install)"]
    else:
        said = [f"Sidelit's Claude Code plugin is installed, {'enabled' if state.get('enabled') else 'disabled'}"
                f" ({state.get('readFromFolder') or state.get('installPath') or PLUGIN})"]
        have = claude_version()
        if have is not None and have < MODS_SINCE:
            said.append(f"! Claude Code {'.'.join(map(str, have))} cannot run the plugin's mod, so there are no usage"
                        f" bars: {'.'.join(map(str, MODS_SINCE))} or later can (claude update)")
    if present():
        said.append(f"! {SETTINGS} still has the hook Sidelit 0.5 wrote there (sidelit hooks install takes it out)")
    return state is not None and bool(state.get("enabled")) and not present(), said


def upgrade() -> list[str]:
    """`sidelit start`, for Claude Code: the hook Sidelit 0.5 wrote into settings.json becomes the plugin."""
    if not present():
        return []
    from sidelit import config
    said = [f"Sidelit's Claude Code hook is a plugin now: installing it, and taking the old hook out of {SETTINGS}"
            + (" (dry run: nothing is changed)" if config.DRY_RUN else "")]
    return said + ([] if config.DRY_RUN else [f"  ✓ {line}" for line in install()])


# ---- the command, over every agent ------------------------------------------------------------
def main(argv: list[str]) -> int:
    from sidelit import agents
    cmd = argv[0] if argv else "status"
    found = agents.installed()
    if cmd not in ("install", "upgrade", "uninstall", "status"):
        print("usage: sidelit hooks install|uninstall|status", file=sys.stderr)
        return 2
    if not found and cmd in ("install", "status"):
        names = " or ".join(a.label for a in agents.every())
        print(f"sidelit hooks: no coding agent found on PATH ({names}): install one first", file=sys.stderr)
        return 1
    code = 0
    # (taking it out, and putting right what an older Sidelit left, is for every agent: one that
    # is gone from PATH may still have Sidelit's leftovers; OpenCode's is a file, taken out
    # without OpenCode)
    for agent in agents.every() if cmd in ("uninstall", "upgrade") else found:
        if cmd == "uninstall" and agent not in found and agent.name not in ("claude", "opencode"):
            continue
        try:
            if cmd == "install":
                for line in agent.install():
                    print(f"✓ {line}")
                for line in agent.after_install:
                    print(line)
            elif cmd == "upgrade":  # (`sidelit start`: quiet unless there is something to do)
                for line in agent.upgrade():
                    print(line if line.startswith(" ") else f"sidelit: {line}")
            elif cmd == "uninstall":
                quiet = agent not in found and agent.name != "claude"  # (no word of an agent you do not have)
                for line in agent.uninstall() or ([] if quiet else [f"Sidelit's {agent.label} plugin was not installed"]):
                    print(f"✓ {line}")
            else:
                ok, said = agent.status()
                for line in said:
                    print(line)
                code = code or (0 if ok else 1)
        except (ValueError, RuntimeError, OSError) as e:
            print(f"sidelit hooks: {agent.label}: {e}", file=sys.stderr)
            code = 1
    # (not an agent, a terminal: its plugin shows a session's tab or split, terminator.py)
    if terminator.present() or cmd == "uninstall":
        try:
            if cmd == "install":
                for line in terminator.install():
                    print(f"✓ {line}")
            elif cmd == "upgrade":
                for line in terminator.upgrade():
                    print(line if line.startswith(" ") else f"sidelit: {line}")
            elif cmd == "uninstall":
                for line in terminator.uninstall():
                    print(f"✓ {line}")
            else:
                for line in terminator.status()[1]:  # (optional: does not fail the status)
                    print(line)
        except (RuntimeError, OSError) as e:
            print(f"sidelit hooks: Terminator: {e}", file=sys.stderr)
            code = 1
    return code
