#!/usr/bin/env bash
# Install Sidelit for the current user from this checkout. No sudo, no system files.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
bindir="$HOME/.local/bin"
desktopdir="$HOME/.local/share/applications"
autostartdir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

fail=0
# COSMIC, KDE Plasma 6, GNOME or Hyprland on Wayland, or an X11 desktop (Cinnamon, Xfce, MATE):
# say so now rather than fail at the first start (any desktop the backend picker does not know
# gets COSMIC's backend)
desktop=${XDG_CURRENT_DESKTOP:-unknown}
backend=$(PYTHONPATH="$repo" python3 -c 'from sidelit.backend import chosen; print(chosen())' 2>/dev/null || true)
gnome=0
[ "$backend" = gnome ] && gnome=1
# (on X11 any window manager that keeps the EWMH hints will do; the daemon says if this one does not)
if [[ $backend != x11 ]]; then
    [[ $backend != cosmic || ":$desktop:" == *:COSMIC:* ]] ||
        echo "! Sidelit runs on COSMIC, KDE Plasma 6, GNOME and Hyprland, and on X11 desktops, not on $desktop: it will not start here"
    [[ ${XDG_SESSION_TYPE:-} == wayland ]] ||
        echo "! the sidebar needs a Wayland session, and this one is ${XDG_SESSION_TYPE:-not Wayland}"
fi
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" &>/dev/null; then
    echo "✗ python3 GTK bindings missing. Install with 'sudo apt install python3-gi gir1.2-gtk-3.0'."
    fail=1
fi
if [[ $backend == x11 ]]; then
    # X11 has no layer shell: the GTK 3 sidebar docks itself through python-xlib, as the daemon talks
    if ! python3 -c "import Xlib.display" &>/dev/null; then
        echo "✗ python-xlib missing. Install with 'sudo apt install python3-xlib' (Fedora: python3-xlib, Arch: python-xlib)."
        fail=1
    fi
elif [[ $gnome == 1 ]]; then
    # GNOME has no layer shell: its extension docks a plain GTK 4 window (GTK 3 if need be)
    python3 -c "import gi; gi.require_version('Gtk','4.0')" &>/dev/null ||
        echo "! GTK 4's python bindings missing: the GTK 3 sidebar runs. For the smooth one: 'sudo apt install gir1.2-gtk-4.0'"
else
    if ! python3 -c "import gi; gi.require_version('GtkLayerShell','0.1'); from gi.repository import GtkLayerShell" &>/dev/null; then
        echo "✗ gtk-layer-shell bindings missing. Install with 'sudo apt install gir1.2-gtklayershell-0.1'."
        fail=1
    fi
    if ! PYTHONPATH="$repo" python3 -c "from sidelit import sidebar4; raise SystemExit(sidebar4.layer_shell_paths() is None)" &>/dev/null; then
        echo "! gtk4-layer-shell not found: the GTK 3 sidebar runs (see \"The GTK 4 sidebar\" in the README for the smooth one)"
    fi
fi
if ! command -v claude &>/dev/null && ! command -v codex &>/dev/null; then
    echo "! claude CLI not found on PATH (nor codex). The sidebar runs, but the session features need Claude Code or Codex."
fi
[[ $fail -eq 1 ]] && exit 1

unitdir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$bindir" "$desktopdir" "$autostartdir" "$unitdir"
for name in sidelit sidelit-open sidelit-hook sidelit-chrome-host; do
    chmod +x "$repo/bin/$name"
    ln -sf "$repo/bin/$name" "$bindir/$name"
done
sed "s|^Exec=.*|Exec=$bindir/sidelit start|" "$repo/data/sidelit.desktop" > "$desktopdir/sidelit.desktop"
sed "s|^Exec=.*|Exec=$bindir/sidelit start|" "$repo/data/sidelit-autostart.desktop" > "$autostartdir/sidelit.desktop"
# the daemon and the sidebar run as systemd user services; the autostart entry starts them
# (after recording the session environment), so they are not enabled on their own
for unit in "$repo"/data/systemd/*.service; do
    sed "s|@BINDIR@|$bindir|g" "$unit" > "$unitdir/$(basename "$unit")"
done
systemctl --user daemon-reload 2>/dev/null || echo "! systemctl --user is not available; the launcher falls back to background processes"

# GNOME: the extension that gives the daemon GNOME's workspaces and windows and runs the
# sidebar, linked from this checkout (a git pull updates it too) and turned on
gnome_uuid=sidelit@kulaxa.github.io
gnome_ext="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$gnome_uuid"
gnome_new=0
if [[ $gnome == 1 ]]; then
    mkdir -p "$(dirname "$gnome_ext")"
    [[ -e $gnome_ext || -L $gnome_ext ]] || gnome_new=1
    ln -sfn "$repo/gnome-extension" "$gnome_ext"
    if gnome-extensions enable "$gnome_uuid" 2>/dev/null; then
        echo "  GNOME Shell extension: on"
    else
        # GNOME finds a new extension at the next login: have it turned on by then
        enabled=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")
        if [[ $enabled != *"'$gnome_uuid'"* ]]; then
            if [[ $enabled == "@as []" || $enabled == "[]" ]]; then
                enabled="['$gnome_uuid']"
            else
                enabled="${enabled%]}, '$gnome_uuid']"
            fi
            gsettings set org.gnome.shell enabled-extensions "$enabled" 2>/dev/null || true
        fi
        gnome_new=1
        echo "  GNOME Shell extension: installed and turned on; GNOME loads it at your next login"
    fi
fi

# every coding agent session reports its status and window through Sidelit's plugin, installed
# in each agent found with its own commands (`claude plugin`, `codex plugin`)
if command -v claude &>/dev/null || command -v codex &>/dev/null; then
    "$bindir/sidelit" hooks install | sed 's/^/  /' || echo "  ! Sidelit's plugin is not installed in every agent (see above); the rest is"
else
    echo "  once Claude Code or Codex is installed: sidelit hooks install (Sidelit's plugin, in each)"
fi
# Chrome windows and their tabs, through the companion extension
"$bindir/sidelit" chrome install 2>/dev/null | sed 's/^/  /' || true
echo "✓ installed: $bindir/sidelit -> $repo"
if [[ $gnome_new == 1 ]]; then
    echo "  on GNOME: log out and back in once, so GNOME loads Sidelit's extension; then it starts by itself"
else
    echo "  starts at login; start now with: sidelit start"
fi
echo "  logs: sidelit log   ·   why something happened: sidelit why TEXT"
echo "  update later with: git -C $repo pull"
echo "  links on the current workspace: set Terminator's custom_url_handler and \$BROWSER to $bindir/sidelit-open (see README)"
case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "! $bindir is not on your PATH; add it to your shell rc" ;;
esac
