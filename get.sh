#!/usr/bin/env bash
# Sidelit's one-line install, which is also its update (run it again for the latest):
#
#   curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
#   wget -qO- https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
#
# Run in a terminal on your desktop, as yourself. It checks the desktop, installs the few system
# packages Sidelit needs with the distribution's own package manager (sudo asks for your password
# once, and only when one is missing), clones Sidelit into ~/.local/share/sidelit (or pulls it
# there, so the sidebar's update button keeps working), runs its install.sh and starts it. What
# the commands said goes to ~/.cache/sidelit-install.log; uninstall.sh takes it all out again.
#
# SIDELIT_REPO clones from another address instead, e.g. git@github.com:kulaxa/sidelit.git.
# A copy whose repository began its history anew is moved onto the new one, unless it has
# changes of its own (update_checkout).
#
# Everything happens in main(), called on the last line: a download cut short runs nothing.

[ -n "${BASH_VERSION:-}" ] || {
    echo "Sidelit's installer runs in bash: curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash" >&2
    exit 1
}

REPO_URL=https://github.com/kulaxa/sidelit.git

if [ -t 1 ]; then bold=$'\e[1m' plain=$'\e[0m'; else bold='' plain=''; fi
say() { printf '%s→ %s%s\n' "$bold" "$*" "$plain"; }
fail() {
    printf '%s✗ %s%s\n' "$bold" "$*" "$plain" >&2
    exit 1
}
tilde() { printf '%s' "${1/#"$HOME"/\~}"; }
# Does a compositor's config (the folder $1, links followed) start Sidelit: a line with
# `sidelit start` (or niri's `"sidelit" "start"`, a path before it too), not commented out
# (#, // and /- in niri's KDL, -- in Hyprland's Lua)?
starts_sidelit() { grep -RhsE -- 'sidelit"?[[:space:]]+"?start' "$1" | grep -qvE '^[[:space:]]*(#|//|/-|--)'; }
# ... with a path before `sidelit` (`~/.local/bin/sidelit start`): a bare `sidelit` is found only
# on the compositor's own PATH
starts_sidelit_by_path() { grep -RhsE -- '/sidelit"?[[:space:]]+"?start' "$1" | grep -qvE '^[[:space:]]*(#|//|/-|--)'; }

# ---- the desktop ------------------------------------------------------------------------------
# An X11 session: as the session says, else an X display and no Wayland one
x11_session() {
    case ${XDG_SESSION_TYPE:-} in
        x11) return 0 ;;
        wayland) return 1 ;;
    esac
    [ -n "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]
}

# On X11: Cinnamon (Linux Mint), Xfce, MATE or another window manager that keeps the EWMH hints
# (sidelit/x11.py). KDE and GNOME have their own way, tried on Wayland only.
check_x11_desktop() {
    local current=${XDG_CURRENT_DESKTOP:-}
    session_name=X11
    case ":${current^^}:" in
        *:KDE:* | *:GNOME:*)
            case ":${current^^}:" in
                *:BUDGIE:* | *:GNOME-FLASHBACK:* | *:UNITY:* | *:PANTHEON:*) ;;
                *) fail "On KDE Plasma and GNOME, Sidelit needs their Wayland session, and this one is X11: pick the Wayland session at the login screen." ;;
            esac
            ;;
    esac
    desktop=x11
    case ":${current^^}:" in
        *:X-CINNAMON:*) desktop_name=Cinnamon ;;
        *:XFCE:*) desktop_name=Xfce ;;
        *:MATE:*) desktop_name=MATE ;;
        ::) desktop_name="your window manager" ;;
        *) desktop_name=${current%%:*} ;;
    esac
}

# Sets `desktop` (cosmic, kde, gnome, hyprland, niri, sway, x11), `desktop_name` and
# `session_name`, or stops with what is wrong.
check_desktop() {
    local current=${XDG_CURRENT_DESKTOP:-}
    if x11_session; then
        check_x11_desktop
        return
    fi
    session_name=Wayland
    # a compositor started by hand may not name itself: its socket variable does
    [ -n "$current" ] || [ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || current=Hyprland
    [ -n "$current" ] || [ -z "${NIRI_SOCKET:-}" ] || current=niri
    [ -n "$current" ] || [ -z "${SWAYSOCK:-}" ] || current=sway
    case ":${current^^}:" in
        # Cinnamon's Wayland session has no way yet for an app to see or move other windows
        *:X-CINNAMON:*)
            fail "Sidelit runs on Cinnamon's X11 session, not on its Wayland one: pick \"Cinnamon\" (not \"Cinnamon on Wayland\") at the login screen." ;;
        *:COSMIC:*) desktop=cosmic desktop_name=COSMIC ;;
        *:KDE:*)
            [ "${KDE_SESSION_VERSION:-6}" -ge 6 ] 2>/dev/null ||
                fail "Sidelit needs KDE Plasma 6, and this is Plasma ${KDE_SESSION_VERSION}."
            desktop=kde desktop_name="KDE Plasma"
            ;;
        # (these name GNOME too, but run a shell of their own: sidelit/backend.py, NOT_GNOME_SHELL)
        *:BUDGIE:* | *:GNOME-FLASHBACK:* | *:UNITY:* | *:PANTHEON:*)
            fail "Sidelit runs on COSMIC, KDE Plasma 6, GNOME, Hyprland, niri and sway, not on $current." ;;
        # on GNOME, GTK 4 is a required package, and a first install ends with logging out and in
        *:GNOME:*) desktop=gnome desktop_name=GNOME ;;
        *:HYPRLAND:*) desktop=hyprland desktop_name=Hyprland ;;
        *:NIRI:*) desktop=niri desktop_name=niri ;;
        *:SWAY:*) desktop=sway desktop_name=sway ;;
        ::) fail "No desktop session found: run this in a terminal on your desktop (COSMIC, KDE Plasma 6, GNOME, Hyprland, niri or sway, or Cinnamon, Xfce or MATE on X11)." ;;
        *) fail "Sidelit runs on COSMIC, KDE Plasma 6, GNOME, Hyprland, niri and sway, and on X11 desktops (Cinnamon, Xfce, MATE), not on $current." ;;
    esac
    [ "${XDG_SESSION_TYPE:-}" = wayland ] || [ -n "${WAYLAND_DISPLAY:-}" ] ||
        fail "Sidelit needs a Wayland session, and this one is ${XDG_SESSION_TYPE:-not Wayland}: pick the Wayland session at the login screen."
}

# ---- the distribution and its packages ----------------------------------------------------------
# Sets `pm` (apt, dnf, pacman, zypper) and `distro_name`, or stops.
# shellcheck disable=SC1091  # (/etc/os-release: the system's own)
check_distro() {
    local ids tool
    ids=" $(. /etc/os-release 2>/dev/null && echo "${ID:-} ${ID_LIKE:-}") "
    distro_name=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}")
    pm=''
    case $ids in
        *" debian "* | *" ubuntu "*) pm=apt ;;
        *" fedora "*) pm=dnf ;;
        *" arch "* | *" archlinux "*) pm=pacman ;;
        *" suse "* | *" opensuse "*) pm=zypper ;;
        *)
            for tool in apt-get dnf pacman zypper; do
                command -v "$tool" >/dev/null && { pm=${tool%-get}; break; }
            done
            [ -n "$pm" ] ||
                fail "This installer knows apt, dnf, pacman and zypper, and found none of them here: see \"Other ways to install\" at https://github.com/kulaxa/sidelit/blob/main/docs/install.md"
            ;;
    esac
}

# What Sidelit needs, by package manager: Python 3 with PyGObject, GTK 3's introspection data,
# gtk-layer-shell's, and git (for the clone and the update button). (Fedora's GTK needs cairo's
# and xlib's introspection data, which only gobject-introspection has, and nothing requires.)
needed_packages() {
    case $pm in
        apt) echo python3 python3-gi gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1 git ;;
        dnf) echo python3 python3-gobject gobject-introspection gtk3 gtk-layer-shell git ;;
        pacman) echo python python-gobject gtk3 gtk-layer-shell git ;;
        zypper) echo python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-GtkLayerShell-0_1 git ;;
    esac
}

# python-xlib: how Sidelit talks to the X server, on X11
xlib_package() {
    case $pm in
        apt | dnf) echo python3-xlib ;;
        pacman) echo python-xlib ;;
        zypper) echo python3-python-xlib ;;
    esac
}

# GTK 4 and gtk4-layer-shell: the smoother GTK 4 sidebar, where the distribution has them
gtk4_package() {
    case $pm in
        apt) echo gir1.2-gtk-4.0 ;;
        dnf | pacman) echo gtk4 ;;
        zypper) echo typelib-1_0-Gtk-4_0 ;;
    esac
}
gtk4_layer_shell_package() {
    case $pm in
        # (Debian's introspection data does not bring the library along)
        apt) echo gir1.2-gtk4layershell-1.0 libgtk4-layer-shell0 ;;
        dnf | pacman) echo gtk4-layer-shell ;;
        zypper) echo typelib-1_0-Gtk4LayerShell-1_0 ;;
    esac
}

installed() {
    case $pm in
        apt) dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed' ;;
        dnf | zypper) rpm -q --whatprovides "$1" >/dev/null 2>&1 ;;
        pacman) pacman -T "$1" >/dev/null 2>&1 ;;
    esac
}

# The command that installs packages as root without asking anything (stdin is this script),
# and the one to give a person
install_command() {
    case $pm in
        apt) echo 'DEBIAN_FRONTEND=noninteractive apt-get install -y -q' ;;
        dnf) echo 'dnf install -y -q' ;;
        pacman) echo 'pacman -S --needed --noconfirm' ;;
        zypper) echo 'zypper --non-interactive install' ;;
    esac
}
manual_command() {
    case $pm in
        pacman) echo 'pacman -S' ;;
        *) echo "$pm install" ;;
    esac
}

# Installs what is missing in one sudo call, so the password is asked once. The GTK 4 extras come
# along when something else is installed anyway, and only where the distribution has them: a
# second run with everything in place never asks for the password.
install_packages() {
    local pkg needed missing=() extras=()
    needed=$(needed_packages)
    # on GNOME the sidebar is GTK 4, without layer shell; on X11 it is GTK 3, docked through Xlib
    [ "$desktop" != gnome ] || needed="$needed $(gtk4_package)"
    [ "$desktop" != x11 ] || needed="$needed $(xlib_package)"
    for pkg in $needed; do
        installed "$pkg" || missing+=("$pkg")
    done
    # the terminal Sidelit opens by default, when none it knows is installed (KDE has Konsole,
    # Hyprland, niri and sway users kitty, foot or Alacritty, Mint, Xfce and MATE their own;
    # sidelit/config.py, TERMINALS)
    local term found_term=''
    for term in terminator konsole kgx xterm foot kitty alacritty gnome-terminal xfce4-terminal mate-terminal; do
        command -v "$term" >/dev/null && { found_term=1; break; }
    done
    [ -n "$found_term" ] || missing+=(terminator)
    if [ ${#missing[@]} -eq 0 ]; then
        say "The system packages Sidelit needs are installed"
        return
    fi
    for pkg in $(gtk4_package) $(gtk4_layer_shell_package); do
        installed "$pkg" || [[ " ${missing[*]} " == *" $pkg "* ]] || extras+=("$pkg")
    done

    [ ! -e /run/ostree-booted ] ||
        fail "This is an image-based system: add ${missing[*]} with 'sudo rpm-ostree install ${missing[*]}', restart, and run this line again."
    command -v sudo >/dev/null ||
        fail "sudo is not installed. As root, run '$(manual_command) ${missing[*]}', then this line again."
    say "Installing ${missing[*]} with $pm (sudo may ask for your password)"
    local cmd script
    cmd=$(install_command)
    script="$cmd ${missing[*]}"
    [ "$pm" != apt ] || script="apt-get update -q && $script"
    [ ${#extras[@]} -eq 0 ] || script="$script && { $cmd ${extras[*]} >/dev/null 2>&1 || true; }"
    # shellcheck disable=SC2024  # (the log is yours, written as you)
    if ! sudo sh -c "$script" </dev/null >>"$log" 2>&1; then
        tail -n 12 "$log" | sed 's/^/  /' >&2
        local hint=''
        [ "$pm" != pacman ] || hint=" (on Arch, 'sudo pacman -Syu' first brings the package lists up to date)"
        fail "Installing the packages failed$hint. Install them yourself with 'sudo $(manual_command) ${missing[*]}', then run this line again."
    fi
}

# ---- Sidelit itself ---------------------------------------------------------------------------
# Brings the checkout $1 up to date with the branch it follows (origin's main): a fast-forward,
# as `git pull --ff-only`. When that branch shares no history with the checkout (the repository
# at Sidelit's address began its history anew, as when Sidelit went public), a checkout with
# nothing of its own (no changed or new files, no commits) is moved onto it; one with something
# of its own is left as it is, saying so. Your layout and settings are in ~/.config/sidelit,
# outside the checkout: never touched.
update_checkout() {
    local dir=$1 shown branch remote merge upstream theirs anew='' changes pulled=() n i rev
    shown=$(tilde "$dir")
    if ! branch=$(git -C "$dir" symbolic-ref --quiet --short HEAD) ||
        ! remote=$(git -C "$dir" config "branch.$branch.remote") ||
        ! merge=$(git -C "$dir" config "branch.$branch.merge") ||
        ! upstream=$(git -C "$dir" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null); then
        fail "Could not update $shown: it is on no branch that follows Sidelit's ('git -C $shown status' says where it is)."
    fi
    # what the branch it follows has now, without moving it there yet: a try that stops below
    # leaves the checkout as it was, so the next one still sees what the checkout had pulled
    git -C "$dir" fetch --quiet --refmap= "$remote" "$merge" </dev/null >>"$log" 2>&1 ||
        fail "Could not update $shown: no network, or no access to $(git -C "$dir" remote get-url "$remote" 2>/dev/null)."
    theirs=$(git -C "$dir" rev-parse FETCH_HEAD)
    if ! git -C "$dir" merge-base HEAD "$theirs" >/dev/null 2>&1; then
        anew=1
        # its own: changed or new files, and commits that never were on the branch it follows,
        # as it is now or was before (an older Sidelit's update button, a `git pull` or `git
        # fetch`, may have moved it onto the new history already: its reflog keeps every value
        # it had, `@{N}` for the oldest entry's old one)
        changes=$(git -C "$dir" status --porcelain 2>/dev/null) || changes='?'
        pulled=("$(git -C "$dir" rev-parse "$upstream")")
        n=$(git -C "$dir" reflog show --format=%H "$upstream" -- 2>/dev/null | wc -l)
        for ((i = 1; i <= n; i++)); do
            rev=$(git -C "$dir" rev-parse --verify --quiet "$upstream@{$i}") && pulled+=("$rev")
        done
        [ "$(git -C "$dir" rev-list --count HEAD --not "${pulled[@]}" 2>/dev/null)" = 0 ] || changes="$changes commits"
        [ -z "$changes" ] ||
            fail "Sidelit's repository began its history anew, and $shown has changes of its own, so it is left as it is ('git -C $shown status' shows them). Keep what you need from it, move it away, then run Sidelit's one-line install again for a fresh copy (your layout and settings in ~/.config/sidelit stay as they are)."
    fi
    git -C "$dir" fetch --prune --quiet "$remote" </dev/null >>"$log" 2>&1 ||
        fail "Could not update $shown: no network, or no access to $(git -C "$dir" remote get-url "$remote" 2>/dev/null)."
    if [ -z "$anew" ]; then
        git -C "$dir" merge --ff-only --quiet "$theirs" </dev/null >>"$log" 2>&1 ||
            fail "Could not update $shown: it has changes of its own ('git -C $shown status' shows them)."
        return
    fi
    say "Sidelit's repository began its history anew: moving $shown onto it"
    git -C "$dir" reset --hard --quiet "$theirs" </dev/null >>"$log" 2>&1 ||
        fail "Could not move $shown onto Sidelit's new history ('git -C $shown status' says why)."
}

# Clones Sidelit into `dir`, or updates it there; sets `before` to the version it had (empty: new).
fetch_sidelit() {
    local repo=${SIDELIT_REPO:-$REPO_URL}
    before=''
    # (a private or mistyped address fails here, rather than asking for a GitHub password)
    export GIT_TERMINAL_PROMPT=0
    if [ -e "$dir/.git" ] && [ -f "$dir/bin/sidelit" ]; then
        before=$(cat "$dir/VERSION" 2>/dev/null || echo '?')
        say "Updating Sidelit in $(tilde "$dir")"
        update_checkout "$dir"
    elif [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
        fail "$(tilde "$dir") is there already and is not a Sidelit checkout: move it away and run this line again."
    else
        say "Downloading Sidelit into $(tilde "$dir")"
        mkdir -p "$(dirname "$dir")"
        git clone --quiet "$repo" "$dir" </dev/null >>"$log" 2>&1 ||
            fail "Could not download Sidelit from $repo: no network, or no access to it."
    fi
}

packaged() {
    case $pm in
        apt) dpkg-query -W -f='${Status}' sidelit 2>/dev/null | grep -q 'ok installed' ;;
        pacman) pacman -Q sidelit >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

# A Chromium-family browser has a profile here, and none of them has loaded the extension yet
chrome_step_left() {
    local config=${XDG_CONFIG_HOME:-$HOME/.config} base found='' extension
    # (its id is fixed by the key in chrome-extension/manifest.json; chrome.py has it)
    extension=$(PYTHONPATH=$dir python3 -c 'from sidelit.chrome import EXTENSION_ID; print(EXTENSION_ID)' 2>/dev/null) ||
        return 1
    for base in google-chrome google-chrome-beta chromium BraveSoftware/Brave-Browser microsoft-edge; do
        [ -d "$config/$base" ] || continue
        found=1
        grep -qs "$extension" "$config/$base"/*/Preferences "$config/$base"/*/"Secure Preferences" && return 1
    done
    [ -n "$found" ]
}

main() {
    set -uo pipefail
    [ "$(uname -s)" = Linux ] || fail "Sidelit runs on Linux only."
    [ "$(id -u)" -ne 0 ] ||
        fail "Run this as yourself, not as root or with sudo: it asks for your password when it needs it."

    local cache=${XDG_CACHE_HOME:-$HOME/.cache}
    mkdir -p "$cache"
    log=$cache/sidelit-install.log
    # `bash get.sh pull DIR` only updates the checkout DIR (the sidebar's update button, which
    # restarts Sidelit itself)
    if [ "${1:-}" = pull ] && [ -n "${2:-}" ]; then
        printf '=== Sidelit update, %s ===\n' "$(date -Is)" >>"$log"
        export GIT_TERMINAL_PROMPT=0
        say "Updating Sidelit in $(tilde "$2")"
        update_checkout "$2"
        return
    fi
    printf '=== Sidelit install, %s ===\n' "$(date -Is)" >"$log"

    check_desktop
    check_distro
    say "${distro_name:-Linux}, $desktop_name on $session_name"
    ! packaged ||
        fail "Sidelit is installed here as a package already, and the sidebar's update button updates it. To switch to this way of installing, remove the package ('sudo $pm remove sidelit') and run this line again."

    install_packages
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null ||
        fail "Sidelit needs Python 3.10 or later, and this system's python3 is $(python3 -V 2>&1 | cut -d' ' -f2)."

    dir=${XDG_DATA_HOME:-$HOME/.local/share}/sidelit
    fetch_sidelit

    # install.sh links the command into ~/.local/bin and installs Sidelit's plugin in each coding
    # agent it finds (`claude`, `codex`, `opencode`), whose own installers put them in
    # ~/.local/bin or a folder of their own: all on PATH for this run, even where a login has not
    # put them there yet
    local bindir=$HOME/.local/bin on_path=1
    case ":$PATH:" in
        *":$bindir:"*) ;;
        *) on_path='' PATH=$bindir:$PATH ;;
    esac
    [ ! -x "$HOME/.claude/local/claude" ] || PATH=$PATH:$HOME/.claude/local
    [ ! -x "$HOME/.opencode/bin/opencode" ] || PATH=$PATH:$HOME/.opencode/bin

    say "Setting it up: the sidelit command, starting at login, the plugin for Claude Code, Codex and OpenCode, the Chrome bridge"
    local out status
    out=$(bash "$dir/install.sh" </dev/null 2>&1)
    status=$?
    printf '%s\n' "$out" >>"$log"
    # its warnings, the rest is in the log; but not that there is no GTK 4 sidebar (docs/install.md's
    # to read about, if you like) nor that there is no claude (the end of this says what to do)
    grep -E '^ *(!|✗|sidelit hooks:)' <<<"$out" | grep -v -e gtk4-layer-shell -e 'claude CLI not found' | sed 's/^ */  /'
    [ $status -eq 0 ] || fail "Setting Sidelit up failed (above; all it said is in $(tilde "$log"))."

    # (`sidelit start` also takes over from a Claude Workspace install; a running Sidelit is
    # restarted, so that an update runs)
    local action=start
    if systemctl --user is-active --quiet sidelit-daemon.service 2>/dev/null; then
        action=restart
        say "Restarting Sidelit"
    else
        say "Starting Sidelit"
    fi
    "$bindir/sidelit" "$action" </dev/null 2>&1 | tee -a "$log" | sed 's/^/  /'
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "Sidelit is installed, but did not start: 'sidelit log' says why."

    local version login=1 bare='' hypr=${XDG_CONFIG_HOME:-$HOME/.config}/hypr niri_conf sway_conf='' conf='' line
    niri_conf=${NIRI_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/niri/config.kdl}
    # (sway reads ~/.sway/config, ~/.config/sway/config, i3's, then /etc/sway/config: it says which)
    [ "$desktop" != sway ] || sway_conf=$(swaymsg -t get_version 2>/dev/null |
        python3 -c 'import json, sys; print(json.load(sys.stdin).get("loaded_config_file_name") or "")' 2>/dev/null) || true
    sway_conf=${sway_conf:-${XDG_CONFIG_HOME:-$HOME/.config}/sway/config}
    version=$(cat "$dir/VERSION")
    # Hyprland, niri and sway themselves run no autostart entries; a session that runs them (uwsm,
    # niri's own session, start systemd's xdg-desktop-autostart.target) starts Sidelit, else it
    # takes a line in the compositor's config, unless that is there already. The line names
    # `sidelit` by its path: a compositor a login manager started may have no ~/.local/bin on its
    # PATH (greetd's, with Arch's /etc/profile), and this terminal, which got its PATH from the
    # compositor, says so when it has none either. (Each runs the line's ~ as the home folder:
    # Hyprland and sway through sh, niri by itself.)
    case $desktop in
        hyprland)
            if [ -f "$hypr/hyprland.lua" ]; then
                conf=$hypr/hyprland.lua line="hl.on(\"hyprland.start\", function () hl.exec_cmd(\"$(tilde "$bindir")/sidelit start\") end)"
            else
                conf=$hypr/hyprland.conf line="exec-once = $(tilde "$bindir")/sidelit start"
            fi
            ;;
        niri) conf=$niri_conf line="spawn-at-startup \"$(tilde "$bindir")/sidelit\" \"start\"" ;;
        sway) conf=$sway_conf line="exec $(tilde "$bindir")/sidelit start" ;;
    esac
    if [ -n "$conf" ] && ! systemctl --user is-active --quiet xdg-desktop-autostart.target 2>/dev/null; then
        if ! starts_sidelit "$(dirname "$conf")"; then
            login=''
        elif [ -z "$on_path" ] && ! starts_sidelit_by_path "$(dirname "$conf")"; then
            login='' bare=1
        fi
    fi
    if [ -z "$before" ] && [ -z "$login" ]; then
        echo "${bold}✓ Sidelit $version is installed and started.${plain} Run this line again to update it."
    elif [ -z "$before" ]; then
        echo "${bold}✓ Sidelit $version is installed and started.${plain} It starts by itself at every login; run this line again to update it."
    elif [ "$before" = "$version" ]; then
        echo "${bold}✓ Sidelit $version is up to date, and ${action}ed.${plain}"
    else
        echo "${bold}✓ Sidelit is updated from $before to $version, and ${action}ed.${plain}"
    fi
    [ "$desktop" != gnome ] || [ -n "$before" ] ||
        echo "  Next: log out and back in once, so that GNOME loads Sidelit's extension."
    if [ -n "$bare" ]; then
        echo "  Next: $desktop_name does not find 'sidelit' at login (~/.local/bin is not on its PATH), so the"
        echo "  line in $(tilde "$conf") that starts Sidelit does nothing then. Change it to:"
        echo "    $line"
    elif [ -z "$login" ] && [ "$desktop" = sway ] && [ "${conf#/etc/}" != "$conf" ]; then
        echo "  Next: to start Sidelit at every login, copy $conf to ~/.config/sway/config (sway reads"
        echo "  that one instead from then on) and add this line to it:"
        echo "    $line"
    elif [ -z "$login" ]; then
        echo "  Next: to start Sidelit at every login, add this line to $(tilde "$conf"):"
        echo "    $line"
    fi
    command -v claude >/dev/null || command -v codex >/dev/null || command -v opencode >/dev/null ||
        echo "  Next: install Claude Code (curl -fsSL https://claude.ai/install.sh | bash), Codex or OpenCode, then run this line again to connect Sidelit to it."
    # Codex runs a plugin's hooks only once you have trusted them (it asks when it next starts)
    ! command -v codex >/dev/null ||
        PYTHONPATH=$dir python3 -c 'from sidelit import codex; raise SystemExit(codex.Codex().hooks_trusted() is False)' 2>/dev/null </dev/null ||
        echo "  Next: the next time Codex starts, it asks you to review Sidelit's hooks: trust them (or later, with /hooks in Codex)."
    ! chrome_step_left ||
        echo "  Next, in Chrome (each profile): chrome://extensions → Developer mode → Load unpacked → $(tilde "$dir")/chrome-extension"
    [ -n "$on_path" ] ||
        echo "  To type 'sidelit' in a terminal, add ~/.local/bin to your PATH (this terminal does not have it)."
}

main "$@"
