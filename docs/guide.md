# Using Sidelit

- [The sidebar](#the-sidebar)
- [Who needs you](#who-needs-you)
- [Keeping the layout](#keeping-the-layout)
- [Links and web apps](#links-and-web-apps)
- [Commands](#commands)

## The sidebar

- **Every workspace is a card.** Headed by its name, else "Workspace N", then a box for each
  agent session on it, in the order they started: its own title, the model and a state stripe.
  Sessions in terminals of their own and in split panes or tabs of one terminal window all show.
  Right of the name, a small dimmed icon for each other window on the workspace, with a browser window's tab
  count (from Sidelit's Chrome extension) and what an app's title says is unread ("(3) …", as
  Discord, Slack and many web apps put it); the icons take their colour while the pointer is on
  the card, so at a glance only the sessions' states stand out. Hover an icon for its title,
  click it to bring that window forward. The first session's title says what the workspace is
  about; a name you give it heads the card above its sessions, and a workspace without sessions
  is headed by its name or number. Hover a card for its folder. The active workspace is cyan.
- **Each box says whose session it is**: ✳ for Claude Code (◐ while it works: the glyphs Claude
  Code puts in its own terminal's title), ◆ for Codex, ◎ for OpenCode.
- **Click** a tab to switch; keyboard focus lands in the session that has waited longest for
  you, else the one you used last. Switching with the workspace's own shortcut does the same
  when a session there needs you. **Click a box** to go to that session's terminal, and its pane
  in tmux, WezTerm and kitty (with remote control on), and its tab or split in Terminator (through
  Sidelit's Terminator plugin, which `sidelit hooks install` puts in; in windows opened since).
  So several sessions can share one terminal window, in tabs or side by side.
- **New workspace** (`+`) pins a fresh workspace (on KDE, adds a desktop; on Hyprland and sway,
  the next number; on niri, the empty one at the end of the screen you are on, named) and opens
  a terminal in your home directory, or whatever the template you pick says
  ([templates](config.md#templates)). Start `claude`, `codex` or `opencode` there
  as usual and the tab picks up its title within seconds. Right-click a tab for a new session,
  in that tab's directory, of each agent installed on this computer.
- **Fork** icon: like `/fork`, opens `claude --resume <id> --fork-session` (`codex fork <id>` for
  a Codex session, [OpenCode's fork](agents.md#opencode) for an OpenCode one) in a new workspace
  in the same directory.
- **×** closes the whole workspace: every window on it gets a normal close request (after a
  confirmation listing what will close), the workspace is unpinned and disappears once empty (on
  KDE, the desktop is removed once its windows have closed). A session that was there stays in
  *Recent sessions*. A kept workspace that is not on the desktop shows as "closed – click to
  reopen": one click brings it and its windows back.
- **Drag** a tab onto another to reorder the workspaces themselves (COSMIC, GNOME and niri, where
  a tab dropped among another screen's moves its workspace to that screen; KWin cannot reorder
  its desktops, and Hyprland and sway order them by number). **Double-click** (or right-click →
  Rename) to name a workspace: the name heads its card; an empty name takes the header away
  again.
- **Workspace history.** The clock button in the header swaps the workspaces for the ones you
  closed, in the sidebar itself: newest first under a heading per day, each a card with when, its
  folder and what else was open on it. The last three days show; "Show older" brings the rest.
  The search box on top matches titles, windows and directories across all of them (Enter opens
  the first match). Pick one and it comes back on a new workspace: its name and directory, its
  session resumed, its terminals, folders and web apps reopened one at a time. ← or Esc goes
  back to the workspaces. The last 100 are kept.
- **Settings.** The gear swaps the workspaces for the settings: the theme (the season's, the
  default look, Halloween's, or [one of your own](config.md)); switches for the restore after
  a login and for reopening windows that crash; and buttons to put everything back, check for updates and restart the sidebar.
- **Header menu.** Right-click the header for "Check for updates", "Put everything back" (every window to its
  workspace and screen, what went missing reopened), *Recent sessions* (closed ones, one click resumes one on a new workspace)
  and "Restart sidebar". Right-click a tab to forget its missing windows.
- **Fits the screen.** When the cards do not fit, the sidebar goes compact by itself: each
  agent's limits on one line, their resets on hover.
- **Usage.** Your plans' 5-hour and weekly limits sit at the bottom, under each agent's name:
  Claude Code's (on a Pro or Max plan) as it measured them after the last turn of any session,
  through the plugin's mod; Codex's (logged in with ChatGPT) as its app-server says. They appear
  after the first turn (Codex's within seconds of the start), the last ones seen stay through a
  login, and a window whose reset has passed goes. Sidelit never reads your logins for them.
  No Claude Code bars? They need Claude Code 2.1.287 or later (`sidelit hooks status` says when
  yours is older), a Pro or Max plan (an API key, a cloud provider's or a gateway's login has no
  such windows), and one finished turn since the plugin went in.
  A model's own weekly limit (Fable's) is not among what Claude Code hands plugins; Sidelit
  shows it as Claude Code's `/usage` last read it (Claude Code keeps that reading in its
  `~/.claude.json`; nothing else in that file is looked at), with the time of that reading once
  it is a quarter of an hour old. Open `/usage` to bring it up to date.
  OpenCode has no such limits of its own, so it has no bars.
- **Updates.** When a newer release is out, an "Update to X.Y.Z" button appears under the usage
  bars. It opens a terminal that pulls the checkout (or, for a package install, downloads the
  package and installs it with apt or pacman, asking for your password there) and restarts the
  sidebar. Nothing is installed behind your back. The sidebar looks when it starts and every hour;
  "Check for updates" (the settings, the header menu) looks at once and updates if there is one.

## Who needs you

- **Status, straight from the agent.** The hook in Sidelit's plugin ([the agents](agents.md);
  for Codex, once you have trusted it) makes every session report what it is doing as it
  happens: working, waiting for a permission or an answer, done. That is its main thread; the
  subagents it runs are small blocks right after the model, one each, and so is a session it
  started itself, which never gets a box. It also says which terminal it lives in: by process
  ancestry for a session the daemon started (or whose window's process the desktop names), else
  by which window had the keyboard when you submitted a prompt (until its first prompt, when you
  started it: a new tab or pane gets its box at once); and which pane, where the terminal says
  (Terminator, tmux, WezTerm, kitty). A box is called by its session's own name, "New session"
  until it has one. Only without the plugin (or before a session's first event) is the terminal
  title matched against Claude Code's list of running sessions (`claude agents --json`, asked
  every few seconds), which cannot tell two sessions with the same title apart. The two
  tool-call events run async, so the agent never waits for the hook.
- **Yellow means the agent needs you.** A session's box goes yellow and bold, and its card glows,
  when it hands back to you while you are not typing in its own terminal (or pane: the one whose
  title the window shows), until you go to it. A finished turn is checked first: a model (Sonnet
  by default) reads its end (what you asked, the last tool calls, what the agent said) and says
  whether it needs you (it is done, stuck or asking) or is only waiting on something to finish,
  such as a CI pipeline, a deploy or a background job. One that only waits stays orange and
  still, with `waiting on CI pipeline` under its title; it gets its next check when it wakes up
  and ends another turn. One that has nothing to wake it (no watcher, background job or
  scheduled check, only "I'll check the pipeline") turns yellow after 20 minutes, since it will
  only go on when you nudge it. The check takes a second or two, and the tab keeps breathing
  orange meanwhile. If it fails or takes more than a minute, the tab turns yellow anyway. A turn
  that ends while you are typing in its terminal is not checked, unless it left a shell running
  (below). The check runs through the session's own agent: `claude -p` on your subscription (no
  API key), `codex exec` for a Codex session, `opencode run` for an OpenCode one, with no tools
  and no session kept; one process is started ahead while a session works. Config:
  `check_turns` (false = yellow at every turn end), `check_model`, `check_model_codex`,
  `check_model_opencode`. `sidelit why turn_checked` lists the verdicts (`why TITLE` one
  session's), and every tab that turned yellow without one.
- **Working, and waiting.** The orange stripe breathes while the agent works, and stays orange
  without breathing while its turn is over but it waits on something (the check above says
  what). A turn that leaves a shell running in the background ("1 shell still running") is
  always checked, even on the tab you are looking at: a session waiting for that deploy stays
  orange, one that is done and only left a dev server running is not. Only with the check off
  does a running shell alone keep the stripe orange (`shell still running`). A session stopped
  on a permission prompt or a question gets a steady yellow stripe and `waiting for you`.

## Keeping the layout

- **One layout, kept by a daemon.** `~/.config/sidelit/layout.json` is the source of truth: the
  kept workspaces in order, and on each the windows that belong there (a *slot* per window: the
  app, the screen it is on, and what the app lets us remember: the session and directory of a
  terminal, the folder of a file manager, the URL of a web app). Every workspace you work on is
  kept (pinned, so it survives a reboot) once a window has been open on it for a moment; one you
  empty yourself is let go again, the way COSMIC drops an empty workspace (a KDE desktop itself
  stays: only closing it from the sidebar removes it). One you named stays, and so does one
  whose windows are only missing (an app crashed, a screen went away). The daemon
  (`sidelit-daemon`, a systemd user service) is the only thing that changes the layout, and it
  keeps the desktop matching it.
- **What you do is remembered; what happens to you is undone.** While nothing unusual is going
  on, the layout follows you: open a window and it is kept where it opened, move it and its slot
  moves, close it and it is forgotten 20 seconds later (a session you close goes to *Recent
  sessions* instead of coming back at the next login). While something *is* going on (a login,
  a screen plugged in or out, waking from suspend, many windows closing at once) the layout stays
  as it is and windows are moved back to where it says. A logout closes apps one by one; those
  closes never stand long enough to count. An app that quits or crashes with all its windows
  keeps them in the layout. A chat app closing to the tray keeps its place, and its window goes
  home when it comes back.
- **Restore after login.** A few seconds after a login the daemon starts what is missing: each
  session resumed by its agent (`claude --resume`, `codex resume`, `opencode -s`) in a terminal
  on its workspace, terminals in their directory, file managers on their folder, web app windows
  on their URL, one at a time, each waiting for its window. Apps that restore their own windows
  (Chrome, VS Code) are started once and their windows placed as they appear, matched by title
  (Chrome windows tab for tab with [the extension](install.md#chrome-windows-and-their-tabs)).
  Then it switches back to the workspace you were on. Nothing is started in the middle of a
  session unless you ask (header menu, or `sidelit restore`). A window that fails to come back
  three logins in a row is dropped. Config: `restore_on_login`, `restore_windows` (false =
  sessions only), `restore_delay`, `ignore_apps`.
- **Crashes come back.** A window that goes without you closing it (an app crash, a terminal
  dying while you work elsewhere, COSMIC restarting) is started again once the desktop is calm,
  without switching workspaces; something that keeps dying is left missing after three tries. A
  window you close (it has the keyboard) is forgotten. `"relaunch": false` turns this off.
- **Docking.** Unplug a screen and the windows COSMIC moves onto the remaining one (or onto the
  workspaces it appends there) go back to their workspaces; they stay remembered as belonging on
  the missing screen and move back when it returns. The daemon has no window of its own, so a
  screen change cannot take it down; the sidebar restarts itself onto the screen config
  `monitor` picks (`leftmost`, `largest`, or a model or maker substring), and follows when you
  rearrange or resize screens and a different one becomes the pick.
- **Why did that happen?** Every decision is journaled (`~/.local/state/sidelit/journal.jsonl`):
  `sidelit why Home` shows what was kept, moved, forgotten or started on the workspace called
  Home, and why.

## Links and web apps

- **Links open where you are.** `sidelit open URL` looks at the active workspace: a browser
  window there gets focused and receives the tab; if there is none, a new browser window opens
  on this workspace instead of a tab landing in whichever window the browser last used somewhere
  else. Wire it up once: Terminator: `[global_config]` `use_custom_url_handler = True`,
  `custom_url_handler = ~/.local/bin/sidelit-open`; CLI tools including Claude Code:
  `export BROWSER=~/.local/bin/sidelit-open` in your shell rc (use `/usr/bin/sidelit-open` with
  a package). Config `browser` sets the executable for new windows (default: the desktop's
  default browser).
- **Web apps on a workspace.** Right-click a tab → "Open web app here…" takes any URL, or run
  `sidelit open --app URL` on the workspace you are on: the page opens as a browser app window
  (no tabs, no address bar) on that workspace, logged in through the browser. The ones you use
  often go in config `web_apps` and get an entry of their own, "Open <name> here", e.g.
  `[{"name": "Discord", "url": "https://discord.com/app"}]`. These windows are remembered and
  restored after a login like any other, reopened on their exact URL.
- **Transcript safety.** Claude Code's session markers (`CLAUDE_CODE_CHILD_SESSION`, …) are
  scrubbed from every terminal the sidebar opens, so sessions started there save transcripts.

## Commands

```bash
sidelit                # start the daemon and the sidebar (also runs at login)
sidelit stop|restart|status|log
sidelit why TEXT       # why a window was kept, moved or forgotten (TEXT: a workspace, title, app)
sidelit restore        # start what is missing from the layout now (runs by itself after a login)
sidelit reapply        # put every window back on its workspace and screen
sidelit state|layout   # what the daemon sees / keeps, as JSON
sidelit ws list|toplevels|activate N|new NAME|pin N|unpin N|move A before|after B|movewin TITLE N [OUTPUT]
sidelit open URL       # open a link on the current workspace (above)
sidelit hooks install|uninstall|status   # Sidelit's plugin, in Claude Code, Codex and OpenCode
sidelit chrome install|uninstall|status  # the Chrome extension's bridge; which profiles are connected
```
