// SPDX-License-Identifier: MIT
//
// Sidelit's sidebar on GNOME.
//
// The sidebar is a GTK window. On COSMIC and KDE it asks the compositor for a panel (the
// layer shell); GNOME has none, so the way Desktop Icons NG runs its own window, this
// extension starts the sidebar itself and tells GNOME that window is a dock: on every
// workspace, above other windows, out of the overview, the dash and Alt+Tab. An invisible
// strip at the screen's left edge (or its right one, as the sidebar asks), which GNOME counts
// like the top bar, keeps maximized windows beside it. A dock or panel already on that edge
// keeps its place; the sidebar goes next to it.
//
// The sidebar runs while Sidelit's daemon does (it owns io.github.kulaxa.sidelit on the
// session bus). It is the program `sidelit` from ~/.local/bin, the PATH or /usr/bin, run as
// `sidelit sidebar`, its log in the journal as sidelit-sidebar. If it stops on its own it is
// started again, after a pause that grows while it keeps stopping.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {connectors} from './monitors.js';

const DAEMON_NAME = 'io.github.kulaxa.sidelit';
const TITLE = 'Sidelit';             // the sidebar's own window; its dialogs have other titles
const RESTART_MS = [500, 2000, 5000, 15000, 60000];
const STABLE_US = 60 * 1000 * 1000;  // a sidebar that ran this long had no trouble
const KEEP_STRIP_MS = 10000;         // after the sidebar went, the strip waits this long for the next
const SIGTERM = 15;

/**
 * The installed `sidelit` program: the user's own install first, as systemd prefers the
 * user's units over the system's.
 *
 * @returns {string|null}
 */
function findSidelit() {
    const own = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'sidelit']);
    for (const path of [own, GLib.find_program_in_path('sidelit'), '/usr/bin/sidelit']) {
        if (path && GLib.file_test(path, GLib.FileTest.IS_EXECUTABLE))
            return path;
    }
    return null;
}

export class SidebarHost {
    constructor() {
        this.onWindowChanged = null;  // set by Desktop: the sidebar's window left the window list
        this._client = null;          // Meta.WaylandClient: GNOME's handle on the process we started
        this._process = null;         // Gio.Subprocess
        this._pid = 0;
        this._running = false;
        this._startedAt = 0;
        this._failures = 0;
        this._restartId = 0;
        this._window = null;          // its main window, once it has shown
        this._waiting = new Set();    // its windows that may still turn into the main one (no title yet)
        this._output = '';            // the screen the sidebar asked for (PlaceSidebar, PlaceSidebarAt)
        this._side = 'left';          // and the edge: 'left' or 'right'
        this._connectors = null;     // the monitors' connector names, until they change
        this._strip = null;           // the invisible actor that reserves the strip
        this._dropStripId = 0;
        this._placeId = 0;
        this._daemonUp = false;
        this._warned = new Set();

        global.display.connectObject(
            'window-created', (_display, win) => this._consider(win),
            'workareas-changed', () => this._placeSoon(), this);
        Main.layoutManager.connectObject('monitors-changed', () => {
            this._connectors = null;
            this._placeSoon();
        }, this);
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, DAEMON_NAME, Gio.BusNameWatcherFlags.NONE,
            () => {
                this._daemonUp = true;
                this._start();
            },
            () => {
                this._daemonUp = false;
                this._stop();
            });
    }

    destroy() {
        Gio.bus_unwatch_name(this._watchId);
        this._daemonUp = false;
        this._stop();
        this._process = null;  // (its exit no longer concerns us)
        this._client = null;
        for (const id of [this._restartId, this._dropStripId, this._placeId]) {
            if (id)
                GLib.source_remove(id);
        }
        this._restartId = this._dropStripId = this._placeId = 0;
        this._release();
        this._dropStrip();
        for (const win of this._waiting)
            win.disconnectObject(this);
        this._waiting.clear();
        global.display.disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        this._connectors = null;
        this._warned.clear();
        this.onWindowChanged = null;
    }

    status() {
        return {running: this._running, pid: this._pid, shown: Boolean(this._window)};
    }

    isSidebarProcess(pid) {
        return this._running && pid > 0 && pid === this._pid;
    }

    /**
     * Is this window the sidebar's (its main window or one of its dialogs)?
     *
     * @param {Meta.Window} win
     * @returns {boolean}
     */
    isSidebar(win) {
        if (win === this._window)
            return true;
        try {
            return this._running && this._client !== null && this._client.owns_window(win);
        } catch {
            return false;
        }
    }

    place(pid, output, side) {
        if (!this.isSidebarProcess(pid))
            return false;
        this._output = output;
        this._side = side === 'right' ? 'right' : 'left';
        this._place();
        return true;
    }

    focus(pid) {
        if (!this.isSidebarProcess(pid) || !this._window)
            return false;
        this._window.focus(global.display.get_current_time_roundtrip());
        return true;
    }

    // ---- the process --------------------------------------------------------------------------
    _warnOnce(message) {
        if (!this._warned.has(message)) {
            this._warned.add(message);
            console.warn(`Sidelit: ${message}`);
        }
    }

    _start() {
        if (this._process || this._restartId || !this._daemonUp)
            return;
        // (GNOME 50 dropped X11 sessions, and with them this function)
        if (typeof Meta.is_wayland_compositor === 'function' && !Meta.is_wayland_compositor()) {
            this._warnOnce('the sidebar needs GNOME on Wayland; on X11 only the workspace interface runs');
            return;
        }
        const program = findSidelit();
        if (!program) {
            this._warnOnce('no sidelit program in ~/.local/bin, on the PATH or in /usr/bin: the sidebar cannot start');
            return;
        }
        // its output to the journal, as sidelit-sidebar (systemd-cat runs it in its own place)
        const cat = GLib.file_test('/run/systemd/journal/stdout', GLib.FileTest.EXISTS)
            ? GLib.find_program_in_path('systemd-cat') : null;
        const argv = cat ? [cat, '--identifier=sidelit-sidebar', program, 'sidebar'] : [program, 'sidebar'];
        const launcher = new Gio.SubprocessLauncher({flags: Gio.SubprocessFlags.NONE});
        launcher.setenv('SIDELIT_HOSTED', 'gnome', true);
        try {
            if (typeof Meta.WaylandClient.new_subprocess === 'function') {  // GNOME 49 and later
                this._client = Meta.WaylandClient.new_subprocess(global.context, launcher, argv);
                this._process = this._client.get_subprocess();
            } else {
                this._client = Meta.WaylandClient.new(global.context, launcher);
                this._process = this._client.spawnv(global.display, argv);
            }
        } catch (e) {
            console.error(`Sidelit: could not start the sidebar: ${e.message}`);
            this._client = this._process = null;
            this._restartLater();
            return;
        }
        this._running = true;
        this._pid = Number(this._process.get_identifier());
        this._startedAt = GLib.get_monotonic_time();
        const process = this._process;
        process.wait_async(null, (_proc, result) => {
            try {
                process.wait_finish(result);
            } catch {
                // (cancelled: we are going too)
            }
            this._exited(process);
        });
    }

    _exited(process) {
        if (process !== this._process)
            return;
        this._running = false;
        this._client = this._process = null;
        this._pid = 0;
        this._release();
        if (GLib.get_monotonic_time() - this._startedAt > STABLE_US)
            this._failures = 0;
        if (this._daemonUp)
            this._restartLater();
        else
            this._dropStrip();
    }

    _restartLater() {
        const delay = RESTART_MS[Math.min(this._failures, RESTART_MS.length - 1)];
        this._failures++;
        this._restartId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._restartId = 0;
            this._start();
            return GLib.SOURCE_REMOVE;
        });
    }

    /** The daemon went, or the extension is turned off: the sidebar goes too. */
    _stop() {
        if (this._restartId) {
            GLib.source_remove(this._restartId);
            this._restartId = 0;
        }
        this._failures = 0;
        if (this._running)
            this._process.send_signal(SIGTERM);  // it keeps nothing of its own: the daemon has it all
        else
            this._dropStrip();
    }

    // ---- its window ---------------------------------------------------------------------------
    _consider(win) {
        if (this._window || !this.isSidebar(win))
            return;
        // It becomes a dock as soon as it has its title, before GNOME shows it (a dock does not
        // take the keyboard when it appears); it is placed once shown: moving a window before
        // that breaks GNOME's first configure of it
        let shown = false, docked = false;
        const forget = () => {
            win.disconnectObject(this);
            this._waiting.delete(win);
        };
        const check = () => {
            if (this._window || win.get_title() !== TITLE || win.get_transient_for())
                return;
            if (!docked) {
                docked = true;
                this._dock(win);
            }
            if (shown) {
                forget();
                this._adopt(win);
            }
        };
        this._waiting.add(win);
        win.connectObject(
            'shown', () => {
                shown = true;
                check();
            },
            'notify::title', check,
            'unmanaged', forget, this);
        check();
    }

    /** A dock: on every workspace, above windows, out of the overview, the dash and Alt+Tab. */
    _dock(win) {
        if (typeof win.set_type === 'function') {  // GNOME 49 and later
            win.set_type(Meta.WindowType.DOCK);
            win.hide_from_window_list();
        } else {
            this._client.make_dock(win);
            this._client.hide_from_window_list(win);
        }
        win.stick();
    }

    _adopt(win) {
        this._window = win;
        win.connectObject(
            'position-changed', () => this._placeSoon(),
            'size-changed', () => this._placeSoon(),
            'unmanaged', () => this._release(), this);
        this._place();
        this.onWindowChanged?.();
    }

    _release() {
        if (!this._window)
            return;
        this._window.disconnectObject(this);
        this._window = null;
        this._dropStripSoon();
        this.onWindowChanged?.();
    }

    /**
     * The monitor the sidebar asked for: by its connector name, else by Sidelit's rule for it
     * ('rightmost', 'largest'; the GTK 3 sidebar knows no connector names), else the leftmost,
     * Sidelit's default.
     *
     * @returns {number}
     */
    _monitorIndex() {
        const monitors = Main.layoutManager.monitors;
        this._connectors ??= connectors();
        const named = this._output ? this._connectors.indexOf(this._output) : -1;
        if (named >= 0)
            return named;
        const better = {
            rightmost: (m, b) => m.x + m.width > b.x + b.width || (m.x + m.width === b.x + b.width && m.y < b.y),
            largest: (m, b) => m.width * m.height > b.width * b.height,
        }[this._output] ?? ((m, b) => m.x < b.x || (m.x === b.x && m.y < b.y));
        let best = 0;
        monitors.forEach((m, i) => {
            if (better(m, monitors[best]))
                best = i;
        });
        return best;
    }

    /**
     * How far from the monitor's left (or right) edge other things already reserve space (a
     * dock, a panel): the sidebar goes beside them. GNOME Shell keeps no list of them but its own.
     *
     * @param {object} monitor - its geometry
     * @param {string} side - 'left' or 'right'
     * @returns {number}
     */
    _inset(monitor, side) {
        const left = monitor.x, right = monitor.x + monitor.width;
        let inset = 0;
        for (const data of Main.layoutManager._trackedActors ?? []) {
            if (!data.affectsStruts || data.actor === this._strip)
                continue;
            const [x, y] = data.actor.get_transformed_position();
            const [w, h] = data.actor.get_transformed_size();
            const x1 = Math.round(x), x2 = Math.round(x + w), y1 = Math.round(y), y2 = Math.round(y + h);
            // on this monitor, and not across its whole width (a top or bottom bar)
            if (y2 <= monitor.y || y1 >= monitor.y + monitor.height || (x1 <= left && x2 >= right))
                continue;
            // along that edge
            if (side === 'right' && x1 < right && x2 >= right)
                inset = Math.max(inset, right - x1);
            else if (side !== 'right' && x1 <= left && x2 > left)
                inset = Math.max(inset, x2 - left);
        }
        return inset;
    }

    _placeSoon() {
        this._placeId ||= GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._placeId = 0;
            this._place();
            return GLib.SOURCE_REMOVE;
        });
    }

    _place() {
        const win = this._window;
        if (!win)
            return;
        const index = this._monitorIndex();
        const monitor = Main.layoutManager.monitors[index];
        if (!monitor)
            return;
        // as wide as the window is: its width in GNOME's own pixels, which GNOME has scaled for
        // the screen (at 200% without fractional scaling, the sidebar's 280 are 560 of them)
        const now = win.get_frame_rect();
        const width = now.width;
        if (width <= 0)
            return;  // (not drawn yet: it says how wide it is with its first frame)
        // the strip from the screen's edge, over a dock already there, to the sidebar's far side
        const inset = this._inset(monitor, this._side);
        const right = this._side === 'right';
        const stripX = right ? monitor.x + monitor.width - inset - width : monitor.x;
        this._reserve(stripX, monitor.y, inset + width, monitor.height);
        // from below the top bar to the bottom of what windows may use
        const area = Main.layoutManager.getWorkAreaForMonitor(index);
        const [x, y, h] = [right ? stripX : monitor.x + inset, area.y, area.height];
        if (now.x !== x || now.y !== y || now.height !== h)
            win.move_resize_frame(false, x, y, width, h);
    }

    // ---- the reserved strip -------------------------------------------------------------------
    _reserve(x, y, width, height) {
        if (this._dropStripId) {
            GLib.source_remove(this._dropStripId);
            this._dropStripId = 0;
        }
        if (!this._strip) {
            // (not reactive: clicks go through it to the sidebar's window under it)
            this._strip = new St.Widget({name: 'sidelit-strip', reactive: false});
            Main.layoutManager.addChrome(this._strip, {affectsStruts: true});
        }
        if (this._strip.x !== x || this._strip.y !== y)
            this._strip.set_position(x, y);
        if (this._strip.width !== width || this._strip.height !== height)
            this._strip.set_size(width, height);
    }

    _dropStripSoon() {
        if (!this._strip || this._dropStripId)
            return;
        this._dropStripId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, KEEP_STRIP_MS, () => {
            this._dropStripId = 0;
            if (!this._window)
                this._dropStrip();
            return GLib.SOURCE_REMOVE;
        });
    }

    _dropStrip() {
        if (this._dropStripId) {
            GLib.source_remove(this._dropStripId);
            this._dropStripId = 0;
        }
        if (this._strip) {
            Main.layoutManager.removeChrome(this._strip);
            this._strip.destroy();
            this._strip = null;
        }
    }
}
