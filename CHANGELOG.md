# Sidelit CHANGELOG

Entries are a line or two: what changed and what to expect from it. These are
what a release page shows, so write them for whoever is deciding whether to
update. The reasoning belongs in the commit message.

## [Unreleased]

- Every workspace is the same card: its sessions together in one well, then a small dimmed icon
  for each other window on it, with a browser window's tab count and what an app's title says is
  unread (Discord, Slack and other apps that title themselves "(3) …"); the icons take their
  colour while the pointer is on the card, so at a glance only the sessions' states stand out.
  Click an icon to bring that window forward. A card is headed only by a name you give it (or, without sessions, its name
  or number); otherwise its first session's title says what it is about. The folder name and the
  shortcut chip are gone from the card: hover it for its folder and shortcut. Subagents are
  small blocks right after the model.
- The sidebar is headed "Sidelit", with Sidelit's new mark, instead of "Workspaces".
- OpenCode, both OpenCode 1 (its releases since September 2026) and 2: its sessions get a box
  of their own, marked ◎, with their state, title and model, the same "needs you" check
  (through `opencode run`, on the model your OpenCode uses), fork, and resume after a login.
  Sidelit's plugin for it is one file in OpenCode's plugin folder: `sidelit hooks install` (and
  the one-line install) puts it there, with nothing to trust, and a Sidelit from before puts it
  in at its first start after the update. OpenCode has no usage limits of its own, so it has
  no bars.
- sway: Sidelit runs there too. The tabs are sway's own workspaces, by number; the ones you work
  on are kept even after sway drops them empty (going there makes them again), and come back
  under the same numbers after a login. Each tab shows the key your sway config binds to it.
  Add `exec sidelit start` to `~/.config/sway/config` unless your session runs autostart
  entries.
- niri: Sidelit runs there too. The tabs are niri's workspaces, screen by screen; the ones you
  work on are kept by a name (`sidelit-1`, `sidelit-2`…) and come back under it after a login.
  Tabs can be dragged to reorder them, onto another screen too, and show Mod+1…9 on the screen
  you are on. In niri's own session Sidelit starts by itself; started any other way, add
  `spawn-at-startup "sidelit" "start"` to `~/.config/niri/config.kdl`.
- Codex: its sessions get a box of their own, marked ◆ (Claude Code's keep ✳), with their
  state, title and model, the same "needs you" check (through `codex exec`), fork, and resume
  after a login; Codex's usage limits sit at the bottom under its name, Claude Code's under
  theirs. `sidelit hooks install` (and the one-line install) puts Sidelit's plugin in every agent
  it finds; Codex then asks you once, when it next starts, to trust the plugin's hooks. A
  Sidelit from before puts it in at its first start after the update. Right-click a tab for a
  new session of each agent.
- X11 desktops: Cinnamon (Linux Mint), Xfce and MATE, and other window managers that keep the
  standard EWMH hints. The sidebar is a panel on the left that maximised windows stop beside,
  next to the desktop's own panels; workspaces are the window manager's desktops and keep their
  place across logins. The one-line install takes these sessions and adds python-xlib. Not on
  Cinnamon's Wayland session. GNOME Terminal, Xfce's and MATE's terminals are now terminals
  Sidelit opens.
- Hyprland: Sidelit runs there too, with a Lua config (Hyprland 0.55 and later) or a classic
  one. The tabs are Hyprland's own workspaces, by number; the ones you work on stay even when
  empty and come back under the same number after a login. Add `exec-once = sidelit start` (or,
  with a Lua config, `hl.on("hyprland.start", function () hl.exec_cmd("sidelit start") end)`)
  unless your session runs under uwsm. kitty, foot and Alacritty are now terminals Sidelit opens.
- GNOME (46 to 51, on Wayland): Sidelit runs there with its own GNOME Shell extension, which
  the packages and `install.sh` install. Turn it on once: log out and back in, then switch on
  Sidelit in the Extensions app (or `gnome-extensions enable sidelit@kulaxa.github.io`). The
  sidebar stays docked on the left of every workspace, beside Ubuntu's dock, and workspaces you
  work on are kept as on COSMIC; tabs can be dragged to reorder them.
- One line installs Sidelit on Ubuntu, Pop!_OS, Debian, Fedora, Arch and openSUSE, and the same
  line again updates it: `curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash`.
  It installs the few packages Sidelit needs with the distribution's own package manager (asking
  for your password once), puts Sidelit in `~/.local/share/sidelit` and starts it.
- Sidelit plugs into Claude Code as an ordinary Claude Code plugin, installed and removed with
  Claude Code's own `claude plugin` commands (`sidelit hooks install|uninstall|status` run
  them). Sidelit no longer writes its hook into Claude's settings: the first `sidelit start`
  after updating installs the plugin and takes out the hook Sidelit 0.5 had written into
  `~/.claude/settings.json` (your own hooks stay; the file is backed up first). Before going
  back to 0.5 or removing Sidelit, run `sidelit hooks uninstall`.
- The usage bars no longer read your Claude login or call an undocumented endpoint: the plugin
  passes on Claude Code's own measure of your plan's limits after each turn (this needs a recent
  Claude Code). They show the session and week windows, once a session has had a turn; the
  per-model weekly bar is gone.
- Running sessions are read from `claude agents --json`, Claude Code's supported list, instead of
  its internal files; a status-line script's files are no longer read.
- Every Claude on a workspace shows, not only the first: a workspace with Claudes is a card with a
  box for each (title, model, its own state stripe), whether they run in terminals of their own
  or in split panes or tabs of one. Subagents at work are dots in their Claude's box. Click a box
  to go to that Claude's terminal (and its pane in tmux, WezTerm and kitty; Terminator cannot
  focus a pane from outside, so there the window comes forward).
- A Claude now calls you (yellow) whenever you are not typing in its own terminal, not only when
  you are on another workspace. Clicking a workspace, or switching to it with its Super+N, lands
  on the Claude that has waited longest for you, else the one you used last.
- A restore brings back every Claude a workspace had (several sharing one window come back in a
  terminal each); Recent sessions and the closed-workspace history list each of them.

## [0.5.0] - 2026-10-17

First release as Sidelit, and the first one in the open, under the MIT licence.

- A new name: the command is `sidelit` (with `sidelit-open`, `sidelit-hook` and
  `sidelit-chrome-host`), its files live in `~/.config/sidelit`, `~/.local/state/sidelit` and
  `$XDG_RUNTIME_DIR/sidelit`, and its settings in the environment are `SIDELIT_HOME`,
  `SIDELIT_DRY_RUN` and `SIDELIT_BACKEND`. The sidebar's menus speak of sessions ("Recent
  sessions", "New session in this directory").
- Moving over is one step: the first `sidelit start` on a machine that ran the old name stops
  it, brings its layout, settings, other sources and journal along, swaps its Claude Code hook
  and Chrome bridge for Sidelit's, and says what is left to do by hand. The README's moving
  section has the steps.
- COSMIC (Wayland) only for now: the KDE Plasma backend is left out of this release, to come
  back rewritten.
- No web app is listed by default any more; add yours under `web_apps` in the config.
- The update button installs the Arch package with pacman on Arch, and every release carries
  an Arch package next to the .deb.
- Everything else works as before: a tab per workspace, the agent's status from its own hook,
  the check before a tab turns yellow, the layout kept and restored after a login, other
  sources, Chrome windows with their tabs.
