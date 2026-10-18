"""One interface, one implementation per kind of app.

An adapter answers, for the windows it claims:
- capture: what is worth remembering about this window (a Claude session, a folder, a URL)
- score:   how sure are we that this window is that remembered slot (0 no idea .. 3 certain)
- launch:  how to bring the slot back if it is missing, as a launch spec the daemon runs
and sets the policy the engine applies to it:
- launch_mode:  "slot" (each slot started on its own: terminals, file managers, app windows),
                "app"  (start the app once, it restores its own windows: browsers, IDEs),
                "none"
- launch_after: seconds into a restore before launching (give self-restoring apps a chance)
- keep_on_close: closing the window keeps the slot (tray apps: the window comes back, the app never left)

Adding an app = subclassing Adapter and listing it in ADAPTERS, ahead of GenericAdapter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from sidelit import chrome
from sidelit.core import titles
from sidelit.core.model import Slot, Win

# a Claude that has not been on screen in its terminal for this long has ended there
CLAUDE_GONE_S = 20.0

IGNORED_APPS = {"", "sidelit","xdg-desktop-portal-cosmic", "xdg-desktop-portal-gtk", "cosmic-osd",
                "polkit-gnome-authentication-agent-1", "org.freedesktop.impl.portal.desktop.cosmic",
                "com.system76.CosmicScreenshot", "cosmic-screenshot"}


@dataclass
class Ctx:
    """What an adapter may know beyond the window itself."""
    claude: dict = field(default_factory=dict)          # window id -> Claude session info
    known_dirs: list = field(default_factory=list)      # directories Claudes run in, for folder names
    resume_command: Callable[[str, str], str] | None = None   # (session, cwd) -> shell command
    ignored: set = field(default_factory=set)            # config ignore_apps
    # (slot, the Claudes of its window that ended: Slot.claudes entries; None: all of them)
    on_session_ended: Callable[..., None] | None = None
    browser: dict = field(default_factory=dict)          # window id -> {"instance", "window", "tabs", "profile"}
    browser_online: set = field(default_factory=set)     # extension instances connected now
    # processes that ended since a time, and whether a crash would be among them (a crash reporter
    # is readable); None: no way to tell an app that crashed from one that quit
    endings: Callable[[float], tuple[list, bool]] | None = None
    # how a browser (by extension instance) last ended: "normal", "crashed" (or running), None: not known
    browser_exit: Callable[[str], str | None] | None = None
    # an app id's executable file name, from its desktop entry
    app_exe: Callable[[str], str | None] | None = None

    def claude_of(self, win: Win) -> dict | None:
        return self.claude.get(win.id)


class Adapter:
    name = "generic"
    launch_mode = "app"
    launch_after = 12.0
    keep_on_close = False

    def claims(self, win: Win, ctx: Ctx) -> bool:
        return bool(win.app) and win.app not in IGNORED_APPS and win.app not in ctx.ignored

    def capture(self, slot: Slot, win: Win, ctx: Ctx, calm: bool, now: float) -> bool:
        """Copy what we remember from the live window into the slot. True if anything changed."""
        changed = False
        if win.title and win.title[:200] != slot.title:
            slot.title = win.title[:200]
            changed = True
        if win.app and win.app != slot.app:
            slot.app = win.app
            changed = True
        return changed

    def score(self, win: Win, slot: Slot, ctx: Ctx) -> float:
        if win.app != slot.app:
            return 0.0
        return titles.match_score(win.title, slot.title)

    def mode(self, slot: Slot, ctx: Ctx) -> str:
        """launch_mode for this slot (an adapter may start some slots on their own)."""
        return self.launch_mode

    def pairable(self, slot: Slot) -> bool:
        """May a window with nothing in common with this slot still be paired with it, when it
        is the only place the window could belong?"""
        return True

    def accepts_launch(self, slot: Slot, win: Win, ctx: Ctx | None = None) -> bool:
        """Is `win` the window a launch for `slot` produced?"""
        return win.app == slot.app

    def may_become_launch(self, slot: Slot, win: Win, ctx: Ctx | None = None) -> bool:
        """May `win`, not accepted yet, still turn into the window a launch for `slot` produces
        (and so be kept from becoming a window of its own meanwhile)?"""
        return False

    def launch(self, slot: Slot, ctx: Ctx) -> dict | None:
        return {"type": "app", "app": slot.app} if slot.app else None

    def describe(self, slot: Slot) -> str:
        app = (slot.app or "?").rsplit(".", 1)[-1].replace("google-", "")
        return f"{app} {slot.title[:40]!r}" if slot.title else app


class TerminalAdapter(Adapter):
    """Terminals, and the Claudes running in one. A terminal is one slot whether or not a
    Claude runs in it; state["session"] is the Claude it shows (resumed on restore),
    state["more"] the other Claudes in the same window (split panes, tabs: each comes back in
    a terminal of its own, Engine._split_claudes), state["cwd"] the directory (from the Claude,
    or from a `user@host: dir` prompt title), state["command"] what it was started to run, if
    that is how it comes back (an other source's item opened in a terminal: its own command
    resumes it, not a bare `claude`)."""
    name = "terminal"
    launch_mode = "slot"
    launch_after = 0.0

    def claims(self, win, ctx):
        return win.app in titles.TERMINAL_APP_IDS

    def capture(self, slot, win, ctx, calm, now):
        # a working Claude animates its title glyph: remember the title without it
        title = (titles.bare_title(win.title) if titles.looks_like_claude(win.title) else win.title) or ""
        changed = False
        if title and title[:200] != slot.title:
            slot.title = title[:200]
            changed = True
        if win.app and win.app != slot.app:
            slot.app = win.app
            changed = True
        st = slot.state
        claude = ctx.claude_of(win)
        if claude:
            had = slot.claudes
            for key, value in (("session", claude.get("session")), ("claude_title", claude.get("title")),
                               ("cwd", claude.get("cwd"))):
                if value and st.get(key) != value:
                    st[key] = value
                    changed = True
            more = [{"session": c["session"], "claude_title": c.get("title") or "", "cwd": c.get("cwd") or ""}
                    for c in claude.get("more") or [] if c.get("session")]
            if more != (st.get("more") or []):
                if more:
                    st["more"] = more
                else:
                    st.pop("more", None)
                changed = True
            now_shown = {c["session"] for c in slot.claudes}
            # fewer Claudes than before: the ones gone ended (one taking another's place is
            # /clear or /resume in the same Claude)
            if len(now_shown) < len(had) and ctx.on_session_ended:
                ctx.on_session_ended(slot, [c for c in had if c["session"] not in now_shown])
            if st.pop("no_claude_since", None) is not None:
                changed = True
            return changed
        prompt = titles.cwd_from_prompt_title(win.title)
        if prompt and st.get("cwd") != prompt and not st.get("session"):
            st["cwd"] = prompt
            changed = True
        if st.get("session") and not titles.looks_like_claude(win.title):
            # Claude exited and left the shell: after a while (and only while nothing is
            # being shuffled around) this terminal is a plain terminal again
            since = st.get("no_claude_since")
            if since is None:
                st["no_claude_since"] = now
            elif calm and now - since >= CLAUDE_GONE_S:
                if ctx.on_session_ended:
                    ctx.on_session_ended(slot)
                for key in ("session", "claude_title", "no_claude_since", "more"):
                    st.pop(key, None)
                if prompt:
                    st["cwd"] = prompt
                changed = True
        elif st.pop("no_claude_since", None) is not None:
            changed = True
        return changed

    def pairable(self, slot):
        return not slot.session  # a Claude comes back in its own terminal, never in a stranger's

    def score(self, win, slot, ctx):
        if not self.claims(win, ctx):
            return 0.0
        claude = ctx.claude_of(win)
        if slot.session:
            # only the same session is this slot; another Claude or a bare shell is not
            return 3.0 if claude and claude.get("session") == slot.session else 0.0
        if claude:
            return 0.0
        prompt = titles.cwd_from_prompt_title(win.title)
        if prompt and prompt == slot.state.get("cwd"):
            return 2.5
        return min(titles.match_score(win.title, slot.title), 1.5)

    def accepts_launch(self, slot, win, ctx=None):
        return win.app in titles.TERMINAL_APP_IDS

    def launch(self, slot, ctx):
        cwd = slot.state.get("cwd") or ""
        if slot.state.get("command"):
            return {"type": "terminal", "cwd": cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~"),
                    "command": slot.state["command"]}
        if slot.session:
            command = ctx.resume_command(slot.session, cwd) if ctx.resume_command else None
            return {"type": "terminal", "cwd": cwd or os.path.expanduser("~"), "command": command}
        if cwd and os.path.isdir(cwd):
            return {"type": "terminal", "cwd": cwd, "command": None}
        return None  # a terminal we know nothing about: keep its place, do not invent one

    def describe(self, slot):
        if slot.session:
            n = len(slot.claudes) - 1
            return describe_claude(slot.claudes[0]) + (f" and {n} more" if n else "")
        return f"terminal in {slot.state.get('cwd') or '?'}"


def describe_claude(c: dict) -> str:
    """A Claude of a slot (Slot.claudes) in a line."""
    return f"Claude {c.get('claude_title') or c['session'][:8]!r}"


class FilesAdapter(Adapter):
    name = "files"
    launch_mode = "slot"
    launch_after = 0.0

    def claims(self, win, ctx):
        return win.app in titles.FILE_MANAGERS

    def capture(self, slot, win, ctx, calm, now):
        changed = super().capture(slot, win, ctx, calm, now)
        folder = titles.folder_from_title(win.app, win.title)
        if folder and slot.state.get("folder") != folder:
            slot.state["folder"] = folder
            path = titles.resolve_folder(folder, ctx.known_dirs)
            if path:
                slot.state["path"] = path
            else:
                slot.state.pop("path", None)
            changed = True
        return changed

    def score(self, win, slot, ctx):
        if win.app != slot.app:
            return 0.0
        folder = titles.folder_from_title(win.app, win.title)
        if folder and folder == slot.state.get("folder"):
            return 3.0
        return titles.match_score(win.title, slot.title)

    def launch(self, slot, ctx):
        path = slot.state.get("path")
        if not path or not os.path.isdir(path):
            return None
        return {"type": "app", "app": slot.app, "paths": [path]}

    def describe(self, slot):
        return f"files {slot.state.get('path') or slot.state.get('folder') or '?'}"


class WebAppAdapter(Adapter):
    """A browser `--app=URL` window (Discord in Chrome, a board, a dashboard): the app id
    carries the URL, so it can be reopened exactly."""
    name = "webapp"
    launch_mode = "slot"
    launch_after = 20.0  # the browser's own session restore may bring it back first

    def claims(self, win, ctx):
        return titles.chrome_app_url(win.app) is not None

    def capture(self, slot, win, ctx, calm, now):
        changed = super().capture(slot, win, ctx, calm, now)
        parsed = titles.chrome_app_url(win.app)
        if parsed and (slot.state.get("url"), slot.state.get("profile")) != parsed:
            slot.state["url"], slot.state["profile"] = parsed
            changed = True
        return changed

    def score(self, win, slot, ctx):
        if win.app != slot.app:
            return 0.0
        return 2.5 + titles.match_score(win.title, slot.title) / 6

    def launch(self, slot, ctx):
        parsed = titles.chrome_app_url(slot.app)
        if not parsed:
            return None
        return {"type": "webapp", "url": parsed[0], "profile": parsed[1]}

    def describe(self, slot):
        return f"web app {(slot.state.get('url') or slot.app).split('//', 1)[-1][:40]}"


class BrowserAdapter(Adapter):
    """Browser windows. With the companion extension (chrome-extension/) connected, a window
    remembers its tabs, is recognised by them, and is reopened tab for tab on its own
    workspace (turn the browser's own "Continue where you left off" off). Without it, the
    browser restores its own windows and we place them by title."""
    name = "browser"
    launch_mode = "app"
    launch_after = 4.0

    def claims(self, win, ctx):
        return win.app in titles.BROWSERS

    def capture(self, slot, win, ctx, calm, now):
        changed = super().capture(slot, win, ctx, calm, now)
        info = ctx.browser.get(win.id)
        if info is None:
            return changed
        st = slot.state
        reported = info.get("tabs") or []
        # a tab that has not loaded yet (a background tab of a window just opened) comes with no
        # URL at all: the window is not done telling us what it holds, so what we know stands
        if any(not t.get("url") for t in reported):
            return changed
        tabs = chrome.clean_tabs(reported)
        if tabs and tabs != st.get("tabs"):
            st["tabs"] = tabs
            changed = True
        for key in ("instance", "profile"):
            value = info.get(key)
            if value and st.get(key) != value:
                st[key] = value
                changed = True
        return changed

    def score(self, win, slot, ctx):
        if win.app != slot.app:
            return 0.0
        info = ctx.browser.get(win.id)
        mine = slot.state.get("tabs")
        if info is not None and mine and info.get("tabs"):
            same = chrome.overlap(info["tabs"], mine)
            if same >= 0.6:
                return 3.0
            if same >= 0.3:
                return 2.5
            if same > 0:
                return 2.0
            return 0.0  # both known and nothing in common: another window
        return titles.match_score(win.title, slot.title)

    def accepts_launch(self, slot, win, ctx=None):
        """A window reopened with its tabs is the one carrying them, not whatever browser window
        shows up next (the blank one a browser opens when it starts, say)."""
        if win.app != slot.app:
            return False
        if not slot.state.get("tabs") or ctx is None:
            return True
        info = ctx.browser.get(win.id)
        return info is not None and chrome.overlap(info.get("tabs") or [], slot.state["tabs"]) > 0

    def may_become_launch(self, slot, win, ctx=None):
        """The blank window a browser opens as it starts: the extension fills it with the tabs
        (reuse_blank) instead of opening another window next to it."""
        if win.app != slot.app or not slot.state.get("tabs") or ctx is None:
            return False
        info = ctx.browser.get(win.id)
        return info is None or not chrome.clean_tabs(info.get("tabs") or [])

    def mode(self, slot, ctx):
        if slot.state.get("tabs") and (slot.state.get("instance") in ctx.browser_online or slot.state.get("profile")):
            return "slot"
        return "app"

    def launch(self, slot, ctx):
        tabs = slot.state.get("tabs")
        if tabs:
            return {"type": "chrome_window", "slot": slot.id, "instance": slot.state.get("instance") or "",
                    "profile": slot.state.get("profile") or None, "tabs": tabs}
        return super().launch(slot, ctx)

    def describe(self, slot):
        tabs = slot.state.get("tabs")
        if tabs:
            active = next((t for t in tabs if t.get("active")), tabs[0])
            n = len(tabs)
            return f"chrome {active.get('title', '')[:32]!r}" + (f" +{n - 1} tabs" if n > 1 else "")
        return super().describe(slot)


class DiscordAdapter(Adapter):
    """The Discord desktop app: one window that hides to the tray when closed."""
    name = "discord"
    launch_mode = "app"
    launch_after = 4.0
    keep_on_close = True
    APPS = {"discord", "Discord", "com.discordapp.Discord", "vesktop", "dev.vencord.Vesktop"}

    def claims(self, win, ctx):
        return win.app in self.APPS

    def score(self, win, slot, ctx):
        return 2.5 if win.app == slot.app else 0.0  # there is only ever one Discord window


class GenericAdapter(Adapter):
    """Everything else: placed by title, started once from its desktop entry."""
    name = "generic"


ADAPTERS: list[Adapter] = [TerminalAdapter(), FilesAdapter(), WebAppAdapter(), BrowserAdapter(),
                           DiscordAdapter(), GenericAdapter()]
BY_NAME = {a.name: a for a in ADAPTERS}


def adapter_for(win: Win, ctx: Ctx) -> Adapter | None:
    """The adapter that takes this window, or None for windows we do not keep."""
    if not win.app or win.app in IGNORED_APPS or win.app in ctx.ignored:
        return None
    return next((a for a in ADAPTERS if a.claims(win, ctx)), None)


def adapter_of(slot: Slot) -> Adapter:
    return BY_NAME.get(slot.kind, BY_NAME["generic"])


def kind_for_app(app: str) -> str:
    """Adapter name for a remembered app id (migration from the old registry)."""
    probe = Win(id="", app=app, title="", space=None, output=None)
    adapter = adapter_for(probe, Ctx())
    return adapter.name if adapter else "generic"
