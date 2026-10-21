# Config

`~/.config/sidelit/config.json`, written with the defaults on the first start; `sidelit reload`
re-reads it.

- `width`: the sidebar's width in pixels.
- `monitor`: the sidebar's screen, `leftmost`, `rightmost`, `largest`, or a model or maker
  substring.
- `side`: the screen edge it docks at, `left` or `right`. The settings (the gear) pick both, the
  side and, with more than one screen, the leftmost or rightmost one; the sidebar moves there at
  once. On X11 maximised windows stop beside it only on the outer edges of all your screens
  together (the left of the leftmost one, the right of the rightmost one); elsewhere they can go
  under it, and the sidebar says so. On GNOME, after an update from a Sidelit that had no `side`,
  it stays on the left until you log out and back in, which loads the updated extension.
- `sidebar`: `auto`, `gtk4` or `gtk3` (see [the GTK 4 sidebar](install.md#the-gtk-4-sidebar-optional)).
- `theme`: the sidebar's look, also picked in the settings (the gear at the top of the sidebar),
  which saves it here and changes the look at once: `default` (the usual look) or `halloween`
  (a cobweb and its spider, bats across the moon, a jack-o'-lantern and usage bars of dripping
  blood). A theme of your own is a CSS file,
  `~/.config/sidelit/themes/<name>.css`, drawn over the sidebar's own CSS (its classes are in
  `sidelit/sidebar4_ui.py`); it shows in the settings by its name. `SIDELIT_THEME=<name>` in the
  sidebar's environment overrides the config.
- `terminal` (a command with `{cwd}`) and `terminal_cmd` (with `{command}` too): the terminal
  the sidebar opens. The default is the first of Terminator, Konsole, GNOME Console, kitty,
  foot, Alacritty, GNOME Terminal, Xfce's and MATE's terminals and xterm that is installed.
- `browser`: the executable `sidelit open` uses for a new window.
- `templates`, `default_template`: what `+` can open ([below](#templates)).
- `web_apps`: one-click web app windows, as `[{"name": …, "url": …}]`.
- `restore_on_login`, `restore_windows`, `restore_delay`, `relaunch`: the restore after a login
  and after a crash. `restore_on_login` and `relaunch` are switches in the settings too: off,
  Sidelit brings nothing back by itself (the settings' and the header menu's buttons still do).
- `ignore_apps`: app ids never kept in the layout.
- `check_turns`, `check_model`, `check_model_codex`, `check_model_opencode`: the check before a
  tab turns yellow (each agent's sessions are checked through that agent; `check_model_codex`
  empty is your Codex's own model, `check_model_opencode` empty your OpenCode's, as
  `provider/model`).

Sessions running elsewhere (a web app's agent jobs, a CI board, another machine's agents) are
set up in files of their own: see [Other sources](sources.md).

## Templates

What a new workspace opens. `+` lists the templates by name (with only one, it opens that one straight away). A template
names what to start on the new workspace, in order; the defaults are a Claude Code session on
its own, with a browser window, with VS Code, and an empty terminal. This one opens a session
next to a browser window on GitHub:

```json
{"name": "Claude Code + browser", "steps": [{"type": "claude"}, {"type": "app", "exec": "{browser} https://github.com"}]}
```

- `{"type": "claude"}` is a terminal with `claude` in it, and the tab follows it;
  `{"type": "codex"}` the same with `codex`, `{"type": "opencode"}` with `opencode`. A
  `{"type": "terminal"}` step is a shell, or with `"command": "..."` runs that; an
  `{"type": "app", "exec": "..."}` step runs any command line.
- An `exec` line can use `{cwd}`, the workspace's directory, and `{browser}`, your default
  browser set to open a *window of its own*.
