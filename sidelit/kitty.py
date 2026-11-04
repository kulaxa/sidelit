"""kitty: which of its windows has each pane, asked over kitty's remote-control socket.

kitty keeps all the windows of one instance in one process, and one you opened yourself is in
no launch of ours: a session in it was placed by the window that had the keyboard at its last
prompt, and only while that window's title may be the session's. A window's title is its active
pane's, so in a split or a tab the other sessions had no box until you typed into them again.

With remote control on (`allow_remote_control socket-only` or `yes`, and `listen_on`), every
pane's shell gets KITTY_LISTEN_ON and KITTY_WINDOW_ID, which the hook records, and `ls` over
that socket says which pane is in which window. The detector reads it (Windows) as the strongest
evidence, as it does what Sidelit's Terminator plugin says (detect.BY_PANE).

The socket is spoken to directly, in kitty's remote-control protocol (a DCS escape around
JSON), not through `kitty @`, which would start a Python process for each kitty every few
seconds. A kitty without remote control, or with a password on it, says nothing.
"""
from __future__ import annotations

import json
import socket
import threading
import time

PREFIX, SUFFIX = b"\x1bP@kitty-cmd", b"\x1b\\"
VERSION = [0, 26, 0]   # (kitty answers `ls` from any client version; 0.26 is old enough to have it)
TIMEOUT_S = 0.3
FRESH_S = 3.0          # an answer is used this long before kitty is asked again
SILENT_S = 60.0        # a socket that did not answer is asked again after this long
MAX_REPLY = 8 << 20


def pane_key(listen: str, window_id) -> str:
    """One pane of one kitty, as the hook records it (KITTY_LISTEN_ON, KITTY_WINDOW_ID)."""
    return f"{listen}#{window_id}"


def address(listen: str) -> str | None:
    """The unix socket of a KITTY_LISTEN_ON ("unix:@name" is an abstract one); None for others."""
    if not listen.startswith("unix:") or len(listen) <= 5:
        return None
    path = listen[5:]
    return "\0" + path[1:] if path.startswith("@") else path


def ls(listen: str, timeout: float = TIMEOUT_S) -> list | None:
    """kitty's own `ls` of the kitty listening there: its windows, their tabs and panes."""
    addr = address(listen)
    if not addr:
        return None
    request = {"cmd": "ls", "version": VERSION, "no_response": False, "payload": {}}
    buf = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall(PREFIX + json.dumps(request).encode() + SUFFIX)
            while not buf.endswith(SUFFIX) and len(buf) < MAX_REPLY:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    if not (buf.startswith(PREFIX) and buf.endswith(SUFFIX)):
        return None
    try:
        reply = json.loads(buf[len(PREFIX):-len(SUFFIX)])
        data = json.loads(reply["data"]) if reply.get("ok") else None
    except (ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data, list) else None


def windows_of(listen: str, data: list) -> list[dict]:
    """The windows in an `ls`: {"id", "app", "titles", "terminals"}. `titles` are what the window
    may be titled: its active tab's title and that tab's active pane's (a tab title you set wins);
    `terminals` every pane in it, in every tab, as pane_key()s."""
    out = []
    for ow in data:
        if not isinstance(ow, dict):
            continue
        tabs = [t for t in ow.get("tabs") or [] if isinstance(t, dict)]
        terminals = [pane_key(listen, w.get("id")) for t in tabs for w in t.get("windows") or []
                     if isinstance(w, dict)]
        active = next((t for t in tabs if t.get("is_active")), tabs[0] if tabs else {})
        titles = [active.get("title")] + [w.get("title") for w in active.get("windows") or []
                                          if isinstance(w, dict) and w.get("is_active")]
        out.append({"id": (listen, ow.get("id")), "app": str(ow.get("wm_class") or "kitty"),
                    "titles": [str(t) for t in dict.fromkeys(titles) if t], "terminals": terminals})
    return out


class Windows:
    """Every window of the kittys asked for, each {"app", "titles", "terminals", "since"}
    (since when it has had just those panes), from the newest answers.

    A kitty takes some 12 ms to answer (it answers from its event loop), too long to wait for
    every one of them in the daemon's: they are asked in a thread of their own, at most every
    FRESH_S, and until a kitty first answers, it has no windows here. `background=False` asks
    them in the call instead (the tests)."""

    def __init__(self, ask=ls, clock=time.time, background: bool = True):
        self.ask = ask
        self.clock = clock
        self.background = background
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[float, list | None]] = {}  # socket -> (when asked, its windows)
        self._since: dict[tuple, tuple[frozenset, float]] = {}    # window -> (its panes, since when)
        self._thread: threading.Thread | None = None

    def _stale(self, listen: str, now: float) -> bool:
        at, wins = self._answers.get(listen, (None, None))
        return at is None or now - at >= (FRESH_S if wins is not None else SILENT_S)

    def _ask_all(self, listens: list[str]):
        for listen in listens:
            data = self.ask(listen)
            wins = windows_of(listen, data) if data is not None else None
            with self._lock:
                self._answers[listen] = (self.clock(), wins)

    def __call__(self, listens) -> list[dict]:
        now = self.clock()
        listens = set(listens)
        with self._lock:
            for listen in [k for k in self._answers if k not in listens]:
                del self._answers[listen]
            stale = sorted(k for k in listens if self._stale(k, now))
        if stale and not self.background:
            self._ask_all(stale)
        elif stale and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._ask_all, args=(stale,), name="sidelit-kitty", daemon=True)
            self._thread.start()
        with self._lock:
            out = [w for listen in sorted(listens) for w in (self._answers.get(listen, (0, None))[1] or [])]
        seen = set()
        for w in out:
            panes = frozenset(w["terminals"])
            had = self._since.get(w["id"])
            if had is None or had[0] != panes:
                self._since[w["id"]] = had = (panes, now)
            seen.add(w["id"])
            w["since"] = had[1]
        for key in [k for k in self._since if k not in seen]:
            del self._since[key]
        return [{k: w[k] for k in ("app", "titles", "terminals", "since")} for w in out]
