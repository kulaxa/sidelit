"""sway workspace + window client, over sway's IPC socket ($SWAYSOCK).

sway speaks i3's IPC: each message is "i3-ipc", the length of its payload and its type (two
32-bit numbers in the machine's byte order), then the payload, JSON. GET_TREE answers with every
screen, workspace and window; RUN_COMMAND runs sway commands (`[con_id=7] focus`). A connection
that SUBSCRIBEs gets a message for every change (workspace, window, screen): the daemon watches
that one (fd, pump), takes a window's new title, or which window has the keyboard, straight
from the event, and reads everything again on anything else. sway drops a connection that falls
too far behind; the client then subscribes again and reads everything anew.

Workspaces are sway's own, by name: numbered ones ("3", "3: web"; Mod+3 goes to the one numbered
3) and named ones. Sidelit's id for one is its name, written as Hyprland's are (a named one as
"name:" and its name), so a kept workspace comes back under the same name after a login. sway
drops a workspace once it is empty and no screen shows it, and has no way to keep one: this
client keeps it instead. A workspace Sidelit keeps ("pin") stays in the list after sway dropped
it, as a stand-in with no windows; going there, or moving a window there, makes sway create it
again under its name. "create" makes such a stand-in for a new workspace, numbered after every
one in use (spare_workspace: none waits at the end). What Sidelit keeps is written down in the
runtime directory, for a daemon restart and the one-shot commands. A workspace renamed outside
Sidelit (`rename workspace` in sway) is the same one to it: what is kept follows the new name,
and the names it had stay among its ids while sway runs. sway orders workspaces by their
numbers, so "move" is not in caps.

sway numbers its windows (containers) from the start each time it runs, so a window's
identifier is a token for this run of sway and its number: a window after a login is never
taken for one from before.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import json
import os
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sidelit import config, model, procs

REQUEST_TIMEOUT_S = 2.0
MAX_NAMES = 4  # the names a workspace had that it is still known by (renamed outside Sidelit)
MAGIC = b"i3-ipc"
HEADER = struct.Struct("=6sII")  # magic, payload length, type
RUN_COMMAND, GET_WORKSPACES, SUBSCRIBE, GET_OUTPUTS, GET_TREE, GET_VERSION, GET_CONFIG = 0, 1, 2, 3, 4, 7, 9
EVENT = 1 << 31
EV_WORKSPACE, EV_OUTPUT, EV_WINDOW, EV_SHUTDOWN = EVENT | 0, EVENT | 1, EVENT | 3, EVENT | 6
SCRATCHPAD = "__i3_scratch"
# what `workspace X` (and `rename workspace X to Y`) take for something else than a name, in any case
RESERVED = {"next", "prev", "next_on_output", "prev_on_output", "back_and_forth", "current", "number",
            "output", "gaps", "to"}
NUMBERED = re.compile(r"(\d+)(?::.*)?", re.DOTALL)  # "3", "3: web": what sway numbers

# sway's modifiers, as a shortcut chip shows them (⊞ is Super, as on COSMIC), in that order
MODS = (("⊞", ("mod4", "super")), ("Ctrl", ("control", "ctrl")), ("Alt", ("mod1", "alt")), ("Shift", ("shift",)))
# xkb keycodes of the number row, for bindcode
NUMBER_ROW = {10 + i: str((i + 1) % 10) for i in range(10)}


@dataclass
class Space(model.Workspace):  # a sway workspace: proxy is sway's container id; None for a stand-in
    monitor: str = ""   # the screen it is on (for a stand-in: the one it was on)
    num: int = -1       # sway's number for it (-1: a named one)
    kept: bool = False  # Sidelit keeps it: it stays in the list while sway has dropped it

    @property
    def pinned(self) -> bool:
        return self.kept


@dataclass
class Window(model.Toplevel):  # proxy: sway's container id; identifier: the same, as text
    workspace: str | None = None  # Sidelit's id of its workspace; None in the scratchpad
    output: str = ""
    active: bool = False
    focused_at: float = 0.0

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str  # connector, e.g. "DP-1"


def ws_ref(name: str) -> str:
    """Sidelit's id for the workspace sway calls `name`: a numbered one's name, else "name:" and
    the name (as on Hyprland: a layout carried from one to the other keeps its workspaces)."""
    return name if NUMBERED.fullmatch(name) else f"name:{name}"


def name_of(ref: str) -> str:
    return ref[5:] if ref.startswith("name:") else ref


def number_of(name: str) -> int:
    """sway's number for a workspace name: its leading digits, -1 for none."""
    m = re.match(r"\d+", name)
    return int(m.group()) if m else -1


def _bringable(ref: str) -> bool:
    """Is `ref` a workspace this client can make? Not an id another desktop gave (a hex string
    or a sidelit-N), nor a word `workspace` takes for something else."""
    if ref.startswith("name:"):
        name = ref[5:]
        return bool(name.strip()) and name == name.strip() and not name.startswith("__") \
            and name.lower() not in RESERVED and not ('"' in name and "'" in name) and not name.endswith("\\")
    return bool(NUMBERED.fullmatch(ref))


def quote(text: str) -> str:
    """`text` as one argument of a sway command. sway takes the quotes off and leaves every
    backslash, so nothing is escaped but `$`: sway puts its config's variables ($mod) in every
    argument, quoted or not, and makes `$$` a `$`. A name with a double quote goes in single
    quotes."""
    # (sway leaves a `$` after one backslash as it is)
    text = re.sub(r"(?<!\\)\$|(?<=\\\\)\$", "$$", str(text))
    if '"' in text and "'" not in text:
        return f"'{text}'"
    return '"' + text.replace('"', "") + '"'


def socket_path(env=None) -> str:
    """sway's socket: $SWAYSOCK (or $I3SOCK, which sway sets too), else (a unit started before
    sway handed it over, or with a sway that has gone) the newest of a sway running here. Its
    name is sway-ipc.<uid>.<pid>.sock, in the runtime directory (in /tmp without one)."""
    env = os.environ if env is None else env
    given = ""
    for var in ("SWAYSOCK", "I3SOCK"):
        given = given or (env.get(var) or "").strip()
        path = (env.get(var) or "").strip()
        m = re.fullmatch(r"sway-ipc\.\d+\.(\d+)\.sock", os.path.basename(path))
        # (a sway that crashed leaves its socket behind)
        if path and os.path.exists(path) and (m is None or procs.alive(int(m.group(1)))):
            return path
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    found = []
    for base in (runtime, Path("/tmp")):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for p in entries:
            m = re.fullmatch(r"sway-ipc\.(\d+)\.(\d+)\.sock", p.name)
            if not m or int(m.group(1)) != os.getuid() or not procs.alive(int(m.group(2))):
                continue
            try:
                stamp = p.stat().st_mtime
            except OSError:
                continue
            found.append((stamp, str(p)))
    if not found:
        raise RuntimeError(f"sway's socket is not there (SWAYSOCK={given}); is this a sway session?" if given
                           else "no sway runs in this session (SWAYSOCK is not set)")
    if given:
        print(f"sway: no socket {given} any more; taking the sway running now", file=sys.stderr, flush=True)
    return max(found)[1]


def _recv_exact(s: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            raise OSError("sway closed the connection")
        data += chunk
    return data


# ---- the shortcut chips: sway's config, read the way sway reads it -----------------------------
def _expand(text: str, variables: dict) -> str:
    """sway's variables ($mod) in a line, the longest names first."""
    for name in sorted(variables, key=len, reverse=True):
        text = text.replace(name, variables[name])
    return text


def _args(text: str) -> list[str]:
    """A line's words as sway splits them: quotes keep spaces together and are taken off."""
    out, cur, quoting, started = [], "", None, False
    for ch in text:
        if quoting:
            if ch == quoting:
                quoting = None
            else:
                cur += ch
        elif ch in "\"'":
            quoting, started = ch, True
        elif ch.isspace():
            if cur or started:
                out.append(cur)
            cur, started = "", False
        else:
            cur += ch
    if cur or started:
        out.append(cur)
    return out


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def config_lines(text: str, path: str = "", depth: int = 0, seen: set | None = None, variables: dict | None = None):
    """Each command of a sway config, its `include`s read in place (their paths with the
    config's variables), continued lines joined (a comment never continues)."""
    seen = set() if seen is None else seen
    variables = {} if variables is None else variables
    here = os.path.dirname(path) if path else os.path.expanduser("~/.config/sway")
    joined = ""
    for raw in text.splitlines():
        if not joined and raw.startswith("#"):
            continue  # (as sway: a comment in the first column never continues)
        if raw.endswith("\\"):
            joined += raw[:-1] + " "
            continue
        line, joined = (joined + raw).strip(), ""
        if not line or line.startswith("#"):
            continue
        words = line.split(None, 2)
        if words[0] == "set" and len(words) == 3:
            variables[words[1]] = _expand(words[2].strip(), variables)
        if words[0] == "include" and depth < 8:
            target = _expand(line.split(None, 1)[1], variables) if len(words) > 1 else ""
            pattern = os.path.expandvars(os.path.expanduser(_unquote(target)))
            if pattern and not os.path.isabs(pattern):
                pattern = os.path.join(here, pattern)
            for name in sorted(glob.glob(pattern)):
                real = os.path.realpath(name)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    with open(real, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                yield from config_lines(content, real, depth + 1, seen, variables)
            continue
        yield line


def _target(command: str):
    """Where a bound command goes: ("number", 3), ("name", "web"), or None (not a workspace)."""
    command = re.split(r"[;,]", command, maxsplit=1)[0].strip()
    words = command.split()
    if not words or words[0] != "workspace":
        return None
    rest = [w for w in words[1:] if not w.startswith("--")]
    if not rest:
        return None
    if rest[0].lower() == "number" and len(rest) > 1:
        n = number_of(_unquote(" ".join(rest[1:])))
        return ("number", n) if n >= 0 else None
    name = _unquote(" ".join(rest))
    return None if name.lower() in RESERVED else ("name", name)


def chip_text(mods: list[str], key: str) -> str:
    """sway's modifiers and key as the chip on a tab shows them: "⊞ 3", "Ctrl Alt F3"."""
    lowered = [m.lower() for m in mods]
    parts = [label for label, names in MODS if any(m in names for m in lowered)]
    parts += [m for m in mods if not any(m.lower() in names for _, names in MODS)]
    return " ".join(parts + [key]) if key else ""


def switch_keys(lines) -> dict:
    return parse_config(lines)[0]


def parse_config(lines) -> tuple[dict, dict]:
    """What Sidelit takes from a sway config: ("number", 3) or ("name", "web") -> the chip for
    the key that goes there, from its `bindsym` and `bindcode` lines (in the default mode); and
    Sidelit's id of a workspace -> the screens `workspace NAME output SCREEN...` puts it on."""
    variables: dict[str, str] = {}
    keys: dict = {}
    outputs: dict[str, list[str]] = {}
    blocks: list[tuple] = []  # the `{ ... }` blocks we are in: (first word, what follows it)
    for line in lines:
        if line == "}" or line.startswith("}"):
            if blocks:
                blocks.pop()
            continue
        words = line.split()
        if words[0] == "set" and len(words) >= 3 and not blocks:
            variables[words[1]] = _expand(line.split(None, 2)[2].strip(), variables)
            continue
        if line.endswith("{"):
            blocks.append((words[0], _unquote(" ".join(w for w in words[1:-1] if not w.startswith("--")))))
            continue
        default = all(b == ("mode", "default") for b in blocks)  # (bindings outside a mode, or in the default one)
        if blocks and blocks[-1][0] in ("bindsym", "bindcode") and all(b == ("mode", "default") for b in blocks[:-1]):
            kind, rest = blocks[-1][0], line
        elif default and words[0] in ("bindsym", "bindcode"):
            kind, rest = words[0], line.split(None, 1)[1] if len(words) > 1 else ""
        elif not blocks and words[0] == "workspace":
            where = _args(_expand(line, variables))[1:]
            at = next((i for i, w in enumerate(where) if w.lower() == "output"), None)
            if at:  # (sway adds the screens of each such line for a workspace to the ones before)
                outputs.setdefault(ws_ref(" ".join(where[:at])), []).extend(where[at + 1:])
            continue
        else:
            continue
        parts = _expand(rest, variables).split()
        while parts and parts[0].startswith("--"):
            parts.pop(0)
        if len(parts) < 2:
            continue
        combo, command = parts[0], " ".join(parts[1:])
        target = _target(command)
        if target is None:
            continue
        *mods, key = combo.split("+")
        if kind == "bindcode":
            key = NUMBER_ROW.get(int(key) if key.isdigit() else 0, "")
            if not key:
                continue
        lowered = {m.lower() for m in mods}
        rank = (not lowered & {"mod4", "super"}, len(mods))  # Super first, then the fewest modifiers
        if target not in keys or rank < keys[target][0]:
            keys[target] = (rank, chip_text(mods, key))
    return {t: chip for t, (_, chip) in keys.items()}, outputs


class SwayClient:
    caps = frozenset(("create", "rename", "remove", "pin", "focus", "close_toplevel"))
    spare_workspace = False  # no empty workspace waits for new windows: create makes a stand-in

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True,
                 env=None, state_dir: Path | None = None):
        """`env` and `state_dir` are for the tests: where to find sway, and where the workspaces
        Sidelit keeps are written down (the runtime directory)."""
        self.on_change = on_change
        self.primary = primary
        self.path = socket_path(env)
        m = re.fullmatch(r"sway-ipc\.\d+\.(\d+)\.sock", os.path.basename(self.path))
        self.token = procs.run_token(int(m.group(1)) if m else 0, self.path)  # (this run of sway)
        self.connections = 0  # event connections made: the daemon watches the newest one
        self.workspaces: dict[str, Space] = {}  # by Sidelit's id (ws_ref)
        self.toplevels: dict[str, Window] = {}  # by identifier (the token and sway's container id)
        self._had: dict[int, list[str]] = {}    # sway's id of a workspace -> its ids while sway runs, the last one now
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()      # (the daemon sets it: every id its layout knows)
        self.focused_screen = ""
        self._ws: list[dict] = []    # sway's workspaces, as it listed them
        self._order: list[str] = []  # the screens in use, left to right
        self._ids: dict[str, str] = {}  # screen -> "make model serial", as a config can name it
        self.blind = False  # no screen on: sway says nothing of what is where (refresh)
        self._keys: dict = {}
        self._bound: dict[str, list[str]] = {}  # workspaces your config puts on screens -> those screens
        self._req: socket.socket | None = None
        self._events: socket.socket | None = None
        self._buf = b""
        self._state = (state_dir or config.RUNTIME_DIR) / f"sway-{m.group(1) if m else 'socket'}.json"
        self._kept: dict[str, dict] = {}  # what Sidelit keeps: id -> {"screen": the screen it was on}
        self._kept_stamp = None
        self._load_kept()
        version = self._ask(GET_VERSION)
        if not isinstance(version, dict):
            raise RuntimeError(f"sway does not answer on {self.path}")
        self._config_path = str(version.get("loaded_config_file_name") or "")
        self._read_binds()
        if primary:
            self._subscribe()
            self._forget_old_states()
        if not self.refresh():
            self.close()
            raise RuntimeError("sway did not say what is on its screens")

    # ---- the socket -------------------------------------------------------------------------
    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        s.settimeout(REQUEST_TIMEOUT_S)
        try:
            s.connect(self.path)
        except OSError:
            s.close()
            raise
        return s

    @staticmethod
    def _send(s: socket.socket, kind: int, payload: str = ""):
        body = payload.encode()
        s.sendall(HEADER.pack(MAGIC, len(body), kind) + body)

    @staticmethod
    def _read(s: socket.socket) -> tuple[int, bytes]:
        magic, length, kind = HEADER.unpack(_recv_exact(s, HEADER.size))
        if magic != MAGIC:
            raise OSError("not an i3-ipc message")
        return kind, _recv_exact(s, length)

    def _ask(self, kind: int, payload: str = ""):
        """sway's answer to one request, as JSON; None when it could not be asked. One
        connection carries them all, made again if it went."""
        for attempt in (1, 2):
            try:
                if self._req is None:
                    self._req = self._connect()
                self._send(self._req, kind, payload)
                got, body = self._read(self._req)
                if got != kind:
                    raise OSError(f"an answer of type {got} to a request of type {kind}")
                return json.loads(body.decode(errors="replace"))
            except (OSError, ValueError) as exc:
                if self._req is not None:
                    self._req.close()
                    self._req = None
                if attempt == 2:
                    print(f"sway: request {kind} {payload[:60]}: {exc}", file=sys.stderr, flush=True)
        return None

    def _command(self, command: str) -> bool:
        """Run sway commands (`;` between them); False, said, when one failed."""
        replies = self._ask(RUN_COMMAND, command)
        bad = [r for r in replies if isinstance(r, dict) and not r.get("success")] if isinstance(replies, list) else [{}]
        if bad:
            print(f"sway: {command[:120]}: {bad[0].get('error') or 'no answer'}", file=sys.stderr, flush=True)
        return not bad

    def _subscribe(self):
        """The events, on a connection of their own."""
        try:
            s = self._connect()
            self._send(s, SUBSCRIBE, json.dumps(["workspace", "window", "output", "shutdown"]))
            kind, body = self._read(s)
            if kind != SUBSCRIBE or not json.loads(body.decode(errors="replace")).get("success"):
                s.close()
                raise OSError("sway refused the subscription")
        except (OSError, ValueError, AttributeError) as exc:
            raise RuntimeError(f"sway's events: {exc}") from None
        s.setblocking(False)
        self._events, self._buf = s, b""
        self.connections += 1

    # ---- what Sidelit keeps, across a daemon restart ----------------------------------------
    def _kept_changed(self) -> bool:
        """Has the record changed since it was read (a one-shot `sidelit ws pin`, say)?"""
        try:
            st = self._state.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        return stamp != self._kept_stamp

    def _load_kept(self):
        try:
            st = self._state.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._kept_stamp and stamp is not None:
            return
        self._kept_stamp = stamp
        try:
            kept = json.loads(self._state.read_text()).get("kept") or {}
            self._kept = {str(r): {"screen": str((p or {}).get("screen") or "")} for r, p in kept.items()
                          if isinstance(p, dict) and _bringable(str(r))}
        except (OSError, ValueError, AttributeError):
            self._kept = {} if stamp is None else self._kept  # (a broken file: what was read last)

    def _note(self, changes: dict, still: dict | None = None):
        """Write down what is kept (id -> its record; None: let go) over what the file says now,
        under a lock: a one-shot `sidelit ws pin` and the daemon both write it. `still`: a record
        that only follows what happened (a move, a rename) is written only while the id it
        follows is kept still (not let go meanwhile by the other)."""
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            lock = open(self._state.with_suffix(".lock"), "a")
        except OSError as exc:
            print(f"sway: could not write {self._state}: {exc}", file=sys.stderr, flush=True)
            return
        with lock:
            end = time.monotonic() + 1.0
            while True:  # (not for ever: a one-shot stopped while it held the lock would hold up the daemon)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > end:
                        break
                    time.sleep(0.02)
            self._load_kept()
            kept = dict(self._kept)
            for ref, rec in changes.items():
                if rec is None:
                    kept.pop(ref, None)
                elif still is None or ref not in still or still[ref] in self._kept:
                    kept[ref] = rec
            try:
                tmp = self._state.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"kept": kept}))
                tmp.replace(self._state)
                st = self._state.stat()
                self._kept_stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            except OSError as exc:
                print(f"sway: could not write {self._state}: {exc}", file=sys.stderr, flush=True)
            self._kept = kept

    def _forget_old_states(self):
        """The notes of sways that have gone (a login ago) go too; not those of another sway
        still running (a nested one, a headless one for tests)."""
        for old in self._state.parent.glob("sway-*.*"):
            m = re.fullmatch(r"sway-(\d+)\.(json|lock)", old.name)
            tmp = re.fullmatch(r"sway-\d+\.(\d+)\.tmp", old.name)  # (left by a writer that stopped halfway)
            if (m and old.with_suffix(".json") != self._state and not procs.alive(int(m.group(1)))) \
                    or (tmp and not procs.alive(int(tmp.group(1)))):
                try:
                    old.unlink()
                except OSError:
                    pass

    def _read_binds(self):
        got = self._ask(GET_CONFIG)
        text = str(got.get("config") or "") if isinstance(got, dict) else ""
        self._keys, self._bound = parse_config(config_lines(text, self._config_path))

    # ---- what sway says ---------------------------------------------------------------------
    def refresh(self) -> bool:
        """Read screens, workspaces and windows again; False when sway did not answer. With no
        screen on (the only one unplugged, a laptop's lid closed), sway keeps every workspace on
        a screen of its own that it lists nowhere (and lists no workspace: it lists one at least
        for each screen on): what was there is taken to be there still, and `blind` says so,
        for the engine to start nothing meanwhile."""
        outputs, spaces, tree = self._ask(GET_OUTPUTS), self._ask(GET_WORKSPACES), self._ask(GET_TREE)
        if not isinstance(spaces, list) or not isinstance(tree, dict):
            return False
        placed = None
        if isinstance(outputs, list):
            # (a VR headset is listed without "active": not a screen)
            placed = [(int((o.get("rect") or {}).get("x") or 0), int((o.get("rect") or {}).get("y") or 0), str(o["name"]))
                      for o in outputs if isinstance(o, dict) and o.get("name") and o.get("active") is True]
        self.blind = placed == [] or not spaces
        if self.blind:
            return True
        if placed is not None:
            self._order = [name for _, _, name in sorted(placed)]
            self._ids = {str(o["name"]): " ".join(str(o.get(k) or "Unknown") for k in ("make", "model", "serial"))
                         for o in outputs if isinstance(o, dict) and o.get("name")}
        self._ws = [w for w in spaces if isinstance(w, dict) and w.get("name") is not None]
        self._windows(tree)
        self._build()
        return True

    def _windows(self, tree: dict):
        """Every window in the tree, with its workspace and screen. One in the scratchpad, or
        sticky (floating, and sway takes it along to every workspace you go to on its screen), is
        on no workspace of its own."""
        now, seen = time.time(), set()

        def walk(node: dict, output: str, ref: str | None):
            kind = node.get("type")
            if kind == "output":
                output = str(node.get("name") or "")
            elif kind == "workspace":
                name = str(node.get("name") or "")
                ref = None if name == SCRATCHPAD else ws_ref(name)
            elif kind == "floating_con" and node.get("sticky"):
                ref = None
            if "shell" in node or "window_properties" in node:  # a window (a view): a leaf
                key = f"{self.token}-{node.get('id')}"
                seen.add(key)
                tl = self.toplevels.get(key)
                if tl is None:
                    tl = self.toplevels[key] = Window(proxy=int(node.get("id") or 0), identifier=key)
                tl.title = str(node.get("name") or "")
                tl.app_id = str(node.get("app_id") or (node.get("window_properties") or {}).get("class") or "")
                tl.pid = int(node.get("pid") or 0)
                tl.workspace = ref
                tl.output = output if ref is not None else ""
                self._focus(tl, bool(node.get("focused")), now)
                return
            for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
                if isinstance(child, dict):
                    walk(child, output, ref)

        walk(tree, "", None)
        for key in [k for k in self.toplevels if k not in seen]:
            self._gone(self.toplevels.pop(key), now)

    def _build(self):
        """Sidelit's workspaces from what sway said, and the stand-ins for those it dropped."""
        self._load_kept()
        live: dict[str, Space] = {}
        order = []
        self.focused_screen = ""
        changes, still = {}, {}
        for item in self._ws:  # (a workspace keeps sway's id when it is renamed)
            ref, had = ws_ref(str(item["name"])), self._had.setdefault(item.get("id"), [])
            screen = str(item.get("output") or "")
            if had and had[-1] != ref and had[-1] in self._kept and ref not in self._kept:
                # renamed outside Sidelit: what is kept follows
                changes[ref], still[ref] = {"screen": screen or self._kept[had[-1]]["screen"]}, had[-1]
                changes.setdefault(had[-1], None)
            elif ref in self._kept and screen in self._order and self._kept[ref]["screen"] != screen \
                    and self._kept[ref]["screen"] in self._order:
                # moved (not taken off a screen that went: sway takes it back there when it returns,
                # and so does its stand-in): it comes back where it was last
                changes[ref], still[ref] = {"screen": screen}, ref
            if not had or had[-1] != ref:
                had[:] = ([r for r in had if r != ref] + [ref])[-MAX_NAMES:]
        if changes:
            self._note(changes, still)
        for place, item in enumerate(self._ws):
            name = str(item["name"])
            ref = ws_ref(name)
            ws = self.workspaces.get(ref) or Space(id=ref)
            ws.proxy, ws.name, ws.monitor = item.get("id"), name, str(item.get("output") or "")
            ws.num = int(item.get("num") if item.get("num") is not None else number_of(name))
            ws.state = int(bool(item.get("focused")))
            if ws.state:
                self.focused_screen = ws.monitor
            live[ref] = ws
            order.append(((0, ws.num, place) if ws.num >= 0 else (1, 0, place), ref))
        named = {ws.name.casefold() for ws in live.values()}
        for place, (ref, rec) in enumerate(self._kept.items(), start=len(self._ws)):
            if ref in live or name_of(ref).casefold() in named:
                continue  # (sway's names go in any case: going there would be going to that one)
            ws = self.workspaces.get(ref) or Space(id=ref)
            ws.proxy, ws.name, ws.monitor, ws.state = None, name_of(ref), rec["screen"], 0
            ws.num = number_of(ws.name)
            live[ref] = ws
            order.append(((0, ws.num, place) if ws.num >= 0 else (1, 0, place), ref))
        for position, (_, ref) in enumerate(sorted(order), start=1):
            ws = live[ref]
            ws.coords = (position,)
            ws.kept = ref in self._kept
            ws.copies_pinned = ws.kept
            ws.removed = False
        for ref, ws in self.workspaces.items():
            if ref not in live:
                ws.removed = True
        self.workspaces = live
        # a workspace renamed outside Sidelit is still known by the names it had, unless another
        # one has such a name now
        proxies = {ws.proxy for ws in live.values() if ws.proxy is not None}
        self._had = {p: had for p, had in self._had.items() if p in proxies}
        for ws in live.values():
            ws.ids = frozenset(r for r in self._had.get(ws.proxy, ()) if r not in live) if ws.proxy is not None \
                else frozenset()
        self.screens = [Screen(n) for n in self._order]
        if not self.focused_screen:
            self.focused_screen = self._order[0] if self._order else ""

    def _focus(self, tl: Window, active: bool, now: float):
        if active and not tl.active:
            tl.focused_at = now  # when it got the keyboard, as it happens
        tl.active = active

    def _gone(self, tl: Window, now: float):
        tl.closed = True
        # clicking a window's close button focuses it a moment before it goes: keep that
        if tl.focused_at:
            self.closed_focus[tl.identifier] = tl.focused_at
        self.closed_focus = {k: t for k, t in self.closed_focus.items() if now - t < 30}

    def _event(self, kind: int, data) -> bool:
        """Take in one event; True when everything must be read again."""
        if not isinstance(data, dict):
            return False
        change = data.get("change")
        if kind == EV_WINDOW:
            con = data.get("container") or {}
            tl = self.toplevels.get(f"{self.token}-{con.get('id')}")
            if change == "title" and tl is not None:
                tl.title = str(con.get("name") or "")
                return False
            if change == "focus" and tl is not None:
                now = time.time()
                for other in self.toplevels.values():
                    self._focus(other, other is tl, now)
                return False
            return change not in ("urgent", "mark")
        if kind == EV_WORKSPACE:
            if change == "reload":
                self._read_binds()
            return change != "urgent"
        return kind == EV_OUTPUT

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    def fd(self) -> int:
        return self._events.fileno() if self._events is not None else -1

    def pump(self):
        """Take in the events that have come. ConnectionError once sway has gone. (sway drops a
        connection that falls too far behind: one dropped while sway still answers is made
        again, and the daemon goes on.)"""
        if self._events is None:
            return
        gone = False
        while True:
            try:
                data = self._events.recv(65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    break
                gone = True
                break
            if not data:
                gone = True
                break
            self._buf += data
        reread = False
        while len(self._buf) >= HEADER.size:
            magic, length, kind = HEADER.unpack_from(self._buf)
            if magic != MAGIC:
                gone = True  # (lost our place in the stream: start over)
                break
            if len(self._buf) < HEADER.size + length:
                break
            body, self._buf = self._buf[HEADER.size:HEADER.size + length], self._buf[HEADER.size + length:]
            if kind == EV_SHUTDOWN:
                raise ConnectionError("sway is quitting (or restarting)")
            try:
                event = json.loads(body.decode(errors="replace"))
            except ValueError:
                continue
            reread = self._event(kind, event) or reread
        if gone:
            self._events.close()
            self._events, self._buf = None, b""
            if self._ask(GET_VERSION) is None:
                raise ConnectionError("sway closed its event connection (it quit or restarted)")
            try:
                self._subscribe()
            except RuntimeError:
                raise ConnectionError("sway closed its event connection (it quit or restarted)") from None
            print("sway: the event connection went; made again", file=sys.stderr, flush=True)
            reread = True
        if reread:
            self.refresh()
        else:
            self._build()  # (what a one-shot command kept or let go)
        if self.on_change:
            self.on_change()

    def wait(self, seconds: float = 1.5):
        """For one-shot commands, which have no event connection: what is where was read on
        connecting and every command is answered once it is done, so only a short pause, then
        everything is read again (for one that waits for a change)."""
        time.sleep(min(max(seconds, 0.0), 0.1))
        if self._events is not None:
            self.pump()
        else:
            self.refresh()

    def roundtrip(self):
        self.wait(0)

    def settle(self, max_rounds: int = 6):
        self.refresh()

    def close(self):
        for s in (self._events, self._req):
            if s is not None:
                s.close()
        self._events = self._req = None

    def list(self, all_groups: bool = False) -> list[Space]:
        if self._kept_changed():
            self._build()  # (a one-shot `sidelit ws pin` or `unpin` since: seen without waiting for an event)
        return sorted((w for w in self.workspaces.values() if not w.removed), key=lambda w: w.coords)

    def active(self) -> Space | None:
        return next((w for w in self.list() if w.active), None)

    def by_name(self, name: str) -> Space | None:
        return next((w for w in self.list() if w.name == name), None)

    def resolve(self, target: str) -> Space | None:
        """A workspace by name, else by Sidelit's id for it, else by its place (from 1)."""
        found = self.by_name(target) or self.workspaces.get(target)
        if found is None and target.isdigit():
            found = next((w for w in self.list() if w.index == int(target)), None)
        return found if found is not None and not found.removed else None

    def shortcut_label(self, ws, total: int) -> str:
        """The chip on a tab: the key bound to go to that workspace, by its name or its number."""
        return self._keys.get(("name", ws.name)) or (self._keys.get(("number", ws.num), "") if ws.num >= 0 else "")

    # windows
    def workspace_of(self, tl: Window) -> Space | None:
        ws = self.workspaces.get(tl.workspace) if tl.workspace is not None else None
        return ws if ws is not None and not ws.removed else None

    def copy_ids_of(self, tl: Window) -> frozenset:
        ws = self.workspace_of(tl)
        return ws.all_ids if ws is not None else frozenset()

    def toplevels_on(self, ws: Space) -> list[Window]:
        return [t for t in self.toplevels.values() if not t.closed and t.workspace == ws.id]

    def output_names(self) -> set[str]:
        return {s.name for s in self.screens}

    def outputs_of(self, tl: Window) -> list[Screen]:
        return [s for s in self.screens if s.name == tl.output]

    def output_of(self, tl: Window) -> Screen | None:
        return next(iter(self.outputs_of(tl)), None)

    @property
    def can_move_windows(self) -> bool:
        return True

    def _focused_window(self) -> Window | None:
        return next((t for t in self.toplevels.values() if t.active and not t.closed), None)

    def focus(self, tl: Window):
        if not tl.closed:
            self._command(f"[con_id={tl.proxy}] focus")

    def close_toplevel(self, tl: Window):
        if not tl.closed:
            self._command(f"[con_id={tl.proxy}] kill")

    def move_toplevel(self, tl: Window, ws: Space, output: str | None = None) -> bool:
        """Put a window on a workspace, staying where you are (sway makes a stand-in's workspace
        again, on the screen you are on). With `output`, that workspace goes to that screen,
        windows and all: a workspace is on one screen in sway."""
        if tl.closed or ws.removed:
            return False
        ok = True
        if tl.workspace != ws.id:
            ok = self._command(f"[con_id={tl.proxy}] move --no-auto-back-and-forth container to workspace {quote(ws.name)}")
            if ok and output:
                self.refresh()
        if ok and output and output != ws.monitor and output in self.output_names() and ws.proxy is not None:
            shown = self.active()
            had = self._focused_window()
            ok = self._command(f"[con_id={tl.proxy}] move workspace to output {quote(output)}")
            # (sway follows the workspace it moved: back to where you were)
            if had is not None and had is not tl and not had.closed:
                self._command(f"[con_id={had.proxy}] focus")
            elif shown is not None and shown is not ws:
                self._command(f"workspace --no-auto-back-and-forth {quote(shown.name)}")
        return ok

    # workspaces
    def activate(self, ws: Space):
        """Show a workspace; a stand-in sway makes again, on the screen it was on."""
        if ws.removed or ws.active:
            return  # (going to the one you are on would go back to the last one, with workspace_auto_back_and_forth)
        go = f"workspace --no-auto-back-and-forth {quote(ws.name)}"
        if ws.proxy is None and ws.monitor and ws.monitor != self.focused_screen and ws.monitor in self.output_names():
            go = f"focus output {quote(ws.monitor)}; {go}"
        if self._command(go):
            for w in self.workspaces.values():  # (the event confirms it in a moment)
                w.state = int(w is ws)

    def create(self, name: str = "", want: tuple = ()) -> Space | None:
        """A new kept workspace, as a stand-in on the screen you are on (sway makes it once you go
        there or a window is put there): the first of `want` that is free (the name a kept
        workspace had), else the number after every one in use or known to the daemon's layout.
        `name` is the daemon's, and stays Sidelit's own. A number your config puts on another
        screen (`workspace 6 output HDMI-A-1`) is passed over: sway would make it there."""
        self._load_kept()
        live = set(self.workspaces) | set(self._kept)
        names = {name_of(r).casefold() for r in live}  # (sway's names go in any case)
        # (the layout learns a workspace's ids as it goes: the last one is the name it had last)
        ref = next((str(r) for r in reversed(want) if _bringable(str(r)) and name_of(str(r)).casefold() not in names),
                   None)
        if ref is None:
            taken = {int(m.group(1)) for r in live | self.preferred_ids if (m := NUMBERED.fullmatch(name_of(r)))}
            elsewhere = {int(m.group(1)) for r in self._bound if (m := NUMBERED.fullmatch(r))
                         and self._screen_for(r) != self.focused_screen}
            number = max(taken, default=0) + 1
            while number in elsewhere:
                number += 1
            ref = str(number)
        self._note({ref: {"screen": self._screen_for(ref)}})
        self._build()
        return self.workspaces.get(ref)

    def _screen_for(self, ref: str) -> str:
        """Where sway makes workspace `ref`: on the first screen your config puts it on that is
        plugged in (named as sway names it, in any case, or by its make, model and serial; `*`
        is sway's first screen), else on the one you are on."""
        for named in self._bound.get(ref, ()):
            if named == "*":
                found = next((n for n in self._ids if n in self._order), None)
            else:
                found = next((n for n in self._order if n.casefold() == named.casefold() or self._ids.get(n) == named),
                             None)
            if found:
                return found
        return self.focused_screen

    def pin(self, ws: Space, pinned: bool = True):
        """Keep a workspace, or let it go (sway drops it once it is empty and not shown)."""
        if pinned and _bringable(ws.id):
            self._note({ws.id: {"screen": ws.monitor}})
        elif not pinned:
            self._note({ws.id: None})
        self._build()

    def rename(self, ws: Space, name: str):
        """sway's name for a workspace, which is also Sidelit's id for it: what is kept follows."""
        name = " ".join(name.split())
        new = ws_ref(name)
        taken = {w.name.casefold() for w in self.workspaces.values()} | {name_of(r).casefold() for r in self._kept}
        if not name or not _bringable(new) or name.casefold() in taken or ws.name.lower() in RESERVED:
            # (sway's names go in any case: it would not rename it, nor make a second one; and
            # `rename workspace to x` renames the one you are on)
            return
        if ws.proxy is not None and not self._command(f"rename workspace {quote(ws.name)} to {quote(name)}"):
            return
        if ws.kept:
            self._note({new: {"screen": ws.monitor}, ws.id: None})
        self.refresh()

    def remove(self, ws: Space):
        """sway drops a workspace once it is empty and not shown: let go, it goes."""
        self.pin(ws, False)
