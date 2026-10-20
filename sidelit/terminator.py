"""Terminator: Sidelit's plugin for it, so a click on a session's box shows that session's tab or
split, not only its window (plugin/terminator/sidelit_terminator.py says how).

It goes in with `sidelit hooks install` (and once by itself with `sidelit start`, when Terminator
is installed): the file is copied into ~/.config/terminator/plugins/, and its name added to
`enabled_plugins` in [global_config] of ~/.config/terminator/config, Terminator's defaults kept
when the line was not there. Only that line is touched. `sidelit hooks uninstall` takes both
out again. Terminator windows opened from then on have it; ones already open do not.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
from pathlib import Path

from sidelit import config

PLUGIN_NAME = "sidelit_terminator.py"
PLUGIN_CLASS = "SidelitTerminator"
MARK = "# Sidelit's Terminator plugin"  # (its first line: a file of that name that is not ours stays)
OFFERED_FILE = "terminator-plugin-offered"  # (in ~/.config/sidelit: sidelit start put the plugin in once)
# what Terminator enables when its config does not say (terminatorlib/config.py)
DEFAULT_PLUGINS = ["LaunchpadBugURLHandler", "LaunchpadCodeURLHandler", "APTURLHandler"]
SOCKETS = config.RUNTIME_DIR / "terminator"


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "terminator"


def plugin_source() -> Path:
    """The checkout's, or the package's /usr/share/sidelit/plugin/terminator."""
    return Path(__file__).resolve().parent.parent / "plugin" / "terminator" / PLUGIN_NAME


def installed_copy() -> Path:
    return config_home() / "plugins" / PLUGIN_NAME


def present() -> bool:
    return shutil.which("terminator") is not None


def ours(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().startswith(MARK)
    except OSError:
        return False


def stale_copy() -> bool:
    path = installed_copy()
    if not ours(path):
        return False
    try:
        return path.read_bytes() != plugin_source().read_bytes()
    except OSError:
        return True


# ---- enabled_plugins in Terminator's config ------------------------------------------------
_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")
_ENABLED = re.compile(r"^(\s*)enabled_plugins\s*=\s*(.*?)\s*$")


def _split(value: str) -> list[str]:
    return [p.strip().strip("\"'") for p in value.split(",") if p.strip().strip("\"'")]


def _join(names: list[str]) -> str:
    """A list as Terminator's config (ConfigObj) writes it: one item ends in a comma, none is a
    lone comma (without, Terminator calls the config invalid)."""
    return ", ".join(names) if len(names) > 1 else f"{names[0]}," if names else ","


def enabled_plugins(text: str) -> list[str] | None:
    """The plugins [global_config] enables; None when it does not say (Terminator's defaults)."""
    section = None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
            continue
        m = _ENABLED.match(line)
        if m and section == "global_config":
            return _split(m.group(2))
    return None


def with_plugin(text: str, enable: bool) -> str:
    """Terminator's config with our plugin enabled (or not), every other line as it was."""
    lines = text.splitlines(keepends=True)
    section, header, found = None, None, False
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
            if section == "global_config":
                header = i
            continue
        m = _ENABLED.match(line)
        if m and section == "global_config":
            names = [n for n in _split(m.group(2)) if n != PLUGIN_CLASS] + ([PLUGIN_CLASS] if enable else [])
            lines[i] = f"{m.group(1)}enabled_plugins = {_join(names)}\n"
            found = True
            break
    if not found and enable:
        entry = f"  enabled_plugins = {', '.join(DEFAULT_PLUGINS + [PLUGIN_CLASS])}\n"
        if header is None:
            lines[:0] = ["[global_config]\n", entry]
        else:
            lines.insert(header + 1, entry)
    return "".join(lines)


def _set_enabled(enable: bool) -> bool:
    """Whether the config changed."""
    path = config_home() / "config"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not enable:
            return False
        text = ""
    new = with_plugin(text, enable)
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sidelit.tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    return True


# ---- in and out ----------------------------------------------------------------------------
def copy_plugin() -> str:
    source, dest = plugin_source(), installed_copy()
    if not source.exists():
        raise RuntimeError(f"{source} is missing: is this Sidelit's checkout or package?")
    if dest.exists() and not ours(dest):
        raise RuntimeError(f"{dest} is there already and is not Sidelit's: not replacing it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".sidelit.tmp")  # (Terminator imports .py files only)
    tmp.write_bytes(source.read_bytes())
    tmp.replace(dest)
    return f"put Sidelit's plugin in Terminator's plugin folder ({dest})"


def install() -> list[str]:
    said = [copy_plugin()]
    if _set_enabled(True):
        said.append(f"enabled it in {config_home() / 'config'} (Terminator windows opened from now on have it)")
    try:  # (from now on it is yours to keep or take out: sidelit start does not put it back)
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / OFFERED_FILE).write_text("Sidelit's plugin went into Terminator (sidelit hooks install|uninstall)\n")
    except OSError:
        pass
    return said


def uninstall() -> list[str]:
    said = []
    path = installed_copy()
    if ours(path):
        path.unlink()
        said.append(f"took Sidelit's plugin out of Terminator's plugin folder ({path})")
    if _set_enabled(False):
        said.append(f"and out of enabled_plugins in {config_home() / 'config'}")
    return said


def status() -> tuple[bool, list[str]]:
    if not ours(installed_copy()):
        return False, ["Sidelit's Terminator plugin is not installed (sidelit hooks install): a click on a"
                       " session's box brings its window forward, not its tab or split"]
    try:
        names = enabled_plugins((config_home() / "config").read_text(encoding="utf-8"))
    except OSError:
        names = None
    if PLUGIN_CLASS not in (names or []):
        return False, [f"Sidelit's Terminator plugin is in {installed_copy()}, but not enabled (sidelit hooks install)"]
    said = [f"Sidelit's Terminator plugin is installed, enabled ({installed_copy()})"]
    if stale_copy():
        said.append("! it is an older Sidelit's (sidelit hooks install, or sidelit start, refreshes it)")
    return not stale_copy(), said


def upgrade() -> list[str]:
    """`sidelit start`: puts the plugin in once when Terminator is here (taken out again, it stays
    out); an update refreshes it."""
    if not present():
        return []
    if not installed_copy().exists() and not (config.CONFIG_DIR / OFFERED_FILE).exists():
        if config.DRY_RUN:
            return ["Terminator is here: Sidelit's plugin would go in (dry run: nothing is changed)"]
        return ["Terminator is here: putting Sidelit's plugin in"] + [f"  ✓ {line}" for line in install()]
    if not stale_copy():
        return []
    if config.DRY_RUN:
        return ["Terminator has an older Sidelit's plugin (dry run: not refreshed)"]
    return ["refreshed Sidelit's plugin in Terminator", f"  ✓ {copy_plugin()}"]


# ---- showing a terminal ----------------------------------------------------------------------
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def focus(uuid: str) -> bool:
    """Ask every Terminator with the plugin to show the terminal `uuid` (the one that has it
    does); whether any was there to ask. A socket whose Terminator is gone is cleaned up."""
    try:
        sockets = list(SOCKETS.glob("*.sock"))
    except OSError:
        return False
    message = json.dumps({"focus": uuid}).encode("utf-8")
    asked = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        for path in sockets:
            try:
                s.sendto(message, str(path))
                asked = True
            except (ConnectionRefusedError, FileNotFoundError):
                if path.stem.isdigit() and not _alive(int(path.stem)):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
    return asked
