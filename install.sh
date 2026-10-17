#!/usr/bin/env bash
# Install Sidelit for the current user from this checkout. No sudo, no system files.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
bindir="$HOME/.local/bin"
desktopdir="$HOME/.local/share/applications"
autostartdir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

fail=0
# COSMIC or KDE Plasma 6, on Wayland: say so now rather than fail at the first start
desktop=${XDG_CURRENT_DESKTOP:-unknown}
[[ ":$desktop:" == *:COSMIC:* || ":$desktop:" == *:KDE:* ]] ||
    echo "! Sidelit runs on COSMIC and KDE Plasma 6, not on $desktop: it will not start here"
[[ ${XDG_SESSION_TYPE:-} == wayland ]] ||
    echo "! the sidebar is a Wayland layer-shell surface, and this session is ${XDG_SESSION_TYPE:-not Wayland}"
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" &>/dev/null; then
    echo "✗ python3 GTK bindings missing. Install with 'sudo apt install python3-gi gir1.2-gtk-3.0'."
    fail=1
fi
if ! python3 -c "import gi; gi.require_version('GtkLayerShell','0.1'); from gi.repository import GtkLayerShell" &>/dev/null; then
    echo "✗ gtk-layer-shell bindings missing. Install with 'sudo apt install gir1.2-gtklayershell-0.1'."
    fail=1
fi
if ! PYTHONPATH="$repo" python3 -c "from sidelit import sidebar4; raise SystemExit(sidebar4.layer_shell_paths() is None)" &>/dev/null; then
    echo "! gtk4-layer-shell not found: the GTK 3 sidebar runs (see \"The GTK 4 sidebar\" in the README for the smooth one)"
fi
if ! command -v claude &>/dev/null; then
    echo "! claude CLI not found on PATH. The sidebar runs, but the session features need Claude Code."
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

# every Claude Code session reports its status and window through a hook
if command -v claude &>/dev/null || [ -d "$HOME/.claude" ]; then
    "$bindir/sidelit" hooks install | sed 's/^/  /' || echo "  ! the Claude Code hook is not installed (see above); the rest is"
fi
# Chrome windows and their tabs, through the companion extension
"$bindir/sidelit" chrome install 2>/dev/null | sed 's/^/  /' || true
echo "✓ installed: $bindir/sidelit -> $repo"
echo "  starts at login; start now with: sidelit start"
echo "  logs: sidelit log   ·   why something happened: sidelit why TEXT"
echo "  update later with: git -C $repo pull"
echo "  links on the current workspace: set Terminator's custom_url_handler and \$BROWSER to $bindir/sidelit-open (see README)"
case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "! $bindir is not on your PATH; add it to your shell rc" ;;
esac
