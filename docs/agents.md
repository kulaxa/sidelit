# Coding agents

Sidelit works with Claude Code (✳), Codex (◆) and OpenCode (◎). It plugs into each as a plugin,
installed the way that agent installs plugins: every session reports what it is doing as it
happens (Codex's once you [trust its hooks](#codex)), and the agent's own measure of your plan's usage limits, where it has one, comes along
for the bars. `sidelit hooks install` puts the plugin in every agent it finds,
`sidelit hooks status` says where it is in, and `sidelit hooks uninstall` takes it out again.

## What Sidelit reads and writes

For Claude Code, session state comes from Sidelit's plugin (each session's events, and the
usage limits), from `claude agents --json` (Claude Code's list of running sessions, which its
docs call the supported way to read session state from outside it) and from the transcripts in
`~/.claude/projects/` (the session's title and model). Nothing else of Claude Code's is read,
and nothing of it is written but through its own `claude plugin` commands (apart from taking
out, once, the hook Sidelit 0.5 had written into `~/.claude/settings.json`). For Codex, from
the plugin's events, its process list (`/proc`), and its app-server (`codex app-server`, over
stdio: a session's name, the usage limits, whether you trusted the hooks); nothing of Codex's is
written but through its own `codex plugin` commands. For OpenCode, from the plugin and its
process list (`/proc`; and `opencode --version`, which OpenCode it is), and only the plugin's
file in OpenCode's plugin folder is written. Your logins are never read.

## Claude Code

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

## Codex

The same plugin folder has a Codex manifest (`plugin/.codex-plugin/plugin.json`), whose hooks
(`plugin/hooks/codex.json`) run the same hook script. Codex reads Sidelit's marketplace too, and
`sidelit hooks install` installs it with Codex's own commands:

```bash
codex plugin marketplace add ~/.local/share/sidelit    # or /usr/share/sidelit
codex plugin add sidelit@sidelit
```

**Codex runs a plugin's hooks only once you have trusted them.** The next time it starts, it
asks you to review Sidelit's hooks: trust them there (or later with `/hooks` in Codex).
`sidelit hooks status` says whether you have. Until then a Codex terminal still gets a box (◆,
named after its project, working while Codex's spinner turns in its title), but no more than
its title says. Codex keeps a copy of the plugin, so `sidelit
start` refreshes that copy after an update; trust stays, since the hooks themselves do not
change. A Sidelit installed before Codex was supported puts the plugin in at its first start
after the update (once: take it out with `sidelit hooks uninstall` and it stays out).

What Codex offers differs a little from Claude Code:

- Its interactive sessions run in its shared background server (unless you start `codex
  --no-daemon`), which runs their hooks too, so the hook cannot tell which terminal a session is
  in. Sidelit finds it: the `codex` that was started for that session (`codex resume <id>`, as a
  restore does), else the one in the session's folder that started last before the session did.
  Two Codex sessions in one folder, in two terminals, can be confused when you start a new one
  inside the older terminal (`/new`).
- It has no event for "a question for you", only for a permission: a Codex session asking you
  something shows done, and the turn check says whether it needs you.
- Its terminal's title is the project's name, not the session's: a box's title is the name you
  gave the session (`/rename`), else what you first asked in it.
- The turn check runs `codex exec --ephemeral` (no tools, no hooks or plugins, nothing saved),
  on the model your Codex is set to unless config `check_model_codex` names one, and reads the
  turn from what the hook kept of it (what you asked, the commands it ran, what it said last):
  Codex says its transcript "isn't a stable interface".
- The usage bars come from Codex's app-server (`account/rateLimits/read`), asked every five
  minutes and soon after a turn ends; they need you logged in to Codex with ChatGPT.

Removing it: `sidelit hooks uninstall` (or by hand, `codex plugin remove sidelit@sidelit`, then
`codex plugin marketplace remove sidelit`).

## OpenCode

Two OpenCodes are in use, and Sidelit works with both: OpenCode 1 (1.18.29 or later; what
`curl -fsSL https://opencode.ai/install | bash` and npm's `opencode-ai` install) and OpenCode 2
(what `https://opencode.ai/v2/install` installs, and Arch's `opencode` package).
`opencode --version` says which you have.

OpenCode's plugins are JavaScript files in its plugin folder, which it loads by itself.
Sidelit's is one file for both, `plugin/opencode/sidelit.js`, with no dependencies; `sidelit
hooks install` copies it there, as OpenCode's docs say to install one:

```bash
cp ~/.local/share/sidelit/plugin/opencode/sidelit.js ~/.config/opencode/plugins/    # or /usr/share/sidelit/…
```

(`$XDG_CONFIG_HOME/opencode/plugins` if you set that.) A file of that name that is not Sidelit's
is left alone. OpenCode asks nothing before it runs a plugin, so there is no step to trust it.
OpenCode 1 loads it into each `opencode` as it starts: sessions started from then on report to
Sidelit, one already running once you start it again. OpenCode 2 runs it in its background
server, which loads it as soon as the file is there, so its sessions report at once. It is a
copy, so `sidelit start` (and `restart`) refreshes it after an update, and a Sidelit installed
before OpenCode was supported puts it in at its first start after the update (once: take it out
with `sidelit hooks uninstall` and it stays out). Nothing of OpenCode's settings is touched.

The plugin writes what each session does to `$XDG_RUNTIME_DIR/sidelit/opencode/`, the same
record the hook writes for the other agents. What each of OpenCode's events means is one table
in the file, with both OpenCodes' names: OpenCode 2 names some anew (a turn is
`session.execution.*`, a question a form).

What OpenCode offers differs a little from Claude Code:

- Its terminal's title is `OC | ` and the session's title (`OpenCode` with none open), so a
  box's title is the session's, and a terminal with an OpenCode session open gets a box (◎) even
  without the plugin, though with no more than its title says.
- OpenCode 2's terminals share one background server (the first `opencode` starts it), which
  runs the plugin, so the plugin cannot tell which terminal a session is in. Sidelit finds it, as
  for Codex: the `opencode` that was started for that session (`opencode -s <id>`, as a restore
  does), else the one in the session's folder that started last before the session did. Two
  OpenCode sessions in one folder, in two terminals, can be confused when you start a new one
  inside the older terminal.
- A session opened with `opencode -s <id>` (as a restore after a login does) is on screen at
  once, before you type in it.
- Its subagents run as sessions of their own; Sidelit counts them as the session's blocks, and a
  permission or a question one of them asks makes the session wait for you.
- One `opencode` window can switch between sessions (`/sessions`, and OpenCode 2's tabs): its
  box stays with the session started there last.
- Fork: the new terminal runs Sidelit's `opencode-fork`, which asks the `opencode` your shell
  finds which OpenCode it is. OpenCode 1 opens `opencode -s <id> --fork`. OpenCode 2's terminal
  cannot fork, so its server is asked to (`opencode api POST /api/session/<id>/fork`) and the
  copy opens; if it cannot (a session with nothing in it yet), it says so and a new session
  starts there.
- The turn check runs `opencode run` with every tool off and every permission refused, and no
  plugins but OpenCode's own, Sidelit's neither (OpenCode 1: `--pure`; OpenCode 2: on a server of
  its own, `--standalone`, so Sidelit never starts your background one). It uses the model your
  OpenCode is set to unless config `check_model_opencode` names one, so your own provider
  (`check_turns: false` turns the check off). Its session is deleted after, so it is not among
  yours. The turn is read from what the plugin kept of it (what you asked, the tools it called,
  what it said last).
- It has no plan with usage limits of its own (it works with whatever provider you give it), so
  there are no usage bars for it.

Removing it: `sidelit hooks uninstall`, or by hand, `rm ~/.config/opencode/plugins/sidelit.js`.
