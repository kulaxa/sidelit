// SPDX-License-Identifier: MIT
//
// GNOME's workspaces and windows, for Sidelit's daemon, over D-Bus.
//
// Sidelit's daemon keeps a layout: which windows belong on which workspace and screen. On
// other desktops it reads that from Wayland protocols or the compositor's own interface;
// GNOME has neither for an outside app, so this object, exported on GNOME Shell's own bus
// connection at /io/github/kulaxa/Sidelit, does it:
//
// - GetState: every workspace (an id of ours, its position, whether you are on it, whether
//   it is kept) and every window (app id, title, process, workspace, screen, focus), as JSON;
// - Changed: the same whenever something changes. A window's title changes on its own (a
//   busy Claude animates its title ten times a second), so such a report carries only the
//   windows that changed ("full": false). Every report is numbered ("seq"): one that arrives
//   after a newer one is old news;
// - methods to switch, add, keep (pin), reorder, rename and remove workspaces, and to focus,
//   move and close windows. They answer false for a workspace or window that is gone, and
//   do nothing while the screen is locked.
//
// GNOME drops an empty workspace by itself (dynamic workspaces, the default). A workspace
// Sidelit keeps is "pinned" here: GNOME's check that drops empty ones sees it as busy.
// With a fixed number of workspaces nothing is dropped and nothing needs pinning.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';

import {connectors} from './monitors.js';

const OBJECT_PATH = '/io/github/kulaxa/Sidelit';
const API_VERSION = 1;
const FLUSH_MS = 40;  // changes that come together go out in one report

const INTERFACE = `<node>
  <interface name="io.github.kulaxa.Sidelit.Shell">
    <property name="Version" type="u" access="read"/>
    <method name="GetState">
      <arg type="s" direction="out" name="state"/>
    </method>
    <method name="ActivateWorkspace">
      <arg type="s" direction="in" name="workspace"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="AddWorkspace">
      <arg type="s" direction="in" name="name"/>
      <arg type="s" direction="out" name="workspace"/>
    </method>
    <method name="RemoveWorkspace">
      <arg type="s" direction="in" name="workspace"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="RenameWorkspace">
      <arg type="s" direction="in" name="workspace"/>
      <arg type="s" direction="in" name="name"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="PinWorkspace">
      <arg type="s" direction="in" name="workspace"/>
      <arg type="b" direction="in" name="pinned"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="MoveWorkspace">
      <arg type="s" direction="in" name="workspace"/>
      <arg type="s" direction="in" name="other"/>
      <arg type="b" direction="in" name="before"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="FocusWindow">
      <arg type="s" direction="in" name="window"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="CloseWindow">
      <arg type="s" direction="in" name="window"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="MoveWindow">
      <arg type="s" direction="in" name="window"/>
      <arg type="s" direction="in" name="workspace"/>
      <arg type="s" direction="in" name="output"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="PlaceSidebar">
      <arg type="s" direction="in" name="output"/>
      <arg type="b" direction="out" name="done"/>
    </method>
    <method name="FocusSidebar">
      <arg type="b" direction="out" name="done"/>
    </method>
    <signal name="Changed">
      <arg type="s" name="report"/>
    </signal>
  </interface>
</node>`;

const REPORTED_TYPES = [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG];

/**
 * What names this GNOME Shell: the boot and the shell's process. A window's id is this and
 * Mutter's own number for it, so it stays the same when the extension is turned off and on
 * again (or Sidelit's daemon restarts), and is never given to another window.
 *
 * @returns {string}
 */
function shellToken() {
    let boot = '';
    try {
        const [, bytes] = GLib.file_get_contents('/proc/sys/kernel/random/boot_id');
        boot = new TextDecoder().decode(bytes).trim().slice(0, 8);
    } catch {
        boot = GLib.uuid_string_random().slice(0, 8);
    }
    return `${boot}-${new Gio.Credentials().get_unix_pid()}`;
}

export class Desktop {
    /**
     * @param {object} sidebar - the SidebarHost, which knows the sidebar's own window
     */
    constructor(sidebar) {
        this._sidebar = sidebar;
        this._token = shellToken();
        this._run = GLib.uuid_string_random();  // this time the extension was turned on
        this._seq = 0;                          // the last report's number
        this._ids = new Map();      // Meta.Workspace -> our id for it
        this._names = new Map();    // Meta.Workspace -> the name Sidelit gave it
        this._pinned = new Set();   // Meta.Workspace kept while empty
        this._watched = new Set();  // Meta.Window we listen to
        this._dirty = new Set();    // Meta.Window changed since the last report
        this._full = true;          // the next report says everything
        this._flushId = 0;
        this._connectors = null;    // by monitor index, until the monitors change
        this._mutterSettings = new Gio.Settings({schema_id: 'org.gnome.mutter'});

        this._injections = new InjectionManager();
        this._canPin = this._keepPinnedAlive();

        const changed = () => this._queue(null);
        global.workspace_manager.connectObject(
            'workspace-added', changed,
            'workspace-removed', () => this._workspaceRemoved(),
            'workspaces-reordered', changed,
            'active-workspace-changed', changed, this);
        global.display.connectObject(
            'window-created', (_display, win) => {
                this._watch(win);
                this._queue(null);
            },
            'notify::focus-window', changed,
            'window-entered-monitor', (_display, _index, win) => this._queue(win),
            'window-left-monitor', (_display, _index, win) => this._queue(win), this);
        Main.layoutManager.connectObject('monitors-changed', () => {
            this._connectors = null;
            this._queue(null);
        }, this);
        this._mutterSettings.connectObject(
            'changed::dynamic-workspaces', changed,
            'changed::workspaces-only-on-primary', changed, this);
        Main.sessionMode.connectObject('updated', changed, this);
        this._sidebar.onWindowChanged = changed;  // its window leaves (or rejoins) the list

        for (const win of global.display.list_all_windows())
            this._watch(win);

        this._dbus = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._queue(null);  // a daemon that is waiting for us learns we are here
    }

    destroy() {
        if (this._flushId) {
            GLib.source_remove(this._flushId);
            this._flushId = 0;
        }
        try {
            this._emit({stopped: true, run: this._run});
            Gio.DBus.session.flush_sync(null);
        } catch {
            // the bus is going too (logout)
        }
        this._dbus.unexport();
        this._dbus = null;
        this._injections.clear();
        this._injections = null;
        for (const win of this._watched)
            win.disconnectObject(this);
        this._watched.clear();
        this._dirty.clear();
        global.workspace_manager.disconnectObject(this);
        global.display.disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        Main.sessionMode.disconnectObject(this);
        this._mutterSettings.disconnectObject(this);
        this._mutterSettings = null;
        this._sidebar.onWindowChanged = null;
        this._sidebar = null;
        // the workspaces kept here are GNOME's own again: it drops the empty ones
        this._pinned.clear();
        this._names.clear();
        this._ids.clear();
        Main.wm._workspaceTracker?._queueCheckWorkspaces?.();
    }

    // ---- keeping workspaces ------------------------------------------------------------------

    /**
     * GNOME drops empty workspaces in WorkspaceTracker._checkWorkspaces, which leaves alone a
     * workspace that is being kept alive for a moment (_keepAliveId, set while an app starts
     * there). For the length of each check, a pinned workspace looks like one of those.
     *
     * @returns {boolean} whether pinning is possible on this GNOME
     */
    _keepPinnedAlive() {
        const tracker = Main.wm._workspaceTracker;
        const proto = tracker ? Object.getPrototypeOf(tracker) : null;
        if (typeof proto?._checkWorkspaces !== 'function') {
            console.warn('Sidelit: this GNOME Shell has no workspace tracker to hook; workspaces cannot be kept');
            return false;
        }
        const pinned = this._pinned;
        this._injections.overrideMethod(proto, '_checkWorkspaces', original => function (...args) {
            const marked = [];
            for (const ws of pinned) {
                if (!ws._keepAliveId) {
                    marked.push([ws, Object.hasOwn(ws, '_keepAliveId'), ws._keepAliveId]);
                    ws._keepAliveId = -1;
                }
            }
            try {
                return original.apply(this, args);
            } finally {
                for (const [ws, had, value] of marked) {
                    if (had)
                        ws._keepAliveId = value;
                    else
                        delete ws._keepAliveId;
                }
            }
        });
        return true;
    }

    _checkWorkspacesSoon() {
        Main.wm._workspaceTracker?._queueCheckWorkspaces?.();
    }

    _workspaceRemoved() {
        const live = new Set(this._workspaces());
        for (const map of [this._ids, this._names]) {
            for (const ws of [...map.keys()]) {
                if (!live.has(ws))
                    map.delete(ws);
            }
        }
        for (const ws of [...this._pinned]) {
            if (!live.has(ws))
                this._pinned.delete(ws);
        }
        this._queue(null);
    }

    // ---- what there is -----------------------------------------------------------------------
    _workspaces() {
        const manager = global.workspace_manager;
        const list = [];
        for (let i = 0; i < manager.get_n_workspaces(); i++)
            list.push(manager.get_workspace_by_index(i));
        return list;
    }

    _idOf(ws) {
        let id = this._ids.get(ws);
        if (!id) {
            id = GLib.uuid_string_random();
            this._ids.set(ws, id);
        }
        return id;
    }

    _workspace(id) {
        return id ? this._workspaces().find(ws => this._ids.get(ws) === id) ?? null : null;
    }

    _windowId(win) {
        return `${this._token}-${win.get_id()}`;
    }

    _window(id) {
        return global.display.list_all_windows().find(w => this._windowId(w) === id && this._reported(w)) ?? null;
    }

    _reported(win) {
        return !win.is_override_redirect() && !win.is_skip_taskbar() &&
            REPORTED_TYPES.includes(win.get_window_type()) && !this._sidebar.isSidebar(win);
    }

    _dynamic() {
        return Meta.prefs_get_dynamic_workspaces();
    }

    _monitorConnectors() {
        this._connectors ??= connectors();
        return this._connectors;
    }

    _workspaceInfo(ws) {
        return {
            id: this._idOf(ws),
            index: ws.index(),
            name: this._names.get(ws) ?? '',
            active: ws.active,
            pinned: this._dynamic() ? this._pinned.has(ws) : true,
        };
    }

    _windowInfo(win) {
        // (with workspaces on the primary screen only, GNOME's default, a window on another
        // screen is on every workspace)
        const all = win.is_on_all_workspaces();
        const ws = all ? null : win.get_workspace();
        return {
            id: this._windowId(win),
            // the Wayland app id; an X11 window's class and instance
            app: win.get_wm_class() ?? '',
            instance: win.get_wm_class_instance() ?? '',
            x11: win.get_client_type() === Meta.WindowClientType.X11,
            title: win.get_title() ?? '',
            pid: Math.max(win.get_pid(), 0),
            workspace: ws ? this._idOf(ws) : null,
            all,
            output: this._monitorConnectors()[win.get_monitor()] ?? '',
            focused: win.has_focus(),
        };
    }

    _state() {
        const outputs = this._monitorConnectors();
        return {
            full: true,
            version: API_VERSION,
            run: this._run,
            dynamic: this._dynamic(),
            can_pin: this._canPin,
            locked: Main.sessionMode.isLocked,
            workspaces: this._workspaces().map(ws => this._workspaceInfo(ws)),
            windows: global.display.list_all_windows().filter(w => this._reported(w)).map(w => this._windowInfo(w)),
            outputs: Main.layoutManager.monitors.map((_m, i) => ({
                index: i, name: outputs[i] ?? '', primary: i === Main.layoutManager.primaryIndex,
            })),
            sidebar: this._sidebar.status(),
        };
    }

    // ---- telling Sidelit ---------------------------------------------------------------------
    _watch(win) {
        if (this._watched.has(win))
            return;
        this._watched.add(win);
        const one = () => this._queue(win);
        win.connectObject(
            'notify::title', one,
            'notify::wm-class', one,
            'notify::on-all-workspaces', one,
            'workspace-changed', one,
            'unmanaged', () => {
                win.disconnectObject(this);
                this._watched.delete(win);
                this._dirty.delete(win);
                this._queue(null);
            }, this);
    }

    /**
     * Report soon: `win` alone, or everything (null).
     *
     * @param {Meta.Window|null} win
     */
    _queue(win) {
        if (win === null)
            this._full = true;
        else
            this._dirty.add(win);
        this._flushId ||= GLib.timeout_add(GLib.PRIORITY_DEFAULT, FLUSH_MS, () => {
            this._flushId = 0;
            this._flush();
            return GLib.SOURCE_REMOVE;
        });
    }

    _flush() {
        if (this._full) {
            this._full = false;
            this._dirty.clear();
            this._emit(this._state());
            return;
        }
        const windows = [...this._dirty].filter(w => this._watched.has(w) && this._reported(w));
        this._dirty.clear();
        if (windows.length)
            this._emit({full: false, run: this._run, windows: windows.map(w => this._windowInfo(w))});
    }

    /**
     * A report, numbered: a signal sent just before a method's answer reaches the caller after
     * it, so the number tells which of the two is newer.
     *
     * @param {object} report
     * @returns {string} JSON
     */
    _numbered(report) {
        return JSON.stringify({...report, seq: ++this._seq});
    }

    _emit(report) {
        this._dbus?.emit_signal('Changed', new GLib.Variant('(s)', [this._numbered(report)]));
    }

    _allowed() {
        return !Main.sessionMode.isLocked;
    }

    /** A timestamp for an action (D-Bus brings none, and GNOME warns about a missing one). */
    _now() {
        return global.display.get_current_time_roundtrip();
    }

    // ---- D-Bus: io.github.kulaxa.Sidelit.Shell -------------------------------------------------
    get Version() {
        return API_VERSION;
    }

    GetState() {
        return this._numbered(this._state());
    }

    ActivateWorkspace(id) {
        const ws = this._workspace(id);
        if (!ws || !this._allowed())
            return false;
        ws.activate(this._now());
        return true;
    }

    /**
     * A workspace at the end, kept and named. With dynamic workspaces the empty one GNOME keeps
     * at the end becomes it (GNOME adds a new empty one after it).
     *
     * @param {string} name
     * @returns {string} its id, or '' if none could be added
     */
    AddWorkspace(name) {
        if (!this._allowed())
            return '';
        const manager = global.workspace_manager;
        let ws = null;
        if (this._dynamic()) {
            const last = manager.get_workspace_by_index(manager.get_n_workspaces() - 1);
            const empty = last && !this._pinned.has(last) &&
                !last.list_windows().some(w => !w.is_on_all_workspaces());
            ws = empty ? last : manager.append_new_workspace(false, this._now());
            this._pinned.add(ws);
            this._checkWorkspacesSoon();
        } else {
            ws = manager.append_new_workspace(false, this._now());
        }
        if (name)
            this._names.set(ws, name);
        this._queue(null);
        return this._idOf(ws);
    }

    RemoveWorkspace(id) {
        const ws = this._workspace(id);
        const manager = global.workspace_manager;
        if (!ws || !this._allowed() || manager.get_n_workspaces() < 2)
            return false;
        this._pinned.delete(ws);
        manager.remove_workspace(ws, this._now());
        return true;
    }

    RenameWorkspace(id, name) {
        const ws = this._workspace(id);
        if (!ws || !this._allowed())
            return false;
        if (name)
            this._names.set(ws, name);
        else
            this._names.delete(ws);
        this._queue(null);
        return true;
    }

    PinWorkspace(id, pinned) {
        const ws = this._workspace(id);
        if (!ws || !this._canPin || !this._allowed())
            return false;
        if (pinned)
            this._pinned.add(ws);
        else
            this._pinned.delete(ws);
        this._checkWorkspacesSoon();
        this._queue(null);
        return true;
    }

    /**
     * Put workspace `id` right before (or after) workspace `other`.
     *
     * @returns {boolean}
     */
    MoveWorkspace(id, other, before) {
        const ws = this._workspace(id), next = this._workspace(other);
        if (!ws || !next || ws === next || !this._allowed())
            return false;
        let to = next.index() + (before ? 0 : 1);
        if (ws.index() < to)
            to -= 1;
        global.workspace_manager.reorder_workspace(ws, to);
        return true;
    }

    FocusWindow(id) {
        const win = this._window(id);
        if (!win || !this._allowed())
            return false;
        if (win.is_on_all_workspaces())
            win.activate(this._now());
        else
            Main.activateWindow(win, this._now());
        return true;
    }

    CloseWindow(id) {
        const win = this._window(id);
        if (!win || !this._allowed())
            return false;
        win.delete(this._now());
        return true;
    }

    /**
     * Put a window on a workspace, and on the monitor `output` (a connector name) if given.
     *
     * @returns {boolean}
     */
    MoveWindow(id, workspace, output) {
        const win = this._window(id);
        const ws = this._workspace(workspace);
        if (!win || (workspace && !ws) || !this._allowed())
            return false;
        const monitor = output ? this._monitorConnectors().indexOf(output) : -1;
        // the monitor first: with workspaces on the primary monitor only, a window that
        // reaches the primary monitor lands on the workspace you are on
        if (monitor >= 0 && win.get_monitor() !== monitor)
            win.move_to_monitor(monitor);
        if (ws && !win.is_on_all_workspaces())
            win.change_workspace(ws);
        return true;
    }

    /**
     * The sidebar says which monitor it goes on (connector name, '' for the leftmost). Its
     * width is its window's own.
     */
    PlaceSidebarAsync([output], invocation) {
        this._fromSidebar(invocation, pid => this._sidebar?.place(pid, output));
    }

    /**
     * The sidebar wants the keyboard (its search field). GNOME gives a dock the keyboard only
     * when asked: a click on it leaves the keyboard where it was.
     */
    FocusSidebarAsync(_params, invocation) {
        this._fromSidebar(invocation, pid => this._allowed() && this._sidebar?.focus(pid));
    }

    /**
     * Run `act(pid)` for a call from the sidebar this extension runs, and only from it.
     *
     * @param {Gio.DBusMethodInvocation} invocation
     * @param {function(number): boolean} act
     */
    _fromSidebar(invocation, act) {
        Gio.DBus.session.call(
            'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetConnectionUnixProcessID', new GLib.Variant('(s)', [invocation.get_sender()]),
            new GLib.VariantType('(u)'), Gio.DBusCallFlags.NONE, -1, null, (bus, result) => {
                let pid = 0;
                try {
                    [pid] = bus.call_finish(result).deepUnpack();
                } catch (e) {
                    invocation.return_gerror(e);
                    return;
                }
                if (!this._sidebar?.isSidebarProcess(pid)) {
                    invocation.return_dbus_error('io.github.kulaxa.Sidelit.Error.NotTheSidebar',
                        'only the sidebar this extension runs may do this');
                    return;
                }
                invocation.return_value(new GLib.Variant('(b)', [Boolean(act(pid))]));
            });
    }
}
