# Desktops

What Sidelit does on each desktop, and what it has to work around there.

## What works where

| Desktop | Session | Drag tabs to reorder | Left to do after the install line |
|---|---|---|---|
| COSMIC (Pop!_OS 24.04 and later) | Wayland | yes | nothing |
| KDE Plasma 6 | Wayland | no | nothing |
| GNOME 46 to 51 (Ubuntu, Debian, Fedora, Zorin) | Wayland | yes | log out and back in once |
| Hyprland | Wayland | no, by number | one line in its config, unless uwsm runs your session |
| niri | Wayland | yes | nothing in niri's own session, else one line in its config |
| sway | Wayland | no, by number | one line in its config, unless your session runs autostart entries |
| Cinnamon (Linux Mint), Xfce, MATE | X11 | no | nothing |

The install line says what is left, if anything; the steps are in
[the install guide](install.md#on-gnome). Other desktops are planned.

## Desktop by desktop

On Wayland:

- **COSMIC** (Pop!_OS 24.04 and later, or any distribution with COSMIC): everything in
  [the guide](guide.md).
- **KDE Plasma 6**: workspaces are KWin's virtual desktops. KDE keeps every desktop until you
  remove it, so there is no pinning, and KWin cannot reorder its desktops, so tabs cannot be
  dragged.
- **GNOME 46 to 51** (Ubuntu 24.04 and 26.04, Debian 13, Fedora Workstation, Zorin): with
  Sidelit's GNOME Shell extension turned on (see [On GNOME](install.md#on-gnome)). Everything works as
  on COSMIC; the sidebar is hidden in the overview, like every window. With GNOME's setting
  "workspaces on the primary display only" (the default), windows on other screens are on
  every workspace, so they belong to none of the tabs.
- **Hyprland** (checked with 0.56, with the new Lua config and with a classic one): the tabs are
  Hyprland's own workspaces, by their numbers. A workspace Sidelit keeps stays even when it is
  empty, and after a login it comes back under the same number, so Super+3 still takes you to
  it. Hyprland orders workspaces by number, so tabs cannot be dragged. Hyprland does not run
  autostart entries by itself: see [On Hyprland](install.md#on-hyprland) for the one line to add.
- **niri** (checked with 26.04): the tabs are niri's workspaces, screen by screen, each screen's
  in niri's order. niri drops an empty workspace unless it has a name, so a workspace Sidelit
  keeps gets one, `sidelit-1`, `sidelit-2`… (one you named yourself keeps your name), and after a
  login it comes back under that name. Tabs can be dragged to reorder the workspaces, onto
  another screen too. niri runs autostart entries only in its own session: see
  [On niri](install.md#on-niri).
- **sway** (checked with 1.12): the tabs are sway's own workspaces, by their numbers (named ones
  after them). sway drops a workspace once it is empty and you leave it, and has no way to keep
  one, so Sidelit keeps the ones you work on itself: such a workspace stays a tab while it is
  empty, and going there, or a window being put there, makes sway create it again under its
  number. After a login they come back under the same numbers, so Super+3 still takes you to
  the same one. sway orders workspaces by number, so tabs cannot be dragged. sway runs no
  autostart entries by itself: see [On sway](install.md#on-sway) for the one line to add.

And on X11:

- **Cinnamon** (Linux Mint's default), **Xfce** and **MATE**, or any other window manager that
  keeps the standard EWMH hints (Openbox, Budgie's): workspaces are the window manager's
  desktops, and the sidebar is a panel on the left that maximised windows stop beside. As on
  KDE, desktops stay until you remove them, so there is no pinning, and they cannot be
  reordered, so tabs cannot be dragged.
- Not on X11: KDE Plasma and GNOME (use their Wayland sessions). Not on Wayland: Cinnamon's
  Wayland session (pick "Cinnamon" at the login screen).

## Notes per desktop

How each backend talks to its desktop is in [How it works](how-it-works.md); these are what it
means for you.

### COSMIC

Only pinned workspaces have a stable id, workspace `rename` is ignored on COSMIC
1.0 (names live in the layout), and toplevel → workspace mapping arrives on the compositor's next
refresh tick. cosmic-comp keeps one workspace set per screen and mirrors them by index; each
screen's copy of an index can carry a pinned id of its own, so a kept workspace learns every id
its copies carry, and an unplugged screen's copies (appended as extra workspaces) are recognised
as extras: their windows go home and they are unpinned. COSMIC can report a window's position
but not set it, so what is restored is the workspace, the screen and the window itself, not its
exact position.

### KDE Plasma

Every desktop has a stable id and lasts until it is removed, so nothing is pinned; a
workspace the layout lets go (forgotten, or emptied) leaves its desktop alone, and only closing
it from the sidebar removes the desktop, once its windows have gone. A window on all desktops
belongs to none of them. The windows' pids come with them, which helps match a terminal to its
Claude session.

### GNOME

GNOME lets no app see or arrange other windows, and has no place for a panel like
the sidebar, so Sidelit's GNOME Shell extension does both: it lists the workspaces and windows
for the daemon and switches, keeps and moves them when it asks, and it runs the sidebar, docked
on the left of every workspace with that strip kept free (beside Ubuntu's dock when that is on
the left). `sidelit stop` makes it take the sidebar away and free the strip. It stays on while
the screen is locked, so the workspaces Sidelit keeps are still there when you come back, and
changes nothing then. An empty workspace disappears unless Sidelit keeps it (it keeps each one
you work on, as on COSMIC); with a fixed number of workspaces (Settings → Multitasking), '+'
adds one at the end and closing one removes it. Workspace names live in Sidelit's tabs only;
GNOME shows none. GNOME on X11 (46 to 49 still offer it) runs the daemon but not the sidebar.

Inside, GNOME drops an empty workspace (dynamic workspaces) unless it is the one you are on
or the empty one it keeps at the end; a workspace Sidelit keeps is pinned in the extension, which
makes GNOME's check see it as busy. With a fixed number of workspaces nothing is dropped, so
`pin` is not in `caps` and '+' adds a workspace. Workspace ids are the extension's, and last
while it runs, also while the screen is locked (it stays on then, and refuses every action);
turned off and on, it numbers its workspaces anew and the client gives each one back the id it
had, by position and the windows on it. Window ids are GNOME's own and the pids come with them.
The extension uses GNOME Shell internals in two places, both checked against GNOME 46, 48, 50
and 51: the workspace tracker's `_checkWorkspaces` (to keep pinned workspaces) and the layout
manager's tracked chrome (to put the sidebar beside a dock already on the left). Each GNOME
release (every six months) needs the extension's `shell-version` raised after a check.

### Hyprland

- **Ids.** A workspace's id is its number (`3`), or `name:` and its name. A kept workspace is
  asked back under its number after a login; a new one gets the next number.
- **Keeping one.** Hyprland drops an empty workspace you are not looking at. Sidelit keeps one
  with a persistent workspace rule, as `workspace = 3, persistent:true` in a config would.
- **Screens.** The rule names the workspace's screen (without one, Hyprland moves it to the
  screen with the keyboard whenever it applies its rules). The rule follows the workspace when
  you move it; two swapped workspaces change together. While its screen is unplugged the rule
  stays, and the workspace goes back when the screen does.
- **Your config.** A workspace your config keeps is never unpinned; one it puts on a screen
  stays there, and `+` skips numbers your config puts on another screen. With two screens, a
  workspace your config keeps without naming a screen moves to the screen with the keyboard
  whenever a rule is set: give it a `monitor:`.
- **Reloads.** Rules added at runtime go when Hyprland reloads its config; Sidelit adds its own
  again. A rule Sidelit lets go is switched off (Lua config), or stays unpinned until the next
  reload (classic config: that number keeps opening on its last screen).
- **Two command forms.** Since 0.55 a Lua config takes commands in Lua
  (`hl.dsp.focus({window = "address:…"})`) and refuses the classic form (`focuswindow
  address:…`). Sidelit asks which on connecting (`eval` exists only with Lua).
- **Events.** Windows go by Hyprland's stable id, with a token for this run of Hyprland (it
  numbers them the same way at every start). Titles and focus come straight from the event
  line (a working Claude retitles its terminal ten times a second); anything else makes Sidelit
  read everything again.

### niri

- **Ids.** A workspace's id is its name, written as Hyprland's named ones are
  (`name:sidelit-1`); an unnamed one has none, like an unpinned one on COSMIC. Sidelit names the
  ones it keeps `sidelit-1`, `sidelit-2`…, and after a login asks for a kept one back under its
  name, yours or Sidelit's: the empty workspace at the end of the screen you are on gets it.
- **Keeping one.** niri keeps a named workspace even when it is empty, so naming one is pinning
  it. Letting one go takes off only a name Sidelit gave: a workspace you named yourself
  (`niri msg action set-workspace-name`), or one your config declares (`workspace "chat"`),
  keeps its name, and stays kept. A workspace you rename in niri is still the same one to
  Sidelit (it knows it by the names it had while niri runs).
- **Screens.** Each screen has workspaces of its own, with an empty one at the end; the tabs go
  screen by screen, left to right. A workspace is on one screen, so it goes, windows and all, to
  the screen most of its open windows were kept on. niri moves an unplugged screen's workspaces
  onto another one, and back when it returns.
- **Events.** The daemon follows niri's event stream. niri drops a client that falls too far
  behind; Sidelit then opens the stream again and reads everything anew.
- **Windows.** niri numbers windows from 1 again at every start, so a window's identifier also
  carries a token for this run of niri: a window after the next login is never taken for one
  from before.

### sway

- **Ids.** A workspace's id is its name, written as Hyprland's are: a numbered one's name (`3`,
  or `3: web`, numbered 3), else `name:` and its name. A new kept workspace gets the number after
  every one in use; after a login a kept one is asked back under its name.
- **Keeping one.** sway drops a workspace once it is empty and no screen shows it, and offers no
  way to keep one. Sidelit keeps the list itself (in `$XDG_RUNTIME_DIR/sidelit/`, for as long
  as sway runs): a kept workspace that sway dropped stays a tab, with no windows, and comes
  back under its name when you go to it, when a window that belongs there is put back (after a
  crash, say) or at a login. sway's own bar shows it only once it is back.
- **Screens.** Each workspace is on one screen; the tabs go by number, whatever the screen. A
  workspace goes, windows and all, to the screen most of its open windows were kept on. sway
  moves an unplugged screen's workspaces onto another one, and back when it returns. A kept
  workspace that sway dropped comes back on the screen it was on last. With no screen on at all
  (a laptop's lid closed, no other screen), sway shows no workspace to anyone: Sidelit takes
  everything to be where it was until a screen is back, and starts nothing again meanwhile. A
  new workspace's number skips one your config puts on another screen (`workspace 6 output
  HDMI-A-1`).
- **Windows.** A sticky window (floating, taken along to every workspace of its screen) and one
  in the scratchpad belong to none of the tabs.
- **Renaming.** A workspace you rename in sway (`rename workspace … to …`) is still the same one
  to Sidelit, kept under its new name. sway compares names in any case, and so does Sidelit.
- **Events.** The daemon follows sway's events. sway drops a client whose events pile up
  unread; Sidelit then subscribes again and reads everything anew. Window ids carry a token for
  this run of sway, which numbers them anew at every start.

### X11

- **Ids.** X11 desktops have positions but no ids. Sidelit gives each one an id and keeps the
  list, with the names, in `~/.local/state/sidelit/x11-desktops.json`; the window manager brings
  the same desktops back at the next login, and they get the same ids.
- **Desktops coming and going.** When one comes or goes without Sidelit (Cinnamon can remove one
  in the middle), the names and the windows say which: the ones after it move down a place.
  EWMH adds and drops desktops only at the end, so to remove one in the middle Sidelit drops the
  last one, then moves the windows and names after it down by one.
- **Windows.** Window numbers start over with every X server, so a window's identifier also
  carries a token kept on the root window (`_SIDELIT_SESSION`): a window after the next login is
  never taken for a remembered one. A window's app id is GTK's own where GTK sets one
  (`org.gnome.Terminal`), else its `WM_CLASS`, as on Wayland. A window on every desktop belongs
  to none of the tabs.
- **Screens.** A window is on the screen its middle is on, and goes to another one with
  `_NET_MOVERESIZE_WINDOW` (a maximised one is maximised there again). The sidebar's strip is
  reserved from the left edge of the whole X screen, so it belongs on the leftmost screen (the
  `monitor` default); it leaves room for the desktop's own panels.
- **Login.** The window manager starting (cinnamon, xfwm4, marco…) is what tells the daemon to
  bring windows back.
