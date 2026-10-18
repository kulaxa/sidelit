"""A Hyprland in miniature for the Hyprland backend's tests: its two sockets, in a thread.

It answers where hyprland.py asks, the way Hyprland 0.56 does (checked against a headless
Hyprland 0.56.2 and its source, with a Lua config and with a classic one): `j/monitors`,
`j/workspaces`, `j/clients`, `j/activewindow`, `j/workspacerules` and `j/binds` as JSON;
`dispatch` in the form its config takes (Lua's `hl.dsp...(...)`, or the classic `focuswindow
address:0x...`), the other one refused as Hyprland refuses it; workspace rules through `eval
hl.workspace_rule(...)` (Lua, a rule kept by name can be switched off) or `keyword workspace
N,monitor:M,persistent:1` (classic).

Workspace rules work as Hyprland's do: a rule for a workspace that has one already is merged
into it; whenever the rules are applied (a keyword at once, a Lua eval once it has run, a config
reload, a screen coming) every persistent workspace is made and put on its rule's screen, or on
the screen with the keyboard when its rule names none, and one whose rule names a screen that is
not there loses its pin for the while. A reload brings back the config's rules alone. Like
Hyprland it drops a workspace that is empty, not shown and not persistent, and tells the event
socket what happened, a line each. Tests drive it directly: open, retitle and close windows,
switch, swap and move workspaces, unplug and replug a screen, reload the config.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
from pathlib import Path

SPECIAL_NAME = "special:magic"
SPECIAL_ID = -98


def _unlua(literal: str):
    """A Lua literal from Sidelit's dispatchers as a Python value."""
    if literal in ("true", "false"):
        return literal == "true"
    if literal.startswith('"'):
        body = literal[1:-1]
        return re.sub(r"\\(\d{3}|.)", lambda m: chr(int(m.group(1))) if m.group(1).isdigit() else m.group(1), body)
    return int(literal)


_STR = r'"(?:[^"\\]|\\.)*"'
_CALL = re.compile(r"^(?:return )?hl\.([\w.]+)\((.*)\)\s*$", re.S)
_FIELD = re.compile(rf'(\w+)\s*=\s*({_STR}|true|false|-?\d+)')
# what Sidelit's evals do with rules: set one (kept by name, or not), or switch a kept one off
_RULES = re.compile(rf'(?:sidelit_rules\[({_STR})\]\s*=\s*)?hl\.workspace_rule\((\{{.*?\}})\)'
                    rf'|sidelit_rules\[({_STR})\]:set_enabled\(false\)')


def _fields(table: str) -> dict:
    return {k: _unlua(v) for k, v in _FIELD.findall(table)}


def _call(code: str):
    """`hl.dsp.window.move({workspace = "3", follow = false})` -> ("dsp.window.move", {...})."""
    m = _CALL.match(code.strip())
    if not m:
        return None, {}
    return m.group(1), _fields(m.group(2))


class FakeHyprland:
    def __init__(self, runtime: Path, lua: bool = True, signature: str = "abc123_1792321613_1",
                 monitors: tuple = ("DP-1",), wayland: str = "wayland-9", pid: int | None = None):
        self.dir = Path(runtime) / "hypr" / signature
        self.dir.mkdir(parents=True)
        (self.dir / "hyprland.lock").write_text(f"{pid or os.getpid()}\n{wayland}\n")
        self.signature = signature
        self.lua = lua
        self.lock = threading.RLock()
        self.requests: list[str] = []
        self.events: list[str] = []
        self.monitors = [{"id": i, "name": n, "ws": i + 1} for i, n in enumerate(monitors)]
        self.focused_monitor = 0
        self.workspaces: dict[int, dict] = {i + 1: {"name": str(i + 1), "monitor": i, "persistent": False}
                                            for i in range(len(monitors))}
        self.config_rules: list[dict] = []  # as the config file has them
        self.rules: list[dict] = []         # Hyprland's: the config's, and those added since
        self.handles: dict[str, dict] = {}  # the Lua config's sidelit_rules
        self.windows: dict[str, dict] = {}  # address -> window
        self.binds: list[dict] = []
        self.focused = ""
        self.named = -1337
        self._gone_monitor = None
        self._next = 0x5000
        self._stable = 0x18000000
        self._listeners: list[socket.socket] = []
        self._servers = []
        for name, handler in ((".socket.sock", self._serve_request), (".socket2.sock", self._serve_events)):
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(self.dir / name))
            srv.listen(16)
            self._servers.append(srv)
            threading.Thread(target=self._accept, args=(srv, handler), daemon=True).start()

    def stop(self):
        """Hyprland quits: both sockets close."""
        for srv in self._servers:
            try:
                srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            srv.close()
        with self.lock:
            for c in self._listeners:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self._listeners = []

    # ---- the sockets --------------------------------------------------------------------------
    def _accept(self, srv, handler):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            handler(conn)

    def _serve_events(self, conn):
        with self.lock:
            self._listeners.append(conn)

    def _serve_request(self, conn):
        try:
            text = conn.recv(65536).decode()
            with self.lock:
                self.requests.append(text)
                reply = self.answer(text)
            conn.sendall(reply.encode())
        finally:
            conn.close()

    def emit(self, name: str, data: str):
        line = f"{name}>>{data}\n".encode()
        with self.lock:
            self.events.append(f"{name}>>{data}")
            for c in list(self._listeners):
                try:
                    c.sendall(line)
                except OSError:  # (the client hung up)
                    self._listeners.remove(c)
                    c.close()

    # ---- what is where -------------------------------------------------------------------------
    def mon(self, mid: int) -> dict:
        """A screen by its id."""
        return next(m for m in self.monitors if m["id"] == mid)

    def mon_named(self, name: str) -> dict | None:
        """A screen by a rule's selector: its connector, or desc: and the start of its description."""
        if name.startswith("desc:"):
            return next((m for m in self.monitors if f"Fake Monitors {m['name']} 0x0001".startswith(name[5:].strip())), None)
        return next((m for m in self.monitors if m["name"] == name), None)

    def _ws_json(self, wid: int) -> dict:
        ws = self.workspaces[wid]
        mon = self.mon(ws["monitor"])
        return {"id": wid, "name": ws["name"], "monitor": mon["name"], "monitorID": mon["id"],
                "windows": sum(1 for w in self.windows.values() if w["ws"] == wid),
                "hasfullscreen": False, "lastwindow": "0x0", "lastwindowtitle": "",
                "ispersistent": ws["persistent"], "tiledLayout": "dwindle"}

    def _win_json(self, w: dict) -> dict:
        ws = self.workspaces.get(w["ws"], {"name": ""})
        return {"address": w["address"], "mapped": True, "hidden": False, "at": [0, 0], "size": [800, 600],
                "workspace": {"id": w["ws"], "name": ws["name"]}, "floating": False,
                "monitor": ws.get("monitor", 0), "class": w["class"], "title": w["title"],
                "initialClass": w["class"], "initialTitle": w["title"], "pid": w["pid"], "xwayland": False,
                "pinned": w.get("pinned", False), "fullscreen": 0, "focusHistoryID": 0 if w["address"] == self.focused else 1,
                "stableId": w["stable"]}

    def _ref(self, wid: int) -> str:
        return str(wid) if wid > 0 else f"name:{self.workspaces[wid]['name']}"

    def _persistent(self, wid: int) -> bool:
        return bool(self.workspaces.get(wid, {}).get("persistent"))

    def _visible(self, wid: int) -> bool:
        return any(m["ws"] == wid for m in self.monitors)

    def rule(self, ref: str) -> dict | None:
        """The enabled rule for a workspace."""
        return next((r for r in self.rules if r["enabled"] and r["ws"] == str(ref)), None)

    def find(self, ref: str) -> int | None:
        """A workspace by how dispatchers name it: its number, or name:NAME."""
        ref = str(ref)
        if ref.startswith("name:"):
            return next((i for i, w in self.workspaces.items() if w["name"] == ref[5:] and i < 0), None)
        return int(ref) if ref.lstrip("-").isdigit() and int(ref) in self.workspaces else None

    def _ensure(self, ref: str, monitor: int | None = None) -> int:
        """The workspace `ref`, made where its rule says, else on the screen with the keyboard."""
        wid = self.find(ref)
        if wid is not None:
            return wid
        if monitor is None:
            bound = self.mon_named((self.rule(ref) or {}).get("monitor", ""))
            monitor = bound["id"] if bound else self.focused_monitor
        if ref.startswith("name:"):
            self.named -= 1
            wid, name = self.named, ref[5:]
        else:
            wid, name = int(ref), ref
        self.workspaces[wid] = {"name": name, "monitor": monitor, "persistent": False}
        self.emit("createworkspacev2", f"{wid},{name}")
        return wid

    def _put(self, wid: int, mid: int):
        """A workspace goes to another screen; the screen it leaves shows another one."""
        ws = self.workspaces[wid]
        if ws["monitor"] == mid:
            return
        source = self.mon(ws["monitor"])
        ws["monitor"] = mid
        self.emit("moveworkspacev2", f"{wid},{ws['name']},{self.mon(mid)['name']}")
        if source["ws"] == wid:
            other = next((i for i, w in self.workspaces.items() if w["monitor"] == source["id"] and i != SPECIAL_ID), None)
            if other is None:
                other = next(n for n in range(1, 100) if n not in self.workspaces)
                self._ensure(str(other), source["id"])
            source["ws"] = other

    def cleanup(self):
        """Hyprland drops a workspace that is empty, not shown and not persistent."""
        for wid in list(self.workspaces):
            if wid == SPECIAL_ID:
                continue
            empty = not any(w["ws"] == wid for w in self.windows.values())
            if empty and not self._visible(wid) and not self._persistent(wid):
                name = self.workspaces.pop(wid)["name"]
                self.emit("destroyworkspacev2", f"{wid},{name}")

    def apply_rules(self):
        """As Hyprland's ensurePersistentWorkspacesPresent: every persistent workspace is made and
        put on its rule's screen (or the one with the keyboard, when its rule names none); one
        whose screen is not there is skipped, and loses its pin for the while."""
        found = set()
        for rule in self.rules:
            if not rule["enabled"] or not rule.get("persistent"):
                continue
            named = self.mon_named(rule.get("monitor", ""))
            if rule.get("monitor") and named is None:
                continue
            target = named["id"] if named else self.focused_monitor
            wid = self._ensure(rule["ws"], target)
            self.workspaces[wid]["persistent"] = True
            found.add(wid)
            self._put(wid, target)
        for wid, ws in self.workspaces.items():
            if ws["persistent"] and wid not in found:
                ws["persistent"] = False
        self.cleanup()

    def _merge(self, ref: str, values: dict) -> dict:
        """A rule merged into the enabled one for that workspace, else added (replaceOrAdd)."""
        rule = self.rule(ref)
        if rule is None:
            rule = {"ws": str(ref), "enabled": True}
            self.rules.append(rule)
        for key in ("persistent", "monitor"):
            if values.get(key) not in (None, ""):
                rule[key] = values[key]
        return rule

    # ---- driving it (the tests) ---------------------------------------------------------------
    def open_window(self, cls="foot", title="", ws: int | None = None, pid=4242, focus=True) -> str:
        with self.lock:
            self._next += 0x10
            self._stable += 1
            address = f"0x{self._next:x}"
            wid = ws if ws is not None else self.mon(self.focused_monitor)["ws"]
            if wid not in self.workspaces:
                self._ensure(str(wid))
            self.windows[address] = {"address": address, "stable": f"{self._stable:x}", "class": cls,
                                     "title": title, "pid": pid, "ws": wid}
            self.emit("openwindow", f"{address[2:]},{self.workspaces[wid]['name']},{cls},{title}")
            if focus:
                self._focus_window(address)
            return address

    def set_title(self, address: str, title: str):
        with self.lock:
            self.windows[address]["title"] = title
            self.emit("windowtitle", address[2:])
            self.emit("windowtitlev2", f"{address[2:]},{title}")

    def close_window(self, address: str):
        with self.lock:
            self.windows.pop(address)
            self.emit("closewindow", address[2:])
            if self.focused == address:
                self.focused = ""
                self.emit("activewindowv2", "")
            self.cleanup()

    def switch(self, ref: str):
        with self.lock:
            wid = self._ensure(str(ref))
            mon = self.mon(self.workspaces[wid]["monitor"])
            mon["ws"] = wid
            self.focused_monitor = mon["id"]
            self.emit("workspacev2", f"{wid},{self.workspaces[wid]['name']}")
            self.cleanup()

    def swap(self):
        """swapactiveworkspaces between the first two screens."""
        with self.lock:
            a, b = self.monitors[0], self.monitors[1]
            wa, wb = a["ws"], b["ws"]
            self.workspaces[wa]["monitor"], self.workspaces[wb]["monitor"] = b["id"], a["id"]
            a["ws"], b["ws"] = wb, wa
            self.emit("moveworkspacev2", f"{wa},{self.workspaces[wa]['name']},{b['name']}")
            self.emit("moveworkspacev2", f"{wb},{self.workspaces[wb]['name']},{a['name']}")

    def reload(self):
        """The config is read again: only its own rules are left, and they are applied."""
        with self.lock:
            self.rules = [dict(r) for r in self.config_rules]
            self.handles = {}
            self.apply_rules()
            self.emit("configreloaded", "")

    def config_rule(self, ref: str, persistent: bool | None = True, monitor: str = ""):
        """A workspace rule in the config file."""
        with self.lock:
            rule = {"ws": str(ref), "enabled": True}
            if persistent is not None:
                rule["persistent"] = persistent
            if monitor:
                rule["monitor"] = monitor
            self.config_rules.append(rule)
            self.rules.append(dict(rule))
            self.apply_rules()

    def unplug(self, name: str):
        """A screen goes: its workspaces go to the first one left, as Hyprland moves them, and
        then it says the screen went."""
        with self.lock:
            gone = next(m for m in self.monitors if m["name"] == name)
            self.monitors = [m for m in self.monitors if m is not gone]
            keep = self.monitors[0]
            for wid, ws in self.workspaces.items():
                if ws["monitor"] == gone["id"]:
                    ws["monitor"] = keep["id"]
                    self.emit("moveworkspacev2", f"{wid},{ws['name']},{keep['name']}")
            self.focused_monitor = keep["id"]
            self.emit("monitorremovedv2", f"{gone['id']},{name},")
            self._gone_monitor = gone

    def plug_back(self):
        """The screen comes back: Hyprland applies its rules, and puts every workspace a rule
        names a screen for on it."""
        with self.lock:
            m = self._gone_monitor
            self.monitors.append(m)
            self.monitors.sort(key=lambda x: x["id"])
            if m["ws"] not in self.workspaces or self.workspaces[m["ws"]]["monitor"] != m["id"]:
                m["ws"] = next(n for n in range(1, 100) if n not in self.workspaces)
                self._ensure(str(m["ws"]), m["id"])
            self.emit("monitoraddedv2", f"{m['id']},{m['name']},")
            self.apply_rules()
            for rule in self.rules:
                named, wid = self.mon_named(rule.get("monitor", "")), self.find(rule["ws"])
                if rule["enabled"] and named is not None and wid is not None:
                    self._put(wid, named["id"])

    def special_window(self, cls="pavucontrol", title="mixer") -> str:
        with self.lock:
            self.workspaces.setdefault(SPECIAL_ID, {"name": SPECIAL_NAME, "monitor": 0, "persistent": False})
            return self.open_window(cls, title, ws=SPECIAL_ID, focus=False)

    def where(self, address: str) -> int:
        return self.windows[address]["ws"]

    # ---- acting --------------------------------------------------------------------------------
    def _focus_window(self, address: str):
        w = self.windows.get(address)
        if w is None:
            return "window not found"
        ws = self.workspaces[w["ws"]]
        mon = self.mon(ws["monitor"])
        if mon["ws"] != w["ws"]:
            mon["ws"] = w["ws"]
            self.emit("workspacev2", f"{w['ws']},{ws['name']}")
        self.focused_monitor = mon["id"]
        self.focused = address
        self.emit("activewindowv2", address[2:])
        self.cleanup()
        return "ok"

    def _move(self, address: str, ref: str, follow: bool):
        w = self.windows.get(address)
        if w is None:
            return "window not found"
        wid = self._ensure(str(ref))
        w["ws"] = wid
        self.emit("movewindowv2", f"{address[2:]},{wid},{self.workspaces[wid]['name']}")
        if follow:
            return self._focus_window(address)
        if self.focused == address:
            self.focused = ""
            self.emit("activewindowv2", "")
        self.cleanup()
        return "ok"

    def _rename(self, ref: str, name: str):
        wid = self.find(ref)
        if wid is None:
            return "no such workspace"
        self.workspaces[wid]["name"] = name
        self.emit("renameworkspace", f"{wid},{name}")
        return "ok"

    def to_monitor(self, ref: str, monitor: str):
        """moveworkspacetomonitor: as you, or Sidelit, move a workspace to another screen."""
        wid = self.find(ref)
        mon = self.mon_named(monitor)
        if wid is None or mon is None:
            return "not found"
        self._put(wid, mon["id"])
        return "ok"

    def _close(self, address: str):
        if address not in self.windows:
            return "window not found"
        self.close_window(address)
        return "ok"

    def _window_arg(self, selector: str) -> str:
        return selector[len("address:"):] if selector.startswith("address:") else ""

    def answer(self, text: str) -> str:
        if text.startswith("j/"):
            what = text[2:]
            if what == "monitors":
                return json.dumps([{"id": m["id"], "name": m["name"], "focused": m["id"] == self.focused_monitor,
                                    "description": f"Fake Monitors {m['name']} 0x0001", "make": "Fake Monitors",
                                    "model": m["name"],
                                    "activeWorkspace": {"id": m["ws"], "name": self.workspaces[m["ws"]]["name"]}}
                                   for m in self.monitors])
            if what == "workspaces":
                return json.dumps([self._ws_json(i) for i in self.workspaces])
            if what == "clients":
                return json.dumps([self._win_json(w) for w in self.windows.values()])
            if what == "activewindow":
                w = self.windows.get(self.focused)
                return json.dumps(self._win_json(w)) if w else "{}"
            if what == "workspacerules":
                return json.dumps([{"workspaceString": r["ws"], "enabled": r["enabled"],
                                    **{k: r[k] for k in ("persistent", "monitor") if k in r}} for r in self.rules])
            if what == "binds":
                return json.dumps(self.binds)
            return "unknown request"
        cmd, _, rest = text.partition(" ")
        if cmd == "eval":
            if not self.lua:
                return "eval is only supported with the lua config manager"
            if rest.strip() == "hl.version()":
                return "ok"
            did = False
            for m in _RULES.finditer(rest):
                did = True
                if m.group(2):
                    args = _fields(m.group(2))
                    rule = self._merge(str(args["workspace"]), args)
                    if m.group(1):
                        self.handles[_unlua(m.group(1))] = rule
                else:
                    rule = self.handles.pop(_unlua(m.group(3)), None)
                    if rule is not None:
                        rule["enabled"] = False
            if not did and "set_enabled" not in rest:
                return f"error: [string \"{rest[:40]}\"]: unknown"
            self.apply_rules()  # (once the eval has run: Hyprland's refresh comes after it)
            return "ok"
        if cmd == "keyword":
            if self.lua:
                return "keyword can't work with non-legacy parsers. Use eval."
            key, _, value = rest.partition(" ")
            parts = [p.strip() for p in value.split(",")]
            if key != "workspace" or len(parts) < 2:
                return "unknown keyword"
            values = {}
            for p in parts[1:]:
                k, _, v = p.partition(":")
                if k == "persistent":
                    values["persistent"] = v == "1"
                elif k == "monitor":
                    values["monitor"] = v
            self._merge(parts[0], values)
            self.apply_rules()
            return "ok"
        if cmd == "dispatch":
            return self._dispatch_lua(rest) if self.lua else self._dispatch_classic(rest)
        return "unknown request"

    def _dispatch_lua(self, code: str) -> str:
        fn, a = _call(code)
        if fn is None:
            return (f"error: [string \"return hl.dispatch({code})\"]:1: ')' expected\n\n"
                    " → Note: dispatch in lua is a shorthand for hl.dispatch(...), your syntax might need to be updated.")
        if fn == "dsp.focus" and "window" in a:
            return self._focus_window(self._window_arg(a["window"]))
        if fn == "dsp.focus" and "workspace" in a:
            self.switch(str(a["workspace"]))
            return "ok"
        if fn == "dsp.window.move" and "workspace" in a:
            return self._move(self._window_arg(a.get("window", "")) or self.focused, str(a["workspace"]),
                              a.get("follow", True))
        if fn == "dsp.window.close":
            return self._close(self._window_arg(a.get("window", "")) or self.focused)
        if fn == "dsp.workspace.rename":
            return self._rename(str(a["workspace"]), a.get("name", ""))
        if fn == "dsp.workspace.move":
            return self.to_monitor(str(a["workspace"]), a["monitor"])
        return f"error: hl.{fn}: unknown"

    def _dispatch_classic(self, text: str) -> str:
        name, _, arg = text.partition(" ")
        if name == "focuswindow":
            return self._focus_window(self._window_arg(arg))
        if name == "workspace":
            self.switch(arg.strip())
            return "ok"
        if name in ("movetoworkspacesilent", "movetoworkspace"):
            ref, _, window = arg.partition(",")
            return self._move(self._window_arg(window) or self.focused, ref.strip(), name == "movetoworkspace")
        if name == "closewindow":
            return self._close(self._window_arg(arg))
        if name == "renameworkspace":
            number, _, new = arg.partition(" ")
            wid = int(number)
            return self._rename(self._ref(wid) if wid in self.workspaces else number, new)
        if name == "moveworkspacetomonitor":
            ref, _, monitor = arg.partition(" ")
            return self.to_monitor(ref, monitor)
        return "Invalid dispatcher"
