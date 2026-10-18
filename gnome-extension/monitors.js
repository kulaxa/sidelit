// SPDX-License-Identifier: MIT
//
// Which monitor is which: Sidelit names screens by connector (DP-1, eDP-1, HDMI-A-1), the
// way the kernel and the other desktops do; GNOME Shell numbers them.

import Gio from 'gi://Gio';

/**
 * The connector name of each monitor, by GNOME's monitor index ('' where it is not known).
 *
 * @returns {string[]}
 */
export function connectors() {
    const n = global.display.get_n_monitors();
    const names = new Array(n).fill('');
    const manager = global.backend.get_monitor_manager();
    if (typeof manager.get_logical_monitors === 'function') {  // GNOME 49 and later
        for (const logical of manager.get_logical_monitors()) {
            const [monitor] = logical.get_monitors();
            if (monitor && logical.get_number() < n)
                names[logical.get_number()] = monitor.get_connector();
        }
        return names;
    }
    // GNOME 46-48 only say which monitor a connector is: ask for every connector the kernel
    // has, and for the names of virtual monitors
    const candidates = ['Meta-0', 'Meta-1', 'Meta-2', 'Meta-3'];
    try {
        const children = Gio.File.new_for_path('/sys/class/drm').enumerate_children(
            'standard::name', Gio.FileQueryInfoFlags.NONE, null);
        for (let info; (info = children.next_file(null));) {
            const match = /^card\d+-(.+)$/.exec(info.get_name());
            if (match)
                candidates.push(match[1]);
        }
        children.close(null);
    } catch {
        // no sysfs: the virtual names only
    }
    for (const name of candidates) {
        const index = manager.get_monitor_for_connector(name);
        if (index >= 0 && index < n && !names[index])
            names[index] = name;
    }
    return names;
}
