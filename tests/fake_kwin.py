"""A KWin in miniature for the KDE backend's tests: org.kde.KWin on a private session bus.

    python3 tests/fake_kwin.py BUS_ADDRESS

It answers where kwin.py asks, the way KWin 6 does (checked against a headless KWin 6.8):
virtual desktops on /VirtualDesktopManager, scripts on /Scripting and /Scripting/Script<N>, run
a moment after `run` is called. It cannot run JavaScript, so it reads what each of Sidelit's
scripts is for from its first lines (who to report to, whose window, what to do), does that to
its own windows, and reports them the way the script would. /Test drives it: open, retitle and
close windows, and read its state.
"""
from __future__ import annotations

import json
import re
import sys
import uuid

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

DESKTOPS_XML = """<node><interface name="org.kde.KWin.VirtualDesktopManager">
  <property access="read" type="u" name="count"/>
  <property access="readwrite" type="s" name="current"/>
  <property access="read" type="a(uss)" name="desktops"/>
  <signal name="countChanged"><arg type="u" name="count"/></signal>
  <signal name="currentChanged"><arg type="s" name="id"/></signal>
  <signal name="desktopDataChanged"><arg type="s" name="id"/><arg type="(uss)" name="desktopData"/></signal>
  <signal name="desktopCreated"><arg type="s" name="id"/><arg type="(uss)" name="desktopData"/></signal>
  <signal name="desktopRemoved"><arg type="s" name="id"/></signal>
  <method name="createDesktop"><arg direction="in" type="u" name="position"/><arg direction="in" type="s" name="name"/></method>
  <method name="setDesktopName"><arg direction="in" type="s" name="id"/><arg direction="in" type="s" name="name"/></method>
  <method name="removeDesktop"><arg direction="in" type="s" name="id"/></method>
</interface></node>"""
SCRIPTING_XML = """<node><interface name="org.kde.kwin.Scripting">
  <method name="start"/>
  <method name="loadScript"><arg type="i" direction="out"/><arg name="filePath" type="s" direction="in"/><arg name="pluginName" type="s" direction="in"/></method>
  <method name="isScriptLoaded"><arg type="b" direction="out"/><arg name="pluginName" type="s" direction="in"/></method>
  <method name="unloadScript"><arg type="b" direction="out"/><arg name="pluginName" type="s" direction="in"/></method>
</interface></node>"""
SCRIPT_XML = """<node><interface name="org.kde.kwin.Script"><method name="stop"/><method name="run"/></interface></node>"""
TEST_XML = """<node><interface name="org.kde.KWin.Test">
  <method name="OpenWindow"><arg type="s" name="spec" direction="in"/><arg type="s" direction="out"/></method>
  <method name="SetTitle"><arg type="s" name="id" direction="in"/><arg type="s" name="title" direction="in"/></method>
  <method name="CloseWindow"><arg type="s" name="id" direction="in"/></method>
  <method name="State"><arg type="s" direction="out"/></method>
</interface></node>"""
IFACE = "io.github.kulaxa.sidelit.KWin"


def _iface(xml: str):
    return Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]


def _value(head: str, key: str):
    """The JSON literal after `key = ` in a script's first line."""
    start = head.index(f"{key} = ") + len(key) + 3
    return json.JSONDecoder().raw_decode(head, start)[0]


class FakeKWin:
    def __init__(self, address: str):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.desktops = [{"id": str(uuid.uuid4()), "name": f"Desktop {i}"} for i in (1, 2, 3)]
        self.current = self.desktops[0]["id"]
        self.screens = ["Virtual-0", "Virtual-1"]
        self.windows: dict[str, dict] = {}
        self.scripts: dict[str, dict] = {}  # name -> {"path", "number", "watch": (service, path) | None}
        self.numbers = iter(range(1 << 30))
        self.log: list[str] = []
        self.bus.register_object("/VirtualDesktopManager", _iface(DESKTOPS_XML), self.on_desktops,
                                 self.get_property, self.set_property)
        self.bus.register_object("/Scripting", _iface(SCRIPTING_XML), self.on_scripting, None, None)
        self.bus.register_object("/Test", _iface(TEST_XML), self.on_test, None, None)
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                           GLib.Variant("(su)", ("org.kde.KWin", 4)), None, 0, -1, None)

    # ---- desktops ---------------------------------------------------------------------------
    def rows(self):
        return [(i, d["id"], d["name"]) for i, d in enumerate(self.desktops)]

    def emit(self, name: str, args: GLib.Variant | None):
        self.bus.emit_signal(None, "/VirtualDesktopManager", "org.kde.KWin.VirtualDesktopManager", name, args)

    def get_property(self, bus, sender, path, iface, prop):
        return {"count": GLib.Variant("u", len(self.desktops)), "current": GLib.Variant("s", self.current),
                "desktops": GLib.Variant("a(uss)", self.rows())}[prop]

    def set_property(self, bus, sender, path, iface, prop, value):
        if prop == "current" and any(d["id"] == value.unpack() for d in self.desktops):
            self.switch(value.unpack())
            return True
        return False

    def switch(self, desktop_id: str):
        if desktop_id != self.current:
            self.current = desktop_id
            self.emit("currentChanged", GLib.Variant("(s)", (desktop_id,)))

    def on_desktops(self, bus, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "createDesktop":
            desktop = {"id": str(uuid.uuid4()), "name": args[1]}
            self.desktops.insert(min(args[0], len(self.desktops)), desktop)
            row = next(r for r in self.rows() if r[1] == desktop["id"])
            self.emit("desktopCreated", GLib.Variant("(s(uss))", (desktop["id"], row)))
            self.emit("countChanged", GLib.Variant("(u)", (len(self.desktops),)))
        elif method == "setDesktopName":
            for row in self.rows():
                if row[1] == args[0]:
                    self.desktops[row[0]]["name"] = args[1]
                    self.emit("desktopDataChanged", GLib.Variant("(s(uss))", (args[0], (row[0], args[0], args[1]))))
        elif method == "removeDesktop" and len(self.desktops) > 1:
            self.desktops = [d for d in self.desktops if d["id"] != args[0]]
            for w in self.windows.values():  # KWin moves them to a desktop that is left
                w["desktops"] = [d for d in w["desktops"] if d != args[0]] or ([] if w["all"] else [self.desktops[0]["id"]])
            if self.current == args[0]:
                self.switch(self.desktops[0]["id"])
            self.emit("desktopRemoved", GLib.Variant("(s)", (args[0],)))
            self.emit("countChanged", GLib.Variant("(u)", (len(self.desktops),)))
            self.report()
        invocation.return_value(None)

    # ---- scripts ----------------------------------------------------------------------------
    def on_scripting(self, bus, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "loadScript":
            if args[1] in self.scripts:
                invocation.return_value(GLib.Variant("(i)", (-1,)))
                return
            number = next(self.numbers)
            script = {"path": args[0], "number": number, "watch": None,
                      "registration": self.bus.register_object(f"/Scripting/Script{number}", _iface(SCRIPT_XML),
                                                               self.on_script, None, None)}
            self.scripts[args[1]] = script
            invocation.return_value(GLib.Variant("(i)", (number,)))
        elif method == "isScriptLoaded":
            invocation.return_value(GLib.Variant("(b)", (args[0] in self.scripts,)))
        elif method == "unloadScript":
            script = self.scripts.pop(args[0], None)
            if script is not None:
                self.bus.unregister_object(script["registration"])
            invocation.return_value(GLib.Variant("(b)", (script is not None,)))
        else:
            invocation.return_value(None)

    def on_script(self, bus, sender, path, iface, method, params, invocation):
        name = next((n for n, s in self.scripts.items() if f"/Scripting/Script{s['number']}" == path), None)
        invocation.return_value(None)
        if method == "run" and name is not None:
            GLib.idle_add(self.execute, name)  # KWin reads the file and runs it a moment later

    def execute(self, name: str):
        script = self.scripts.get(name)
        if script is None:
            return False
        with open(script["path"]) as f:
            source = f.read()
        first, head = source.splitlines()[:2]
        what = first.split()[2]
        to = (_value(head, "SERVICE"), _value(head, "OUR_PATH"))
        self.log.append(f"{what} {name}")
        if what == "windows":
            if "sidelitWatch" in source:
                script["watch"] = to
            self.call(to, "Windows", self.snapshot(full=True))
        else:
            wid = json.loads(re.search(r"sidelit\.byId\((\"(?:[^\"\\]|\\.)*\")\)", source).group(1))
            self.act(what, wid, _value(head, "ARG"))
        if "sidelit.done();" in source:
            self.call(to, "Done", _value(head, "NAME"))
        return False

    def act(self, action: str, wid: str, arg: dict):
        w = self.windows.get(wid)
        if w is None:
            return
        if action == "focus":
            self.focus(wid)
            self.report()
        elif action == "close":
            del self.windows[wid]
            self.report()
        elif action == "move":
            if any(d["id"] == arg.get("desktop") for d in self.desktops):
                w["desktops"], w["all"] = [arg["desktop"]], False
            if arg.get("output") in self.screens:
                w["output"] = arg["output"]
            self.report(only=wid)

    def focus(self, wid: str):
        for other in self.windows.values():
            other["active"] = other["id"] == wid
        w = self.windows[wid]
        if w["desktops"] and self.current not in w["desktops"]:
            self.switch(w["desktops"][0])

    # ---- reports ----------------------------------------------------------------------------
    def snapshot(self, full: bool, only: str | None = None) -> str:
        windows = [self.windows[only]] if only else list(self.windows.values())
        return json.dumps({"full": full, "windows": windows, "screens": self.screens})

    def call(self, to: tuple, method: str, text: str):
        self.bus.call(to[0], to[1], IFACE, method, GLib.Variant("(s)", (text,)), None, 0, -1, None, None, None)

    def report(self, only: str | None = None):
        for script in self.scripts.values():
            if script["watch"]:
                self.call(script["watch"], "Windows", self.snapshot(full=only is None, only=only))

    # ---- what the tests drive ---------------------------------------------------------------
    def on_test(self, bus, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "OpenWindow":
            spec = json.loads(args[0])
            wid = "{%s}" % uuid.uuid4()
            on_all = bool(spec.get("all"))
            self.windows[wid] = {"id": wid, "app": spec.get("app", "foot"), "title": spec.get("title", ""),
                                 "pid": spec.get("pid", 4242), "all": on_all, "active": False,
                                 "desktops": [] if on_all else [spec.get("desktop") or self.current],
                                 "output": spec.get("output", self.screens[0])}
            if on_all or self.current in self.windows[wid]["desktops"]:
                self.focus(wid)  # a new window on the desktop you are on gets the keyboard
            self.report()
            invocation.return_value(GLib.Variant("(s)", (wid,)))
            return
        if method == "SetTitle" and args[0] in self.windows:
            self.windows[args[0]]["title"] = args[1]
            self.report(only=args[0])
        elif method == "CloseWindow" and self.windows.pop(args[0], None) is not None:
            self.report()
        elif method == "State":
            state = {"desktops": self.rows(), "current": self.current, "windows": self.windows,
                     "scripts": sorted(self.scripts), "watching": sorted(n for n, s in self.scripts.items() if s["watch"]),
                     "log": self.log}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(state),)))
            return
        invocation.return_value(None)


if __name__ == "__main__":
    FakeKWin(sys.argv[1])
    GLib.MainLoop().run()
