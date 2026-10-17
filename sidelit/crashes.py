"""What crashed: the system's crash reporter, read to tell an app that crashed from one that
ended cleanly (you closed its window from the overview, or quit it).

The kernel hands every process killed by a crashing signal (SIGSEGV, SIGABRT, ...) to the
program in /proc/sys/kernel/core_pattern. On Ubuntu and Pop!_OS that is apport, which logs
each one to /var/log/apport.log (readable by the adm group, which the first user is in):

  INFO: apport (pid 870551) 2026-10-03 19:26:59,900: called for global pid 829290, signal 11, ...
  INFO: apport (pid 870551) 2026-10-03 19:27:00,909: executable: /usr/bin/cosmic-settings (command line ...)

Where no reporter can be read, nothing is known: the engine falls back on who had the keyboard.
A process killed outright (SIGKILL, the OOM killer) never reaches a reporter, so it reads as
ended cleanly.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from sidelit.core.model import Ended

CORE_PATTERN = Path("/proc/sys/kernel/core_pattern")
APPORT_LOG = Path("/var/log/apport.log")

_LINE = re.compile(r"^\w+: apport \(pid (\d+)\) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+): (.*)$")
_CALLED = re.compile(r"(?:called for (?:global )?pid|host pid) (\d+)")
_EXE = re.compile(r"^executable: (\S+)")


def parse_apport(text: str, since: float) -> list[Ended]:
    """The crashes in an apport log at or after `since`, newest last."""
    found: dict[str, dict] = {}   # apport's own pid -> the crash it handles
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        handler, stamp, ms, msg = m.groups()
        called = _CALLED.search(msg)
        if called:
            try:
                t = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) + int(ms) / 1000
            except ValueError:
                continue
            found[handler] = {"t": t, "pid": int(called.group(1)), "exe": ""}
            continue
        exe = _EXE.match(msg)
        if exe and handler in found:
            found[handler]["exe"] = os.path.basename(exe.group(1))
    return [Ended(t=c["t"], pid=c["pid"], exe=c["exe"], crashed=True)
            for c in found.values() if c["t"] >= since]


def readable() -> bool:
    """A crash reporter we can read is on duty."""
    try:
        pattern = CORE_PATTERN.read_text()
    except OSError:
        return False
    return "apport" in pattern and os.access(APPORT_LOG, os.R_OK)


def since(t: float) -> list[Ended]:
    try:
        return parse_apport(APPORT_LOG.read_text(errors="replace"), t)
    except OSError:
        return []
