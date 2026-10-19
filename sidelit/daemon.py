"""The daemon: the one process that owns the layout and drives the compositor.

Runs headless (GLib loop, no GTK, no surfaces), so screens coming and going cannot take
it down; systemd restarts it if anything else does. It:
- observes the desktop over Wayland and steps the engine (core/engine.py) on every
  change and once a second,
- saves the layout (debounced, atomic) and journals every decision,
- tells the engine about suspend/resume (logind) and shuts down without committing
  closes that have not stood long enough (a logout closes apps one by one),
- serves the sidebar and the CLI on a unix socket (ipc.py): a state push on every
  change, and commands for everything that changes the layout,
- reports this desktop's Claudes to the other sources that ask for them (sources.py),
- asks the agents that offer their plan's usage limits for them (agents.py: polls_usage) and
  writes them where the sidebar's bars read them.
The coding agents (Claude Code, Codex, OpenCode) it only knows through their adapters (agents.py).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from sidelit import agents, chrome, claude, config, crashes, detect, ipc, launch, panes, session, sources, turncheck, version  # noqa: E402
from sidelit.procs import load_json, shell_command  # noqa: E402
from sidelit.tabstate import Attention  # noqa: E402
from sidelit.backend import open_client  # noqa: E402
from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core import journal as journal_mod  # noqa: E402
from sidelit.core import titles  # noqa: E402
from sidelit.core.engine import Engine  # noqa: E402
from sidelit.core.model import Ended, Layout, Obs, Slot, Space, Win  # noqa: E402
from sidelit.core.store import Store, migrate_registry  # noqa: E402

SESSION_FRESH_S = 600   # a compositor younger than this means we were started by a login
STEP_DEBOUNCE_MS = 250  # busy Claudes animate their titles ~10x/s: coalesce
SAVE_DEBOUNCE_MS = 1500
# COSMIC slides from one workspace to the next; activating a window while it starts makes it
# jump there instead. A session tab's terminal gets the keyboard once the slide has played.
FOCUS_AFTER_SWITCH_MS = 350
CHROME_START_S = 30     # after starting a browser for a profile, wait this long for its extension
CLOSE_WAIT_S = 30       # a closed workspace on KDE goes once its windows have, if they do by then
USAGE_EVERY_S = 300     # an agent's usage limits are asked for this often (polls_usage)...
USAGE_AFTER_TURN_S = 60  # ...and again once one of its turns has ended, at most this often
USAGE_FAILED_S = 1800   # (not logged in, say: asked again after this long)


def say(msg: str):
    print(msg, file=sys.stderr, flush=True)


def session_age_s() -> float | None:
    """Seconds since the compositor started; None if unknown."""
    comp = session.compositor()
    return comp.age if comp is not None else None


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.loop = GLib.MainLoop()
        self.journal = journal_mod.Journal(config.JOURNAL)
        self.store = Store(config.LAYOUT)
        self.detector = detect.Detector()
        # before a session's tab turns yellow, a model says whether it needs you (turncheck.py)
        self.turns = turncheck.Turns(turncheck.Checker(lambda: GLib.idle_add(self._turns_in), cfg=self.cfg,
                                                       cwd=str(config.RUNTIME_DIR)),
                                     config.RUNTIME_DIR / "turns.json", journal=self._journal_turn)
        # agent -> (when its usage limits are next asked for, whether they are being asked now)
        self._usage: dict[str, list] = {a.name: [time.time() + 5, False] for a in agents.every() if a.polls_usage}
        self._unmatched: dict[str, str] = {}  # window -> state, for Claude-titled windows with no session matched
        self._models: dict[str, tuple[float, str | None]] = {}
        self._tl: dict[str, object] = {}
        self.attention = Attention()  # which Claudes call you back (yellow), by box key
        self._focus_pending = 0  # GLib source of a focus waiting for a workspace slide to end
        self._active_space = None  # the workspace you were on at the last step (its layout id, else its ids)
        self._switched_at = 0.0  # when we last switched workspace ourselves
        self._step_pending = False
        self._save_pending = False
        self._last_view = None
        self._clocks = (time.time(), time.monotonic())
        self.exit_code = 0
        # Chrome, via the extension: instance -> {"conn", "windows", "profile", "t", "version"}
        self.chrome: dict[str, dict] = {}
        self._pairer = chrome.Pairer()               # which compositor window is which Chrome window
        self._pairer_asked = 0.0
        self._chrome_pending: list[dict] = []        # windows to open once their profile's extension is up
        self._copies = ""                            # each screen's copies, as last journaled
        self._chrome_started: dict[tuple, float] = {}  # (data dir, profile) -> when we started the browser
        self._chrome_req = 0
        self._chrome_profiles: dict[str, tuple] = {}  # instance -> (data dir, profile), kept after it goes
        self._ended: list[Ended] = []                 # what we started that ended, the last few minutes
        self._app_exes: dict[str, str | None] = {}
        self.reporter = sources.Reporter(log=say)     # this desktop's Claudes, to the sources that ask

        self.ws = open_client(on_change=self._schedule_step)
        now = time.time()
        layout, absorb = self._load_layout(now)
        self.store.rotate_on_start()
        ctx = ad.Ctx(known_dirs=[], resume_command=lambda sid, cwd, agent=None: agents.get(agent).resume_command(sid, cwd),
                     ignored=set(self.cfg.get("ignore_apps") or []),
                     endings=self._endings, browser_exit=self._browser_exit, app_exe=self._app_exe)
        self.engine = Engine(layout, self, ctx=ctx, journal=self.journal, now=now)
        self.engine.relaunch = bool(self.cfg.get("relaunch", True))
        if absorb:
            self.engine.absorb_until = now + 15.0

        self._watching = None
        self._watch()
        GLib.timeout_add(1000, self._tick)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_term)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._on_hup)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, lambda *_: (self.cmd_restore({}), True)[1])
        self._watch_logind()
        self.server = ipc.Server(self._handle)
        self.server.on_drop = self._chrome_dropped

        # a compositor that just started has nothing on screen: after a login, and after COSMIC
        # restarted itself (a crash kills every window but not the session), bring it all back
        comp = session.compositor()
        fresh = session.needs_restore(comp, config.RESTORED_MARKER)
        self.journal("daemon_started", version=version(), session_age=int(comp.age if comp else -1), fresh=fresh,
                     compositor=comp.ident if comp else None,
                     workspaces=len(layout.workspaces), slots=sum(1 for _ in layout.slots()))
        if fresh and self.cfg.get("restore_on_login", True):
            session.mark_restored(comp, config.RESTORED_MARKER)
            self.engine.start_restore(now, "new compositor (login or restart)",
                                      delay=float(self.cfg.get("restore_delay", 6)),
                                      claudes_only=not self.cfg.get("restore_windows", True))
        self._step()

    # ---- layout on disk ----------------------------------------------------------------------
    def _load_layout(self, now: float) -> tuple[Layout, bool]:
        layout = self.store.load()
        if layout is not None:
            return layout, False
        if config.REGISTRY.exists():
            registry = load_json(config.REGISTRY, {}) or {}
            age = session_age_s()
            fresh = age is not None and age < SESSION_FRESH_S
            live = claude.live_session_ids()
            term = shlex.split(self.cfg.get("terminal") or "terminator")[0]
            layout = migrate_registry(registry, live, fresh, now, terminal_app=os.path.basename(term))
            self.journal("migrated", workspaces=len(layout.workspaces), slots=sum(1 for _ in layout.slots()),
                         recent=len(layout.recent), fresh=fresh,
                         why="registry.json -> layout.json (closed Claudes go to the recent list)")
            self.store.save(layout)
            try:
                config.REGISTRY.replace(config.REGISTRY.with_name("registry.json.migrated"))
            except OSError:
                pass
            return layout, not fresh
        return Layout(), False

    def _save_soon(self):
        if not self._save_pending:
            self._save_pending = True
            GLib.timeout_add(SAVE_DEBOUNCE_MS, self._save)

    def _save(self):
        self._save_pending = False
        try:
            self.store.save(self.engine.layout)
            self.engine.dirty = False
        except OSError as e:
            say(f"layout: could not save: {e}")
        return False

    # ---- the loop ----------------------------------------------------------------------------
    def _watch(self) -> bool:
        """Watch the compositor connection the backend pumps. COSMIC pumps a Wayland fd; a backend
        without one wakes us through on_change. One that makes its event connection again (niri
        and sway drop a reader that falls behind) counts them (`connections`): the newest one is
        watched, even on the old one's fd number. True when the watch changed. One that lost its
        connection and could not make it again has gone: the daemon stops, and systemd starts
        it again once the compositor is back."""
        key = (self.ws.fd(), getattr(self.ws, "connections", 0))
        if self._watching is not None and key == self._watching[:2]:
            return False
        if key[0] < 0 and key[1]:
            say("compositor connection lost, and not made again")
            self.exit_code = 1
            self.shutdown()
        if self._watching is not None and self._watching[2]:
            GLib.source_remove(self._watching[2])  # (now, not later: its fd may be closed already)
        source = 0
        if key[0] >= 0:
            source = GLib.io_add_watch(key[0], GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                                       lambda fd, cond, key=key: self._on_wayland(fd, cond, key))
        self._watching = (*key, source)
        return True

    def _on_wayland(self, fd, cond, key=None):
        if key is not None and key != self._watching[:2]:
            return False  # a connection the backend has replaced
        connections = getattr(self.ws, "connections", 0)
        if cond & (GLib.IO_HUP | GLib.IO_ERR) and connections:
            # a backend that makes its connection again: it says whether the compositor has gone
            try:
                self.ws.pump()
            except Exception as e:  # noqa: BLE001
                say(f"compositor connection lost: {e}")
                self.exit_code = 1
                self.shutdown()
                return False
            if getattr(self.ws, "connections", 0) != connections:
                self._watch()
                return False
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            try:  # the compositor says why before it hangs up: read that first
                self.ws.pump()
            except Exception as e:  # noqa: BLE001
                say(f"wayland: {e}")
            say("wayland: compositor connection lost; last requests:\n  " +
                "\n  ".join(getattr(getattr(self.ws, "display", None), "sent", []) or ["?"]))
            self.exit_code = 1
            self.shutdown()
            return False
        try:
            self.ws.pump()
        except Exception as e:  # compositor went away
            say(f"wayland error: {e}; last requests:\n  " +
                "\n  ".join(getattr(getattr(self.ws, "display", None), "sent", []) or ["?"]))
            self.exit_code = 1
            self.shutdown()
            return False
        return not self._watch()

    def _schedule_step(self):
        if not self._step_pending:
            self._step_pending = True
            GLib.timeout_add(STEP_DEBOUNCE_MS, self._debounced_step)

    def _debounced_step(self):
        self._step_pending = False
        self._step()
        return False

    def _tick(self):
        wall, mono = time.time(), time.monotonic()
        last = self._clocks
        self._clocks = (wall, mono)
        if (wall - last[0]) - (mono - last[1]) > 20:
            self.journal("suspend_detected", minutes=int((wall - last[0]) / 60))
            self.engine.suspend_resumed(wall)
        self._ended = [e for e in self._ended if wall - e.t < 300]
        for pid, exe, code in launch.reap():
            self._ended.append(Ended(t=wall, pid=pid, exe=exe, crashed=code < 0))
        self._step()
        try:
            self.reporter.tick()
        except Exception:  # noqa: BLE001 (reporting must never stop the daemon)
            import traceback
            traceback.print_exc()
        self._ask_usage(wall)
        return True

    # ---- usage limits (the agents that offer them to whoever asks) ------------------------------
    def _ask_usage(self, now: float):
        """Each agent that offers its plan's usage limits (polls_usage) is asked, on a thread,
        every USAGE_EVERY_S and soon after its turns end; what it says goes where the bars read
        it (agent.usage_file). An agent not installed is not asked."""
        for name, due in self._usage.items():
            when, asking = due
            if asking or now < when:
                continue
            agent = agents.get(name)
            if not agent.installed():
                due[0] = now + USAGE_FAILED_S
                continue
            due[1] = True

            def ask(agent=agent, due=due):
                limits = None
                try:
                    limits = agent.usage()
                    if limits:
                        path = agent.usage_file
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_name(path.name + ".tmp")
                        tmp.write_text(json.dumps(limits) + "\n")
                        tmp.replace(path)
                except Exception as e:  # noqa: BLE001 (no bars is no reason to stop anything)
                    say(f"usage: {agent.label}: {e}")
                finally:
                    due[0] = time.time() + (USAGE_EVERY_S if limits else USAGE_FAILED_S)
                    due[1] = False
            threading.Thread(target=ask, name=f"usage-{name}", daemon=True).start()

    def _turn_ended(self, agent: str):
        """A turn of `agent` ended: its usage changed, ask again soon."""
        due = self._usage.get(agent)
        if due is not None and not due[1]:
            due[0] = min(due[0], max(time.time(), due[0] - USAGE_EVERY_S + USAGE_AFTER_TURN_S))

    # ---- how things ended ---------------------------------------------------------------------
    def _endings(self, since: float) -> tuple[list, bool]:
        """What ended since then (our own launches, and crashes on record), and whether a crash
        anywhere would be on record."""
        watched = crashes.readable()
        out = [e for e in self._ended if e.t >= since]
        return (out + crashes.since(since) if watched else out), watched

    def _browser_exit(self, inst: str) -> str | None:
        """How the browser behind an extension instance last ended (chrome.last_exit)."""
        prof = self._chrome_profiles.get(inst) or chrome.find_profile(inst)
        return chrome.last_exit(*prof) if prof else None

    def _app_exe(self, app: str) -> str | None:
        if app not in self._app_exes:
            info = launch.desktop_app(app)
            exe = info.get_executable() if info is not None else None
            self._app_exes[app] = os.path.basename(exe) if exe else None
        return self._app_exes[app]

    def _step(self):
        now = time.time()
        try:
            obs = self.observe(now)
            self.engine.ctx.known_dirs = [lw.cwd for lw in self.engine.layout.workspaces if lw.cwd]
            self.engine.step(obs, now)
            self._landed(now)
        except Exception:
            import traceback
            traceback.print_exc()
            self._watch()  # (one that failed may have lost the backend's event connection)
            return
        if self.engine.dirty:
            self._save_soon()
        self._push()
        self._watch()  # (an action may have made the backend's event connection again)

    def observe(self, now: float) -> Obs:
        # where copies of an index disagree, the one the layout knows represents it
        self.ws.preferred_ids = {i for lw in self.engine.layout.workspaces for i in lw.ids}
        spaces = []
        self._ws_by_index = {}
        for w in self.ws.list():
            spaces.append(self._as_space(w))
            self._ws_by_index[w.index] = w
        copies = getattr(self.ws, "copies_summary", None)
        summary = copies() if copies is not None else ""
        if summary != self._copies:
            # each screen's copy of every index: what to go on when windows land on the wrong workspace
            self._copies = summary
            self.journal("copies", copies=summary)
        windows = {}
        self._tl = {}
        copy_ids = getattr(self.ws, "copy_ids_of", None)
        for tl in list(self.ws.toplevels.values()):
            if tl.closed or not tl.identifier:
                continue
            here = self.ws.workspace_of(tl)
            windows[tl.identifier] = Win(id=tl.identifier, app=tl.app_id or "", title=tl.title or "",
                                         space=here.index if here is not None else None,
                                         output=self._output(tl), pid=getattr(tl, "pid", 0) or 0,
                                         focused=bool(getattr(tl, "focused", False)),
                                         space_ids=copy_ids(tl) if copy_ids is not None else frozenset())
            self._tl[tl.identifier] = tl
        browser = self._browser_windows(windows)
        self.detector.note_focus(now, next((wid for wid, w in windows.items() if w.focused), None))
        self.detector.refresh(now)
        self.detector.assign(windows, self.engine.launched_pid)
        for agent in self.detector.agents:
            agent.watch([sid for sid, rec in self.detector.hooked.items() if rec["agent"] == agent.name])
        found = {}
        for wid, w in windows.items():
            infos = self.detector.claudes_for(w.title, w.pid, wid)
            if infos:  # the first is the window's Claude to the engine; "more": the others in it
                found[wid] = {**infos[0], "more": infos[1:]}
        focused_at = dict(getattr(self.ws, "closed_focus", {}))
        focused_at.update({tl.identifier: tl.focused_at for tl in self._tl.values() if getattr(tl, "focused_at", 0)})
        # (a reconnected extension's windows are the last connection's until it reports again)
        reported = {inst: {w["id"] for w in c.get("windows") or []} for inst, c in self.chrome.items() if c.get("reported")}
        return Obs(spaces=spaces, windows=windows, outputs=frozenset(self.ws.output_names()), claude=found,
                   browser=browser, browser_reported=reported, focused_at=focused_at,
                   blind=bool(getattr(self.ws, "blind", False)))

    # ---- Chrome ------------------------------------------------------------------------------
    def _browser_windows(self, windows: dict) -> dict:
        """Which compositor window is which reported Chrome window, with its tabs."""
        self.engine.ctx.browser_online = set(self.chrome)
        if not self.chrome:
            return {}
        for inst, c in self.chrome.items():  # the extension's storage reaches disk a little after hello
            if c.get("profile") is None and time.time() - c.get("looked", 0) > 20:
                c["looked"] = time.time()
                c["profile"] = chrome.find_profile(inst)
                if c["profile"]:
                    self._chrome_profiles[inst] = tuple(c["profile"])
        tops = [w for w in windows.values() if w.app in titles.BROWSERS]
        reported = [(inst, w) for inst, c in self.chrome.items() for w in c.get("windows") or []]
        now = time.time()
        found = self._pairer.update(tops, reported, now, {i: c.get("report_t", 0) for i, c in self.chrome.items()})
        if self._pairer.wants_report(now) and now - self._pairer_asked > 3:
            self._pairer_asked = now  # a focused window to confirm: ask for a fresh report
            for c in self.chrome.values():
                self.server.send_event(c["conn"], "chrome", {"cmd": "snapshot"})
        out = {}
        for wid, (inst, w) in found.items():
            prof = self.chrome[inst].get("profile")
            out[wid] = {"instance": inst, "window": w["id"], "tabs": w.get("tabs") or [],
                        "profile": list(prof) if prof else None}
        return out

    def _chrome_message(self, msg: dict, conn):
        inst = str(msg.get("instance") or "")
        kind = msg.get("type")
        if kind == "hello" and inst:
            known = self.chrome.get(inst) or {}
            profile = known.get("profile") or chrome.find_profile(inst)
            self.chrome[inst] = {"conn": conn, "windows": known.get("windows") or [], "profile": profile,
                                 "t": time.time(), "version": msg.get("version"), "reported": False}
            if profile:
                self._chrome_profiles[inst] = tuple(profile)
            self.journal("chrome_connected", instance=inst, profile="/".join(profile) if profile else None)
            return True
        c = next((c for c in self.chrome.values() if c["conn"] is conn), None)
        if c is None:
            return False  # not said hello yet (the host replays it on reconnect)
        if kind == "windows":
            c["windows"] = [w for w in msg.get("windows") or [] if isinstance(w, dict) and "id" in w]
            c["report_t"] = time.time()
            first = not c["reported"]
            c["reported"] = True
            if first and self._chrome_pending:
                # let the engine see this profile's windows first: one it brought back itself
                # is recognised by its tabs and needs no second copy
                GLib.timeout_add(1500, lambda: (self._flush_chrome_pending(), False)[1])
            self._schedule_step()
        elif kind == "opened":
            inst = next(i for i, x in self.chrome.items() if x is c)
            if isinstance(msg.get("window"), int):
                self._pairer.note_opened(inst, msg["window"], time.time())
            self.journal("chrome_opened", instance=inst, window=msg.get("window"))
            self._schedule_step()
        elif kind == "error":
            self.journal("chrome_error", error=str(msg.get("error"))[:200])
        return True

    def _chrome_dropped(self, conn):
        for inst in [i for i, c in self.chrome.items() if c["conn"] is conn]:
            self.chrome.pop(inst, None)
            self.journal("chrome_disconnected", instance=inst)
            self._schedule_step()

    def _chrome_for(self, spec: dict) -> str | None:
        """The connected instance that should open this window: its own, or its profile's."""
        if spec.get("instance") in self.chrome:
            return spec["instance"]
        prof = tuple(spec.get("profile") or ()) or None
        return next((i for i, c in self.chrome.items() if prof and c.get("profile") == prof), None)

    def _chrome_open(self, inst: str, spec: dict) -> bool:
        c = self.chrome[inst]
        self._chrome_req += 1
        started = self._chrome_started.get(tuple(c.get("profile") or ()), 0)
        cmd = {"cmd": "open", "req": self._chrome_req, "tabs": spec["tabs"],
               "reuse_blank": time.time() - started < 120}
        return self.server.send_event(c["conn"], "chrome", cmd)

    def _chrome_launch(self, spec: dict) -> int | None:
        inst = self._chrome_for(spec)
        if inst is not None:
            return 0 if self._chrome_open(inst, spec) else None
        prof = tuple(spec.get("profile") or ())
        if len(prof) != 2:
            return None  # no extension and no idea which profile: nothing to open it with
        self._chrome_pending.append({**spec, "queued": time.time()})
        if time.time() - self._chrome_started.get(prof, 0) > CHROME_START_S:
            self._chrome_started[prof] = time.time()
            argv = chrome.launch_argv(*prof)
            self.journal("chrome_start", profile="/".join(prof), argv=argv)
            if launch.spawn(argv, str(Path.home())) is None:
                return None
        return 0

    def _flush_chrome_pending(self):
        bound = self.engine.bound_slots
        keep = []
        for spec in self._chrome_pending:
            if spec.get("slot") in bound:
                self.journal("chrome_skipped", slot=spec.get("slot"), why="the browser brought that window back itself")
                continue
            inst = self._chrome_for(spec)
            if inst is not None:
                self._chrome_open(inst, spec)
            elif time.time() - spec["queued"] < 120:
                keep.append(spec)
        self._chrome_pending = keep

    def _output(self, tl) -> str | None:
        live = getattr(self.ws, "outputs_of", None)
        if live is not None:
            outs = live(tl)
            if len(outs) != 1:
                return None  # not on a screen yet, or spanning two: no opinion
            return outs[0].name or None
        out = self.ws.output_of(tl)
        return out.name if out is not None and out.name else None

    # ---- the actuator (called by the engine) ---------------------------------------------------
    def _space(self, index: int):
        return next((w for w in self.ws.list() if w.index == index), None)

    def _as_space(self, w) -> Space:
        return Space(index=w.index, ids=frozenset(w.all_ids), pinned=w.pinned, active=w.active, name=w.name or "",
                     copies_pinned=getattr(w, "copies_pinned", True), output=getattr(w, "monitor", "") or "")

    def _dry(self, what: str) -> bool:
        if config.DRY_RUN:
            say(f"dry run: would {what}")
        return config.DRY_RUN

    def move(self, win_id: str, space: int, output: str | None) -> bool:
        tl = self._tl.get(win_id)
        if self._dry(f"move {getattr(tl, 'app_id', '?')} {getattr(tl, 'title', '')[:40]!r} to workspace {space}"
                     f"{' on ' + output if output else ''}"):
            return True
        ws = self._space(space)
        if tl is None or ws is None or not self.ws.can_move_windows:
            return False
        return bool(self.ws.move_toplevel(tl, ws, output))

    def move_space(self, index: int, before: int) -> bool:
        """Put workspace `index` right before workspace `before` (every screen's copy goes)."""
        a, b = self._space(index), self._space(before)
        if self._dry(f"move workspace {index} before {before}"):
            return True
        if a is None or b is None or a.coords == b.coords or "move" not in self.ws.caps:
            return False
        if getattr(a, "monitor", "") != getattr(b, "monitor", ""):
            return False  # (where each workspace is on one screen, putting it back in order never moves it to another)
        self.ws.move(a, b, before=True)
        return True

    def pin_new_space(self, want: tuple = ()) -> Space | None:
        """A fresh workspace, kept. `want`: the ids the workspace it is for had, for a desktop
        that can bring one back under its id (Hyprland: its number)."""
        if self._dry("pin a new workspace"):
            return None
        if "pin" not in self.ws.caps:
            ws = self.ws.create("Workspace")  # a desktop whose workspaces persist anyway
            return self._as_space(ws) if ws is not None else None
        if not getattr(self.ws, "spare_workspace", True):
            # no empty workspace waits at the end (Hyprland): the desktop makes one, pinned
            ws = self.ws.create("Workspace", want=tuple(want))
            return self._as_space(ws) if ws is not None and ws.all_ids else None
        lst = self.ws.list()
        if not lst:
            return None
        ws = lst[-1]
        if ws.pinned or self.ws.toplevels_on(ws):
            return None  # COSMIC appends a fresh empty workspace shortly
        self.ws.pin(ws, True)
        self.ws.settle()
        fresh = next((w for w in self.ws.list() if w.coords == ws.coords), None)
        return self._as_space(fresh) if fresh is not None and fresh.id else None

    def pin(self, index: int) -> Space | None:
        ws = self._space(index)
        if ws is None:
            return None
        if self._dry(f"pin workspace {index}"):
            return self._as_space(ws) if ws.all_ids else None
        if "pin" in self.ws.caps and not (ws.pinned and ws.copies_pinned):
            self.ws.pin(ws, True)
            self.ws.settle()
            ws = next((w for w in self.ws.list() if w.coords == ws.coords), ws)
        return self._as_space(ws) if ws.all_ids else None

    def unpin(self, index: int):
        """No longer kept: COSMIC drops it once it is empty. A desktop whose workspaces persist
        (KDE) keeps it, windows and all; only closing it removes it (_remove_when_empty)."""
        ws = self._space(index)
        if ws is None or self._dry(f"unpin workspace {index}"):
            return
        if "pin" in self.ws.caps:
            self.ws.pin(ws, False)

    def _remove_when_empty(self, index: int | None):
        """You closed workspace `index` on a desktop whose workspaces persist (KDE): remove it
        once its windows have gone. Not before: the desktop would move a window still open (one
        asking about unsaved work, say) to a neighbour. Never the last one."""
        ws = self._space(index) if index is not None else None
        if ws is None or "pin" in self.ws.caps or "remove" not in self.ws.caps:
            return
        ids, until = set(ws.all_ids), time.time() + CLOSE_WAIT_S

        def drop():
            fresh = next((w for w in self.ws.list() if w.all_ids & ids), None)
            if fresh is None or len(self.ws.list()) < 2:
                return False
            if self.ws.toplevels_on(fresh):
                return time.time() < until  # still closing; past that, you kept something open
            if not self._dry(f"remove workspace {fresh.index}"):
                self.ws.remove(fresh)
            return False
        GLib.timeout_add(1500, drop)

    def activate(self, index: int):
        ws = self._space(index)
        if ws is not None and not self._dry(f"activate workspace {index}"):
            self._switch(ws)

    def _switch(self, ws):
        """Show a workspace. A switch of ours lands where it means to (_landed leaves it be)."""
        self._switched_at = time.time()
        self.ws.activate(ws)

    def launch(self, spec: dict) -> int | None:
        if self._dry(f"launch {spec if spec.get('type') != 'chrome_window' else {**spec, 'tabs': len(spec['tabs'])}}"):
            return None
        if spec.get("type") == "chrome_window":
            return self._chrome_launch(spec)
        return launch.run(spec, self.cfg)

    # ---- signals, logind, shutdown -------------------------------------------------------------
    def _watch_logind(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as e:  # noqa: BLE001
            say(f"logind: no system bus ({e}); suspend is noticed by the clock jump only")
            return

        def on_signal(conn, sender, path, iface, name, params):
            going = bool(params.unpack()[0])
            if name == "PrepareForSleep" and not going:
                self.journal("resumed", why="woke from suspend (logind)")
                self.engine.suspend_resumed(time.time())
                self._step()
            elif name == "PrepareForShutdown" and going:
                self.journal("shutdown_coming", why="logind: the machine is shutting down")
                self.engine.drop_pending()
                self.engine.disturb(600, "shutting down", time.time())
                self._save()

        for name in ("PrepareForSleep", "PrepareForShutdown"):
            bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", name,
                                 "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, on_signal)
        self._system_bus = bus

    def _on_term(self, *_):
        self.shutdown()
        signal.alarm(5)  # a wedged shutdown must not make us unkillable
        return False

    def _on_hup(self, *_):
        self.cfg = config.load()
        self.engine.ctx.ignored = set(self.cfg.get("ignore_apps") or [])
        self.engine.relaunch = bool(self.cfg.get("relaunch", True))
        self.turns.checker.cfg = self.cfg
        self.journal("config_reloaded")
        return True

    def shutdown(self):
        self.turns.checker.stop()
        try:
            self.engine.drop_pending()
            self._save()
        finally:
            try:
                self.server.close()
            except Exception:  # noqa: BLE001
                pass
            self.loop.quit()

    def run(self) -> int:
        self.loop.run()
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        return self.exit_code

    # ---- the view (what the sidebar shows) -------------------------------------------------------
    def _model(self, session: str | None) -> str | None:
        if not session:
            return None
        now = time.time()
        hit = self._models.get(session)
        if hit and now - hit[0] < 15:
            return hit[1]
        model = self.detector.agent_of(session).model(session, self.detector.hooked.get(session))
        self._models[session] = (now, model)
        return model

    def _claudes_on(self, obs: Obs, wins: list[Win], now: float) -> list[dict]:
        """The Claudes a workspace shows, a box each, in the order they started: every session
        its terminals show; with none, each window whose title looks like Claude's."""
        boxes = []
        for w in wins:
            info = obs.claude.get(w.id)
            if info:
                group = [info] + list(info.get("more") or [])
                typing = self._typing_in(w, group)
                boxes += [self._box(c, w, c["session"] in typing, now) for c in group]
        if not boxes:
            # (an agent whose title names its project is asked first: its test is the narrower,
            # and only of a terminal, since a file manager on that folder has that title too)
            asked = sorted(self.detector.agents, key=lambda a: a.window_title)
            for w in sorted(wins, key=lambda w: 0 if w.app in titles.TERMINAL_APP_IDS else 1):
                agent = next((a for a in asked if (a.window_title or w.app in titles.TERMINAL_APP_IDS)
                              and a.unmatched_title(w.title)), None)
                if agent is not None:
                    boxes.append(self._unmatched_box(w, now, agent.name))
        return sorted(boxes, key=lambda b: b["started"])

    @staticmethod
    def _typing_in(w: Win, group: list[dict]) -> set[str]:
        """The sessions of this window you are typing in: none unless it has the keyboard. With
        several in it (split panes, tabs) the one whose title it shows, the pane with the
        keyboard; none when it shows a shell's; all when it cannot tell (two with that title)."""
        if not w.focused:
            return set()
        every = {c["session"] for c in group}
        if len(group) == 1:
            return every
        if not titles.looks_like_claude(w.title):
            return set()
        hits = {c["session"] for c in group if c.get("title") and c["title"] == titles.bare_title(w.title)}
        return hits if len(hits) == 1 else every

    def _box(self, c: dict, w: Win, typing: bool, now: float) -> dict:
        """A Claude's box: what it does, and since when it calls you (0: it does not)."""
        sid = c["session"]
        state, on = c.get("status") or "done", ""
        if self.cfg.get("check_turns", True):
            state, on = self.turns.state(sid, self.detector.hooked.get(sid), state, typing, now)
        calls = self.attention.update(sid, state, typing, now=now)
        return {"key": sid, "session": sid, "agent": c.get("agent") or "claude", "title": c.get("title") or "",
                "state": state, "on": on,
                "model": self._model(sid), "agents": c.get("agents") or 0, "cwd": c.get("cwd") or "",
                "window": w.id, "pane": c.get("pane") or {}, "started": c.get("started") or 0.0,
                "typed": c.get("prompt_t") or 0.0, "how": c.get("how"), "hooked": bool(c.get("hooked")),
                "typing": typing, "calls": self.attention.since(sid) if calls else 0.0}

    def _unmatched_box(self, w: Win, now: float, agent: str = "claude") -> dict:
        """A window whose title looks like an agent's, with no session matched: what its title says."""
        state = "busy" if titles.title_state(w.title) == "busy" else "done"
        # its turns cannot be checked, and it turns yellow on its title alone. Say so, or it goes unseen.
        if self._unmatched.get(w.id) == "busy" and state == "done" and not w.focused \
                and self.cfg.get("check_turns", True):
            self.journal("turn_checked", title=titles.bare_title(w.title) or "", session=None,
                         verdict="unchecked", on="", error="no Claude session matched to this terminal", secs=0)
        self._unmatched[w.id] = state
        key = f"w:{w.id}"
        calls = self.attention.update(key, state, w.focused, now=now)
        return {"key": key, "session": None, "agent": agent, "title": titles.bare_title(w.title) or "", "state": state, "on": "",
                "model": None, "agents": 0, "cwd": "", "window": w.id, "pane": {}, "started": 0.0, "typed": 0.0,
                "how": None, "hooked": False, "typing": w.focused,
                "calls": self.attention.since(key) if calls else 0.0}

    def _landing(self, boxes: list[dict]) -> dict | None:
        """Where going to a workspace puts you: the Claude that has waited for you longest, else
        the one you used last (its window had the keyboard last; in it, you typed into it
        last), else the first."""
        calling = [b for b in boxes if b.get("calls")]
        if calling:
            return min(calling, key=lambda b: b["calls"])
        focused_at = self.engine.obs.focused_at if self.engine.obs is not None else {}
        return max(boxes, key=lambda b: (focused_at.get(b["window"], 0.0), b.get("typed") or 0.0), default=None)

    def view(self) -> dict:
        e, obs = self.engine, self.engine.obs
        if obs is None:
            return {}
        total = len(obs.spaces)
        tabs = []
        bound = e.bound_slots
        now = time.time()
        for space in sorted(obs.spaces, key=lambda s: s.index):
            lw = e.layout.ws(e.owner.get(space.index))
            wins = obs.windows_on(space.index)
            ws_obj = self._ws_by_index.get(space.index)
            tabs.append({
                "key": f"w:{lw.id}" if lw else f"s:{space.index}", "ws": lw.id if lw else None,
                "space": space.index, "active": space.active, "pinned": space.pinned,
                "name": lw.name if lw else "", "cwd": lw.cwd if lw else "", "item": self._item_of(lw),
                "shortcut": self.ws.shortcut_label(ws_obj, total) if ws_obj is not None else "",
                "claudes": self._claudes_on(obs, wins, now),
                "windows": [{"app": w.app, "title": w.title, "slot": e.bind.get(w.id)} for w in wins],
                "missing": [{"slot": s.id, "what": ad.adapter_of(s).describe(s), "app": s.app}
                            for s in (lw.slots if lw else []) if s.id not in bound],
                "last_title": self._last_title(lw),
            })
        for lw in e.layout.workspaces:  # kept workspaces that are not on the desktop now
            if lw.id in e.primary:
                continue
            tabs.append({"key": f"w:{lw.id}", "ws": lw.id, "space": None, "active": False, "pinned": True,
                         "name": lw.name, "cwd": lw.cwd, "item": self._item_of(lw), "shortcut": "", "claudes": [],
                         "windows": [],
                         "missing": [{"slot": s.id, "what": ad.adapter_of(s).describe(s), "app": s.app}
                                     for s in lw.slots],
                         "last_title": self._last_title(lw)})
        shown = {b["key"] for t in tabs for b in t["claudes"]}
        self.attention.forget(shown)
        r = e.restore
        return {
            "version": version(),
            "caps": sorted(self.ws.caps),
            "calm": e.calm(now),
            "disturbed": "" if e.calm(now) else e.disturb_reason,
            "restore": {"running": bool(r and not r.finished), "log": (r.log[-3:] if r else []),
                        "reason": r.reason if r else "",
                        "planned": bool(r and r.planned), "total": len(r.planned_slots) if r else 0,
                        "back": sum(1 for s in r.planned_slots if s in bound) if r else 0,
                        "finished": r.finished_t if r and r.finished else 0},
            "pending_forget": len(e.pending_delete),
            "tabs": tabs,
            "recent": [rec for rec in e.layout.recent if rec.get("session") not in shown][:12],  # (none open now)
            "history_count": len(e.layout.history),  # (the entries: the history command)
            "chrome": [{"instance": i, "profile": "/".join(c["profile"]) if c.get("profile") else None,
                        "windows": len(c.get("windows") or [])} for i, c in self.chrome.items()],
        }

    @staticmethod
    def _history_item(h: dict) -> dict:
        """What the history button lists for a closed workspace."""
        ws = h.get("workspace") or {}
        slots = [Slot.from_json(s) for s in ws.get("slots") or [] if isinstance(s, dict) and s.get("id")]
        claude_slot = next((s for s in slots if s.session), None)
        title = ws.get("name") or (claude_slot.state.get("claude_title") or claude_slot.title if claude_slot else "") \
            or os.path.basename((ws.get("cwd") or "").rstrip("/")) or (ad.adapter_of(slots[0]).describe(slots[0]) if slots else "")
        # every Claude on a line of its own, those that shared a window too
        what = [line for s in slots
                for line in ([ad.describe_claude(c) for c in s.claudes] or [ad.adapter_of(s).describe(s)])]
        return {"id": h.get("id"), "title": title, "closed": h.get("closed"), "cwd": ws.get("cwd") or "", "what": what}

    @staticmethod
    def _item_of(lw) -> dict | None:
        """The other source's item a workspace was opened for: its tab is drawn from it."""
        return {"source": lw.source, "id": lw.item} if lw is not None and lw.source else None

    @staticmethod
    def _last_title(lw) -> str:
        if lw is None or lw.claude is None:
            return ""
        return lw.claude.state.get("claude_title") or lw.claude.title or ""

    def _journal_turn(self, kind: str, **fields):
        """A journal line about a Claude's turn, with the title its box shows."""
        shown = next((c.get("title") or "" for t in (self._last_view or {}).get("tabs") or []
                      for c in t.get("claudes") or [] if c.get("session") == fields.get("session")), "")
        self.journal(kind, title=shown, **fields)

    def _turns_in(self):
        """Answers from the turn checker (on the main loop): journal them, show them."""
        for got in self.turns.collect():
            self._journal_turn("turn_checked", **got)
        self.turns.forget(set(self.detector.hooked))
        live = {w for w in self.engine.obs.windows} if self.engine.obs else set()
        self._unmatched = {w: s for w, s in self._unmatched.items() if w in live}
        self._push()
        return False

    def _push(self):
        view = self.view()
        if view != self._last_view:
            self._last_view = view
            self.server.push("state", view)
            try:
                self.reporter.update(view.get("tabs") or [])
            except Exception:  # noqa: BLE001 (reporting must never stop the daemon)
                import traceback
                traceback.print_exc()

    # ---- commands ----------------------------------------------------------------------------
    def _handle(self, method: str, params: dict, conn):
        if method == "subscribed":
            self.server.push("state", self._last_view or self.view(), only=conn)
            return None
        if method == "chrome":  # the Chrome extension, through its native host
            return self._chrome_message(params, conn)
        if method in ("agent_event", "claude_event"):  # the plugin's hook: a session did something
            self.detector.refresh(time.time(), force=True)
            rec = self.detector.hooked.get(str(params.get("session") or ""))
            if rec is not None and rec.get("event") == "Stop":
                self._turn_ended(rec["agent"])
            self._schedule_step()  # (tool events come in bursts: coalesce)
            return None
        fn = getattr(self, "cmd_" + method.replace("-", "_"), None)
        if fn is None:
            raise ValueError(f"unknown command {method!r}")
        result = fn(params)
        self._step()
        return result

    def _lw_for(self, params: dict, register: bool = False):
        """(layout workspace or None, live space index or None) named by params ws / space."""
        e = self.engine
        lw = e.layout.ws(params.get("ws")) if params.get("ws") else None
        index = params.get("space")
        if lw is not None and index is None and lw.id in e.primary:
            index = e.primary[lw.id].index
        if lw is None and index is not None:
            lw = e.layout.ws(e.owner.get(int(index)))
            if lw is None and register:
                lw = e.register_space(int(index), time.time(), why="you used it from the sidebar")
        return lw, (int(index) if index is not None else None)

    def cmd_state(self, params):
        return self.view()

    def cmd_layout(self, params):
        return self.engine.layout.to_json()

    def cmd_ping(self, params):
        return {"version": version(), "pid": os.getpid()}

    def cmd_activate(self, params):
        lw, index = self._lw_for(params)
        if index is None:
            return self.cmd_reopen(params) if lw is not None else False
        ws = self._space(index)
        if ws is None:
            return False
        obs = self.engine.obs
        # land the keyboard in a Claude, not in whatever was focused last: the one that needs you
        box = self._landing(self._claudes_on(obs, obs.windows_on(index), time.time())) if obs is not None else None
        self._go_to(ws, box)
        return True

    def cmd_focus(self, params):
        """A Claude's box clicked: its terminal gets the keyboard (on its workspace), and its
        pane where the terminal lets us (panes.py)."""
        view = self._last_view or self.view()
        for tab in view.get("tabs") or []:
            box = next((b for b in tab.get("claudes") or [] if b["key"] == params.get("key")), None)
            ws = self._space(tab["space"]) if box is not None and tab.get("space") is not None else None
            if ws is not None:
                self._go_to(ws, box)
                return True
        return False

    def _go_to(self, ws, box: dict | None, sliding: bool = False):
        """Show a workspace with this Claude's terminal (and pane) in front. `sliding`: it is
        on its way there already (asking again mid-slide could make it jump)."""
        already_there = ws.active and not sliding
        if not sliding:
            self._switch(ws)
        if self._focus_pending:  # a click on another tab before the last slide ended: not that one
            GLib.source_remove(self._focus_pending)
            self._focus_pending = 0
        tl = self._tl.get(box["window"]) if box else None
        if tl is None:
            return
        self.attention.seen(box["key"])
        if already_there:
            self._focus(tl, box)
        else:
            self._focus_pending = GLib.timeout_add(FOCUS_AFTER_SWITCH_MS, self._focus_after_switch, ws, tl, box)

    def _focus(self, tl, box: dict | None = None):
        self.ws.focus(tl)
        if box and box.get("pane"):
            panes.focus(box["pane"])

    def _focus_after_switch(self, ws, tl, box=None):
        """The slide is over: focus Claude, if you are still on its workspace."""
        self._focus_pending = 0
        if ws.active and any(w is ws for w in self.ws.list()):
            self._focus(tl, box)
        return False

    def _landed(self, now: float):
        """You switched workspace yourself (Super+N, the desktop's own switcher): if a Claude
        there needs you, its terminal gets the keyboard once the slide is over, the one that
        has waited longest first. A switch of ours lands by itself; a busy desktop (a restore,
        screens changing) is left alone."""
        obs = self.engine.obs
        space = next((s for s in obs.spaces if s.active), None) if obs is not None else None
        # which workspace, not which number: reordering or closing another one renumbers yours
        key = (self.engine.owner.get(space.index) or space.ids or space.index) if space is not None else None
        last, self._active_space = self._active_space, key
        if key is None or last is None or key == last or now - self._switched_at < 2 \
                or not self.engine.calm(now):
            return
        active = space.index
        boxes = self._claudes_on(obs, obs.windows_on(active), now)
        box = min((b for b in boxes if b.get("calls")), key=lambda b: b["calls"], default=None)
        ws = self._space(active)
        if box is not None and ws is not None:
            self.journal("landed", space=active, title=box["title"], why="you switched to a workspace where a Claude needs you")
            self._go_to(ws, box, sliding=True)

    def _start_on(self, lw, index: int, spec: dict, accepts, what: str, state: dict | None = None):
        now = time.time()
        self.activate(index)
        pid = launch.run(spec, self.cfg)
        if pid is None:
            raise RuntimeError(f"could not start {what}")
        self.engine.expect_window(lw.id, now, accepts, pid=pid or None, what=what, state=state)
        return pid

    def _terminal_accepts(self, w: Win) -> bool:
        return w.app in titles.TERMINAL_APP_IDS

    def cmd_terminal(self, params):
        """A terminal on a workspace (kept from now on), optionally running a command."""
        lw, index = self._lw_for(params, register=True)
        if lw is None or index is None:
            raise RuntimeError("no such workspace")
        cwd = params.get("cwd") or lw.cwd or str(Path.home())
        command = params.get("command")
        self._start_on(lw, index, {"type": "terminal", "cwd": cwd, "command": command}, self._terminal_accepts,
                       "terminal")
        return True

    def cmd_webapp(self, params):
        lw, index = self._lw_for(params, register=True)
        if lw is None or index is None:
            raise RuntimeError("no such workspace")
        url = params["url"] if "://" in params["url"] else "https://" + params["url"]
        self._start_on(lw, index, {"type": "webapp", "url": url, "profile": None},
                       lambda w: titles.chrome_app_url(w.app) is not None, f"web app {url}")
        return True

    def cmd_new(self, params):
        """A new workspace from a template ('+'), or with a given command (fork, resume)."""
        now = time.time()
        e = self.engine
        cwd = params.get("cwd") or str(Path.home())
        lw = e.create_workspace(now, name=params.get("name") or "", cwd=cwd, after=params.get("after"))
        if lw is None:
            raise RuntimeError("could not create a pinned workspace (the compositor gave no id)")
        index = e.primary[lw.id].index
        self._switch(self._space(index))
        if params.get("agent") and params.get("command") is None:  # a new session of that agent
            params = {**params, "command": agents.get(params["agent"]).new_command()}
        if params.get("command") is not None:
            self._start_on(lw, index, {"type": "terminal", "cwd": cwd, "command": params["command"]},
                           self._terminal_accepts, "terminal")
            return lw.id
        tpls = config.templates(self.cfg)
        tpl = next((t for t in tpls if t.get("name") == params.get("template")), tpls[0])
        steps = tpl.get("steps") or [{"type": "claude"}]
        # the first agent's step ("claude", "codex"), else terminal step, is the workspace's terminal
        names = {a.name for a in agents.every()}
        first = next((s for s in steps if s.get("type") in names), None) or \
            next((s for s in steps if s.get("type") == "terminal"), None)
        for step in ([first] if first else []) + [s for s in steps if s is not first]:
            kind = step.get("type", "app")
            if kind in names or kind == "terminal":
                # its own command if it names one; else the agent's for an agent's step, a shell for a terminal
                run = step.get("command")
                if not run and kind in names:
                    run = agents.get(kind).command
                command = shell_command(run) if run else None
                self._start_on(lw, index, {"type": "terminal", "cwd": cwd, "command": command},
                               self._terminal_accepts, "terminal")
                continue
            argv = shlex.split((step.get("exec") or "").format(cwd=cwd, browser=launch.browser_new_window()))
            if argv:
                self._start_on(lw, index, {"type": "exec", "argv": argv, "cwd": cwd}, lambda w: True, argv[0])
        return lw.id

    def cmd_item_workspace(self, params):
        """An other source's item on a workspace of its own (sources.py): a kept workspace named
        after it, in its folder, bound to it. Asked for by you, it is shown, and the item's page
        opens there as a web app unless something is open there already. Asked for by the
        sidebar on its own ("auto": a source with "workspaces": true) it is only made, so nothing
        takes your screen, and an item whose workspace you closed is left alone. A source with a
        "terminal" command opens that in a terminal instead of the page."""
        source, item = str(params["source"]), str(params["item"])
        auto = bool(params.get("auto"))
        e = self.engine
        lw = e.layout.for_item(source, item)
        if lw is None:
            if auto and e.layout.had_item(source, item):
                return None
            lw = e.create_workspace(time.time(), name=str(params.get("title") or "")[:80],
                                    cwd=self._item_cwd(params.get("folder")), source=source, item=item)
            if lw is None:
                raise RuntimeError("could not create a pinned workspace (the compositor gave no id)")
        if auto:
            return lw.id
        if lw.id not in e.primary:  # kept, not on the desktop now
            self.cmd_reopen({"ws": lw.id})
            if lw.slots:
                return lw.id
        index = e.primary[lw.id].index
        url = params.get("url") or ""
        command = sources.item_command(next((s for s in sources.load_sources() if s["key"] == source), None), item, url)
        if lw.slots or (e.obs is not None and e.obs.windows_on(index)) \
                or not (command or url.startswith(("http://", "https://"))):
            self.cmd_activate({"ws": lw.id})
            return lw.id
        if command:
            wrapped = shell_command(command)
            self._start_on(lw, index, {"type": "terminal", "cwd": lw.cwd or str(Path.home()), "command": wrapped},
                           self._terminal_accepts, f"terminal for {source} item {item}", state={"command": wrapped})
        else:
            self._start_on(lw, index, {"type": "webapp", "url": url, "profile": None},
                           lambda w: titles.chrome_app_url(w.app) is not None, f"web app {url}")
        return lw.id

    @staticmethod
    def _item_cwd(folder) -> str:
        """An item's folder (under home) as its workspace's directory; home when it has none."""
        home = Path.home()
        if isinstance(folder, str) and folder:
            path = (home / folder).resolve()
            if path.is_dir() and (path == home or home in path.parents):
                return str(path)
        return str(home)

    def cmd_fork(self, params):
        """Like /fork: the conversation so far in a new session, on a workspace of its own,
        by the session's own agent."""
        session = params["session"]
        lw, _ = self._lw_for(params)
        cwd = (lw.cwd if lw else "") or self.detector.cwd_of(session) or str(Path.home())
        command = self.detector.agent_of(session).fork_command(session, cwd)
        if command is None:
            raise RuntimeError(f"{self.detector.agent_of(session).label} cannot fork a session")
        return self.cmd_new({"cwd": cwd, "after": lw.id if lw else None, "command": command})

    def cmd_resume(self, params):
        """A closed session from the recent list, on a new workspace, by its own agent."""
        session = params["session"]
        rec = next((r for r in self.engine.layout.recent if r.get("session") == session), {})
        cwd = params.get("cwd") or rec.get("cwd") or str(Path.home())
        ws_id = self.cmd_new({"cwd": cwd, "name": rec.get("workspace") or "",
                              "command": agents.get(rec.get("agent")).resume_command(session, cwd)})
        self.engine.layout.recent = [r for r in self.engine.layout.recent if r.get("session") != session]
        self.engine.dirty = True
        return ws_id

    def cmd_reopen(self, params):
        """A kept workspace that is not on the desktop: pin one for it and start what it had."""
        lw, _ = self._lw_for(params)
        if lw is None:
            raise RuntimeError("no such workspace")
        now = time.time()
        e = self.engine
        e._pinned_this_step = False
        space = e._ensure_space(lw, now)
        if space is None:
            raise RuntimeError("could not create a pinned workspace")
        self._switch(self._space(space.index))
        if not e.start_restore(now, "reopen", delay=0, scope={lw.id}):
            raise RuntimeError("a restore is already running")
        return lw.id

    def cmd_close(self, params):
        """Close a workspace: every window on it gets a normal close request, and it leaves the layout."""
        lw, index = self._lw_for(params)
        self.engine.leave_when_empty(index, time.time())
        self._remove_when_empty(index)
        if lw is not None:
            wins = self.engine.remove_workspace(lw.id, time.time(), "closed by you")
        else:
            obs = self.engine.obs
            wins = [w.id for w in obs.windows_on(index)] if obs and index is not None else []
        for wid in wins:
            tl = self._tl.get(wid)
            if tl is not None:
                self.ws.close_toplevel(tl)
        return len(wins)

    def cmd_history(self, params):
        """The closed workspaces, newest first: asked for when the history opens, not pushed
        with every state change."""
        return [self._history_item(h) for h in self.engine.layout.history]

    def cmd_history_restore(self, params):
        """A closed workspace from the history: back on a new workspace, windows reopened."""
        lw = self.engine.restore_from_history(params["id"], time.time())
        space = self.engine.primary.get(lw.id)
        if space is not None:
            self.activate(space.index)
        return lw.id

    def cmd_chrome_windows(self, params):
        """What each connected extension reports, for `sidelit chrome status -v`."""
        return {inst: {"profile": c.get("profile"), "windows": c.get("windows") or []} for inst, c in self.chrome.items()}

    def cmd_history_forget(self, params):
        entry = self.engine.layout.take_from_history(params["id"])
        self.engine.dirty = self.engine.changed = entry is not None
        return entry is not None

    def cmd_forget(self, params):
        """Stop keeping a workspace: unpinned and out of the layout, its windows left alone."""
        lw, _ = self._lw_for(params)
        if lw is None:
            return False
        self.engine.remove_workspace(lw.id, time.time(), "forgotten by you")
        return True

    def cmd_forget_slot(self, params):
        self.engine.forget_slot(params["slot"], time.time())
        return True

    def cmd_rename(self, params):
        lw, _ = self._lw_for(params, register=bool(params.get("name")))
        if lw is None:
            return False
        self.engine.rename(lw.id, (params.get("name") or "").strip())
        return True

    def cmd_reorder(self, params):
        """Drag a tab: put workspace `space` before/after `other`."""
        if "move" not in self.ws.caps:
            raise RuntimeError("this desktop cannot reorder workspaces")
        a, b = self._space(int(params["space"])), self._space(int(params["other"]))
        if a is None or b is None or a.coords == b.coords:
            return False
        before = bool(params.get("before", True))
        self.ws.move(a, b, before=before)
        e = self.engine
        la, lb = e.owner.get(a.index), e.owner.get(b.index)
        if la and lb:
            e.reorder(la, lb, before)
        return True

    def cmd_restore(self, params):
        now = time.time()
        return self.engine.start_restore(now, "requested", delay=float(params.get("delay", 0)),
                                         claudes_only=not self.cfg.get("restore_windows", True))

    def cmd_reapply(self, params):
        self.engine.reapply(time.time())
        return True

    def cmd_why(self, params):
        """The journal, for whatever TEXT names: a workspace (by name or Claude title), a
        window title, an app, a slot id."""
        text = (params.get("text") or "").strip()
        also = []
        if text:
            low = text.lower()
            for lw, slot in self.engine.layout.slots():
                if low in (lw.name or "").lower() or low in slot.title.lower() or low in slot.app.lower() \
                        or low in str(slot.state.get("claude_title", "")).lower():
                    also += [lw.id, slot.id] if low in (lw.name or "").lower() else [slot.id]
            for lw in self.engine.layout.workspaces:
                if low in (lw.name or "").lower():
                    also.append(lw.id)
        recs = journal_mod.read(config.JOURNAL, text, int(params.get("limit", 40)), also=sorted(set(also)))
        return [journal_mod.format_record(r) for r in recs]

    def cmd_reload(self, params):
        self._on_hup()
        return True


def main() -> int:
    try:
        d = Daemon()
    except RuntimeError as e:
        say(f"sidelit daemon: {e}")
        return 1
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
