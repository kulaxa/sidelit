"""Which window shows which coding agent session, and what each is doing.

Agent-neutral: what an agent itself says comes through its adapter (agents.py), the window
logic is the same for all of them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from sidelit import agents as agents_mod
from sidelit.core.titles import bare_title, looks_like_claude, title_state
from sidelit.procs import ancestors, descendants, in_terminal, load_json, proc_table, process_started

# `status` in an agent's own list of its sessions -> tab state: "waiting" is a permission
# prompt or a question, "idle" the turn is over. Everything else is done.
LIVE_STATE = {"busy": "busy", "waiting": "waiting"}

# ---- which window shows which session ----------------------------------------------------
# Evidence, strongest first. A mapping holds until stronger evidence (or equally strong,
# newer evidence) says otherwise, or its window or session goes.
BY_PANE = 4     # the terminal says which of its windows has the session's pane (Terminator's plugin)
BY_LAUNCH = 3   # the session's process ancestry contains the terminal we started for that window
BY_PID = 3      # ...or the window's own pid (compositors that report it)
BY_FOCUS = 2    # you submitted a prompt while that terminal had the keyboard
BY_TITLE = 1    # the terminal's title is what the session's would be
FOCUS_LAG_S = 0.6  # a focus change reaches us a little after it happens
SAME_PROCESS = ("clear", "compact")  # session starts in a process that was running already
# what names a pane (the hook's PANE_VARS), innermost first: tmux in a Terminator pane is tmux's
PANE_KEYS = ("TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID", "TERMINATOR_UUID")
WOKEN_MAX_S = 300  # an agent's own list alone says it works again for at most this long
AGENT_QUIET_S = 1800  # a subagent not heard from for this long is taken for gone (its stop lost)
HELPER_QUIET_S = 30  # the same for one with no transcript: the agent's own helper, which never stops
TIDY_S = 60  # the record of a session gone this long is deleted


# terminal emulators' process names (as /proc says them: 15 characters at most)
TERMINAL_COMMS = {"terminator", "konsole", "kgx", "kitty", "foot", "alacritty", "gnome-terminal-",
                  "xfce4-terminal", "mate-terminal", "xterm", "wezterm-gui", "ghostty", "tilix", "ptyxis-agent"}


def outer_agent(pid: int, table: dict[int, tuple[int, str]], comms: set[str] | None = None) -> int:
    """The agent process that started agent `pid` (a Claude running `claude -p`, a Codex
    running `codex exec`), or 0. Not one above a terminal window: a session in a window an
    agent opened is yours, in that window."""
    comms = comms if comms is not None else agents_mod.comms()
    for p in ancestors(table.get(pid, (0, ""))[0], table):
        if table[p][1] in TERMINAL_COMMS:
            return 0
        if table[p][1] in comms:
            return p
    return 0


def tmux_clients(panes: set[tuple[str, str]]) -> dict[tuple[str, str], list[int]]:
    """(socket, pane) -> the processes of the tmux clients showing that pane's session: the
    terminals a session in tmux is on screen in, none while it is detached. The socket is the
    first field of the pane's $TMUX ("" for tmux's default)."""
    out: dict[tuple[str, str], list[int]] = {}
    if not panes or not shutil.which("tmux"):
        return out
    for sock in {s for s, _ in panes}:
        base = ["tmux"] + (["-S", sock] if sock else [])
        try:
            listed = subprocess.run(base + ["list-panes", "-a", "-F", "#{pane_id} #{session_id}"],
                                    capture_output=True, text=True, timeout=2).stdout
            attached = subprocess.run(base + ["list-clients", "-F", "#{client_pid} #{session_id}"],
                                      capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        session_of = dict(line.split(" ", 1) for line in listed.splitlines() if " " in line)
        shown: dict[str, list[int]] = {}
        for line in attached.splitlines():
            pid, _, session = line.partition(" ")
            if pid.isdigit():
                shown.setdefault(session, []).append(int(pid))
        for s, pane in panes:
            if s == sock and pane in session_of:
                out[(s, pane)] = shown.get(session_of[pane], [])
    return out


def pane_id(pane) -> str:
    """Which pane of its terminal an agent runs in, from what the hook recorded; "" when the
    terminal does not say."""
    if not isinstance(pane, dict):
        return ""
    return next((f"{k}={pane[k]}" for k in PANE_KEYS if pane.get(k)), "")


class Detector:
    """Which sessions each window shows, and what they are doing.

    Two sources. The hook of Sidelit's plugin (plugin/hooks/hook.py, `sidelit hooks install`)
    reports every session's events as they happen, with the process ancestry the hook ran in
    (or, where the hook cannot see the session's process, its adapter finds it: resolve()):
    status straight from the events, and the window from launch ancestry or from which window
    had the keyboard when you submitted a prompt. Without the plugin (or for a session started
    before it was installed), an agent that lists its own sessions (Claude Code's `claude agents
    --json`) has them matched by the title it puts on its terminal, and their status read from
    that list.

    A window can show several sessions: split panes or tabs of one terminal window. Each is
    shown on the strongest kind of evidence any of them has for that window: if some run in the
    window's own process (BY_PANE, BY_LAUNCH, BY_PID), those; else those you typed into there (BY_FOCUS);
    else the one whose title it shows. A session is shown in one window only, and in one pane
    only the newest (one suspended under another one in the same shell is not on screen).

    A box shows its main thread: subagents, and agents another agent started (its own `claude
    -p`, a test harness), are only counted while they work, and never take a window. Nor does an
    agent with no terminal (an app driving it), whose prompts are no one typing.

    A session in tmux runs under tmux's server, not under the terminal it is on screen in: the
    process ancestry of the tmux clients showing it stands in for the terminal's (tmux_clients),
    so it is placed as one run in the terminal itself, and follows when another terminal attaches.

    Terminator keeps all its windows in one process, so neither process nor launch says which
    of them a pane is in, and a pane dragged to another window kept only the keyboard's word for
    the window it left. Sidelit's Terminator plugin says which terminals each window has
    (BY_PANE, _through_terminator), so such a pane follows to its new window.

    `agents`: the adapters (agents.every()); `started(pid)` when a process started, which
    orders the sessions of a window; `tmux` asks tmux for its clients (tmux_clients);
    `terminator` lists Terminator's windows (terminator.windows()). The window mapping is kept
    in `windows`, so a daemon restart keeps it."""

    def __init__(self, agents=None, ttl: float = 1.0, started=process_started, windows: Path | None = None,
                 tmux=tmux_clients, terminator=None):
        from sidelit import config
        from sidelit import terminator as terminator_mod
        self.tmux_clients = tmux
        self.terminator_windows = terminator or terminator_mod.windows
        self.agents = list(agents if agents is not None else agents_mod.every())
        self.by_name = {a.name: a for a in self.agents}
        self.comms = {a.comm for a in self.agents}
        self.windows_file = Path(windows) if windows else self.agents[0].hook_dir / "windows.json" \
            if self.agents else config.RUNTIME_DIR / "claude" / "windows.json"
        self.ttl = ttl
        self.started_of = started
        self._at = 0.0
        self._live_at: dict[str, float] = {}           # agent -> when its list of sessions was asked
        self._quiet_at: dict[str, dict[str, float]] = {}    # agent -> session -> when its list last said it did not work
        self._busy_since: dict[str, dict[str, float]] = {}  # agent -> session -> since when the list says it works
        self.table: dict = {}
        self.sessions: list[dict] = []                 # the agents' own lists: the live ones, each with "agent"
        self.index: dict = {}
        self.hooked: dict[str, dict] = {}              # live sessions the hook reports, each with "agent"
        self.inner: dict[int, list[str]] = {}          # agent pid -> sessions of agents it started, working now
        self.window_of: dict[str, tuple] = {}          # session -> (window id, strength, evidence time)
        self.launched: dict[str, tuple] = {}           # window id -> (pid, its start) of what we started for it
        self.focus: list[tuple[float, str]] = []       # (when, window id) the keyboard moved
        self.focus_since = 0.0
        self._by_window: dict[str, list[str]] = {}     # window id -> the sessions it shows, oldest first
        self._load_windows()

    def agent_of(self, sid: str) -> agents_mod.Agent:
        """The adapter of a session this detector knows (Claude Code's for one it does not)."""
        rec = self.hooked.get(sid)
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None)
        return self.by_name.get((rec or sess or {}).get("agent")) or agents_mod.get(None)

    # ---- inputs
    def note_focus(self, now: float, wid: str | None):
        if not self.focus_since:
            self.focus_since = now
        if wid and (not self.focus or self.focus[-1][1] != wid):
            self.focus.append((now, wid))
            del self.focus[:-200]

    def focus_at(self, t: float) -> str | None:
        """The window that had the keyboard at `t`, if we were watching then."""
        if not self.focus_since or t < self.focus_since:
            return None
        hit = None
        for when, wid in self.focus:
            if when <= t:
                hit = wid
            else:
                break
        return hit

    def refresh(self, now: float, force: bool = False, table: dict | None = None):
        if not force and now - self._at < self.ttl:
            return
        self._at = now
        self.table = table if table is not None else proc_table()
        self.hooked = self._read_hooks(now)
        self.sessions = []
        for agent in self.agents:
            entries, at = agent.live_sessions(now, self.table)
            mine = [{**s, "agent": agent.name} for s in entries]
            self.sessions += mine
            if at is not None and at != self._live_at.get(agent.name, 0.0):
                self._live_at[agent.name] = at
                self._note_busy(agent.name, mine, at)
        self.index = self.session_title_index(self.sessions)

    def _note_busy(self, agent: str, listed: list[dict], at: float):
        """When an agent's list last said each of its sessions did not work, and since when it
        says it works."""
        busy = {s["sessionId"]: str(s.get("status") or "").lower() == "busy" for s in listed}
        quiet, since = self._quiet_at.setdefault(agent, {}), self._busy_since.setdefault(agent, {})
        for seen in (quiet, since):
            for sid in [s for s in seen if s not in busy]:
                del seen[sid]
        for sid, works in busy.items():
            if not works:
                quiet[sid] = at
                since.pop(sid, None)
            else:
                since.setdefault(sid, at)

    def session_title_index(self, sessions: list[dict]) -> dict[str, dict]:
        """bare title -> listed session, for every title a session may be showing on its terminal."""
        index: dict[str, dict] = {}
        for sess in sessions:
            agent = self.by_name.get(sess.get("agent"))
            if agent is None or not agent.window_title:
                continue
            for t in (agent.listed_name(sess.get("name"), sess.get("cwd") or ""),
                      agent.title(sess.get("sessionId", ""), sess.get("cwd", ""))):
                if t:
                    index.setdefault(t.strip(), sess)
        return index

    def _read_hooks(self, now: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for agent in self.agents:
            recs = []
            try:
                files = list(agent.hook_dir.glob("*.json"))
            except OSError:
                continue
            for f in files:
                if f.name == "windows.json":
                    continue
                rec = load_json(f, None)
                if isinstance(rec, dict) and rec.get("session"):
                    recs.append((f, {**rec, "agent": agent.name}))
            agent.resolve([r for _, r in recs if r.get("state") != "ended"], self.table)
            for f, rec in recs:
                pid = int(rec.get("pid") or 0)
                alive = pid and self.table.get(pid, (0, ""))[1] == agent.comm
                if rec.get("state") == "ended" or not alive:
                    if now - float(rec.get("t") or 0) > TIDY_S:  # long gone: tidy up
                        for p in (f, f.with_suffix(".prompt"), f.with_suffix(".lock")):
                            try:
                                p.unlink()
                            except OSError:
                                pass
                    continue
                try:
                    rec["prompt_t"] = float(f.with_suffix(".prompt").read_text())
                except (OSError, ValueError):
                    pass
                rec["outer"] = outer_agent(pid, self.table, self.comms)
                rec["windowless"] = bool(rec["outer"]) or not in_terminal(pid)
                out[rec["session"]] = rec
        self._through_tmux(out)
        self.inner = {}
        for rec in out.values():
            if rec["outer"] and rec.get("state") in ("busy", "waiting"):
                self.inner.setdefault(rec["outer"], []).append(rec["session"])
        # /clear, /resume and friends start a new session in the same agent process: the
        # newest one is the live one, and it inherits the window
        by_pid: dict[int, list[dict]] = {}
        for rec in out.values():
            by_pid.setdefault(int(rec.get("pid") or 0), []).append(rec)
        for pid, recs in by_pid.items():
            if len(recs) < 2:
                continue
            recs.sort(key=lambda r: (float(r.get("started") or 0), float(r.get("t") or 0)))
            newest = recs[-1]
            for old in recs[:-1]:
                out.pop(old["session"], None)
                if old["session"] in self.window_of and newest["session"] not in self.window_of:
                    self.window_of[newest["session"]] = self.window_of[old["session"]]
        return out

    def _through_tmux(self, recs: dict[str, dict]):
        """Add to each tmux session's ancestry that of the tmux clients showing it now."""
        where: dict[str, tuple[str, str]] = {}
        for sid, rec in recs.items():
            pane = rec.get("pane") if isinstance(rec.get("pane"), dict) else {}
            if pane.get("TMUX_PANE") and pane.get("TMUX"):
                where[sid] = (str(pane["TMUX"]).split(",")[0], str(pane["TMUX_PANE"]))
        if not where:
            return
        clients = self.tmux_clients(set(where.values()))
        for sid, key in where.items():
            extra = [p for client in clients.get(key, []) for p in ancestors(client, self.table)]
            if extra:
                recs[sid]["ancestors"] = list(recs[sid].get("ancestors") or []) + extra

    # ---- the mapping
    def assign(self, windows: dict, launched_pid: dict[str, int] | None = None):
        """Work out which window shows which session. `windows`: id -> Win."""
        from sidelit.core.titles import TERMINAL_APP_IDS
        launched_before = dict(self.launched)
        launched_pid = self._launches(windows, launched_pid or {})
        live = {sid for sid, rec in self.hooked.items() if not rec["windowless"]} \
            | {s.get("sessionId") for s in self.sessions if s.get("pid") and not self._windowless(s)}
        before = dict(self.window_of)
        for sid in list(self.window_of):
            if sid not in live or self.window_of[sid][0] not in windows:
                del self.window_of[sid]
        by_title: dict[str, dict[str, list[str]]] = {a.name: {} for a in self.agents}  # agent -> title key -> windows
        by_pid: dict[int, list[str]] = {}
        for wid, w in windows.items():
            if w.app in TERMINAL_APP_IDS:
                for agent in self.agents:
                    key = agent.window_key(w.title)
                    if key is not None:
                        by_title[agent.name].setdefault(key, []).append(wid)
            if w.pid:
                by_pid.setdefault(w.pid, []).append(wid)
        for sid, rec in self.hooked.items():
            if rec["windowless"]:
                continue  # its terminal, if any, is another agent's
            agent = self.by_name[rec["agent"]]
            anc = set(rec.get("ancestors") or [])
            found = []
            for wid, pid in launched_pid.items():
                if pid in anc and wid in windows:
                    found.append((BY_LAUNCH, float(rec.get("started") or rec.get("t") or 0), wid))
            for pid, wids in by_pid.items():
                # (a terminal that keeps all its windows in one process says nothing about which)
                if pid in anc and len(wids) == 1:
                    found.append((BY_PID, float(rec.get("t") or 0), wids[0]))
            title = agent.title_key(sid, rec)
            prompt_t = rec.get("prompt_t")
            if prompt_t:
                wid = self.focus_at(float(prompt_t) + FOCUS_LAG_S)
                w = windows.get(wid) if wid else None
                # A prompt is submitted when you press Enter, or, typed while the agent worked,
                # when its turn ends, by which time you may be on another session: the
                # terminal that had the keyboard counts only if its title may be this session's
                shown = agent.focus_key(w.title) if w is not None and w.app in TERMINAL_APP_IDS else None
                if shown is not None and (not title or shown == title):
                    found.append((BY_FOCUS, float(prompt_t), wid))
            started = float(rec.get("started") or 0)
            if started and not prompt_t and rec.get("source") not in SAME_PROCESS:
                # until its first prompt: you started it by typing its command in a terminal, the
                # one with the keyboard then (a new tab or pane of a window that has a Claude in
                # it already gets a box of its own at once)
                wid = self.focus_at(started)
                w = windows.get(wid) if wid else None
                if w is not None and w.app in TERMINAL_APP_IDS:
                    found.append((BY_FOCUS, started, wid))
            hits = by_title[agent.name].get(title or "\0", [])
            if len(hits) == 1:
                found.append((BY_TITLE, float(rec.get("t") or 0), hits[0]))
            if found:
                self._offer(sid, max(found))
        # sessions the hook does not know (started before it was installed): by title only
        for agent in self.agents:
            if not agent.window_title:
                continue
            for title, wids in by_title[agent.name].items():
                sess = self.index.get(title)
                if sess and sess.get("agent") == agent.name and len(wids) == 1 \
                        and sess.get("sessionId") not in self.hooked and not self._windowless(sess):
                    self._offer(sess["sessionId"], (BY_TITLE, 0.0, wids[0]))
        self._through_terminator(windows)
        # each window shows the sessions with the strongest kind of evidence for it; a title
        # alone names only one, the newest
        claims: dict[str, list[tuple]] = {}
        for sid, (wid, strength, t) in self.window_of.items():
            claims.setdefault(wid, []).append((strength, t, sid))
        self._by_window = {}
        for wid, cs in claims.items():
            top = min(max(c[0] for c in cs), BY_LAUNCH)  # (a pane's word or a process's: as good)
            shown = self._one_per_pane([c for c in cs if c[0] >= top] if top >= BY_FOCUS else [max(cs)])
            for _, _, sid in cs:
                if sid not in shown:
                    self.window_of.pop(sid, None)
            self._by_window[wid] = sorted(shown, key=lambda s: (self._started(s), s))
        if self.window_of != before or self.launched != launched_before:
            self._save_windows()

    def _through_terminator(self, windows: dict):
        """Each session in a Terminator pane, in the window Terminator says has that pane. Its
        windows are matched to ours by title, else by the window the other sessions in it are
        known to be in, one not already taken."""
        uuid_of = {}
        for sid, rec in self.hooked.items():
            pane = rec.get("pane") if isinstance(rec.get("pane"), dict) else {}
            if pane.get("TERMINATOR_UUID") and not pane.get("TMUX_PANE") and not rec["windowless"]:
                uuid_of[sid] = str(pane["TERMINATOR_UUID"])
        if not uuid_of:
            return
        ours = {wid: w for wid, w in windows.items() if w.app == "terminator"}
        todo = []
        for tw in self.terminator_windows():  # (one with no session in it still takes its window)
            members = [sid for sid, uuid in uuid_of.items() if uuid in tw["terminals"]]
            titled = [wid for wid, w in ours.items() if w.title and w.title == tw["title"]]
            known = {self.window_of[s][0] for s in members if s in self.window_of and self.window_of[s][0] in ours}
            todo.append((members, tw["since"], titled, known))
        # (one whose title says nothing goes by its sessions' windows, unless another of
        # Terminator's windows has a session that window was known for: one of them moved)
        taken: set[str] = set()
        while todo:
            placed = []
            claimed: dict[str, int] = {}
            for _, _, _, known in todo:
                for wid in known - taken:
                    claimed[wid] = claimed.get(wid, 0) + 1
            for item in todo:
                members, since, titled, known = item
                wids = [w for w in titled if w not in taken] if len(titled) == 1 else []
                left = [w for w in known - taken if claimed.get(w) == 1]
                wids = wids or (left if len(left) == 1 else [])
                if wids:
                    taken.add(wids[0])
                    placed.append(item)
                    for sid in members:
                        self._offer(sid, (BY_PANE, since, wids[0]))
            if not placed:
                break
            todo = [item for item in todo if item not in placed]

    def _one_per_pane(self, claims: list[tuple]) -> list[str]:
        """The sessions of these claims, but of two in the same pane only the one heard from
        last: the other is suspended under it (Ctrl+Z, then another claude)."""
        newest: dict[str, tuple[float, str]] = {}
        out = []
        for _, _, sid in claims:
            pane = pane_id(self.hooked.get(sid, {}).get("pane"))
            if not pane:
                out.append(sid)
                continue
            heard = float(self.hooked[sid].get("t") or 0)
            if pane not in newest or heard > newest[pane][0]:
                newest[pane] = (heard, sid)
        return out + [sid for _, sid in newest.values()]

    def _started(self, sid: str) -> float:
        """When this session's process started (it keeps its place through /clear), else when
        the session did."""
        rec = self.hooked.get(sid) or {}
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None) or {}
        pid = int(rec.get("pid") or sess.get("pid") or 0)
        return (self.started_of(pid) if pid else None) or float(rec.get("started") or rec.get("t") or 0)

    def _windowless(self, sess: dict) -> bool:
        """A session another agent started, or one with no terminal."""
        pid = int(sess.get("pid") or 0)
        return bool(outer_agent(pid, self.table, self.comms)) or not in_terminal(pid)

    def _launches(self, windows: dict, fresh: dict[str, int]) -> dict[str, int]:
        """Window id -> pid of what we started for it: the engine's, which a daemon restart
        empties, and what it said before, while that process runs (a pane split off it later
        is still in the terminal we started)."""
        for wid, pid in fresh.items():
            if self.launched.get(wid, (None,))[0] != pid:
                self.launched[wid] = (pid, self.started_of(pid))
        for wid, (pid, at) in list(self.launched.items()):
            if wid not in windows or pid not in self.table or self.started_of(pid) != at:
                del self.launched[wid]
        return {wid: pid for wid, (pid, _) in self.launched.items()}

    def _offer(self, sid: str, evidence: tuple):
        strength, t, wid = evidence
        cur = self.window_of.get(sid)
        if cur is None or strength > cur[1] or (strength == cur[1] and t > cur[2]):
            self.window_of[sid] = (wid, strength, t)

    def _save_windows(self):
        try:
            self.windows_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.windows_file.with_name("windows.json.tmp")
            data = {s: list(v) for s, v in self.window_of.items()}
            data["launched"] = {w: list(v) for w, v in self.launched.items()}
            tmp.write_text(json.dumps(data))
            tmp.replace(self.windows_file)
        except OSError:
            pass

    def _load_windows(self):
        """A daemon restart keeps what it had worked out (window ids live as long as the
        compositor session)."""
        data = load_json(self.windows_file, {})
        if isinstance(data, dict):
            for sid, v in data.items():
                if isinstance(v, list) and len(v) == 3:
                    self.window_of[sid] = (str(v[0]), int(v[1]), float(v[2]))
            launched = data.get("launched")
            for wid, v in (launched.items() if isinstance(launched, dict) else ()):
                if isinstance(v, list) and len(v) == 2:
                    self.launched[str(wid)] = (int(v[0]), None if v[1] is None else float(v[1]))

    # ---- outputs
    def status(self, sid: str) -> str:
        """busy | waiting | shell | done: a session whose subagents work is busy too"""
        state = self._status(sid)
        return "busy" if state in ("done", "shell") and self.agents_at_work(sid) else state

    def _status(self, sid: str) -> str:
        rec = self.hooked.get(sid)
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None)
        live_state = LIVE_STATE.get(str(sess.get("status") or "").lower(), "done") if sess else None
        if rec is not None:
            state = rec.get("state") or "done"
            # the hook cannot see a background task waking Claude up again (no prompt, and no
            # event until it uses a tool or stops): Claude Code's list says it did not work after
            # the hook's last word, and then that it works. (Busy all along is the turn that ended,
            # still winding down: its other Stop hooks, say.) An agent that works sends events,
            # so after WOKEN_MAX_S without one the list's word alone is taken for stale.
            agent = rec["agent"]
            heard, quiet = float(rec.get("t") or 0), self._quiet_at.get(agent, {}).get(sid, 0.0)
            woke = self._busy_since.get(agent, {}).get(sid, 0.0)
            if state == "done" and live_state == "busy" and quiet > heard and woke > quiet \
                    and self._at - woke < WOKEN_MAX_S:
                return "busy"
            if state == "done" and rec.get("shells"):
                return "shell"  # (the turn is over; a shell it started is not)
            return state if state in ("busy", "waiting", "done") else "done"
        return live_state or "done"

    def agents_at_work(self, sid: str) -> int:
        """Subagents of this session working now, and agents it started that work."""
        subagents, started = self._at_work(sid)
        return len(subagents) + len(started)

    def models_at_work(self, sid: str) -> list[str | None]:
        """The model of each agent agents_at_work counts (None: not known, or not yet)."""
        subagents, started = self._at_work(sid)
        rec, agent = self.hooked.get(sid), self.agent_of(sid)
        return [agent.subagent_model(sid, rec, a) for a in subagents] + \
            [self.agent_of(s).model(s, self.hooked.get(s)) for s in started]

    def _at_work(self, sid: str) -> tuple[list[str], list[str]]:
        """(this session's subagents heard from lately, sessions of the agents it started that work).
        A subagent with no transcript is heard from lately only for HELPER_QUIET_S: Claude Code's
        own helpers send events with an agent_id but no SubagentStop, and would count for half an
        hour, each a "Model not known yet"."""
        rec = self.hooked.get(sid) or {}
        sess = next((s for s in self.sessions if s.get("sessionId") == sid and s.get("pid")), None)
        pid = int(rec.get("pid") or (sess or {}).get("pid") or 0)
        agent = self.agent_of(sid)
        heard = [(a, self._at - float(t)) for a, t in (rec.get("agents") or {}).items()]
        lately = [a for a, ago in heard if ago < HELPER_QUIET_S
                  or ago < AGENT_QUIET_S and agent.keeps_subagent(sid, rec, a)]
        return lately, list(self.inner.get(pid, ()))

    def for_window(self, title: str, pid: int = 0, wid: str | None = None) -> dict | None:
        """The first session this window shows (claudes_for), or None."""
        return next(iter(self.claudes_for(title, pid, wid)), None)

    def claudes_for(self, title: str, pid: int = 0, wid: str | None = None) -> list[dict]:
        """{"session", "agent", "title", "status", "agents", "models", "cwd", "pid", "pane", "started",
        "how", "hooked"} for each session this window shows, in the order they started."""
        sids = self._by_window.get(wid) if wid else None
        if sids:
            return [self._info(sid, self.window_of.get(sid, (None, 0, 0))[1]) for sid in sids]
        # no mapping yet (first look, no hooks): the title, then the process tree
        sess = None
        if looks_like_claude(title) and title_state(title) != "other":
            sess = self.index.get(bare_title(title) or "")
        if sess is None and pid:
            kids = descendants([pid], self.table)
            sess = next((s for s in self.sessions if s.get("pid") and int(s["pid"]) in kids
                         and not self._windowless(s)), None)
        if sess is None or sess.get("sessionId") in self.window_of:
            return []  # (a session shown in a window we know is not also in this one)
        return [self._info(sess["sessionId"], BY_TITLE)]

    def _info(self, sid: str, how: int) -> dict:
        """What a box says of a session. Its title is the session's own, never its window's: that
        is whichever pane has the keyboard, maybe another session's ("" while it has none)."""
        sess = next((s for s in self.sessions if s.get("sessionId") == sid), None)
        rec = self.hooked.get(sid)
        agent = self.agent_of(sid)
        cwd = (rec or {}).get("cwd") or (sess or {}).get("cwd") or ""
        return {"session": sid, "agent": agent.name,
                "title": agent.title(sid, cwd, rec) or agent.listed_name((sess or {}).get("name"), cwd) or "",
                "status": self.status(sid), "agents": self.agents_at_work(sid), "models": self.models_at_work(sid),
                "cwd": cwd,
                "pid": int((rec or sess or {}).get("pid") or 0), "pane": dict((rec or {}).get("pane") or {}),
                "started": self._started(sid), "prompt_t": float((rec or {}).get("prompt_t") or 0),
                "how": {BY_PANE: "pane", BY_LAUNCH: "launch", BY_FOCUS: "focus", BY_TITLE: "title"}.get(how, "pid"),
                "hooked": rec is not None}

    def cwd_of(self, sid: str) -> str:
        """Where a session this detector knows works."""
        rec = self.hooked.get(sid) or next((s for s in self.sessions if s.get("sessionId") == sid), None) or {}
        return rec.get("cwd") or ""
