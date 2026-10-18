"""A niri in miniature for the niri backend's tests: its IPC socket, in a thread.

It answers where niri.py asks, the way niri 26.04 does (checked against a nested niri 26.04 and
its source): a JSON request a line, a JSON reply a line (`{"Ok": {"Workspaces": [...]}}`,
`{"Ok": "Handled"}`, `{"Err": "..."}`); `"EventStream"` gets the reply, the whole state, then
every change as an event line. Its workspaces behave as niri's: per screen, an empty one always
at the end of each, an empty unnamed one you leave dropped, a name refused when another
workspace has it (in any case), a workspace moved to an index taken out and put in there. Tests
drive it directly: open, retitle and close windows, switch workspaces, unplug and replug a
screen, drop the event streams.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path


class FakeNiri:
    def __init__(self, runtime: Path, outputs: tuple = ("DP-1",), wayland: str = "wayland-9", pid: int | None = None):
        """`pid`: the niri process its socket names (this one's, unless a test wants another)."""
        self.path = str(Path(runtime) / f"niri.{wayland}.{pid or os.getpid()}.sock")
        self.lock = threading.RLock()
        self.requests: list = []
        self.next_ws = 1
        self.next_win = 1
        self.outputs = {name: {"x": 1920 * i, "y": 0} for i, name in enumerate(outputs)}
        self.screens: dict[str, list[dict]] = {}  # output -> its workspaces, in order
        self.active: dict[str, int] = {}          # output -> its shown workspace's id
        self.focused_output = outputs[0]
        self.windows: dict[int, dict] = {}
        self.focused_window: int | None = None
        self._gone_output = None
        for name in outputs:
            self.screens[name] = [self._new_ws()]
            self.active[name] = self.screens[name][0]["id"]
        self._listeners: list[socket.socket] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        """niri quits: its socket goes, and the event streams close."""
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.drop_streams()

    def drop_streams(self):
        """As niri drops an event stream that reads too slowly."""
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

    def _serve(self, conn):
        f = conn.makefile("rb")
        try:
            for line in f:
                try:
                    request = json.loads(line)
                except ValueError:
                    conn.sendall(b'{"Err":"error parsing request"}\n')
                    continue
                with self.lock:
                    self.requests.append(request)
                    if request == "EventStream":
                        conn.sendall(b'{"Ok":"Handled"}\n')
                        for event in self._full_state():
                            conn.sendall((json.dumps(event) + "\n").encode())
                        self._listeners.append(conn)
                        return
                    reply = self.answer(request)
                conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        f.close()
        conn.close()

    def _send(self, event: dict):
        line = (json.dumps(event) + "\n").encode()
        for c in list(self._listeners):
            try:
                c.sendall(line)
            except OSError:
                self._listeners.remove(c)
                c.close()

    def _full_state(self) -> list[dict]:
        return [{"WorkspacesChanged": {"workspaces": self._ws_json()}},
                {"WindowsChanged": {"windows": [self._win_json(w) for w in self.windows.values()]}},
                {"KeyboardLayoutsChanged": {"keyboard_layouts": {"names": ["English (US)"], "current_idx": 0}}},
                {"OverviewOpenedOrClosed": {"is_open": False}}, {"ConfigLoaded": {"failed": False}}]

    # ---- what is where -------------------------------------------------------------------------
    def _new_ws(self, name=None) -> dict:
        ws = {"id": self.next_ws, "name": name}
        self.next_ws += 1
        return ws

    def find(self, wid: int) -> tuple[str, dict] | None:
        for output, spaces in self.screens.items():
            for ws in spaces:
                if ws["id"] == wid:
                    return output, ws
        return None

    def by_name(self, name: str):
        return next(((o, ws) for o, spaces in self.screens.items() for ws in spaces
                     if ws["name"] and ws["name"].lower() == name.lower()), None)

    def _resolve(self, ref) -> tuple[str, dict] | None:
        if not isinstance(ref, dict):
            return None
        if "Id" in ref:
            return self.find(int(ref["Id"]))
        if "Name" in ref:
            return self.by_name(str(ref["Name"]))
        if "Index" in ref:
            spaces = self.screens[self.focused_output]
            i = int(ref["Index"]) - 1
            return (self.focused_output, spaces[i]) if 0 <= i < len(spaces) else None
        return None

    def windows_on(self, wid: int) -> list[dict]:
        return [w for w in self.windows.values() if w["ws"] == wid]

    def _ws_json(self) -> list[dict]:
        out = []
        for output, spaces in self.screens.items():
            for i, ws in enumerate(spaces, start=1):
                shown = self.active.get(output) == ws["id"]
                out.append({"id": ws["id"], "idx": i, "name": ws["name"], "output": output, "is_urgent": False,
                            "is_active": shown, "is_focused": shown and output == self.focused_output,
                            "active_window_id": None})
        return out[::-1]  # (niri lists them in no particular order)

    def _win_json(self, w: dict) -> dict:
        return {"id": w["id"], "title": w["title"], "app_id": w["app"], "pid": w["pid"], "workspace_id": w["ws"],
                "is_focused": w["id"] == self.focused_window, "is_floating": False, "is_urgent": False,
                "layout": {"pos_in_scrolling_layout": [1, 1], "tile_size": [800.0, 600.0], "window_size": [800, 600],
                           "tile_pos_in_workspace_view": None, "window_offset_in_tile": [0.0, 0.0]},
                "focus_timestamp": None}

    def _tidy(self):
        """As niri: an empty workspace at the end of each screen, empty unnamed ones you left
        dropped. Then the new state goes out."""
        for output, spaces in self.screens.items():
            for ws in list(spaces[:-1]):
                if ws["id"] != self.active.get(output) and not ws["name"] and not self.windows_on(ws["id"]):
                    spaces.remove(ws)
            last = spaces[-1]
            if last["name"] or self.windows_on(last["id"]):
                spaces.append(self._new_ws())
        self._send({"WorkspacesChanged": {"workspaces": self._ws_json()}})

    def _focus_ws(self, output: str, ws: dict):
        self.active[output] = ws["id"]
        self.focused_output = output
        self._send({"WorkspaceActivated": {"id": ws["id"], "focused": True}})
        self._tidy()

    # ---- driving it (the tests) ---------------------------------------------------------------
    def open_window(self, app="foot", title="", ws: int | None = None, pid=4242, focus=True) -> int:
        with self.lock:
            wid = self.next_win
            self.next_win += 1
            target = ws if ws is not None else self.active[self.focused_output]
            self.windows[wid] = {"id": wid, "app": app, "title": title, "pid": pid, "ws": target}
            if focus:
                self.focused_window = wid
            self._send({"WindowOpenedOrChanged": {"window": self._win_json(self.windows[wid])}})
            self._tidy()
            return wid

    def set_title(self, wid: int, title: str):
        with self.lock:
            self.windows[wid]["title"] = title
            self._send({"WindowOpenedOrChanged": {"window": self._win_json(self.windows[wid])}})

    def close_window(self, wid: int):
        with self.lock:
            self.windows.pop(wid)
            self._send({"WindowClosed": {"id": wid}})
            if self.focused_window == wid:
                self.focused_window = None
                self._send({"WindowFocusChanged": {"id": None}})
            self._tidy()

    def switch(self, wid: int):
        with self.lock:
            output, ws = self.find(wid)
            self._focus_ws(output, ws)

    def name(self, wid: int, name: str):
        """You name a workspace (niri msg action set-workspace-name)."""
        with self.lock:
            self._act({"SetWorkspaceName": {"name": name, "workspace": {"Id": wid}}})

    def unplug(self, output: str):
        """A screen goes: its workspaces go to the end of the first one left (before its empty one)."""
        with self.lock:
            spaces = self.screens.pop(output)
            self._gone_output = (output, self.outputs.pop(output), [ws["id"] for ws in spaces])
            keep = next(iter(self.screens))
            for ws in spaces:
                if ws["name"] or self.windows_on(ws["id"]):
                    self.screens[keep].insert(len(self.screens[keep]) - 1, ws)
            self.active.pop(output, None)
            self.focused_output = keep
            self._tidy()

    def plug_back(self):
        """It comes back, and niri puts its workspaces back on it."""
        with self.lock:
            output, place, ids = self._gone_output
            self.outputs[output] = place
            back = [ws for ws in self.screens[next(iter(self.screens))] if ws["id"] in ids]
            for spaces in self.screens.values():
                spaces[:] = [ws for ws in spaces if ws["id"] not in ids]
            self.screens[output] = back + [self._new_ws()]
            self.active[output] = self.screens[output][0]["id"]
            self._tidy()

    def where(self, wid: int) -> int:
        return self.windows[wid]["ws"]

    def output_of(self, ws_id: int) -> str:
        return self.find(ws_id)[0]

    def names(self) -> list:
        return [ws["name"] for spaces in self.screens.values() for ws in spaces]

    # ---- answering ------------------------------------------------------------------------------
    def answer(self, request):
        if request == "Version":
            return {"Ok": {"Version": "26.04 (fake)"}}
        if request == "Workspaces":
            return {"Ok": {"Workspaces": self._ws_json()}}
        if request == "Windows":
            return {"Ok": {"Windows": [self._win_json(w) for w in self.windows.values()]}}
        if request == "Outputs":
            return {"Ok": {"Outputs": {n: {"name": n, "make": "Fake", "model": n, "serial": None,
                                           "logical": {"x": p["x"], "y": p["y"], "width": 1920, "height": 1080,
                                                       "scale": 1.0, "transform": "Normal"}}
                                       for n, p in self.outputs.items()}}}
        if request == "FocusedWindow":
            w = self.windows.get(self.focused_window)
            return {"Ok": {"FocusedWindow": self._win_json(w) if w else None}}
        if isinstance(request, dict) and "Action" in request:
            return self._act(request["Action"])
        return {"Err": "error parsing request"}

    def _act(self, action: dict):
        name, a = next(iter(action.items()))
        if name == "FocusWindow":
            w = self.windows.get(int(a["id"]))
            if w is None:
                return {"Ok": "Handled"}
            output, ws = self.find(w["ws"])
            self.focused_window = w["id"]
            self._send({"WindowFocusChanged": {"id": w["id"]}})
            self._focus_ws(output, ws)
        elif name == "CloseWindow":
            if int(a["id"]) in self.windows:
                self.close_window(int(a["id"]))
        elif name == "MoveWindowToWorkspace":
            w, found = self.windows.get(int(a["window_id"])), self._resolve(a["reference"])
            if w is not None and found is not None:
                w["ws"] = found[1]["id"]
                if self.focused_window == w["id"] and not a.get("focus", True):
                    self.focused_window = None
                    self._send({"WindowFocusChanged": {"id": None}})
                self._send({"WindowOpenedOrChanged": {"window": self._win_json(w)}})
                self._tidy()
        elif name == "FocusWorkspace":
            found = self._resolve(a["reference"])
            if found is not None:
                self._focus_ws(*found)
        elif name == "SetWorkspaceName":
            found = self._resolve(a.get("workspace") or {"Id": self.active[self.focused_output]})
            if found is not None and self.by_name(a["name"]) is None:  # (a name in use: ignored, as niri does)
                found[1]["name"] = a["name"]
                self._tidy()
        elif name == "UnsetWorkspaceName":
            found = self._resolve(a.get("reference") or {"Id": self.active[self.focused_output]})
            if found is not None:
                found[1]["name"] = None
                self._tidy()
        elif name == "MoveWorkspaceToMonitor":
            found = self._resolve(a["reference"])
            if found is not None and a["output"] in self.screens and found[0] != a["output"]:
                source, ws = found
                self.screens[source].remove(ws)
                if self.active.get(source) == ws["id"]:
                    self.active[source] = self.screens[source][0]["id"]
                self.screens[a["output"]].insert(len(self.screens[a["output"]]) - 1, ws)
                self._tidy()
        elif name == "MoveWorkspaceToIndex":
            found = self._resolve(a["reference"])
            if found is not None:
                output, ws = found
                spaces = self.screens[output]
                spaces.remove(ws)
                spaces.insert(min(max(int(a["index"]) - 1, 0), len(spaces)), ws)
                self._tidy()
        else:
            return {"Err": f"unknown action {name}"}
        return {"Ok": "Handled"}
