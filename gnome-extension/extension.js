// SPDX-License-Identifier: MIT
//
// Sidelit on GNOME: what Sidelit's daemon and sidebar need from GNOME Shell, which has no
// Wayland protocol for it. desktop.js gives the daemon GNOME's workspaces and windows over
// D-Bus; sidebar.js runs the sidebar and keeps it docked on the left.

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {Desktop} from './desktop.js';
import {SidebarHost} from './sidebar.js';

export default class SidelitExtension extends Extension {
    enable() {
        this._sidebar = new SidebarHost();
        this._desktop = new Desktop(this._sidebar);
    }

    // Sidelit stays on while the screen is locked (the unlock-dialog session mode). Turned off
    // at every lock, GNOME would drop the empty workspaces Sidelit keeps, every workspace would
    // come back under a new id the daemon does not know, and the sidebar would close and its
    // reserved strip go, so maximized windows would resize twice around every unlock. While
    // locked it changes nothing on the desktop (the D-Bus methods refuse), and it listens to no
    // keyboard event at any time.
    disable() {
        this._desktop?.destroy();
        this._desktop = null;
        this._sidebar?.destroy();
        this._sidebar = null;
    }
}
