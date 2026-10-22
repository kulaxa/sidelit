# Maintainer: Mario Jalšovec <78656906+kulaxa@users.noreply.github.com>
#
# Builds from this checkout, not from a release tarball: run makepkg here, or
# './package.sh arch' to keep the artifacts out of the tree.
#
# pkgver is checked against VERSION in CI — bump both together.

pkgname=sidelit
pkgver=0.12.2
pkgrel=1
pkgdesc='A workspace sidebar for COSMIC, KDE Plasma, GNOME, Hyprland, niri, sway and X11 desktops: one tab per workspace with its coding agent, and a daemon that keeps the layout'
arch=('any')
url='https://github.com/kulaxa/sidelit'
license=('MIT')
depends=('python' 'python-gobject' 'gtk3' 'gtk-layer-shell' 'python-xlib' 'systemd')
optdepends=('claude-code: session titles, status, model line and usage'
            'openai-codex: the same for Codex sessions'
            'opencode: the same for OpenCode sessions'
            'terminator: the default terminal the sidebar opens'
            'gtk4-layer-shell: the GTK 4 sidebar, drawn on the GPU'
            'gtk4: the GTK 4 sidebar on GNOME'
            'cosmic-comp: runs on the COSMIC desktop'
            'kwin: runs on KDE Plasma 6'
            'gnome-shell: runs on GNOME, with the GNOME Shell extension in the package'
            'hyprland: runs on Hyprland'
            'niri: runs on niri'
            'sway: runs on sway'
            'cinnamon: runs on Cinnamon, on X11'
            'xfwm4: runs on Xfce, on X11 (as on other window managers that keep the EWMH hints)'
            'marco: runs on MATE, on X11')
# the project's earlier name
conflicts=('claude-workspace')
replaces=('claude-workspace')
source=()
# after a removal: how to take the plugin out of Claude Code, Codex and OpenCode
install=sidelit.install

package() {
    # package.sh owns the installed layout, shared with the debian package
    "$startdir/package.sh" stage "$pkgdir"
    install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
