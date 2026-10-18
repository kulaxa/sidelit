#!/usr/bin/env bash
# Remove what install.sh created. ~/.config/sidelit (layout, config) is left alone.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
"$HOME/.local/bin/sidelit" stop 2>/dev/null || true
"$HOME/.local/bin/sidelit" hooks uninstall 2>/dev/null || true  # (the Claude Code plugin)
"$HOME/.local/bin/sidelit" chrome uninstall 2>/dev/null || true
for name in sidelit sidelit-open sidelit-hook sidelit-chrome-host; do
    rm -f "$HOME/.local/bin/$name"
done
# the old name's hook and opener links, which a move-over pointed at ours
for name in hook open; do
    old="$HOME/.local/bin/claude-workspace-$name"
    if [ -L "$old" ]; then
        case "$(readlink "$old")" in
            */sidelit-$name) rm -f "$old" ;;
        esac
    fi
done
rm -f "$HOME/.local/share/applications/sidelit.desktop"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sidelit.desktop"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}"/systemd/user/sidelit-{daemon,sidebar}.service
systemctl --user daemon-reload 2>/dev/null || true
echo "✓ removed launcher, desktop entry, autostart and the systemd user units"
echo "  the clone itself, $repo, is untouched; delete it if you no longer need it"
