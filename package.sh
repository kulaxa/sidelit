#!/usr/bin/env bash
# Build distro packages from this checkout.
#
#   ./package.sh deb              dist/sidelit_<version>_all.deb
#   ./package.sh arch             dist/sidelit-<version>-1-any.pkg.tar.zst
#   ./package.sh all              both
#   ./package.sh gnome-extension  dist/sidelit@kulaxa.github.io.shell-extension.zip, the GNOME
#                                 Shell extension as extensions.gnome.org takes an upload
#   ./package.sh stage DEST       install the tree into DEST and stop
#
# 'stage' is the one place that knows the installed layout: debian/rules and
# PKGBUILD both call it, so a package can never disagree with what this script
# builds by hand. Pass --no-docs when the packaging tool installs docs itself.
#
# The .deb is built directly (dpkg-deb, or ar + tar where dpkg is not
# installed) so it works on any distro. debian/ is still a normal source
# package: run './package.sh debian-files && dpkg-buildpackage -us -uc' on a
# Debian machine if you want the official route.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
pkgname=sidelit
version=$(cat "$repo/VERSION")
pkgrel=1          # arch only; the deb is a native package and has no revision
dist="$repo/dist"

# --- the installed layout ---------------------------------------------------
# /usr/bin/sidelit                      launcher, finds the package via ../share
# /usr/bin/sidelit-open                 `sidelit open URL` for url-handler settings
# /usr/bin/sidelit-hook                 the plugin's hook by its old name (what Sidelit 0.5 put in settings.json)
# /usr/bin/sidelit-chrome-host          native host for the Chrome extension (`sidelit chrome install`)
# /usr/share/sidelit/.claude-plugin/    the marketplace that lists Sidelit's Claude Code plugin
# /usr/share/sidelit/plugin/            the plugin (`sidelit hooks install` installs it from here)
# /usr/share/sidelit/chrome-extension/  the Chrome extension, to load unpacked
# /usr/share/sidelit/sidelit/           the python package + protocol XML
#                                       (not on sys.path; the launcher sets PYTHONPATH)
# /usr/lib/systemd/user/                the daemon and sidebar units (started by the
#                                       launcher, not enabled: see bin/sidelit)
# /usr/share/gnome-shell/extensions/sidelit@kulaxa.github.io/
#                                       the GNOME Shell extension (each user turns it on)
# /usr/share/applications/              desktop entry
# /etc/xdg/autostart/                   runs `sidelit start` at login
# /usr/share/doc/sidelit/               README, CHANGELOG (and the .deb's copyright)

gnome_uuid=sidelit@kulaxa.github.io

stage() {
    local dest=$1 docs=${2-}
    local py="$dest/usr/share/$pkgname/sidelit" plugin="$dest/usr/share/$pkgname/plugin"
    local shellext="$dest/usr/share/gnome-shell/extensions/$gnome_uuid"

    install -d "$dest/usr/bin" "$py/protocols" "$py/core" "$dest/usr/share/$pkgname/chrome-extension" \
        "$dest/usr/share/$pkgname/.claude-plugin" "$plugin/.claude-plugin" "$plugin/hooks" \
        "$dest/usr/share/applications" "$dest/etc/xdg/autostart" "$dest/usr/lib/systemd/user" "$shellext"
    local name
    for name in sidelit sidelit-open sidelit-hook sidelit-chrome-host; do
        install -m 755 "$repo/bin/$name" "$dest/usr/bin/$name"
    done
    install -m 644 "$repo/.claude-plugin/marketplace.json" "$dest/usr/share/$pkgname/.claude-plugin/"
    install -m 644 "$repo/plugin/.claude-plugin/plugin.json" "$plugin/.claude-plugin/"
    install -m 644 "$repo/plugin/hooks/hooks.json" "$repo/plugin/hooks/usage.js" "$plugin/hooks/"
    install -m 755 "$repo/plugin/hooks/hook.py" "$plugin/hooks/"
    install -m 644 "$repo"/chrome-extension/* "$dest/usr/share/$pkgname/chrome-extension/"
    install -m 644 "$repo"/gnome-extension/*.js "$repo"/gnome-extension/metadata.json "$shellext/"
    install -m 644 "$repo"/sidelit/*.py "$py/"
    install -m 644 "$repo"/sidelit/core/*.py "$py/core/"
    install -m 644 "$repo"/sidelit/protocols/*.xml "$py/protocols/"
    install -m 644 "$repo/VERSION" "$py/VERSION"
    install -m 644 "$repo/data/sidelit.desktop" \
        "$dest/usr/share/applications/sidelit.desktop"
    install -m 644 "$repo/data/sidelit-autostart.desktop" \
        "$dest/etc/xdg/autostart/sidelit.desktop"
    local unit
    for unit in "$repo"/data/systemd/*.service; do
        sed "s|@BINDIR@|/usr/bin|g" "$unit" > "$dest/usr/lib/systemd/user/$(basename "$unit")"
        chmod 644 "$dest/usr/lib/systemd/user/$(basename "$unit")"
    done

    if [ "$docs" != --no-docs ]; then
        install -d "$dest/usr/share/doc/$pkgname"
        install -m 644 "$repo/README.md" "$repo/CHANGELOG.md" \
            "$dest/usr/share/doc/$pkgname/"
    fi
}

# --- debian ----------------------------------------------------------------

# debian/changelog is generated: VERSION and CHANGELOG.md are the only places a
# version or a date is written by hand.
debian_files() {
    local date maintainer
    date=$(sed -n 's/^## \[[0-9.]*\] - \([0-9-]*\)$/\1/p' "$repo/CHANGELOG.md" | head -1)
    date=$(date -u -R -d "${date:-@0}")
    maintainer=$(sed -n 's/^Maintainer: //p' "$repo/debian/control")

    cat > "$repo/debian/changelog" <<CHANGELOG
$pkgname ($version) unstable; urgency=medium

  * Release $version. See the upstream changelog for what changed.

 -- $maintainer  $date
CHANGELOG
    echo "wrote debian/changelog ($version)"
}

# The binary control file, derived from debian/control so the dependency list
# lives in exactly one file. debian/control deliberately uses no substvars.
binary_control() {
    local root=$1 size
    size=$(du -ks --exclude=DEBIAN "$root" | cut -f1)

    sed -n '/^Package:/{p;q}' "$repo/debian/control"
    printf 'Version: %s\n' "$version"
    printf 'Installed-Size: %s\n' "$size"
    sed -n '/^Maintainer:/p;/^Section:/p;/^Priority:/p' "$repo/debian/control"
    sed -n '/^Package:/,$p' "$repo/debian/control" | sed 1d
}

build_deb() {
    local build="$dist/deb" root out
    root="$build/root"
    out="$dist/${pkgname}_${version}_all.deb"

    rm -rf "$build"
    stage "$root"

    gzip -9nc "$repo/CHANGELOG.md" > "$root/usr/share/doc/$pkgname/changelog.gz"
    chmod 644 "$root/usr/share/doc/$pkgname/changelog.gz"
    install -m 644 "$repo/debian/copyright" "$root/usr/share/doc/$pkgname/copyright"

    install -d "$root/DEBIAN"
    binary_control "$root" > "$root/DEBIAN/control"
    # what to do about the Claude Code plugin after a removal (debhelper's line left out)
    sed '/#DEBHELPER#/d' "$repo/debian/sidelit.postrm" > "$root/DEBIAN/postrm"
    chmod 755 "$root/DEBIAN/postrm"
    # the autostart entry is configuration: dpkg keeps local edits across upgrades
    echo "/etc/xdg/autostart/sidelit.desktop" > "$root/DEBIAN/conffiles"
    (
        cd "$root"
        find . -path ./DEBIAN -prune -o -type f -printf '%P\0' \
            | LC_ALL=C sort -z | xargs -0 md5sum > DEBIAN/md5sums
    )

    mkdir -p "$dist"
    if command -v dpkg-deb >/dev/null 2>&1; then
        dpkg-deb --root-owner-group --build "$root" "$out" >/dev/null
    else
        local ar="$build/ar"
        install -d "$ar"
        echo 2.0 > "$ar/debian-binary"
        tar czf "$ar/control.tar.gz" -C "$root/DEBIAN" --owner=0 --group=0 --numeric-owner .
        tar czf "$ar/data.tar.gz" -C "$root" --owner=0 --group=0 --numeric-owner --exclude=./DEBIAN .
        rm -f "$out"
        (cd "$ar" && ar rcD "$out" debian-binary control.tar.gz data.tar.gz)
    fi

    echo "built $out"
}

# --- the GNOME Shell extension, for extensions.gnome.org ------------------------
# Its files as they are (no build step, no binaries), metadata.json at the top of the zip
# as `gnome-extensions pack` makes it; extensions.gnome.org sets the version itself.
build_gnome_extension() {
    local out="$dist/$gnome_uuid.shell-extension.zip"
    mkdir -p "$dist"
    rm -f "$out"
    python3 - "$repo/gnome-extension" "$out" <<'PY'
import sys, zipfile
from pathlib import Path
src, out = Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.iterdir()):
        if f.suffix in (".js", ".json"):
            z.write(f, f.name)
PY
    echo "built $out"
}

# --- arch ------------------------------------------------------------------

build_arch() {
    command -v makepkg >/dev/null 2>&1 || {
        echo "package.sh: makepkg not found (run this on Arch)" >&2
        return 1
    }
    mkdir -p "$dist"
    (
        cd "$repo"
        PKGDEST="$dist" BUILDDIR="$dist/arch" makepkg -f
    )
    echo "built $dist/${pkgname}-${version}-${pkgrel}-any.pkg.tar.zst"
}

case ${1-deb} in
    deb) debian_files; build_deb ;;
    arch) build_arch ;;
    all) debian_files; build_deb; build_arch ;;
    gnome-extension) build_gnome_extension ;;
    debian-files) debian_files ;;
    stage)
        [ $# -ge 2 ] || { echo "package.sh: stage needs a destination" >&2; exit 2; }
        stage "$2" "${3-}"
        ;;
    -h | --help | help)
        sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//;$d'
        ;;
    *)
        echo "package.sh: unknown command '$1' (try --help)" >&2
        exit 2
        ;;
esac
