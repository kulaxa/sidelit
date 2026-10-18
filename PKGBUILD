# Maintainer: Mario Jalšovec <78656906+kulaxa@users.noreply.github.com>
#
# Builds from this checkout, not from a release tarball: run makepkg here, or
# './package.sh arch' to keep the artifacts out of the tree.
#
# pkgver is checked against VERSION in CI — bump both together.

pkgname=sidelit
pkgver=0.5.0
pkgrel=1
pkgdesc='A workspace sidebar for COSMIC, KDE Plasma, GNOME and Hyprland: one tab per workspace with its coding agent, and a daemon that keeps the layout'
arch=('any')
url='https://github.com/kulaxa/sidelit'
license=('MIT')
depends=('python' 'python-gobject' 'gtk3' 'gtk-layer-shell' 'systemd')
optdepends=('claude-code: session titles, status, model line and usage'
            'terminator: the default terminal the sidebar opens'
            'gtk4-layer-shell: the GTK 4 sidebar, drawn on the GPU'
            'gtk4: the GTK 4 sidebar on GNOME'
            'cosmic-comp: runs on the COSMIC desktop'
            'kwin: runs on KDE Plasma 6'
            'gnome-shell: runs on GNOME, with the GNOME Shell extension in the package'
            'hyprland: runs on Hyprland')
# the project's earlier name
conflicts=('claude-workspace')
replaces=('claude-workspace')
source=()
# after a removal: how to take the Claude Code plugin out of Claude Code
install=sidelit.install

package() {
    # package.sh owns the installed layout, shared with the debian package
    "$startdir/package.sh" stage "$pkgdir"
    install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
