"""GNOME Shell with Sidelit's extension, in miniature, for the GNOME backend's tests: it owns
org.gnome.Shell on a private session bus and exports what gnome-extension/desktop.js does.

    python3 tests/fake_gnome.py BUS_ADDRESS [--static] [--off]

It keeps workspaces the way GNOME does with dynamic workspaces (the default): an empty one is
dropped unless it is pinned, the one you are on, or the first empty one after the last busy one,
and there is always an empty one at the end (checked against a headless GNOME Shell 51 by
tests/gnome_live.py). With --static it keeps a fixed number. /Test drives it: open, retitle and
close windows, turn the extension off and on (new workspace ids, as the extension's are), and
read its state. --off starts it with the extension turned off.
"""
from __future__ import annotations

import itertools
import json
import sys
import uuid

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PATH, IFACE = "/io/github/kulaxa/Sidelit", "io.github.kulaxa.Sidelit.Shell"
SHELL_XML = f"""<node><interface name="{IFACE}">
  <property name="Version" type="u" access="read"/>
  <method name="GetState"><arg type="s" direction="out"/></method>
  <method name="ActivateWorkspace"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AddWorkspace"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
  <method name="RemoveWorkspace"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="RenameWorkspace"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="PinWorkspace"><arg type="s" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="out"/></method>
  <method name="MoveWorkspace"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="out"/></method>
  <method name="FocusWindow"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="CloseWindow"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="MoveWindow"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="Changed"><arg type="s"/></signal>
</interface></node>"""
TEST_XML = """<node><interface name="org.gnome.Shell.Test">
  <method name="OpenWindow"><arg type="s" name="spec" direction="in"/><arg type="s" direction="out"/></method>
  <method name="SetTitle"><arg type="s" name="id" direction="in"/><arg type="s" name="title" direction="in"/></method>
  <method name="CloseWindow"><arg type="s" name="id" direction="in"/></method>
  <method name="Extension"><arg type="b" name="on" direction="in"/></method>
  <method name="Lock"><arg type="b" name="locked" direction="in"/></method>
  <method name="State"><arg type="s" direction="out"/></method>
</interface></node>"""
OUTPUTS = ["DP-1", "HDMI-A-1"]


def _iface(xml: str):
    return Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]


class FakeShell:
    def __init__(self, address: str, static: bool, on: bool):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.dynamic = not static
        self.locked = False
        self.spaces: list[dict] = [{"id": str(uuid.uuid4()), "name": "", "pinned": False}
                                   for _ in range(4 if static else 2)]
        self.active = 0
        self.windows: dict[str, dict] = {}
        self.focus: str | None = None
        self.numbers = itertools.count(1)
        self.run = ""
        self.seq = 0
        self.ext_reg = 0
        self.log: list[str] = []
        self.bus.register_object("/Test", _iface(TEST_XML), self.on_test, None, None)
        if on:
            self.extension(True)
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                           GLib.Variant("(su)", ("org.gnome.Shell", 4)), None, 0, -1, None)

    # ---- the extension being turned on and off ----------------------------------------------
    def extension(self, on: bool):
        if on and not self.ext_reg:
            self.run, self.seq = str(uuid.uuid4()), 0
            for space in self.spaces:  # its ids and pins are its own, new every time it is turned on
                space["id"] = str(uuid.uuid4())
                space["pinned"] = False
            self.ext_reg = self.bus.register_object(PATH, _iface(SHELL_XML), self.on_call, self.get_property, None)
            self.emit(self.state())
        elif not on and self.ext_reg:
            self.emit({"stopped": True, "run": self.run})
            self.bus.unregister_object(self.ext_reg)
            self.ext_reg = 0

    def numbered(self, report: dict) -> str:
        self.seq += 1
        return json.dumps({**report, "seq": self.seq})

    def emit(self, report: dict):
        self.bus.emit_signal(None, PATH, IFACE, "Changed", GLib.Variant("(s)", (self.numbered(report),)))

    # ---- GNOME's own rules ------------------------------------------------------------------
    def _busy(self, i: int) -> bool:
        return any(w["space"] == i and not w["all"] for w in self.windows.values())

    def settle(self):
        """GNOME's WorkspaceTracker._checkWorkspaces, with pinned ones counted as busy."""
        if not self.dynamic:
            return
        busy = [self._busy(i) or s["pinned"] for i, s in enumerate(self.spaces)]
        while busy[-1] or len(busy) < 2:  # an empty one at the end, and never fewer than two
            self.spaces.append({"id": str(uuid.uuid4()), "name": "", "pinned": False})
            busy.append(False)
        last_empty = len(busy) - 1 - busy[::-1].index(True) + 1 if True in busy else 0
        busy[self.active] = True
        for i in range(len(self.spaces) - 1, -1, -1):
            if len(self.spaces) == 2:
                break
            if not busy[i] and i != last_empty:
                self._remove(i)

    def _remove(self, i: int):
        neighbour = i - 1 if i > 0 else 1
        for w in self.windows.values():
            if w["space"] == i:
                w["space"] = neighbour
        for w in self.windows.values():
            if w["space"] > i:
                w["space"] -= 1
        if self.active == i:
            self.active = neighbour
        if self.active > i:
            self.active -= 1
        self.spaces.pop(i)

    def index_of(self, ws_id: str) -> int:
        return next((i for i, s in enumerate(self.spaces) if s.get("id") == ws_id), -1)

    def state(self) -> dict:
        return {"full": True, "version": 1, "run": self.run, "dynamic": self.dynamic, "can_pin": True,
                "locked": self.locked,
                "workspaces": [{"id": s["id"], "index": i, "name": s["name"], "active": i == self.active,
                                "pinned": s["pinned"] if self.dynamic else True} for i, s in enumerate(self.spaces)],
                "windows": [self.window_info(wid) for wid in self.windows],
                "outputs": [{"index": i, "name": n, "primary": i == 0} for i, n in enumerate(OUTPUTS)],
                "sidebar": {"running": False, "pid": 0, "shown": False}}

    def window_info(self, wid: str) -> dict:
        w = self.windows[wid]
        return {"id": wid, "app": w["app"], "instance": w.get("instance", ""), "x11": w.get("x11", False),
                "title": w["title"], "pid": w["pid"],
                "workspace": None if w["all"] else self.spaces[w["space"]]["id"], "all": w["all"],
                "output": w["output"], "focused": self.focus == wid}

    def changed(self):
        self.settle()
        if self.ext_reg:
            self.emit(self.state())

    # ---- the extension's methods ------------------------------------------------------------
    def get_property(self, bus, sender, path, iface, name):
        return GLib.Variant("u", 1) if name == "Version" else None

    def on_call(self, bus, sender, path, iface, method, params, invocation):
        args = params.unpack()
        self.log.append(method)
        result = getattr(self, "m_" + method)(*args)
        if method == "GetState":
            invocation.return_value(GLib.Variant("(s)", (self.numbered(self.state()),)))
            return
        invocation.return_value(GLib.Variant("(s)" if method == "AddWorkspace" else "(b)", (result,)))
        self.changed()

    def m_GetState(self):
        return None

    def m_ActivateWorkspace(self, ws_id):
        i = self.index_of(ws_id)
        if i < 0 or self.locked:
            return False
        self.active = i
        return True

    def m_AddWorkspace(self, name):
        if self.locked:
            return ""
        last = self.spaces[-1]
        if self.dynamic and not last["pinned"] and not self._busy(len(self.spaces) - 1):
            space = last
        else:
            space = {"id": str(uuid.uuid4()), "name": "", "pinned": False}
            self.spaces.append(space)
        space["pinned"] = self.dynamic
        space["name"] = name
        return space["id"]

    def m_RemoveWorkspace(self, ws_id):
        i = self.index_of(ws_id)
        if i < 0 or len(self.spaces) < 2 or self.locked:
            return False
        self._remove(i)
        return True

    def m_RenameWorkspace(self, ws_id, name):
        i = self.index_of(ws_id)
        if i >= 0:
            self.spaces[i]["name"] = name
        return i >= 0

    def m_PinWorkspace(self, ws_id, pinned):
        i = self.index_of(ws_id)
        if i < 0 or self.locked:
            return False
        self.spaces[i]["pinned"] = pinned
        return True

    def m_MoveWorkspace(self, ws_id, other_id, before):
        i, j = self.index_of(ws_id), self.index_of(other_id)
        if i < 0 or j < 0 or i == j or self.locked:
            return False
        to = j + (0 if before else 1)
        if i < to:
            to -= 1
        ids = [s["id"] for s in self.spaces]
        on = {wid: ids[w["space"]] for wid, w in self.windows.items()}  # (windows go with their workspace)
        active = ids[self.active]
        self.spaces.insert(to, self.spaces.pop(i))
        ids = [s["id"] for s in self.spaces]
        for wid, w in self.windows.items():
            w["space"] = ids.index(on[wid])
        self.active = ids.index(active)
        return True

    def m_FocusWindow(self, wid):
        w = self.windows.get(wid)
        if w is None or self.locked:
            return False
        self.focus = wid
        if not w["all"]:
            self.active = w["space"]
        return True

    def m_CloseWindow(self, wid):
        if wid not in self.windows or self.locked:
            return False
        self.windows.pop(wid)
        if self.focus == wid:
            self.focus = None
        return True

    def m_MoveWindow(self, wid, ws_id, output):
        w = self.windows.get(wid)
        i = self.index_of(ws_id) if ws_id else -1
        if w is None or (ws_id and i < 0) or self.locked:
            return False
        if output in OUTPUTS:
            w["output"] = output
        if i >= 0 and not w["all"]:
            w["space"] = i
        return True

    # ---- /Test ------------------------------------------------------------------------------
    def on_test(self, bus, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "OpenWindow":
            spec = json.loads(args[0])
            wid = f"b00t-1-{next(self.numbers)}"
            self.windows[wid] = {"app": spec.get("app", "foot"), "title": spec.get("title", ""),
                                 "pid": spec.get("pid", 0), "space": spec.get("space", self.active),
                                 "all": bool(spec.get("all")), "output": spec.get("output", OUTPUTS[0]),
                                 "instance": spec.get("instance", ""), "x11": bool(spec.get("x11"))}
            if spec.get("focus", True):
                self.focus = wid
            invocation.return_value(GLib.Variant("(s)", (wid,)))
            self.changed()
        elif method == "SetTitle":
            self.windows[args[0]]["title"] = args[1]
            invocation.return_value(None)
            if self.ext_reg:  # a title alone goes out alone, as the extension sends it
                self.emit({"full": False, "run": self.run, "windows": [self.window_info(args[0])]})
        elif method == "CloseWindow":
            self.windows.pop(args[0], None)
            invocation.return_value(None)
            self.changed()
        elif method == "Extension":
            invocation.return_value(None)
            self.extension(args[0])
        elif method == "Lock":
            self.locked = args[0]
            invocation.return_value(None)
            self.changed()
        elif method == "State":
            state = self.state()
            state["log"] = self.log
            state["extension"] = bool(self.ext_reg)
            state["internal_ids"] = [s.get("id") for s in self.spaces]
            invocation.return_value(GLib.Variant("(s)", (json.dumps(state),)))


def main():
    FakeShell(sys.argv[1], static="--static" in sys.argv, on="--off" not in sys.argv)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
