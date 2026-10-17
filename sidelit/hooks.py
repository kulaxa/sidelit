"""`sidelit hooks install|uninstall|status`: the Claude Code hook in ~/.claude/settings.json.

Our entries are recognised by their command (it ends in sidelit-hook), so
installing twice changes nothing, uninstalling leaves every other hook alone, and the
rest of settings.json is kept as it is. The first install backs the file up next to it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
BACKUP = "settings.json.before-sidelit"  # the file as it was before our first change, next to it
NAME = "sidelit-hook"
# the ones fired on every tool call run async, so Claude never waits for us there
EVENTS = {"SessionStart": False, "UserPromptSubmit": False, "PermissionRequest": False, "Notification": False,
          "Stop": False, "SessionEnd": False, "SubagentStart": False, "SubagentStop": False,
          "PreToolUse": True, "PostToolUse": True}
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PermissionRequest"}


def hook_command() -> str:
    """The installed hook launcher: next to our own launcher, else on PATH."""
    here = Path(__file__).resolve().parent.parent / "bin" / NAME
    for cand in (shutil.which(NAME), str(Path.home() / ".local/bin" / NAME) if (Path.home() / ".local/bin" / NAME).exists() else None,
                 str(here) if here.exists() else None):
        if cand:
            return cand
    raise RuntimeError(f"{NAME} not found (run install.sh, or install the package)")


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
                           f"change the hooks where you manage that file") from e


def _keep_mode(path: Path, tmp: Path):
    """A private file stays private."""
    try:
        shutil.copymode(path, tmp)
    except OSError:  # (not there yet; a filesystem without modes)
        pass


def _strip(data: dict, name: str = NAME) -> int:
    """Drop the hooks whose command ends in `name` (ours, or the old name's: moveover.py), and an
    entry they leave empty; another command sharing their entry stays. How many entries changed."""
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


def _add(data: dict, command: str):
    _strip(data)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f'"hooks" in {SETTINGS} is not a JSON object; not touching it')
    for event, run_async in EVENTS.items():
        hook = {"type": "command", "command": command, "timeout": 5}
        if run_async:
            hook["async"] = True
        entry = {"hooks": [hook]}
        if event in TOOL_EVENTS:
            entry = {"matcher": "*", **entry}
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise RuntimeError(f'"hooks.{event}" in {SETTINGS} is not a list; not touching it')
        entries.append(entry)


def install() -> str:
    command = hook_command()
    data = _load()
    _add(data, command)
    _save(data)
    return command


def swap(old_name: str) -> tuple[int, str | None]:
    """Replace the entries of another hook (the old name's, moveover.py) with ours, in one save:
    (how many went, our command; None when ours is not installed, and only the old ones went)."""
    data = _load()
    removed = _strip(data, old_name)
    if not removed:
        return 0, None
    try:
        command = hook_command()
    except RuntimeError:
        command = None
    if command:
        _add(data, command)
    _save(data)
    return removed, command


def uninstall() -> int:
    data = _load()
    removed = _strip(data)
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


def installed() -> list[str]:
    try:
        data = _load()
    except (ValueError, RuntimeError):
        return []
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return sorted(e for e, entries in hooks.items() if isinstance(entries, list) and any(isinstance(x, dict) and _ours(x) for x in entries))


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "install":
            command = install()
            print(f"✓ Claude Code hook installed in {SETTINGS} ({command})")
            print("  Claude Code sessions started from now on report their status and window directly;")
            print("  running ones are still followed by their terminal title.")
        elif cmd == "uninstall":
            n = uninstall()
            print(f"✓ removed {n} hook entr{'y' if n == 1 else 'ies'} from {SETTINGS}")
        elif cmd == "status":
            events = installed()
            missing = sorted(set(EVENTS) - set(events))
            print(f"installed for: {', '.join(events) or 'nothing'}" + (f"; missing: {', '.join(missing)}" if events and missing else ""))
            return 0 if events and not missing else 1
        else:
            print("usage: sidelit hooks install|uninstall|status", file=sys.stderr)
            return 2
    except (ValueError, RuntimeError, OSError) as e:
        print(f"sidelit hooks: {e}", file=sys.stderr)
        return 1
    return 0
