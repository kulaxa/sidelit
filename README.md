# Sidelit

A sidebar for COSMIC and KDE Plasma where every workspace is a tab, and the coding agent working
on it shows its status there: working, waiting for you, done. Each agent keeps its terminal,
its browser and whatever else it needs on a workspace of its own, and Sidelit keeps that layout:
windows go back where they belong after a screen change, a crash or a login, and the sessions
that were open come back resumed. You look after agents, not windows.

<!-- screenshot: docs/sidelit.png, the sidebar on COSMIC with a few agents at work -->

**What works today**, on Wayland:

- **COSMIC** (Pop!_OS 24.04 and later, or any distribution with COSMIC): everything below.
- **KDE Plasma 6**: workspaces are KWin's virtual desktops. KDE keeps every desktop until you
  remove it, so there is no pinning, and KWin cannot reorder its desktops, so tabs cannot be
  dragged. The shortcut on each tab is whatever "Switch to Desktop N" is bound to in KDE's
  shortcuts (Ctrl+F1… out of the box).

Other desktops are planned.

**Works with Claude Code.** Sidelit plugs into Claude Code as a plugin, installed with Claude
Code's own plugin commands: every session reports what it is doing as it happens, and Claude
Code's own measure of your plan's usage limits comes along for the bars. Beyond that it only
reads what Claude Code offers for it (its list of running sessions, the titles in its
transcripts), touches Claude's settings itself only once, to take out the hook Sidelit 0.5 had
put there, and never touches your login. Sidelit is an
independent project, not made by or affiliated with Anthropic; Claude and Claude Code are
Anthropic's trademarks.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
```

or, with wget:

```bash
wget -qO- https://raw.githubusercontent.com/kulaxa/sidelit/main/get.sh | bash
```

Run it in a terminal on your desktop (COSMIC or KDE Plasma 6, on Wayland). It installs the few
packages Sidelit needs (asking for your password once), puts Sidelit in `~/.local/share/sidelit`
and starts it; from then on it starts at every login. Run the same line again to update. It
knows Ubuntu, Pop!_OS, Debian, Fedora, Arch (and EndeavourOS, CachyOS, Manjaro) and openSUSE;
what it did is in `~/.cache/sidelit-install.log`, and `~/.local/share/sidelit/uninstall.sh`
takes it out again.

### Other ways to install

Sidelit needs COSMIC or KDE Plasma 6 on Wayland, Python 3.10 or later with GTK 3 and
gtk-layer-shell (the packages pull them in), and the `claude` CLI for the session features.
Nothing is compiled: the Wayland client is pure Python, in the tree, and on KDE it talks to KWin
over D-Bus.

**Ubuntu, Pop!_OS, Debian:** the .deb from the latest release:

```bash
curl -LO https://github.com/kulaxa/sidelit/releases/latest/download/sidelit_latest_all.deb
sudo apt install ./sidelit_latest_all.deb
```

**Arch:** the package from the latest release:

```bash
curl -LO https://github.com/kulaxa/sidelit/releases/latest/download/sidelit-latest-any.pkg.tar.zst
sudo pacman -U ./sidelit-latest-any.pkg.tar.zst
```

Then, once, as yourself: `sidelit hooks install` (Sidelit's Claude Code plugin, see below)
and, if you use Chrome, `sidelit chrome install`. Start it with `sidelit start`; after that it starts at
every login.

**From a checkout** (no sudo; the update button pulls it):

```bash
git clone https://github.com/kulaxa/sidelit.git ~/.local/share/sidelit
~/.local/share/sidelit/install.sh
sidelit start
```

`install.sh` links `sidelit` into `~/.local/bin`, adds a desktop entry, an autostart entry and
two systemd user units (the daemon and the sidebar), and installs Sidelit's Claude Code plugin
and the Chrome bridge.
`uninstall.sh` takes all of that away again and leaves your layout alone. The line above and a
package do not mix: to go from a package to the line, remove the package first.

Or build the packages yourself: `./package.sh deb` or `./package.sh arch` (into `dist/`).

### The GTK 4 sidebar (optional)

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

### Chrome windows and their tabs

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

### The Claude Code plugin

Sidelit's side of Claude Code is an ordinary Claude Code plugin, `sidelit`, in the `plugin`
folder of the checkout (`/usr/share/sidelit/plugin` for a package); the folder above it is its
marketplace. `sidelit hooks install` installs it with Claude Code's own commands, for your user:

```bash
claude plugin marketplace add ~/.local/share/sidelit    # or /usr/share/sidelit
claude plugin install sidelit@sidelit --scope user
```

Claude Code keeps track of it from then on (`/plugin` lists it; you can disable it there) and
loads it in place, so updating Sidelit updates the plugin. Sessions started from then on report
to Sidelit; one already running does after `/reload-plugins`. `sidelit hooks status` says
whether it is in, and `sidelit hooks uninstall` takes it out again (and the marketplace).

It carries two things:

- **A hook** (`plugin/hooks/hook.py`, standard library only) for the session's events: start,
  prompt, tool use (async, so Claude never waits for it), permission requests, notifications,
  the end of a turn (and a turn that failed), subagents, the session's end. It writes the
  session's state to `$XDG_RUNTIME_DIR/sidelit/claude/` and nudges the daemon.
- **A mod** (`plugin/hooks/usage.js`) that writes your plan's usage limits, as Claude Code
  measures them after each turn, to `$XDG_RUNTIME_DIR/sidelit/usage.json`: for each window how
  much is used and when it resets, nothing else. Mods need Claude Code 2.1.287 or later;
  `plugin/tests` has its tests (`claude plugin test plugin`).

Sidelit 0.5 wrote its hook into `~/.claude/settings.json` instead. The first `sidelit start`
after updating installs the plugin, and once it is in takes those entries out (and only them:
your own hooks stay, and the file as it was is kept as `settings.json.before-sidelit`).

Removing Sidelit, or going back to 0.5: run `sidelit hooks uninstall` first (`uninstall.sh`
does it for a checkout), so Claude Code does not keep a plugin whose folder is gone. If the
package is already removed, the same by hand: `claude plugin uninstall sidelit@sidelit`, then
`claude plugin marketplace remove sidelit`. Sidelit 0.5's `sidelit hooks install` puts its old
hook back.

## How to run

```bash
sidelit                # start the daemon and the sidebar (also runs at login)
sidelit stop|restart|status|log
sidelit why TEXT       # why a window was kept, moved or forgotten (TEXT: a workspace, title, app)
sidelit restore        # start what is missing from the layout now (runs by itself after a login)
sidelit reapply        # put every window back on its workspace and screen
sidelit state|layout   # what the daemon sees / keeps, as JSON
sidelit ws list|toplevels|activate N|new NAME|pin N|unpin N|move A before|after B|movewin TITLE N [OUTPUT]
sidelit open URL       # open a link on the current workspace (see below)
sidelit hooks install|uninstall|status   # Sidelit's Claude Code plugin
sidelit chrome install|uninstall|status  # the Chrome extension's bridge; which profiles are connected
```

## What it does

- **Every workspace is a tab.** A workspace with agent sessions is a card with a box for each
  session, in the order they started: its own title, the model and a state stripe, under a
  header naming the workspace's folder (or the name you gave it). Sessions in terminals of their
  own and in split panes or tabs of one terminal window all show. Plain tabs (dim, italic) show
  what is open on them. The active workspace has the cyan line.
- **Click** a tab to switch; keyboard focus lands in the session that has waited longest for
  you, else the one you used last. Switching with the workspace's own shortcut does the same
  when a session there needs you. **Click a box** to go to that session's terminal, and its pane
  in tmux, WezTerm and kitty (with remote control on); Terminator offers no way to focus a pane
  from outside, so there its window comes forward. Each tab shows its switch shortcut (COSMIC's
  default Super+1…9, Super+0 for the last; on KDE, the key bound to "Switch to Desktop N").
- **`+`** pins a fresh workspace (on KDE, adds a desktop) and opens a terminal in your home
  directory, or whatever the template you pick says. Start `claude` there as usual and the tab
  picks up its title within seconds.
- **Fork** icon: like `/fork`, opens `claude --resume <id> --fork-session` in a new workspace in
  the same directory.
- **×** closes the whole workspace: every window on it gets a normal close request (after a
  confirmation listing what will close), the workspace is unpinned and disappears once empty (on
  KDE, the desktop is removed once its windows have closed). A session that was there stays in
  *Recent sessions*. A kept workspace that is not on the desktop shows as "closed – click to
  reopen": one click brings it and its windows back.
- **Drag** a tab onto another to reorder the workspaces themselves (COSMIC; KWin cannot reorder
  its desktops). **Double-click** (or right-click → Rename) to name a plain workspace; a
  session's title always wins.
- **Status, straight from the agent.** The hook in Sidelit's Claude Code plugin (above) makes
  every session report what it is doing as it happens: working, waiting for a permission or an
  answer, done. That is its main thread; the subagents it runs are dots beside the model, one
  each, and so is a session it started itself, which never gets a box. It also says which
  terminal it lives in: by process ancestry for a session the daemon started (or whose window's
  process the desktop names), else by which window had the keyboard when you submitted a prompt;
  and which pane, where the terminal says (Terminator, tmux, WezTerm, kitty). Only without the plugin (or before a
  session's first event) is the terminal title matched against Claude Code's list of running
  sessions (`claude agents --json`, asked every few seconds), which cannot tell two sessions with
  the same title apart. The two tool-call events run async, so the agent never waits for the hook.
- **Yellow means the agent needs you.** A session's box goes yellow and bold, and its card glows,
  when it hands back to you while you are not typing in its own terminal (or pane: the one whose
  title the window shows), until you go to it. A finished turn is checked first:
  a model (Sonnet by default) reads its end (what you asked, the last tool calls, what the agent said) and says
  whether it needs you (it is done, stuck or asking) or is only waiting on something to finish,
  such as a CI pipeline, a deploy or a background job. One that only waits stays orange and
  still, with `waiting on CI pipeline` under its title; it gets its next check when it wakes up
  and ends another turn. One that has nothing to wake it (no watcher, background job or
  scheduled check, only "I'll check the pipeline") turns yellow after 20 minutes, since it will
  only go on when you nudge it. The check takes a second or two, and the tab keeps breathing
  orange meanwhile. If it fails or takes more than a minute, the tab turns yellow anyway. A turn
  that ends while you are typing in its terminal is not checked, unless it left a shell running
  (below). The check runs `claude -p` on your subscription (no API key), with no tools and no
  session saved; one process is started ahead while a session works. Config: `check_turns`
  (false = yellow at every turn end), `check_model`. `sidelit why turn_checked` lists the
  verdicts (`why TITLE` one session's), and every tab that turned yellow without one.
- **Working, and waiting.** The orange stripe breathes while the agent works, and stays orange
  without breathing while its turn is over but it waits on something (the check above says
  what). A turn that leaves a shell running in the background ("1 shell still running") is
  always checked, even on the tab you are looking at: a session waiting for that deploy stays
  orange, one that is done and only left a dev server running is not. Only with the check off
  does a running shell alone keep the stripe orange (`shell still running`). A session stopped
  on a permission prompt or a question gets a steady yellow stripe and `waiting for you`.
- **One layout, kept by a daemon.** `~/.config/sidelit/layout.json` is the source of truth: the
  kept workspaces in order, and on each the windows that belong there (a *slot* per window: the
  app, the screen it is on, and what the app lets us remember: the session and directory of a
  terminal, the folder of a file manager, the URL of a web app). Every workspace you work on is
  kept (pinned, so it survives a reboot) once a window has been open on it for a moment; one you
  empty yourself is let go again, the way COSMIC drops an empty workspace (a KDE desktop itself
  stays: only closing it from the sidebar removes it). One you named stays,
  and so does one whose windows are only missing (an app crashed, a screen went away). The daemon
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
  session resumed with `claude --resume` in a terminal on its workspace, terminals in their
  directory, file managers on their folder, web app windows on their URL, one at a time, each
  waiting for its window. Apps that restore their own windows (Chrome, VS Code) are started once
  and their windows placed as they appear, matched by title. Then it switches back to the
  workspace you were on. Nothing is started in the middle of a session unless you ask (header
  menu, or `sidelit restore`). A window that fails to come back three logins in a row is dropped.
  Config: `restore_on_login`, `restore_windows` (false = sessions only), `restore_delay`,
  `ignore_apps`.
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
- **Header menu.** Right-click the header for "Put windows back on their workspaces", "Reopen
  missing windows", *Recent sessions* (closed ones, one click resumes one on a new workspace)
  and "Restart sidebar". Right-click a tab to forget its missing windows.
- **Workspace history.** The clock button in the header opens the workspaces you closed, newest
  first under a heading per day, each with when, its folder and what else was open on it. The
  search box on top matches titles, windows and directories (Enter opens the first match). Pick
  one and it comes back on a new workspace: its name and directory, its session resumed, its
  terminals, folders and web apps reopened one at a time. The last 100 are kept.
- **Usage.** Your plan's 5-hour and weekly limits sit at the bottom (on a Pro or Max plan), as
  Claude Code measured them after the last turn of any session, through the plugin's mod. They
  appear after the first turn, the last ones seen stay through a login, and a window whose reset
  has passed goes. Sidelit never reads your Claude login for them.
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
- **Updates.** When a newer release is out, an "Update to X.Y.Z" button appears under the usage
  bars. It opens a terminal that pulls the checkout (or, for a package install, downloads the
  package and installs it with apt or pacman, asking for your password there) and restarts the
  sidebar. Nothing is installed behind your back.
- **Transcript safety.** Claude Code's session markers (`CLAUDE_CODE_CHILD_SESSION`, …) are
  scrubbed from every terminal the sidebar opens, so sessions started there save transcripts.

### Other sources

Sessions that don't run in a terminal on this desktop (a web app's agent jobs, a CI board,
another machine's agents) can show up too, as their own group under the workspaces. Add one
file per source to `~/.config/sidelit/sources/` (yours, never part of this repo):

```json
{
  "name": "My jobs",
  "url": "http://127.0.0.1:8080/feed",
  "token_file": "~/.local/share/my-app/feed-token",
  "every": 10,
  "report": "http://127.0.0.1:8080/report"
}
```

`token_file` is optional (its contents go out as `Authorization: Bearer …`, read anew each
time, so a new token just works); `every` is the seconds between fetches (at least 3). The URL
answers with:

```json
{"items": [{"id": "1", "title": "Port the login screen", "state": "waiting",
            "detail": "May it push the branch?", "url": "https://…", "updated": 1791016000}]}
```

`state` is one of `working`, `waiting`, `idle`, `done`, `failed`, and picks the stripe colour
(waiting is yellow, failed red). What waits for you is listed first. Clicking an entry opens
its `url` on the current workspace; only http(s) links are fetched, posted to or opened. A
source that can't be reached says so under its heading and is tried again on its next turn.

**An item on its own workspace.** Right-click an entry → "Open on its own workspace": a new
kept workspace named after it, with its `url` open there as a web app window. Its tab is drawn
from the item, like a session's: its title, the stripe by its state, the state and `detail`
under it, and it turns yellow and breathes when the item starts waiting for you while you are
on another workspace. Whatever else you open there is kept and restored like on any workspace.
While it has its workspace, its tab stands for it and the group leaves it out. An item that
leaves the feed keeps its tab, drawn as a plain one; closing the workspace ends the link, and
the group lists the item again. An item may carry `"folder"`, a directory under your home: its
workspace works there ("Open terminal here" and the like).

**Every item on its own workspace.** With `"workspaces": true` a source has no list: each item
gets its workspace by itself once it is working or waiting, without taking your screen; its
page opens when you first go there. Close one and it stays closed for that item. `"only"` keeps
the items whose fields have the given values, e.g. `{"here": true}` for a feed that marks what
runs on this machine; it applies to the list and to the workspaces.

**An item in a terminal.** With `"terminal": "my-tool open {id}"`, an item's workspace opens a
terminal running that command in the item's folder instead of its page, and after a login it
comes back running it again. `{url}` is the item's link, e.g. to open its page when the command
fails: `"my-tool open {id} || sidelit open --app {url}"`. The command is yours, from your file;
the feed only supplies the id and the link, and only plain ones (an id of letters, digits, `.`,
`_`, `:`, `-`; a link without `?`, `&` or anything else a shell reads).

**Reporting back.** With `report`, the daemon sends this desktop's sessions there as a POST of
the same JSON (`{"items": [...]}`, with the same token), a couple of seconds after one changes
and every `every` seconds anyway. One item per session (several on one workspace are an item
each): `id` its session, `title` its box's title, `state` `waiting` while the box is yellow,
`working` while it is orange (busy, a shell running, waiting on CI) and `done` otherwise,
`detail` the note under the title ("waiting on CI pipeline"), `updated` when that last changed;
no `url`. Sessions on workspaces opened for an item are left out. A receiver that fails is logged once and tried again `every` seconds later; it
never holds up the daemon.

## Config

`~/.config/sidelit/config.json`, written with the defaults on the first start; `sidelit reload`
re-reads it.

- `width`: the sidebar's width in pixels.
- `monitor`: the sidebar's screen, `leftmost`, `largest`, or a model or maker substring.
- `sidebar`: `auto`, `gtk4` or `gtk3` (see the GTK 4 sidebar above).
- `terminal` (a command with `{cwd}`) and `terminal_cmd` (with `{command}` too): the terminal
  the sidebar opens. The default is the first of Terminator, Konsole, GNOME Console and xterm
  that is installed.
- `browser`: the executable `sidelit open` uses for a new window.
- `templates`, `default_template`: what `+` can open (below).
- `web_apps`: one-click web app windows, as `[{"name": …, "url": …}]`.
- `restore_on_login`, `restore_windows`, `restore_delay`, `relaunch`: the restore after a login
  and after a crash.
- `ignore_apps`: app ids never kept in the layout.
- `check_turns`, `check_model`: the check before a tab turns yellow.

### What `+` opens: templates

`+` lists the templates by name (with only one, it opens that one straight away). A template
names what to start on the new workspace, in order; the defaults are a Claude Code session on
its own, with a browser window, with VS Code, and an empty terminal. This one opens a session
next to a browser window on GitHub:

```json
{"name": "Claude Code + browser", "steps": [{"type": "claude"}, {"type": "app", "exec": "{browser} https://github.com"}]}
```

- `{"type": "claude"}` is a terminal with `claude` in it, and the tab follows it. A
  `{"type": "terminal"}` step is a shell, or with `"command": "..."` runs that; an
  `{"type": "app", "exec": "..."}` step runs any command line.
- An `exec` line can use `{cwd}`, the workspace's directory, and `{browser}`, your default
  browser set to open a *window of its own*.

## How it works

Two processes, both systemd user services that `sidelit start` launches (after recording the
few session variables systemd lacks, such as the input method):

- **The daemon** (`sidelit/daemon.py`) owns the connection to the compositor and the layout. It
  has no surfaces, so screens coming and going cannot kill it, and systemd restarts it if
  anything else does. It observes the desktop on every change and once a second and steps the
  engine. The turn checker (`turncheck.py`) runs in it: before a tab turns yellow, it asks a
  model (a `claude -p` it starts ahead) whether the session needs you or only waits.
- **The sidebar** is a view: it draws the state the daemon pushes over
  `$XDG_RUNTIME_DIR/sidelit/daemon.sock` and sends a command for everything you do. It keeps
  nothing, so it can crash or restart without losing anything. What it does is
  `sidebar_common.py`; the drawing is GTK 4 (`sidebar4.py` picks it and loads gtk4-layer-shell,
  `sidebar4_ui.py` draws) or, without gtk4-layer-shell, GTK 3 (`sidebar.py`).

The engine (`sidelit/core/`, pure Python, tested against a fake desktop in `tests/`):

- `model.py`: the observed desktop and the layout (workspaces, slots).
- `engine.py`: the recorder (a change while calm is you: the layout follows; a change while
  disturbed is drift: the layout stays) and the reconciler (a window bound to a slot that is not
  on its slot's workspace and screen is moved there; the moves it makes are expected, so the
  recorder never mistakes them for you). Window identifiers are unique for a whole compositor
  session, so after a daemon restart every window finds its slot again without guessing; after a
  login, windows are recognised by what the adapters know.
- `adapters.py`: one class per kind of app, same interface: what to remember about its windows,
  how sure a window is a remembered one, how to start it again, and the policy (started per
  window or once per app, whether a closed window keeps its place). Adding an app is one
  subclass.
- `store.py`: atomic saves, five rotating backups, a broken file set aside instead of
  overwritten.
- `journal.py`: every decision, for `sidelit why`.

`sidelit/wire.py` is a small pure-Python Wayland client that loads the protocol XML in
`sidelit/protocols/` at runtime. `wsclient.py` uses it to talk to cosmic-comp over
`ext-workspace-v1` + `cosmic-workspace-v2` (list, pin, activate, reorder workspaces) and
`ext-foreign-toplevel-list-v1` + `cosmic-toplevel-info-v1` + `cosmic-toplevel-management-v1`
(windows, titles, which workspace they are on, focus, close, and `move_to_ext_workspace` to put
a window on a workspace). On KDE, `kwin.py` talks to KWin over the session bus: virtual desktops
from `org.kde.KWin /VirtualDesktopManager` (list, add, rename, remove, switch), and windows from
a small KWin script it loads, which calls the daemon (bus name `io.github.kulaxa.sidelit`) with
every window whenever one opens, closes, gets the keyboard or changes its title, desktop or
screen. A KWin script cannot be called, so focusing, closing and moving a window are each a
script run once. `sidelit ws` and `sidelit open` get a single report on a bus name of their own,
so they never take the daemon's. `backend.py` picks the client for the desktop
(`XDG_CURRENT_DESKTOP`; `SIDELIT_BACKEND=cosmic|kde` overrides it): every client has the same
interface (`model.py` has the shapes), and what one cannot do is left out of its `caps`, so the
sidebar hides those controls.

Session state comes from Sidelit's Claude Code plugin (each session's events, and the usage
limits), from `claude agents --json` (Claude Code's list of running sessions, which its docs
call the supported way to read session state from outside it) and from the transcripts in
`~/.claude/projects/` (the session's title and model). Nothing else of Claude Code's is read,
and nothing of it is written but through its own `claude plugin` commands (apart from taking
out, once, the hook Sidelit 0.5 had written into `~/.claude/settings.json`).

COSMIC notes: only pinned workspaces have a stable id, workspace `rename` is ignored on COSMIC
1.0 (names live in the layout), and toplevel → workspace mapping arrives on the compositor's next
refresh tick. cosmic-comp keeps one workspace set per screen and mirrors them by index; each
screen's copy of an index can carry a pinned id of its own, so a kept workspace learns every id
its copies carry, and an unplugged screen's copies (appended as extra workspaces) are recognised
as extras: their windows go home and they are unpinned. COSMIC can report a window's position
but not set it, so what is restored is the workspace, the screen and the window itself, not its
exact position.

KDE notes: every desktop has a stable id and lasts until it is removed, so nothing is pinned; a
workspace the layout lets go (forgotten, or emptied) leaves its desktop alone, and only closing
it from the sidebar removes the desktop, once its windows have gone. A window on all desktops
belongs to none of them. The windows' pids come with them, which helps match a terminal to its
Claude session.

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

## Packaging

`package.sh` owns the installed layout (`/usr/bin/sidelit`, `/usr/share/sidelit/`,
`/usr/lib/systemd/user/`, `/usr/share/applications/`, `/etc/xdg/autostart/`); `debian/rules`
and `PKGBUILD` both call it. CI (GitHub Actions, `.github/workflows/ci.yml`) runs the tests and
builds the .deb and the Arch package on every push and, when `VERSION` is bumped on main, cuts a
`v<version>` GitHub release with both attached.

## Tests

```bash
python3 -m unittest discover -s tests
```

The engine against a fake desktop that behaves like cosmic-comp where it matters: closing
windows (one, a logout cascade, an app crash, a tray app), moving them, unplugging and
replugging a screen, daemon restarts, a login restore. The KDE backend against a fake KWin on a
private session bus (skipped without PyGObject and `dbus-daemon`). To watch the real daemon
decide without letting it act, run a second one in dry-run mode on a copy of your config:
`SIDELIT_HOME=/tmp/sidelit-test SIDELIT_DRY_RUN=1 python3 -m sidelit daemon`.

On a real KWin (a KDE session you can spare, or a headless `kwin_wayland --virtual` with a
private session bus), `tests/kwin_live.py` checks the KDE backend and `tests/kwin_daemon_live.py`
a test daemon on it; both open and close windows and desktops of their own.

## Licence

MIT, see [LICENSE](LICENSE). Copyright 2026 Mario Jalšovec.
