"""A sway in miniature for the sway backend's tests: its IPC socket, in a thread.

It answers where sway.py asks, the way sway 1.12 does (checked against its source and a headless
sway 1.12): i3's framing ("i3-ipc", the payload's length and type, then JSON), GET_TREE,
GET_WORKSPACES, GET_OUTPUTS, GET_VERSION, GET_CONFIG, RUN_COMMAND (with `[con_id=N]` criteria
and `;` between commands), SUBSCRIBE and then the events. Its workspaces behave as sway's: each
on one screen, made when you go there or a window is moved there, dropped once empty and not
shown, ordered on their screen by number (named ones after, as they came). Tests drive it
directly: open, retitle and close windows, switch workspaces, unplug and replug a screen, drop
the event connections, quit.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
from pathlib import Path

HEADER = struct.Struct("=6sII")
EVENT = 1 << 31
EVENTS = {"workspace": EVENT | 0, "output": EVENT | 1, "window": EVENT | 3, "shutdown": EVENT | 6}

DEFAULT_CONFIG = """# a sway config, as sway's own begins
set $mod Mod4
set $term foot
bindsym $mod+Return exec $term
    bindsym $mod+1 workspace number 1
    bindsym $mod+2 workspace number 2
    bindsym $mod+3 workspace number 3
    bindsym $mod+Shift+3 move container to workspace number 3
    bindsym $mod+0 workspace number 10
mode "resize" {
    bindsym 4 workspace number 4
}
"""


def _num(name: str) -> int:
    m = re.match(r"\d+", name)
    return int(m.group()) if m else -1


def _args(text: str) -> list[str]:
    """A command's words, as sway splits them: quotes keep spaces together and are taken off."""
    out, cur, quote, any_ = [], "", None, False
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'":
            quote, any_ = ch, True
        elif ch.isspace():
            if cur or any_:
                out.append(cur)
            cur, any_ = "", False
        else:
            cur += ch
    if cur or any_:
        out.append(cur)
    return out


def _split(text: str) -> list[str]:
    """Commands separated by `;` (outside quotes)."""
    out, cur, quote = [], "", None
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == ";":
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return [c.strip() for c in out if c.strip()]


class FakeSway:
    def __init__(self, runtime: Path, outputs: tuple = ("DP-1",), config: str = DEFAULT_CONFIG,
                 config_path: str = "/etc/sway/config", pid: int | None = None):
        """`pid`: the sway process its socket names (this one's, unless a test wants another)."""
        self.path = str(Path(runtime) / f"sway-ipc.{os.getuid()}.{pid or os.getpid()}.sock")
        self.lock = threading.RLock()
        self.config, self.config_path = config, config_path
        self.commands: list[str] = []  # every RUN_COMMAND, as sent
        self.asked: list[int] = []     # every request's type
        self._next = 2                 # sway numbers outputs, workspaces and windows alike
        self.outputs = {name: {"id": self._id(), "x": 1920 * i, "y": 0} for i, name in enumerate(outputs)}
        self.screens: dict[str, list[dict]] = {}  # output -> its workspaces, in sway's order
        self.shown: dict[str, str] = {}           # output -> the name of the workspace it shows
        self.focused_output = outputs[0]
        self.windows: dict[int, dict] = {}
        self.focused_window: int | None = None
        self.scratch: list[int] = []
        self._gone = None
        for i, name in enumerate(outputs, start=1):
            self.screens[name] = [self._new_ws(str(i))]
            self.shown[name] = str(i)
        self._listeners: list[socket.socket] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _id(self) -> int:
        self._next += 1
        return self._next - 1

    def stop(self, shutdown: bool = False):
        """sway quits (saying so first, with `shutdown`): its socket goes, and so do the
        event connections."""
        if shutdown:
            with self.lock:
                self._event("shutdown", {"change": "exit"})
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.drop_streams()

    def drop_streams(self):
        """As sway drops a connection whose events pile up unread."""
        with self.lock:
            for c in self._listeners:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self._listeners = []

    # ---- the socket ---------------------------------------------------------------------------
    def _accept(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    @staticmethod
    def _message(kind: int, payload) -> bytes:
        body = json.dumps(payload).encode()
        return HEADER.pack(b"i3-ipc", len(body), kind) + body

    def _serve(self, conn):
        try:
            while True:
                magic, length, kind = HEADER.unpack(self._recv(conn, HEADER.size))
                if magic != b"i3-ipc":
                    break
                payload = self._recv(conn, length).decode()
                with self.lock:
                    self.asked.append(kind)
                    if kind == 2:  # SUBSCRIBE
                        conn.sendall(self._message(2, {"success": True}))
                        self._listeners.append(conn)
                        return
                    reply = self.answer(kind, payload)
                    conn.sendall(self._message(kind, reply))
        except OSError:
            pass
        conn.close()

    def _event(self, name: str, payload: dict):
        data = self._message(EVENTS[name], payload)
        for c in list(self._listeners):
            try:
                c.sendall(data)
            except OSError:
                self._listeners.remove(c)
                c.close()

    # ---- what is where -------------------------------------------------------------------------
    def _new_ws(self, name: str) -> dict:
        return {"id": self._id(), "name": name}

    def find(self, name: str) -> tuple[str, dict] | None:
        for output, spaces in self.screens.items():
            for ws in spaces:
                if ws["name"] == name:
                    return output, ws
        return None

    def names(self) -> list[str]:
        return [ws["name"] for spaces in self.screens.values() for ws in spaces]

    def windows_on(self, name: str) -> list[dict]:
        return [w for w in self.windows.values() if w["ws"] == name]

    def _sort(self, output: str):
        self.screens[output].sort(key=lambda ws: (0, _num(ws["name"])) if _num(ws["name"]) >= 0 else (1, 0))

    def _initial_output(self, name: str) -> str:
        """Where sway makes a workspace: the first plugged screen of the config's `workspace NAME
        output ...` lines, else the one with the keyboard."""
        for line in self.config.splitlines():
            words = _args(line)
            if words[:1] == ["workspace"] and "output" in words:
                at = words.index("output")
                if " ".join(words[1:at]) == name:
                    found = next((s for o in words[at + 1:] for s in self.screens
                                  if o.casefold() == s.casefold() or o == f"Fake {s} 0001"), None)
                    if found:
                        return found
        return self.focused_output

    def _create(self, name: str, output: str | None = None) -> dict:
        output = output or self._initial_output(name)
        ws = self._new_ws(name)
        self.screens[output].append(ws)
        self._sort(output)
        self._event("workspace", {"change": "init", "current": self._ws_node(output, ws), "old": None})
        return ws

    def _consider_destroy(self, name: str):
        found = self.find(name)
        if found is None:
            return
        output, ws = found
        if self.windows_on(name) or self.shown.get(output) == name:
            return
        self.screens[output].remove(ws)
        self._event("workspace", {"change": "empty", "current": self._ws_node(output, ws), "old": None})

    def _last_focus_on(self, name: str) -> int | None:
        on = [w for w in self.windows_on(name)]
        return max(on, key=lambda w: w["focused_at"])["id"] if on else None

    def _show(self, name: str, output: str | None = None):
        """Go to a workspace (made if need be): its screen shows it and gets the keyboard."""
        found = self.find(name)
        if found is None:
            self._create(name, output)
            found = self.find(name)
        output, ws = found
        old_name = self.shown.get(self.focused_output)
        old = self.find(old_name) if old_name else None
        previous = self.shown.get(output)
        self.shown[output] = name
        self.focused_output = output
        self._set_focus(self._last_focus_on(name))
        self._event("workspace", {"change": "focus", "current": self._ws_node(output, ws),
                                  "old": self._ws_node(*old) if old else None})
        for gone in {previous, old_name} - {name}:
            if gone:
                self._consider_destroy(gone)

    def _set_focus(self, wid: int | None):
        self.focused_window = wid
        if wid is not None:
            self._tick = getattr(self, "_tick", 0) + 1
            self.windows[wid]["focused_at"] = self._tick
            self._event("window", {"change": "focus", "container": self._win_node(self.windows[wid])})

    def _ws_node(self, output: str, ws: dict) -> dict:
        on = [self._win_node(w) for w in self.windows_on(ws["name"]) if not w.get("floating")]
        if len(on) > 1:  # (side by side: in a split container of their own)
            on = [{"id": ws["id"] * 1000, "type": "con", "name": None, "layout": "splith", "focused": False,
                   "nodes": on, "floating_nodes": []}]
        return {"id": ws["id"], "type": "workspace", "name": ws["name"], "num": _num(ws["name"]), "output": output,
                "focused": self.focused_window is None and self.shown.get(self.focused_output) == ws["name"]
                and output == self.focused_output,
                "nodes": on, "floating_nodes": [self._win_node(w) for w in self.windows_on(ws["name"]) if w.get("floating")]}

    def _win_node(self, w: dict) -> dict:
        node = {"id": w["id"], "type": "floating_con" if w.get("floating") else "con", "name": w["title"],
                "pid": w["pid"], "focused": w["id"] == self.focused_window, "visible": True,
                "sticky": bool(w.get("sticky")), "nodes": [], "floating_nodes": []}
        if w.get("xwayland"):
            node.update(app_id=None, shell="xwayland", window=1000 + w["id"],
                        window_properties={"class": w["app"], "instance": w["app"].lower(), "title": w["title"]})
        else:
            node.update(app_id=w["app"], shell="xdg_shell")
        return node

    def tree(self) -> dict:
        scratch = {"id": 1001, "type": "workspace", "name": "__i3_scratch", "nodes": [],
                   "floating_nodes": [self._win_node(self.windows[i]) for i in self.scratch]}
        outputs = [{"id": 1000, "type": "output", "name": "__i3", "nodes": [scratch], "floating_nodes": []}]
        for name, place in self.outputs.items():
            outputs.append({"id": place["id"], "type": "output", "name": name, "floating_nodes": [],
                            "nodes": [self._ws_node(name, ws) for ws in self.screens[name]]})
        return {"id": 1, "type": "root", "name": "root", "nodes": outputs, "floating_nodes": []}

    # ---- driving it (the tests) ---------------------------------------------------------------
    def open_window(self, app="foot", title="", ws: str | None = None, pid=4242, focus=True,
                    xwayland=False, floating=False, sticky=False) -> int:
        with self.lock:
            target = ws if ws is not None else self.shown[self.focused_output]
            if self.find(target) is None:
                self._create(target)
            wid = self._id()
            self.windows[wid] = {"id": wid, "app": app, "title": title, "pid": pid, "ws": target,
                                 "xwayland": xwayland, "floating": floating or sticky, "sticky": sticky,
                                 "focused_at": 0}
            self._event("window", {"change": "new", "container": self._win_node(self.windows[wid])})
            if focus:
                output, _ = self.find(target)
                if self.shown.get(output) == target:
                    self.focused_output = output
                    self._set_focus(wid)
            return wid

    def to_scratchpad(self, wid: int):
        with self.lock:
            w = self.windows[wid]
            old, w["ws"] = w["ws"], "__i3_scratch"
            self.scratch.append(wid)
            self._event("window", {"change": "move", "container": self._win_node(w)})
            self._consider_destroy(old)

    def set_title(self, wid: int, title: str):
        with self.lock:
            self.windows[wid]["title"] = title
            self._event("window", {"change": "title", "container": self._win_node(self.windows[wid])})

    def close_window(self, wid: int):
        with self.lock:
            w = self.windows.pop(wid)
            self._event("window", {"change": "close", "container": self._win_node(w)})
            if self.focused_window == wid:
                self._set_focus(self._last_focus_on(w["ws"]))
            self._consider_destroy(w["ws"])

    def switch(self, name: str):
        with self.lock:
            self._show(name)

    def rename(self, old: str, new: str):
        """You rename a workspace (swaymsg rename workspace OLD to NEW)."""
        with self.lock:
            return self._run(f'rename workspace "{old}" to "{new}"')

    def unplug(self, output: str):
        """A screen goes: its workspaces with windows go to the first screen left (sway keeps
        where they belong, and takes them back when the screen does). The last one going, they
        go to a screen of sway's own that it lists nowhere (its fallback output)."""
        with self.lock:
            spaces = self.screens.pop(output)
            self._gone = (output, self.outputs.pop(output), [ws["name"] for ws in spaces])
            self.shown.pop(output, None)
            keep = next(iter(self.screens), None)
            if keep is None:
                self.hidden = spaces
            else:
                for ws in spaces:
                    if self.windows_on(ws["name"]):
                        self.screens[keep].append(ws)
                self._sort(keep)
                self.focused_output = keep
            self._event("output", {"change": "unspecified"})

    def plug_back(self):
        with self.lock:
            output, place, names = self._gone
            self.outputs[output] = place
            if getattr(self, "hidden", None):
                back, self.hidden = self.hidden, []
            else:
                back = [ws for ws in self.screens[next(iter(self.screens))] if ws["name"] in names]
            for spaces in self.screens.values():
                spaces[:] = [ws for ws in spaces if ws["name"] not in names]
            self.screens[output] = back or [self._new_ws(str(max(_num(n) for n in self.names()) + 1))]
            self.shown[output] = self.screens[output][0]["name"]
            self.focused_output = output if self.focused_output not in self.screens else self.focused_output
            self._event("output", {"change": "unspecified"})

    def move_workspace(self, name: str, output: str):
        """You move a workspace to another screen (move workspace to output ...)."""
        with self.lock:
            source, ws = self.find(name)
            self.screens[source].remove(ws)
            if not self.screens[source]:
                self.screens[source].append(self._new_ws(str(max([_num(n) for n in self.names()] + [0]) + 1)))
            if self.shown.get(source) == name:
                self.shown[source] = self.screens[source][0]["name"]
            self.screens[output].append(ws)
            self._sort(output)
            self._event("workspace", {"change": "move", "current": self._ws_node(output, ws), "old": None})
            self._show(name)

    def reload(self, config: str):
        """You change sway's config and reload it."""
        with self.lock:
            self.config = config
            self._event("workspace", {"change": "reload", "current": None, "old": None})

    def send_split(self, wid: int, title: str):
        """A title change whose event arrives in two pieces, a moment apart."""
        with self.lock:
            self.windows[wid]["title"] = title
            data = self._message(EVENTS["window"], {"change": "title", "container": self._win_node(self.windows[wid])})
            for c in self._listeners:
                c.sendall(data[:9])
        threading.Event().wait(0.1)
        with self.lock:
            for c in self._listeners:
                c.sendall(data[9:])

    def where(self, wid: int) -> str:
        return self.windows[wid]["ws"]

    def output_of(self, name: str) -> str:
        return self.find(name)[0]

    # ---- answering ------------------------------------------------------------------------------
    def answer(self, kind: int, payload: str):
        if kind == 0:
            return [self._run(c) for c in _split(payload)]
        if kind == 1:
            return [{"id": ws["id"], "type": "workspace", "name": ws["name"], "num": _num(ws["name"]),
                     "visible": self.shown.get(o) == ws["name"],
                     "focused": self.shown.get(o) == ws["name"] and o == self.focused_output,
                     "urgent": False, "output": o, "rect": {"x": self.outputs[o]["x"], "y": 0, "width": 1920, "height": 1080}}
                    for o, spaces in self.screens.items() for ws in spaces]
        if kind == 3:
            return [{"id": p["id"], "type": "output", "name": n, "active": True, "focused": n == self.focused_output,
                     "current_workspace": self.shown.get(n), "make": "Fake", "model": n, "serial": "0001",
                     "rect": {"x": p["x"], "y": p["y"], "width": 1920, "height": 1080}} for n, p in self.outputs.items()]
        if kind == 4:
            return self.tree()
        if kind == 7:
            return {"major": 1, "minor": 12, "patch": 0, "human_readable": "1.12 (fake)",
                    "loaded_config_file_name": self.config_path}
        if kind == 9:
            return {"config": self.config}
        return {"success": False, "error": f"unknown message type {kind}"}

    def _run(self, command: str) -> dict:
        self.commands.append(command)
        con = None
        m = re.match(r"\[con_id=(\d+)\]\s*", command)
        if m:
            con = self.windows.get(int(m.group(1)))
            command = command[m.end():]
            if con is None:
                return {"success": False, "error": "No matching node."}
        words = _args(command)
        if not words:
            return {"success": True}
        words = words[:1] + [self._expand(w) for w in words[1:]]
        if words[0] == "focus" and len(words) == 1 and con is not None:
            output, _ = self.find(con["ws"])
            if self.shown.get(output) != con["ws"] or self.focused_output != output:
                self._show(con["ws"])
            self._set_focus(con["id"])
            return {"success": True}
        if words[:2] == ["focus", "output"] and len(words) == 3:
            if words[2] not in self.screens:
                return {"success": False, "error": f"There is no output with that name"}
            self._show(self.shown[words[2]])
            return {"success": True}
        if words[0] == "kill" and con is not None:
            self.close_window(con["id"])
            return {"success": True}
        if words[0] == "workspace":
            rest = [w for w in words[1:] if not w.startswith("--")]
            self._show(" ".join(rest))
            return {"success": True}
        if words[0] == "move" and con is not None:
            rest = [w for w in words[1:] if not w.startswith("--")]
            if rest[:2] == ["workspace", "to"]:
                return self._move_workspace(con, rest[3:] if rest[2:3] == ["output"] else rest[2:])
            while rest and rest[0] in ("container", "window", "to"):
                rest.pop(0)
            if rest and rest[0] == "workspace":
                target = " ".join(rest[1:])
                if self.find(target) is None:
                    self._create(target)
                old, con["ws"] = con["ws"], target
                self._event("window", {"change": "move", "container": self._win_node(con)})
                if self.focused_window == con["id"]:
                    self._set_focus(self._last_focus_on(old))
                self._consider_destroy(old)
                return {"success": True}
        if words[:2] == ["rename", "workspace"] and "to" in words:
            at = words.index("to")
            old, new = " ".join(words[2:at]) or self.shown[self.focused_output], " ".join(words[at + 1:])
            found = self.find(old)
            if found is None or self.find(new) is not None:
                return {"success": False, "error": "There is no workspace with that name" if found is None
                        else f"Workspace already exists"}
            found[1]["name"] = new
            for w in self.windows.values():
                if w["ws"] == old:
                    w["ws"] = new
            for o, s in self.shown.items():
                if s == old:
                    self.shown[o] = new
            self._sort(found[0])
            self._event("workspace", {"change": "rename", "current": self._ws_node(*found), "old": None})
            return {"success": True}
        return {"success": False, "parse_error": True, "error": f"Unknown/invalid command '{command}'"}

    def _expand(self, arg: str) -> str:
        """As sway does with every argument but the command's name: its config's variables put
        in, quoted or not, and `$$` made a `$`."""
        variables = {}
        for line in self.config.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0] == "set":
                variables[parts[1]] = parts[2]

        def put(m):
            if m.group() == "$$":
                return "$"
            name = max((v for v in variables if m.group().startswith(v)), key=len, default=None)
            return variables[name] + m.group()[len(name):] if name else m.group()
        return re.sub(r"\$\$|\$\w+", put, arg)

    def _move_workspace(self, con: dict, where: list[str]) -> dict:
        target = " ".join(where)
        if target not in self.screens:
            return {"success": False, "error": f"Can't find output with name/direction '{target}'"}
        source, ws = self.find(con["ws"])
        if source == target:
            return {"success": True}
        self.screens[source].remove(ws)
        if not self.screens[source]:  # (a screen is never without a workspace)
            free = max([_num(n) for n in self.names()] + [0]) + 1
            self.screens[source].append(self._new_ws(str(free)))
        if self.shown.get(source) == ws["name"]:
            self.shown[source] = self.screens[source][0]["name"]
        before = self.shown.get(target)
        self.screens[target].append(ws)
        self._sort(target)
        self._event("workspace", {"change": "move", "current": self._ws_node(target, ws), "old": None})
        self._show(ws["name"])  # (sway follows the workspace it moved)
        if before:
            self._consider_destroy(before)
        return {"success": True}
