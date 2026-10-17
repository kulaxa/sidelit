# Sidelit CHANGELOG

Entries are a line or two: what changed and what to expect from it. These are
what a release page shows, so write them for whoever is deciding whether to
update. The reasoning belongs in the commit message.

## [Unreleased]

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
