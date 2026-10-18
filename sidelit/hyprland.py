"""Hyprland workspace + window client, over Hyprland's two sockets.

Hyprland keeps them in $XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/ (in /tmp/hypr/ before
0.40). Each request is a connection of its own to .socket.sock: "j/clients" answers with every
window as JSON, "dispatch ..." acts. .socket2.sock sends whoever listens a line per event
("openwindow>>..."): the daemon watches it (fd, pump), and takes a window's new title, or which
window has the keyboard, straight from the line; anything else is read again in full.

Hyprland 0.55 brought a Lua config, and a Hyprland running one takes its dispatchers in Lua
(`hl.dsp.focus({window = "address:0x..."})`) and rejects the old form (`focuswindow
address:0x...`). This client asks which it is when it connects (eval exists only with Lua) and
speaks that one.

Workspaces are Hyprland's own: numbered ones (Super+3 goes to 3) and named ones. Sidelit's id
for one is its number ("3"), or "name:" and its name, so a kept workspace comes back under the
same number after a login. Hyprland drops a workspace once it is empty and not shown, unless it
is persistent: "pin" makes it so (a workspace rule, as `workspace = 3, persistent:true` in the
config), and the daemon pins the workspaces it keeps. Hyprland keeps no empty workspace at hand
for new windows (spare_workspace), so the daemon creates a pinned one where it needs a new
workspace, under the number the kept workspace had when that one is free. Rules Sidelit adds
last until Hyprland reloads its config, and are added again then. A rule names its workspace's
screen and follows it when you move the workspace (_set_rules, _follow_moves). Workspaces your
own config keeps are never unpinned, and those it puts on a screen stay on it. Hyprland orders
workspaces by their numbers, so "move" is not in caps.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sidelit import config, model

REQUEST_TIMEOUT_S = 2.0
CREATE_WAIT_S = 1.0   # a workspace pinned under a Lua config appears on Hyprland's next refresh
MAX_NUMBER = 99       # numbered workspaces this client makes or brings back go up to this
SPECIAL = range(-99, -1)  # scratchpads ("special:"); named workspaces are numbered from -1338 down

# events that change what is where: everything is read again (titles and focus are taken from
# the line; the rest, a window's flags and the like, Sidelit does not use)
REREAD = {
    "openwindow", "closewindow", "movewindow", "movewindowv2", "workspace", "workspacev2",
    "createworkspace", "createworkspacev2", "destroyworkspace", "destroyworkspacev2",
    "renameworkspace", "moveworkspace", "moveworkspacev2", "focusedmon", "focusedmonv2",
    "monitoradded", "monitoraddedv2", "monitorremoved", "monitorremovedv2", "pin", "minimized",
    "changefloatingmode", "activespecial", "activespecialv2",
}

# Hyprland's modifier bits, as a shortcut chip shows them (⊞ is Super, as on COSMIC)
MODS = ((64, "⊞"), (4, "Ctrl"), (8, "Alt"), (1, "Shift"))
# xkb keycodes of the number row, for binds written as code:10 (layout-independent)
NUMBER_ROW = {10 + i: str((i + 1) % 10) for i in range(10)}


@dataclass
class Space(model.Workspace):  # a Hyprland workspace: proxy is Hyprland's number for it
    monitor: str = ""         # the screen it is on
    persistent: bool = False  # Hyprland keeps it while it is empty

    @property
    def pinned(self) -> bool:
        return self.persistent


@dataclass
class Window(model.Toplevel):  # identifier: Hyprland's stable id (its address before Hyprland had one)
    address: str = ""         # "0x...": what dispatchers select it by, and events name
    workspace: int | None = None  # Hyprland's number of its workspace; None on a scratchpad or pinned to all
    output: str = ""          # the screen it is on
    active: bool = False      # it has the keyboard
    focused_at: float = 0.0   # when it last got the keyboard

    @property
    def focused(self) -> bool:
        return self.active


@dataclass(frozen=True)
class Screen:
    name: str  # connector, e.g. "DP-1"


def _lua(text: str) -> str:
    """`text` as a Lua string literal."""
    out = []
    for ch in str(text):
        if ch in '\\"':
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def ws_ref(hypr_id: int, name: str) -> str:
    """Sidelit's id for a workspace, which is also how Hyprland's dispatchers name it."""
    return str(hypr_id) if hypr_id > 0 else f"name:{name}"


def _bringable(ref: str) -> bool:
    """Is `ref` a workspace this client can make (an id another desktop gave is not)?"""
    if ref.startswith("name:"):
        return len(ref) > 5
    return ref.isdigit() and 0 < int(ref) <= MAX_NUMBER


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock(d: Path) -> tuple[int | None, str]:
    """(pid, Wayland display) from a Hyprland's lock file; pid None when that Hyprland has gone
    (0 when the file does not say)."""
    try:
        lines = (d / "hyprland.lock").read_text().split()
        pid, display = int(lines[0]), (lines[1] if len(lines) > 1 else "")
    except (OSError, ValueError, IndexError):
        return 0, ""
    return (pid if _alive(pid) else None), display


def instance_dir(env=None) -> Path:
    """The folder with this session's Hyprland sockets; RuntimeError when there is none.

    Without HYPRLAND_INSTANCE_SIGNATURE (a unit started before Hyprland handed it over), or with
    one of a Hyprland that has gone (the unit kept the one from before Hyprland restarted), the
    Hyprland that runs here: the one on our WAYLAND_DISPLAY, else the newest."""
    env = os.environ if env is None else env
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    bases = (runtime / "hypr", Path("/tmp/hypr"))
    sig = (env.get("HYPRLAND_INSTANCE_SIGNATURE") or "").strip()
    if sig:
        for base in bases:
            if (base / sig / ".socket.sock").exists() and _lock(base / sig)[0] is not None:
                return base / sig
    found = []
    for base in bases:
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for d in entries:
            if not (d / ".socket.sock").exists():
                continue
            pid, display = _lock(d)
            if pid is None:
                continue  # a Hyprland that crashed left its folder behind
            try:
                stamp = d.stat().st_mtime
            except OSError:
                stamp = 0.0
            found.append((bool(display) and display == env.get("WAYLAND_DISPLAY"), stamp, str(d)))
    if not found:
        raise RuntimeError(f"Hyprland's socket is not there (HYPRLAND_INSTANCE_SIGNATURE={sig}); is this a "
                           "Hyprland session?" if sig else "no Hyprland runs in this session")
    if sig:
        print(f"hyprland: no Hyprland {sig} any more; taking the one running now", file=sys.stderr, flush=True)
    return Path(max(found)[2])


def chip_text(modmask: int, key: str) -> str:
    """Hyprland's modifier bits and key as the chip on a tab shows them: "⊞ 3", "Ctrl Alt F3"."""
    parts = [label for bit, label in MODS if modmask & bit]
    return " ".join(parts + [key]) if key else ""


def switch_keys(binds: list, lua: bool) -> dict[str, str]:
    """Workspace id -> the chip for the key that goes there, from Hyprland's `j/binds`.

    A bind of the classic config says where it goes ("workspace", "3"). One made in a Lua
    config is a Lua function Hyprland cannot show: where Super and a number key are bound by
    Lua, they are taken to go to that workspace, as Hyprland's own config has them."""
    keys: dict[str, tuple] = {}
    for b in binds:
        if not isinstance(b, dict) or b.get("submap"):
            continue
        key = str(b.get("key") or "") or NUMBER_ROW.get(int(b.get("keycode") or 0), "")
        mods = int(b.get("modmask") or 0)
        if b.get("dispatcher") == "workspace":
            target = str(b.get("arg") or "").strip()
            if not _bringable(target):
                continue
        elif lua and b.get("dispatcher") == "__lua" and mods == 64 and key.isdigit():
            target = str(int(key) or 10)
        else:
            continue
        # Super first, then the fewest modifiers: the key you would press
        rank = (not mods & 64, bin(mods).count("1"))
        if target not in keys or rank < keys[target][0]:
            keys[target] = (rank, chip_text(mods, key))
    return {t: chip for t, (_, chip) in keys.items()}


class HyprlandClient:
    caps = frozenset(("create", "rename", "remove", "pin", "focus", "close_toplevel"))
    spare_workspace = False  # no empty workspace waits for new windows: the daemon creates one

    def __init__(self, on_change: Callable[[], None] | None = None, primary: bool = True,
                 env=None, state_dir: Path | None = None):
        """`env` and `state_dir` are for the tests: where to find Hyprland, and where the
        workspaces Sidelit pinned are written down (the runtime directory)."""
        self.on_change = on_change
        self.primary = primary
        self.dir = instance_dir(env)
        self.signature = self.dir.name
        self.workspaces: dict[str, Space] = {}  # by Sidelit's id (ws_ref)
        self.toplevels: dict[str, Window] = {}  # by identifier
        self.screens: list[Screen] = []
        self.closed_focus: dict[str, float] = {}  # identifier -> when a window that just closed last had focus
        self.preferred_ids: set[str] = set()      # (the daemon sets it: every id its layout knows)
        self.active_id = ""                       # the workspace on the screen with the keyboard
        self._by_address: dict[str, Window] = {}
        self._keys: dict[str, str] = {}
        self._rules: dict[str, dict] = {}  # Hyprland's workspace rules: workspace -> {"persistent", "monitor"}
        self._theirs: set[str] = set()     # workspaces your config keeps (persistent)
        self._bound: dict[str, str] = {}   # workspaces your config puts on a screen -> that screen
        self._state = (state_dir or config.RUNTIME_DIR) / f"hyprland-{self.signature}.json"
        # workspaces Sidelit pinned -> {"screen": its rule's, "bound": that screen is your config's,
        # "own": the rule is Sidelit's alone (not merged into one of yours)}
        self._ours: dict[str, dict] = self._load_ours()
        self.focused_screen = ""
        self.shown: dict[str, str] = {}  # screen -> the workspace it shows
        self._descriptions: dict[str, tuple] = {}  # screen -> (its description, make and model)
        self._events: socket.socket | None = None
        self._buf = b""
        self._moved = False  # a workspace went to another screen since the last pump
        reply = self._request("eval hl.version()")
        if reply is None:
            raise RuntimeError(f"Hyprland does not answer on {self.dir / '.socket.sock'}")
        self.lua = reply.strip() == "ok"  # (only a Lua config has eval)
        self._read_rules()
        self._read_binds()
        if primary:
            self._events = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            try:
                self._events.connect(str(self.dir / ".socket2.sock"))
            except OSError as exc:
                self._events.close()
                self._events = None
                raise RuntimeError(f"Hyprland's event socket: {exc}") from None
            self._events.setblocking(False)
            self._forget_old_states()
            self._repin()  # (after a reload while the daemon was away, Sidelit's rules are gone)
        if not self.refresh():
            self.close()
            raise RuntimeError("Hyprland did not say what is on its screens")

    # ---- the request socket -----------------------------------------------------------------
    def _request(self, text: str) -> str | None:
        """Hyprland's answer to one request; None when it could not be asked."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        s.settimeout(REQUEST_TIMEOUT_S)
        try:
            s.connect(str(self.dir / ".socket.sock"))
            s.sendall(text.encode())
            chunks = []
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks).decode(errors="replace")
        except OSError as exc:
            print(f"hyprland: {text.split(' ', 1)[0]}: {exc}", file=sys.stderr, flush=True)
            return None
        finally:
            s.close()

    def _json(self, what: str):
        reply = self._request(f"j/{what}")
        if reply is None:
            return None
        try:
            return json.loads(reply)
        except ValueError:
            print(f"hyprland: {what}: not JSON: {reply[:120]!r}", file=sys.stderr, flush=True)
            return None

    def _dispatch(self, classic: str, lua: str) -> bool:
        """One dispatcher, in the form this Hyprland speaks."""
        reply = self._request("dispatch " + (lua if self.lua else classic))
        if reply is None or not reply.strip().startswith("ok"):
            print(f"hyprland: dispatch {lua if self.lua else classic}: {(reply or '').strip()[:200]}",
                  file=sys.stderr, flush=True)
            return False
        return True

    def _set_rules(self, rules: list[tuple]) -> bool:
        """Workspace rules: (workspace, persistent, screen, own) each. One that keeps a workspace
        names its screen: whenever Hyprland applies its rules (each time a rule is set, and when a
        screen comes), it moves a persistent workspace whose rule names none to the screen with
        the keyboard. With a Lua config they all go in one go, and Hyprland applies them once;
        Sidelit's own rule is kept by name (sidelit_rules), so that unpinned it is switched off
        and leaves nothing behind. A classic config takes one keyword at a time, and its rule
        keeps its screen until the config is reloaded."""
        if not rules:
            return True
        if self.lua:
            code = []
            for ref, on, screen, own in rules:
                key = _lua(ref)
                if on:
                    where = f", monitor = {_lua(screen)}" if screen else ""
                    rule = f"hl.workspace_rule({{workspace = {key}{where}, persistent = true}})"
                    code.append(f"sidelit_rules = sidelit_rules or {{}}; sidelit_rules[{key}] = {rule}" if own else rule)
                elif own:
                    code.append(f"if sidelit_rules and sidelit_rules[{key}] then sidelit_rules[{key}]:set_enabled(false); "
                                f"sidelit_rules[{key}] = nil end")
                else:
                    code.append(f"hl.workspace_rule({{workspace = {key}, persistent = false}})")
            replies = [self._request("eval " + "; ".join(code))]
        else:
            replies = [self._request(f"keyword workspace {ref}{f',monitor:{screen}' if on and screen else ''},"
                                     f"persistent:{int(on)}") for ref, on, screen, _own in rules]
        bad = [r for r in replies if r is None or r.strip() != "ok"]
        if bad:
            print(f"hyprland: could not set the rules for {', '.join(r[0] for r in rules)}: "
                  f"{(bad[0] or '').strip()[:200]}", file=sys.stderr, flush=True)
        return not bad

    # ---- what Sidelit pinned, across a daemon restart ---------------------------------------
    def _load_ours(self) -> dict[str, dict]:
        try:
            ours = json.loads(self._state.read_text()).get("ours") or {}
            return {str(r): {"screen": str(p.get("screen") or ""), "bound": bool(p.get("bound")),
                             "own": bool(p.get("own"))} for r, p in ours.items() if isinstance(p, dict)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_ours(self, ours: dict):
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"ours": dict(sorted(ours.items()))}))
            tmp.replace(self._state)
        except OSError as exc:
            print(f"hyprland: could not write {self._state}: {exc}", file=sys.stderr, flush=True)

    def _note(self, changes: dict):
        """Write down pins (workspace -> its record; None: unpinned) over what the file says now:
        a one-shot `sidelit ws pin` and the daemon both write it."""
        ours = self._load_ours() if self._state.exists() else dict(self._ours)
        for ref, pin in changes.items():
            if pin is None:
                ours.pop(ref, None)
            else:
                ours[ref] = pin
        self._save_ours(ours)
        self._ours = ours

    def _forget_old_states(self):
        """The notes of Hyprlands that have gone (a login ago) go too."""
        for old in self._state.parent.glob("hyprland-*.json"):
            if old != self._state:
                try:
                    old.unlink()
                except OSError:
                    pass

    def _read_rules(self, reloaded: bool = False):
        """Hyprland's workspace rules, and from them what your config says: the workspaces it
        keeps (Sidelit never unpins those) and the ones it puts on a screen (Sidelit's pin keeps
        them there). Sidelit's pins are merged into the rules of the same workspace, so a rule of
        a workspace Sidelit pinned is taken for yours only just after a config reload, when
        every rule is the config's (`reloaded`)."""
        rules = self._json("workspacerules")
        if not isinstance(rules, list):
            return
        self._rules = {}
        for r in rules:
            ref = str(r.get("workspaceString") or "").strip() if isinstance(r, dict) else ""
            if ref and r.get("enabled", True):
                self._rules[ref] = {"persistent": bool(r.get("persistent")), "monitor": str(r.get("monitor") or "")}
        # (just after a reload, a rule just as Sidelit sets it may be one it set again meanwhile:
        # taken for Sidelit's, not to be kept from unpinning it)
        yours = {ref: r for ref, r in self._rules.items()
                 if ref not in self._ours or (reloaded and not self._as_set(ref, r))}
        self._theirs = {ref for ref, r in yours.items() if r["persistent"]}
        self._bound = {ref: r["monitor"] for ref, r in yours.items() if r["monitor"]}
        self._bound.update({ref: p["screen"] for ref, p in self._ours.items()
                            if p["bound"] and p["screen"] and not reloaded and ref not in self._bound})

    def _as_set(self, ref: str, rule: dict) -> bool:
        """Is this rule just as Sidelit set it for a workspace it pinned?"""
        pin = self._ours.get(ref)
        return pin is not None and rule["persistent"] and rule["monitor"] == pin["screen"]

    def _reloaded(self):
        """Hyprland read its config again: every rule Sidelit added is gone, and your config may
        say something new about the workspaces it pinned. Pin them again."""
        if self._state.exists():
            self._ours = self._load_ours()
        self._read_rules(reloaded=True)
        notes = {}
        for ref, pin in self._ours.items():
            if ref in self._theirs:
                notes[ref] = None  # your config keeps it now
                continue
            screen = self._bound.get(ref)
            own = pin["own"] if self._as_set(ref, self._rules.get(ref) or {"persistent": False, "monitor": ""}) \
                else ref not in self._rules
            notes[ref] = {"screen": screen or pin["screen"], "bound": screen is not None, "own": own}
        self._note(notes)
        self._repin(everything=True)

    def _repin(self, everything: bool = False):
        """Pin again what Sidelit pinned: all of it after a config reload (`everything`), else
        only what Hyprland's rules lack (setting a rule makes Hyprland apply them all again)."""
        todo = []
        for ref, pin in sorted(self._ours.items()):
            have = self._rules.get(ref)
            if ref in self._theirs or (not everything and have and have["persistent"]
                                       and have["monitor"] == pin["screen"]):
                continue
            todo.append((ref, True, pin["screen"], pin["own"]))
        self._set_rules(todo)

    def _follow_moves(self):
        """Workspaces Sidelit pinned that are now on another screen than their rule's, while that
        screen is still there: you moved them, so their rules follow (Hyprland would take them back
        the next time it applies its rules). By where each one is now, read after the moves, and
        all in one go: two workspaces swapped make two rules that change together. One whose
        screen went keeps its rule, so that it goes back there when the screen does; one your
        config puts on a screen stays your config's."""
        if self._state.exists():
            self._ours = self._load_ours()
        moved = {}
        for ref, pin in self._ours.items():
            ws = self.workspaces.get(ref)
            screen = self._screen(pin["screen"]) if pin["screen"] else ""
            if ws is None or pin["bound"] or not ws.monitor or ws.monitor == screen or screen is None:
                continue
            moved[ref] = {**pin, "screen": ws.monitor}
        if not moved:
            return
        shown = dict(self.shown)
        if self._set_rules([(ref, True, p["screen"], p["own"]) for ref, p in sorted(moved.items())]):
            self._note(moved)
        if len(moved) > 1 and not self.lua:
            self._show_again(shown)

    def _show_again(self, shown: dict):
        """A classic config takes rules one at a time, and Hyprland applies them all after each:
        with two workspaces swapped, the first rule takes the other one back to its old screen
        for a moment, and the screen it leaves gets a new, empty workspace. Each screen shows
        again what it showed, and the keyboard goes back where it was."""
        focused = self.focused_screen
        self.refresh()
        switched = False
        for screen, ref in sorted(shown.items()):
            ws = self.workspaces.get(ref)
            if self.shown.get(screen) != ref and ws is not None and ws.monitor == screen:
                switched = self._dispatch(f"workspace {ref}", f"hl.dsp.focus({{workspace = {_lua(ref)}}})") or switched
        if not switched:
            return
        self.refresh()
        back = shown.get(focused)
        if self.focused_screen != focused and back in self.workspaces:
            self._dispatch(f"workspace {back}", f"hl.dsp.focus({{workspace = {_lua(back)}}})")
            self.refresh()

    def _read_binds(self):
        binds = self._json("binds")
        self._keys = switch_keys(binds if isinstance(binds, list) else [], self.lua)

    # ---- what Hyprland says -----------------------------------------------------------------
    def refresh(self) -> bool:
        """Read screens, workspaces and windows again; False when Hyprland did not answer."""
        monitors, spaces, clients = self._json("monitors"), self._json("workspaces"), self._json("clients")
        focused = self._json("activewindow")
        if not isinstance(monitors, list) or not isinstance(spaces, list) or not isinstance(clients, list):
            return False
        now = time.time()
        monitors = [m for m in monitors if isinstance(m, dict) and m.get("name")]
        names = {m.get("id"): str(m["name"]) for m in monitors}
        self.screens = [Screen(n) for n in names.values()]
        self._descriptions = {str(m["name"]): (str(m.get("description") or ""),
                                               f"{m.get('make') or ''} {m.get('model') or ''}".strip()) for m in monitors}
        self.shown = {str(m["name"]): ws_ref(int((m.get("activeWorkspace") or {}).get("id") or 0),
                                             str((m.get("activeWorkspace") or {}).get("name") or "")) for m in monitors}
        on_screen = next((m for m in monitors if m.get("focused")), None)
        self.focused_screen = str((on_screen or {}).get("name") or "")
        self.active_id = self.shown.get(self.focused_screen, "")

        live: dict[str, Space] = {}
        order = []
        for item in spaces:
            if not isinstance(item, dict):
                continue
            hid, name = int(item.get("id") or 0), str(item.get("name") or "")
            if hid == 0 or hid in SPECIAL or name.startswith("special:"):
                continue
            ref = ws_ref(hid, name)
            ws = self.workspaces.get(ref) or Space(id=ref)
            ws.proxy, ws.name, ws.monitor = hid, name, str(item.get("monitor") or "")
            # while the screen of Sidelit's rule is unplugged, Hyprland lets the pin lapse (until
            # the screen is back): still pinned, as far as Sidelit is concerned, so its rule stays
            pin = self._ours.get(ref)
            away = bool(pin and pin["screen"] and self._screen(pin["screen"]) is None)
            ws.persistent = bool(item.get("ispersistent")) or away
            ws.copies_pinned = ws.persistent
            ws.state = int(ref == self.active_id)
            ws.removed = False
            live[ref] = ws
            order.append(((0, hid) if hid > 0 else (1, -hid), ref))  # numbers first, then names as made
        for position, (_, ref) in enumerate(sorted(order), start=1):
            live[ref].coords = (position,)
        for ref, ws in self.workspaces.items():
            if ref not in live:
                ws.removed = True
        self.workspaces = live
        by_number = {ws.proxy: ws for ws in live.values()}

        focused_address = str(focused.get("address") or "") if isinstance(focused, dict) else ""
        seen, by_address = set(), {}
        for item in clients:
            if not isinstance(item, dict) or not item.get("address") or item.get("mapped") is False:
                continue
            address = str(item["address"])
            wid = str(item.get("stableId") or address)
            seen.add(wid)
            tl = self.toplevels.get(wid)
            if tl is None:
                tl = self.toplevels[wid] = Window(proxy=wid, identifier=wid)
            tl.address = address
            tl.app_id = str(item.get("class") or item.get("initialClass") or "")
            tl.title = str(item.get("title") or "")
            tl.pid = int(item.get("pid") or 0)
            number = int((item.get("workspace") or {}).get("id") or 0)
            tl.workspace = number if number in by_number and not item.get("pinned") else None
            tl.output = names.get(item.get("monitor"), "")
            self._focus(tl, address == focused_address, now)
            by_address[_addr(address)] = tl
        for wid in [w for w in self.toplevels if w not in seen]:
            self._gone(self.toplevels.pop(wid), now)
        self._by_address = by_address
        return True

    def _screen(self, selector: str) -> str | None:
        """The screen a rule names (its connector, or desc: and the start of its description), by
        its connector; None when it is not plugged in."""
        if selector in self._descriptions:
            return selector
        if selector.startswith("desc:"):
            start = selector[5:].strip()
            return next((name for name, (desc, short) in self._descriptions.items()
                         if start and (desc.startswith(start) or short.startswith(start))), None)
        return None

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

    def _event(self, name: str, data: str) -> bool:
        """Take in one event line; True when everything must be read again."""
        if name == "windowtitlev2":
            address, _, title = data.partition(",")
            tl = self._by_address.get(_addr(address))
            if tl is None:
                return True
            tl.title = title
            return False
        if name == "activewindowv2":
            now, target = time.time(), self._by_address.get(_addr(data)) if data else None
            for tl in self.toplevels.values():
                self._focus(tl, tl is target, now)
            return bool(data) and target is None
        if name == "configreloaded":
            self._reloaded()
            self._read_binds()
            return True
        if name == "moveworkspacev2":
            # (seen once everything is read again: Hyprland says a screen went only after it
            # moved its workspaces away, and a rule set meanwhile may move them on)
            self._moved = True
        return name in REREAD

    # ---- the client interface (see wsclient.WorkspaceClient) ------------------------------------
    def fd(self) -> int:
        return self._events.fileno() if self._events is not None else -1

    def pump(self):
        """Read the events that have come in. ConnectionError once Hyprland has gone."""
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
                raise ConnectionError(f"Hyprland's event socket: {exc}") from None
            if not data:
                gone = True
                break
            self._buf += data
        reread = False
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            name, sep, rest = line.decode(errors="replace").partition(">>")
            if sep:
                reread = self._event(name, rest) or reread
        if gone:
            raise ConnectionError("Hyprland closed its event socket (it quit or restarted)")
        if reread:
            self.refresh()
        if self._moved:
            self._moved = False
            self._follow_moves()
        if self.on_change:
            self.on_change()

    def wait(self, seconds: float = 1.5):
        """For one-shot commands, which have no event socket: what is where was read on
        connecting and every action is answered as it is made, so only a short pause, then
        everything is read again (for one that waits for a change)."""
        time.sleep(min(max(seconds, 0.0), 0.1))
        if self._events is not None:
            self.pump()
        else:
            self.refresh()

    def roundtrip(self):
        self.pump()

    def settle(self, max_rounds: int = 6):
        self.refresh()

    def close(self):
        if self._events is not None:
            self._events.close()
            self._events = None

    def list(self, all_groups: bool = False) -> list[Space]:
        return sorted((w for w in self.workspaces.values() if not w.removed), key=lambda w: w.coords)

    def active(self) -> Space | None:
        return next((w for w in self.list() if w.active), None)

    def by_name(self, name: str) -> Space | None:
        return next((w for w in self.list() if w.name == name), None)

    def resolve(self, target: str) -> Space | None:
        """A workspace by name, else by its Hyprland number, else by its place (from 1)."""
        found = self.by_name(target) or self.workspaces.get(target)
        if found is None and target.isdigit():
            found = next((w for w in self.list() if w.index == int(target)), None)
        return found if found is not None and not found.removed else None

    def shortcut_label(self, ws, total: int) -> str:
        """The chip on a tab: the key bound to go to that workspace."""
        return self._keys.get(ws.id, "")

    # windows
    def workspace_of(self, tl: Window) -> Space | None:
        """The workspace a window is on; None on a scratchpad, or pinned to every workspace."""
        return next((w for w in self.workspaces.values() if w.proxy == tl.workspace and not w.removed), None) \
            if tl.workspace is not None else None

    def copy_ids_of(self, tl: Window) -> frozenset:
        ws = self.workspace_of(tl)
        return frozenset({ws.id}) if ws is not None else frozenset()

    def toplevels_on(self, ws: Space) -> list[Window]:
        return [t for t in self.toplevels.values() if not t.closed and t.workspace is not None and t.workspace == ws.proxy]

    def output_names(self) -> set[str]:
        return {s.name for s in self.screens}

    def outputs_of(self, tl: Window) -> list[Screen]:
        return [s for s in self.screens if s.name == tl.output]

    def output_of(self, tl: Window) -> Screen | None:
        return next(iter(self.outputs_of(tl)), None)

    @property
    def can_move_windows(self) -> bool:
        return True

    def _window(self, tl: Window) -> str:
        return f"address:{tl.address}"

    def focus(self, tl: Window):
        if not tl.closed:
            self._dispatch(f"focuswindow {self._window(tl)}", f"hl.dsp.focus({{window = {_lua(self._window(tl))}}})")

    def close_toplevel(self, tl: Window):
        if not tl.closed:
            self._dispatch(f"closewindow {self._window(tl)}",
                           f"hl.dsp.window.close({{window = {_lua(self._window(tl))}}})")

    def move_toplevel(self, tl: Window, ws: Space, output: str | None = None) -> bool:
        """Put a window on a workspace, staying where you are. With `output`, that workspace goes
        to that screen, windows and all: a workspace is on one screen in Hyprland (this puts it
        back where it was after a screen came back)."""
        if tl.closed or ws.removed:
            return False
        ok = True
        if tl.workspace != ws.proxy:
            ok = self._dispatch(f"movetoworkspacesilent {ws.id},{self._window(tl)}",
                                f"hl.dsp.window.move({{workspace = {_lua(ws.id)}, follow = false, "
                                f"window = {_lua(self._window(tl))}}})")
        if ok and output and output != ws.monitor and output in self.output_names():
            ok = self._dispatch(f"moveworkspacetomonitor {ws.id} {output}",
                                f"hl.dsp.workspace.move({{workspace = {_lua(ws.id)}, monitor = {_lua(output)}}})")
        return ok

    # workspaces
    def activate(self, ws: Space):
        if ws.removed or ws.id == self.active_id:
            return  # (going to the one you are on would go back to the last one, with workspace_back_and_forth)
        if self._dispatch(f"workspace {ws.id}", f"hl.dsp.focus({{workspace = {_lua(ws.id)}}})"):
            self.active_id = ws.id  # (the event confirms it in a moment)
            for w in self.workspaces.values():
                w.state = int(w is ws)

    def create(self, name: str = "", want: tuple = ()) -> Space | None:
        """A new workspace, pinned: the first of `want` that is free (the number a kept workspace
        had), else the number after every one in use or known to the daemon's layout. Hyprland
        names it by its number; `name` is the daemon's, and stays Sidelit's own."""
        live = set(self.workspaces)

        def can_make(r: str) -> bool:
            # (not one your config keeps and Hyprland has not made, nor one it puts on a screen
            # that is not plugged in: Hyprland would make neither now)
            return _bringable(r) and r not in live and r not in self._theirs \
                and (r not in self._bound or self._screen(self._bound[r]) is not None)
        ref = next((str(r) for r in want if can_make(str(r))), None)
        if ref is None:
            def numbers(refs):
                return {int(r) for r in refs if _bringable(r) and not r.startswith("name:")}
            taken = numbers(live | self.preferred_ids | set(self._ours) | self._theirs)
            # (passing over one your config puts on another screen: it would open there)
            elsewhere = numbers(r for r, screen in self._bound.items() if self._screen(screen) != self.focused_screen)
            number = next((n for n in range(max(taken, default=0) + 1, MAX_NUMBER + 1) if n not in elsewhere), 0) \
                or next((n for n in range(1, MAX_NUMBER + 1) if n not in taken | elsewhere), 0)
            if not number:
                print("hyprland: every workspace number is in use", file=sys.stderr, flush=True)
                return None
            ref = str(number)
        if not self._pin_ref(ref, True, self.focused_screen):  # (where Hyprland makes it)
            return None
        end = time.monotonic() + CREATE_WAIT_S
        while True:
            self.refresh()
            if ref in self.workspaces or time.monotonic() > end:
                break
            time.sleep(0.02)
        return self.workspaces.get(ref)

    def _pin_ref(self, ref: str, pinned: bool, screen: str = "") -> bool:
        """Pin or unpin a workspace by its id. One your config keeps is left as your config has
        it; one your config puts on a screen is pinned there (its rule's screen stays yours)."""
        if ref in self._theirs:
            return True
        if self._state.exists():
            self._ours = self._load_ours()
        was = self._ours.get(ref)
        if pinned:
            bound = self._bound.get(ref)
            pin = {"screen": bound or screen, "bound": bound is not None,
                   "own": was["own"] if was else ref not in self._rules}
            if not self._set_rules([(ref, True, pin["screen"], pin["own"])]):
                return False
            self._note({ref: pin})
        else:
            if not self._set_rules([(ref, False, "", bool(was and was["own"]))]):
                return False
            self._note({ref: None})
        return True

    def pin(self, ws: Space, pinned: bool = True):
        if self._pin_ref(ws.id, pinned, ws.monitor):
            self.refresh()

    def rename(self, ws: Space, name: str):
        """Hyprland's name for a workspace. A named one's id is its name, so a pin goes along."""
        name = " ".join(name.split())  # (one line: a request is one line)
        pin = self._ours.get(ws.id) if ws.id.startswith("name:") else None
        if not name or not self._dispatch(f"renameworkspace {ws.proxy} {name}",
                                          f"hl.dsp.workspace.rename({{workspace = {_lua(ws.id)}, name = {_lua(name)}}})"):
            return
        if pin is not None:
            self._pin_ref(ws_ref(ws.proxy, name), True, pin["screen"])  # (first: not to lose it, empty)
            self._pin_ref(ws.id, False)
        self.refresh()

    def remove(self, ws: Space):
        """Hyprland drops a workspace once it is empty and not shown: unpinned, it goes."""
        self.pin(ws, False)


def _addr(address: str) -> str:
    """An address as events write it (no 0x), the same however it was written."""
    a = address.strip().lower()
    return a[2:] if a.startswith("0x") else a
