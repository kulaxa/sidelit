# Installing Sidelit

- [The install line](#the-install-line)
- [Other ways to install](#other-ways-to-install): the .deb, the Arch package, a checkout
- The step left on [GNOME](#on-gnome), [Hyprland](#on-hyprland), [niri](#on-niri) and [sway](#on-sway)
- [The GTK 4 sidebar](#the-gtk-4-sidebar-optional), smoother where your distribution has it
- [Chrome windows and their tabs](#chrome-windows-and-their-tabs), the companion extension
- [Moving from Claude Workspace](#moving-from-claude-workspace), Sidelit's old name

## The install line

```bash
curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
```

or, with wget:

```bash
wget -qO- https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
```

Run it in a terminal on your desktop (COSMIC, KDE Plasma 6, GNOME, Hyprland, niri or sway on
Wayland; Cinnamon, Xfce or MATE on X11). It installs
the few packages Sidelit needs (asking for your password once), puts Sidelit in
`~/.local/share/sidelit` and starts it; from then on it starts at every login. Run the same line
again to update. It knows Ubuntu, Linux Mint, Pop!_OS, Debian, Fedora, Arch (and EndeavourOS,
CachyOS, Manjaro) and openSUSE; what it did is in `~/.cache/sidelit-install.log`, and
`~/.local/share/sidelit/uninstall.sh` takes it out again. On GNOME, see [On GNOME](#on-gnome)
for the one thing left after it, on Hyprland, [On Hyprland](#on-hyprland), on niri,
[On niri](#on-niri), and on sway, [On sway](#on-sway).

## Other ways to install

Sidelit needs COSMIC, KDE Plasma 6, GNOME 46 or later, Hyprland, niri or sway, on Wayland, or an X11
desktop whose window manager keeps the EWMH hints (Cinnamon, Xfce, MATE), Python 3.10 or later
with GTK 3 and gtk-layer-shell (the packages pull them in; on GNOME, GTK 4 instead of the layer
shell; on X11, python-xlib), and the `claude`, `codex` or `opencode` CLI for the session features. Nothing is
compiled: the Wayland client is pure Python, in the tree, on KDE it talks to KWin over D-Bus, on
GNOME to its own GNOME Shell extension, which is JavaScript, on Hyprland to Hyprland's own
sockets, on niri and sway to theirs, and on X11 to the window manager through python-xlib.

**Ubuntu, Linux Mint, Pop!_OS, Debian:** the .deb from the latest release:

```bash
curl -LO https://github.com/kulaxa/sidelit/releases/latest/download/sidelit_latest_all.deb
sudo apt install ./sidelit_latest_all.deb
```

**Arch:** the package from the latest release:

```bash
curl -LO https://github.com/kulaxa/sidelit/releases/latest/download/sidelit-latest-any.pkg.tar.zst
sudo pacman -U ./sidelit-latest-any.pkg.tar.zst
```

Then, once, as yourself: `sidelit hooks install` (Sidelit's plugin, in Claude Code, Codex and
OpenCode: see [the agents](agents.md)) and, if you use Chrome, `sidelit chrome install`. Start it with `sidelit start`; after that it starts at
every login.

**From a checkout** (no sudo; the update button pulls it):

```bash
git clone https://github.com/kulaxa/sidelit.git ~/.local/share/sidelit
~/.local/share/sidelit/install.sh
sidelit start
```

`install.sh` links `sidelit` into `~/.local/bin`, adds a desktop entry, an autostart entry and
two systemd user units (the daemon and the sidebar), and installs Sidelit's plugin in each
coding agent it finds (Claude Code, Codex, OpenCode) and the Chrome bridge.
`uninstall.sh` takes all of that away again and leaves your layout alone. The line above and a
package do not mix: to go from a package to the line, remove the package first.

Or build the packages yourself: `./package.sh deb` or `./package.sh arch` (into `dist/`).

## On GNOME

The packages and `install.sh` install Sidelit's GNOME Shell extension. Then:

1. Install Sidelit, any way above. On Ubuntu and Debian, GTK 4 for Python comes with the
   package (`gir1.2-gtk-4.0`); on Fedora: `sudo dnf install python3-gobject gtk4`.
2. **Log out and back in.** GNOME looks for new extensions only when you log in.
3. **Turn the extension on**: open the **Extensions** app and switch on **Sidelit**, or run
   `gnome-extensions enable sidelit@kulaxa.github.io`. (`install.sh` turns it on for you.)
4. Run `sidelit start` once (and `sidelit hooks install`, as above). The sidebar appears on the
   left within a few seconds; from then on it starts at every login.

If no sidebar appears, `sidelit status` says whether the extension is running. To take Sidelit
off the screen, `sidelit stop`, or switch the extension off.

## On Hyprland

Hyprland itself runs no autostart entries, so Sidelit starts by itself only when your session
runs them (uwsm does). Otherwise add one line to your Hyprland config, so it starts at every
login (the install line says so when it is needed):

- with a classic config (`~/.config/hypr/hyprland.conf`): `exec-once = ~/.local/bin/sidelit start`
- with a Lua config (`~/.config/hypr/hyprland.lua`, Hyprland 0.55 and later):
  `hl.on("hyprland.start", function () hl.exec_cmd("~/.local/bin/sidelit start") end)`

The line names `sidelit` by its path, where the install line and `install.sh` put it: Hyprland
started by a login manager often has no `~/.local/bin` on its PATH (greetd on Arch has none), and
a bare `sidelit start` then does nothing. Installed as a package, Sidelit is in `/usr/bin`, and
`sidelit start` is enough. The same goes for niri and sway below.

## On niri

niri's own session (`niri-session`, what a login screen starts when you pick niri) runs
autostart entries, so there Sidelit starts by itself at every login. Started any other way, niri
runs none: add this line to `~/.config/niri/config.kdl` (the install line says so when it is
needed):

```kdl
spawn-at-startup "~/.local/bin/sidelit" "start"
```

## On sway

sway itself runs no autostart entries, so Sidelit starts by itself only when your session runs
them (some distributions' sway sessions do). Otherwise add one line to your sway config,
`~/.config/sway/config` (the install line says so when it is needed; with no config of your own
yet, copy `/etc/sway/config` there first):

```
exec ~/.local/bin/sidelit start
```

## The GTK 4 sidebar (optional)

With [gtk4-layer-shell](https://github.com/wmww/gtk4-layer-shell) installed, the sidebar runs
on GTK 4: drawn on the GPU, so pointing at a tab swells its card and slides the cards below it
aside smoothly. Without it the GTK 3 sidebar runs, the same in every other way. When the
install line installs packages, it adds this one where the distribution has it: Arch
(`gtk4-layer-shell`), Debian 13 (`gir1.2-gtk4layershell-1.0 libgtk4-layer-shell0`), Fedora
(`gtk4-layer-shell`), openSUSE (`typelib-1_0-Gtk4LayerShell-1_0`); where nothing does (Ubuntu
24.04, Pop!_OS), build it into `~/.local`, where the sidebar looks for it:

```bash
sudo apt install meson ninja-build libgtk-4-dev wayland-protocols gobject-introspection libgirepository1.0-dev
git clone https://github.com/wmww/gtk4-layer-shell.git && cd gtk4-layer-shell && git checkout v1.3.0
meson setup build --prefix="$HOME/.local" -Dintrospection=true -Dvapi=false
ninja -C build install
sidelit restart
```

Config `sidebar` picks one by hand (`auto`, `gtk4`, `gtk3`); `sidelit log` says which one
started and why the GTK 4 one did not.

## Chrome windows and their tabs

With the companion extension, every Chrome window remembers its tabs and comes back on its
own workspace, tab for tab (pinned tabs pinned, the active one active, the rest loading when
you first look at them). `sidelit chrome install` registers the extension's native host; then,
once in each Chrome profile:

1. `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → pick the
   `chrome-extension` folder of the checkout (`/usr/share/sidelit/chrome-extension` for a
   package).
2. `chrome://settings/onStartup` → **Open the New Tab page**. Sidelit brings the windows back
   now; with Chrome's "Continue where you left off" on too, a window Chrome restores itself is
   recognised by its tabs and not opened twice, but Chrome then decides where it goes first.

`sidelit chrome status` lists the connected profiles. At login Chrome is started in each
profile that had windows, and its first blank window is filled with the first remembered
window's tabs. Incognito windows are never reported. Chromium, Brave and Edge work the same way.

## Moving from Claude Workspace

Sidelit is Claude Workspace under a new name. To move over:

1. Install Sidelit, any way above. If you had the Claude Workspace .deb or Arch package,
   Sidelit's package replaces it (on Arch, answer `y` when pacman asks to remove
   claude-workspace).
2. Run `sidelit start` once, in a terminal. It takes over from Claude Workspace:
   - stops it, and removes what started it: its systemd units, its autostart and desktop
     entries, its links in `~/.local/bin` (but `claude-workspace-open` now runs
     `sidelit-open`, so links still open from what names it);
   - moves your layout, config, other sources, usage cache and journal from
     `~/.config/claude-workspace` and `~/.local/state/claude-workspace` into Sidelit's folders,
     never over anything Sidelit already has;
   - changes `claude-workspace open` to `sidelit open` in your config and your sources' files;
   - installs Sidelit's Claude Code plugin, then takes Claude Workspace's hook out of
     `~/.claude/settings.json` (the file as it was is kept next to it as
     `settings.json.before-sidelit`);
   - replaces Claude Workspace's Chrome bridge with Sidelit's.

   It ends with a list of what is left for you, and `sidelit why moved_over` shows it again
   later. To see first what it would do, without changing anything:
   `SIDELIT_DRY_RUN=1 sidelit moveover`. To try it on a copy, with Claude Workspace left
   running as it is: `SIDELIT_HOME=/tmp/sidelit-try sidelit moveover` copies its folders there
   and changes nothing else.
3. What may be left for you:
   - Chrome: in `chrome://extensions`, remove the Claude Workspace extension and load Sidelit's
     (**Load unpacked**, the folder from "Chrome windows and their tabs" above), in each profile.
   - Links: wherever you set `claude-workspace-open` (`$BROWSER` in your shell rc, Terminator's
     `custom_url_handler`, your default browser), put `sidelit-open` instead. Then delete
     `~/.local/bin/claude-workspace-open`.
   - Claude Code sessions that were already running get Sidelit's plugin when you type
     `/reload-plugins` in them, or restart them (until then their tabs may follow only their
     terminal titles). From a checkout, the old hook's link in `~/.local/bin` points at Sidelit's hook
     until your next login, for whatever still calls it.
   - Settings in your environment: `CLAUDE_WORKSPACE_*` are now `SIDELIT_*`.
   - The old checkout (`~/.local/share/claude-workspace`) is not used any more: delete it when
     you like. If the old package is still installed, remove it (`sudo apt remove
     claude-workspace`).
