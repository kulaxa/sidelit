"""What /proc says about processes, and the shell commands Sidelit starts in terminals.

Shared by the agent adapters (claude.py, codex.py) and the detector (detect.py): no agent of
its own. (And by the compositor clients that number windows anew at every start: run_token.)
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


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


def ancestors(pid: int, table: dict[int, tuple[int, str]]) -> list[int]:
    """`pid` and every process above it, up to init."""
    chain = []
    for _ in range(40):
        if pid <= 1 or pid not in table:
            break
        chain.append(pid)
        pid = table[pid][0]
    return chain


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


def alive(pid: int) -> bool:
    """Does process `pid` run (another user's counts)?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_token(pid: int, path: str) -> str:
    """A short token for one run of a compositor (`pid`, its socket at `path`). Its windows are
    numbered from 1 again each time it starts, so a window's identifier carries this token: a
    window after the next login is never taken for one from before. From the pid and when it
    started; without them, from the socket file, made anew by each run."""
    started = process_started(pid) if pid else None
    if started is not None:
        seed = f"{pid}:{started:.2f}"
    else:
        try:
            st = os.stat(path)
            seed = f"{st.st_ino}:{st.st_mtime_ns}"
        except OSError:
            seed = path
    return hashlib.sha1(seed.encode()).hexdigest()[:8]


def in_terminal(pid: int) -> bool:
    """Whether process `pid` runs in a terminal: an interactive agent has it on stdin; `claude -p`,
    `codex exec`, and an app driving an agent through its SDK, a pipe or a socket."""
    try:
        return os.readlink(f"/proc/{pid}/fd/0").startswith(("/dev/pts/", "/dev/tty"))
    except OSError:
        return True  # cannot tell


def cmdline(pid: int) -> list[str]:
    """The arguments process `pid` was started with; [] when it is gone (or not ours to read)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def cwd_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


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
