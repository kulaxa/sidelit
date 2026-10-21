# How it works

For whoever wants to read the code, package it or run its tests.

## Two processes

Two processes, both systemd user services that `sidelit start` launches (after recording the
few session variables systemd lacks, such as the input method):

- **The daemon** (`sidelit/daemon.py`) owns the connection to the compositor and the layout. It
  has no surfaces, so screens coming and going cannot kill it, and systemd restarts it if
  anything else does. It observes the desktop on every change and once a second and steps the
  engine. The turn checker (`turncheck.py`) runs in it: before a tab turns yellow, it asks a
  model (through the session's agent, a `claude -p`, `codex exec` or `opencode run` it starts
  ahead) whether the session needs you or only waits.
- **The sidebar** is a view: it draws the state the daemon pushes over
  `$XDG_RUNTIME_DIR/sidelit/daemon.sock` and sends a command for everything you do. It keeps
  nothing, so it can crash or restart without losing anything. What it does is
  `sidebar_common.py`; the drawing is GTK 4 (`sidebar4.py` picks it and loads gtk4-layer-shell,
  `sidebar4_ui.py` draws) or, without gtk4-layer-shell, GTK 3 (`sidebar.py`). On GNOME the
  extension runs it instead of systemd (its log is in the journal as `sidelit-sidebar`). On X11
  it is the GTK 3 one, as a dock window (`xdock.py`).

## The engine

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

## Talking to each desktop

`backend.py` picks the client for the desktop (`XDG_CURRENT_DESKTOP`, else the compositor's own
socket variable, and `XDG_SESSION_TYPE=x11` for X11;
`SIDELIT_BACKEND=cosmic|kde|gnome|hyprland|niri|sway|x11` overrides it): every client has the
same interface (`model.py` has the shapes), and what one cannot do is left out of its `caps`, so
the sidebar hides those controls. What each desktop's quirks mean for you is in
[Desktops](desktops.md#notes-per-desktop).

- **COSMIC.** `sidelit/wire.py` is a small pure-Python Wayland client that loads the protocol
  XML in `sidelit/protocols/` at runtime. `wsclient.py` uses it to talk to cosmic-comp over
  `ext-workspace-v1` + `cosmic-workspace-v2` (list, pin, activate, reorder workspaces) and
  `ext-foreign-toplevel-list-v1` + `cosmic-toplevel-info-v1` + `cosmic-toplevel-management-v1`
  (windows, titles, which workspace they are on, focus, close, and `move_to_ext_workspace` to
  put a window on a workspace).
- **KDE.** `kwin.py` talks to KWin over the session bus: virtual desktops from
  `org.kde.KWin /VirtualDesktopManager` (list, add, rename, remove, switch), and windows from a
  small KWin script it loads, which calls the daemon (bus name `io.github.kulaxa.sidelit`) with
  every window whenever one opens, closes, gets the keyboard or changes its title, desktop or
  screen. A KWin script cannot be called, so focusing, closing and moving a window are each a
  script run once. `sidelit ws` and `sidelit open` get a single report on a bus name of their
  own, so they never take the daemon's.
- **GNOME.** `gnome.py` talks to Sidelit's GNOME Shell extension (`gnome-extension/`, uuid
  `sidelit@kulaxa.github.io`): `desktop.js` exports `io.github.kulaxa.Sidelit.Shell` at
  `/io/github/kulaxa/Sidelit` on GNOME Shell's own bus connection (`GetState`, a `Changed`
  signal, and the workspace and window actions), and `sidebar.js` starts the sidebar while the
  daemon holds `io.github.kulaxa.sidelit`, the way Desktop Icons NG runs its window: GNOME then
  knows that window as its own and makes it a dock (every workspace, above windows, out of the
  overview, the dash and Alt+Tab), and an invisible strip at its edge (left or right), counted like the top
  bar, keeps windows beside it.
- **Hyprland.** `hyprland.py` talks to Hyprland's two sockets (in
  `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/`): `.socket.sock` for requests
  (`j/clients`, `j/workspaces`, `j/monitors`, and the dispatchers that act) and `.socket2.sock`,
  whose event lines the daemon watches.
- **niri.** `niri.py` talks to niri's socket (`$NIRI_SOCKET`): a JSON request a line
  (`Workspaces`, `Windows`, `Outputs`, and the actions), and a connection of its own asking for
  the `EventStream`, whose events the daemon keeps its picture of the desktop from.
- **sway.** `sway.py` talks to sway's socket (`$SWAYSOCK`) in i3's IPC protocol: requests
  (`GET_TREE`, `GET_WORKSPACES`, `GET_OUTPUTS`, and commands) on one connection, and another one
  subscribed to its workspace, window and screen events.
- **X11.** `x11.py` reads the window manager's EWMH hints through python-xlib (desktops, their
  names and the current one, and each window's title, app, process and desktop, all followed as
  they change) and asks it for changes the way a pager does, with client messages to the root
  window; `xdock.py` makes the GTK 3 sidebar a dock on every desktop with
  `_NET_WM_STRUT_PARTIAL` set.

## Talking to each agent

Sidelit knows coding agents only through one interface (`sidelit/agents.py`), with an adapter
per agent: `claude.py` for Claude Code, `codex.py` for Codex, `opencode.py` for OpenCode. Each
says how to recognise the agent's process, how to put Sidelit's plugin in and take it out
(always the way the agent installs plugins), what its sessions are and what they are called, how
to resume and fork one, how to ask it, headless, whether a turn needs you, and what its plan's
usage limits are. The plugin's one hook (`plugin/hooks/hook.py`) turns Claude Code's and
Codex's events into Sidelit's states, with a table per agent; for OpenCode, whose plugins are
JavaScript running inside it, `plugin/opencode/sidelit.js` does the same with a table of its
own and writes the same record. `detect.py`, which works out which window shows which session,
is the same for all of them; where an agent's shared server writes the records (Codex's,
OpenCode 2's), `agents.TerminalClients` finds the agent's process in a terminal that shows each
session.

## Packaging

`package.sh` owns the installed layout (`/usr/bin/sidelit`, `/usr/share/sidelit/`,
`/usr/lib/systemd/user/`, `/usr/share/gnome-shell/extensions/sidelit@kulaxa.github.io/`,
`/usr/share/applications/`, `/etc/xdg/autostart/`); `debian/rules`
and `PKGBUILD` both call it. CI (GitHub Actions, `.github/workflows/ci.yml`) runs the tests and
builds the .deb and the Arch package on every push and, when `VERSION` is bumped on main, cuts a
`v<version>` GitHub release with both attached.

The GNOME Shell extension keeps to extensions.gnome.org's rules (JavaScript only, no binaries,
MIT licensed): `./package.sh gnome-extension` builds the zip to upload there, and each release
carries it too.

## Tests

```bash
python3 -m unittest discover -s tests
```

The engine against a fake desktop that behaves like cosmic-comp where it matters: closing
windows (one, a logout cascade, an app crash, a tray app), moving them, unplugging and
replugging a screen, daemon restarts, a login restore. The KDE backend against a fake KWin, and
the GNOME backend against a fake GNOME Shell with the extension, each on a private session bus
(skipped without PyGObject and `dbus-daemon`); the Hyprland backend against a fake Hyprland's
two sockets, once with a Lua config and once with a classic one; the niri backend against a
fake niri socket with its event stream (`tests/fake_niri.py`), alone and with the engine
driving it (`tests/sim_backend.py`: a login, a screen coming back, a rename in niri); the sway
backend against a fake sway speaking i3's IPC (`tests/fake_sway.py`), alone and with the engine
driving it (`+`, a login, a crash, a screen coming back, a rename in sway); the X11
backend against a fake X server with a small window manager in it (`tests/fake_x11.py`). To
watch the real daemon decide without letting it act, run a second one in dry-run mode on a copy
of your config:
`SIDELIT_HOME=/tmp/sidelit-test SIDELIT_DRY_RUN=1 python3 -m sidelit daemon`.

On a real KWin (a KDE session you can spare, or a headless `kwin_wayland --virtual` with a
private session bus), `tests/kwin_live.py` checks the KDE backend and `tests/kwin_daemon_live.py`
a test daemon on it; both open and close windows and desktops of their own. On a real GNOME
Shell with the extension (a session you can spare, or `gnome-shell --headless` with a private
session bus), `tests/gnome_live.py --sidebar` checks the GNOME backend and the docked sidebar,
`tests/gnome_daemon_live.py` a test daemon (with `--real`, in a throwaway session only, it
clicks and types into the sidebar the extension runs), and `tests/gnome_restore_live.py` a
login restore. On a real Hyprland (a session you can spare, or a nested one), `tests/hyprland_live.py`
checks the Hyprland backend, with two screens also moving workspaces between them, and
`tests/hyprland_daemon_live.py` a test daemon, then (`--restore`, in a fresh Hyprland) that a
login brings a kept workspace back under its number. On a real niri (a session you can spare, or
a nested one: niri started inside another session opens as a window), `tests/niri_live.py`
checks the niri backend and `tests/niri_daemon_live.py` a test daemon, then (`--restore`, in a
fresh niri) that a login brings a kept workspace back under its name; both need foot. On a real
sway (a session you can spare, or a headless one: `WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=2
sway` has two screens), `tests/sway_live.py` checks the sway backend, with two screens also
moving a workspace between them, and `tests/sway_daemon_live.py` a test daemon, then
(`--restore`, in a fresh sway) that a login brings a kept workspace back under its number; both
need foot and swaymsg. On a real X11 window manager (a session you can spare, or Xvfb with xfwm4, Cinnamon, Marco or Openbox),
`tests/x11_live.py` checks the X11 backend and a test daemon; it needs xterm.

The whole of it, on other distributions and desktops: `tests/vm/vm.py` builds test machines
(QEMU with KVM) with Ubuntu and GNOME, Fedora and KDE Plasma, Linux Mint and Cinnamon, and Arch
and Hyprland, which log in by themselves, and drives them from outside. It installs a checkout
there with its own `get.sh`, runs commands in the desktop session, logs out and in, restarts,
takes screenshots, clicks and types; `tests/vm/README.md` says how.

Each agent's adapter is tested against events and answers recorded from the real agent
(`tests/test_claude.py`, `tests/test_codex.py`, `tests/test_opencode.py`, which runs the
OpenCode plugin under Node the way OpenCode 1 and 2 each load it, skipped without Node). With
Codex installed, `tests/codex_live.py`
runs the real Codex CLI, nothing of yours: a throwaway `CODEX_HOME` whose model is a stand-in
on localhost (no account, nothing billed). It installs the plugin with Codex's commands, trusts
its hooks when Codex asks, runs turns in Codex's terminal UI (with its background server and
with `--no-daemon`), and checks what Sidelit makes of them: the session in its own `codex`, its
state, title and model, the turn check through `codex exec`, resume and fork. With OpenCode
installed (1 or 2; `SIDELIT_OPENCODE=` picks a binary), `tests/opencode_live.py` does the same
with the real OpenCode, in a throwaway home whose model is a stand-in on localhost too (OpenCode
2's background server on a port of its own): the plugin in its folder, turns in its terminal UI
(an answer, a command, a permission), the turn check through `opencode run` leaving no session
or record behind, `opencode -s` on screen before you type (also with its record gone, as after a
reboot), and Sidelit's fork.
