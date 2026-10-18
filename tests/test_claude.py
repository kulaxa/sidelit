"""Which window shows which Claude, and what it is doing: hook events, focus, launches, titles."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import claude  # noqa: E402
from sidelit import hooks  # noqa: E402
from sidelit.core.model import Win  # noqa: E402

CLAUDE_PID, SHELL_PID, TERM_PID = 500, 400, 300


def term(wid, title, pid=0, focused=False):
    return Win(id=wid, app="terminator", title=title, space=1, output="eDP-1", pid=pid, focused=focused)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.hook_dir = base / "claude"
        self.hook_dir.mkdir()
        # no real ~/.claude in here, and Claude Code's list of sessions is made up
        self._saved = claude.PROJECTS
        claude.PROJECTS = base / "p"
        self.live: dict[int, dict] = {}
        self.table = {CLAUDE_PID: (SHELL_PID, "claude"), SHELL_PID: (TERM_PID, "zsh"), TERM_PID: (1, "terminator"),
                      501: (401, "claude"), 401: (301, "zsh"), 301: (1, "terminator")}
        self.headless: set[int] = set()
        self._in_terminal = claude.in_terminal
        claude.in_terminal = lambda pid: pid not in self.headless
        self.d = self.detector()

    def detector(self):
        return claude.Detector(hook_dir=self.hook_dir, agents=lambda now: (list(self.live.values()), now),
                               started=lambda pid: None)

    def tearDown(self):
        claude.PROJECTS = self._saved
        claude.in_terminal = self._in_terminal
        self.tmp.cleanup()

    def hook(self, sid, state, t, pid=CLAUDE_PID, prompt_t=None, **extra):
        rec = {"session": sid, "state": state, "t": t, "pid": pid, "cwd": "/tmp",
               "ancestors": [pid, pid - 100, pid - 200, 1], "started": extra.pop("started", t), **extra}
        (self.hook_dir / f"{sid}.json").write_text(json.dumps(rec))
        if prompt_t is not None:
            (self.hook_dir / f"{sid}.prompt").write_text(repr(prompt_t))

    def listed(self, sid, pid, status, name=None):
        """What `claude agents --json` says of a session"""
        self.live[pid] = {"pid": pid, "sessionId": sid, "cwd": "/tmp", "kind": "interactive", "status": status,
                          "startedAt": 1000, **({"name": name} if name else {})}

    def look(self, windows, now, launched=None):
        self.d.refresh(now, force=True, table=self.table)
        self.d.assign(windows, launched or {})
        return {wid: self.d.for_window(w.title, w.pid, wid) for wid, w in windows.items()}

    def test_the_terminal_you_typed_into_is_the_one(self):
        """Two Claudes with the same title: focus at prompt time tells them apart."""
        a, b = term("A", "✳ Fix tests"), term("B", "✳ Fix tests")
        self.d.note_focus(100.0, "B")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        seen = self.look({"A": a, "B": b}, 102.0)
        self.assertIsNone(seen["A"])
        self.assertEqual(seen["B"]["session"], "s1")
        self.assertEqual(seen["B"]["how"], "focus")
        self.assertEqual(seen["B"]["status"], "busy")

    def test_the_terminal_we_started_is_known_by_its_process(self):
        a = term("A", "✳ Resumed")
        self.hook("s1", "done", 100.0)
        seen = self.look({"A": a}, 101.0, launched={"A": TERM_PID})
        self.assertEqual(seen["A"]["session"], "s1")
        self.assertEqual(seen["A"]["how"], "launch")

    def test_status_follows_the_events(self):
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "waiting", 101.0, prompt_t=100.5)
        self.assertEqual(self.look({"A": a}, 102.0)["A"]["status"], "waiting")
        self.hook("s1", "busy", 103.0, prompt_t=100.5)
        self.assertEqual(self.look({"A": a}, 104.0)["A"]["status"], "busy")
        self.hook("s1", "done", 105.0, prompt_t=100.5, shells=1)  # the turn ended, a background shell runs on
        self.listed("s1", CLAUDE_PID, "idle")
        self.assertEqual(self.look({"A": a}, 106.0)["A"]["status"], "shell")
        self.hook("s1", "busy", 107.0, prompt_t=100.5)  # it ended and woke Claude up
        self.assertEqual(self.look({"A": a}, 108.0)["A"]["status"], "busy")

    def test_a_background_task_waking_claude_up_is_busy(self):
        """No hook event until it uses a tool or stops: Claude Code's list says it works again."""
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.listed("s1", CLAUDE_PID, "busy")
        self.hook("s1", "busy", 104.0, prompt_t=100.5)
        self.look({"A": a}, 104.5)
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        self.assertEqual(self.look({"A": a}, 106.0)["A"]["status"], "done")  # (the list a moment behind)
        self.listed("s1", CLAUDE_PID, "idle")
        self.assertEqual(self.look({"A": a}, 110.0)["A"]["status"], "done")
        self.listed("s1", CLAUDE_PID, "busy")  # the pipeline watcher reported
        self.assertEqual(self.look({"A": a}, 120.0)["A"]["status"], "busy")
        self.assertEqual(self.look({"A": a}, 125.0)["A"]["status"], "busy")

    def test_a_turn_still_winding_down_is_not_a_wake(self):
        """Your own slow Stop hook keeps Claude Code's list on busy after ours has said done."""
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.listed("s1", CLAUDE_PID, "busy")
        self.hook("s1", "busy", 103.0, prompt_t=100.5)
        self.look({"A": a}, 104.0)
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        for now in (110.0, 120.0, 130.0):
            self.assertEqual(self.look({"A": a}, now)["A"]["status"], "done")

    def test_the_list_alone_counts_for_five_minutes(self):
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        self.listed("s1", CLAUDE_PID, "idle")
        self.look({"A": a}, 110.0)
        self.listed("s1", CLAUDE_PID, "busy")
        self.assertEqual(self.look({"A": a}, 120.0)["A"]["status"], "busy")
        self.assertEqual(self.look({"A": a}, 120.0 + claude.WOKEN_MAX_S + 1)["A"]["status"], "done")

    def test_claude_code_is_asked_only_while_a_claude_runs(self):
        asked = []
        self.d = claude.Detector(hook_dir=self.hook_dir, agents=lambda now: (asked.append(now) or [], now))
        self.d.refresh(100.0, force=True, table={1: (0, "systemd"), 777: (os.getpid(), "claude")})
        self.assertEqual(asked, [])  # (only the turn checker's)
        self.d.refresh(101.0, force=True, table=self.table)
        self.assertEqual(asked, [101.0])

    def test_a_prompt_sent_while_you_are_on_another_claude_does_not_move_it(self):
        """Typed while it worked, it is submitted when its turn ends: the keyboard is elsewhere."""
        folder = claude.PROJECTS / claude.project_slug("/tmp")
        folder.mkdir(parents=True)
        for sid, title in (("s1", "Fix tests"), ("s2", "Write docs")):
            (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": title}) + "\n")
        a, b = term("A", "✳ Fix tests"), term("B", "✳ Write docs")
        self.hook("s1", "busy", 101.0, prompt_t=90.0)
        self.hook("s2", "busy", 101.0, pid=501, prompt_t=90.0)
        self.assertEqual(self.look({"A": a, "B": b}, 102.0)["A"]["session"], "s1")
        self.d.note_focus(110.0, "B")                      # you are on the other Claude
        self.hook("s1", "busy", 120.0, prompt_t=120.0)     # s1's queued prompt goes in
        seen = self.look({"A": a, "B": b}, 121.0)
        self.assertEqual((seen["A"]["session"], seen["B"]["session"]), ("s1", "s2"))

    def test_our_own_claude_p_is_not_a_session(self):
        self.table[777] = (os.getpid(), "claude")  # the turn checker's claude -p
        self.listed("checker", 777, "idle")
        self.listed("s9", CLAUDE_PID, "busy", name="Deploy")
        self.listed("gone", 999, "idle")           # (ended since the list was asked)
        self.d.refresh(100.0, force=True, table=self.table)
        self.assertEqual([s["sessionId"] for s in self.d.sessions], ["s9"])

    def test_a_claude_that_moved_into_a_session_clone_is_known_by_its_title(self):
        """Its transcript stays in the folder of the directory it started in."""
        folder = claude.PROJECTS / claude.project_slug("/repo")
        folder.mkdir(parents=True)
        (folder / "s1.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Staled message marking"}) + "\n")
        self.hook("s1", "done", 101.0, cwd="/repo-sessions/fix")
        seen = self.look({"A": term("A", "✳ Staled message marking")}, 102.0)
        self.assertEqual(seen["A"]["session"], "s1")

    def test_a_dead_claude_is_gone(self):
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        self.assertIsNotNone(self.look({"A": a}, 102.0)["A"])
        del self.table[CLAUDE_PID]  # killed, no SessionEnd
        self.assertIsNone(self.look({"A": term("A", "alex@laptop: ~")}, 103.0)["A"])

    def test_clear_in_the_same_claude_keeps_the_window(self):
        a = term("A", "✳ Old")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "done", 101.0, prompt_t=101.0, started=90.0)
        self.look({"A": a}, 102.0)
        self.hook("s2", "done", 110.0, started=110.0)  # /clear: new session, same process
        seen = self.look({"A": term("A", "✳ New")}, 111.0)
        self.assertEqual(seen["A"]["session"], "s2")

    def test_a_stronger_claim_wins_the_window(self):
        a, b = term("A", "✳ One"), term("B", "✳ Two")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        self.hook("s2", "busy", 101.0, pid=501, prompt_t=None)
        seen = self.look({"A": a, "B": b}, 102.0, launched={"A": 301})  # A was started for s2's terminal
        self.assertEqual(seen["A"]["session"], "s2")
        self.assertIsNone(seen["B"])

    def test_a_claude_started_by_a_claude_is_counted_not_shown(self):
        """(its process ancestry holds the outer one's terminal too, and it started later)"""
        self.table.update({502: (501, "claude")})  # s1 runs `claude -p` in its shell
        self.table[501] = (CLAUDE_PID, "bash")
        a = term("A", "✳ Fix tests")
        self.hook("s1", "busy", 101.0, started=90.0, agents={"a1": 100.0})
        self.hook("s2", "busy", 102.0, pid=502, started=101.0, ancestors=[502, 501, CLAUDE_PID, SHELL_PID, TERM_PID, 1])
        seen = self.look({"A": a}, 103.0, launched={"A": TERM_PID})
        self.assertEqual((seen["A"]["session"], seen["A"]["agents"]), ("s1", 2))
        self.hook("s1", "done", 103.5, started=90.0, agents={"a1": 100.0})
        self.assertEqual(self.look({"A": a}, 104.0, launched={"A": TERM_PID})["A"]["status"], "busy")
        self.hook("s2", "done", 104.0, pid=502, started=101.0, ancestors=[502, 501, CLAUDE_PID, SHELL_PID, TERM_PID, 1])
        self.assertEqual(self.look({"A": a}, 105.0, launched={"A": TERM_PID})["A"]["agents"], 1)

    def test_a_claude_an_app_drives_is_not_the_terminal_you_were_in(self):
        """Its prompts come while you are in another Claude's terminal, and it has no title yet."""
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        self.look({"A": a}, 102.0)
        self.headless.add(501)
        self.hook("s2", "done", 110.0, pid=501, prompt_t=109.0)
        seen = self.look({"A": a}, 111.0)
        self.assertEqual((seen["A"]["session"], seen["A"]["status"]), ("s1", "busy"))

    def test_subagents_at_work_keep_a_finished_claude_busy(self):
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "done", 101.0, prompt_t=100.5, agents={"a1": 101.0})
        self.assertEqual(self.look({"A": a}, 102.0)["A"]["status"], "busy")
        # one not heard from for half an hour: its stop got lost
        seen = self.look({"A": a}, 101.0 + claude.AGENT_QUIET_S + 1)
        self.assertEqual((seen["A"]["status"], seen["A"]["agents"]), ("done", 0))

    def test_without_hooks_the_title_still_works(self):
        self.listed("s9", CLAUDE_PID, "busy", name="Deploy")
        seen = self.look({"A": term("A", "⠂ Deploy")}, 100.0)
        self.assertEqual(seen["A"]["session"], "s9")
        self.assertEqual(seen["A"]["status"], "busy")
        self.assertFalse(seen["A"]["hooked"])
        self.listed("s9", CLAUDE_PID, "waiting", name="Deploy")
        self.assertEqual(self.look({"A": term("A", "✳ Deploy")}, 105.0)["A"]["status"], "waiting")
        self.listed("s9", CLAUDE_PID, "idle", name="Deploy")
        self.assertEqual(self.look({"A": term("A", "✳ Deploy")}, 110.0)["A"]["status"], "done")

    # ---- several Claudes in one window, several on one workspace
    def pane(self, sid, pid, t, started, term=TERM_PID, uuid=None, **extra):
        """A Claude in a pane of the terminal process `term`: its own shell under it."""
        self.table.update({pid: (pid - 1, "claude"), pid - 1: (term, "zsh")})
        self.hook(sid, extra.pop("state", "done"), t, pid=pid, started=started, ancestors=[pid, pid - 1, term, 1],
                  pane={"TERMINATOR_UUID": uuid or f"urn:uuid:{sid}"}, **extra)

    def sessions(self, seen, wid):
        return [c["session"] for c in seen[wid]]

    def look_all(self, windows, now, launched=None):
        self.d.refresh(now, force=True, table=self.table)
        self.d.assign(windows, launched or {})
        return {wid: self.d.claudes_for(w.title, w.pid, wid) for wid, w in windows.items()}

    def test_split_panes_of_a_terminal_we_started_show_every_claude(self):
        """Two Claudes in one Terminator window: both run in its process. In the order they started."""
        self.pane("s2", 702, 101.0, started=95.0)
        self.pane("s1", 602, 100.0, started=90.0)
        seen = self.look_all({"A": term("A", "✳ Review tests")}, 102.0, launched={"A": TERM_PID})
        self.assertEqual(self.sessions(seen, "A"), ["s1", "s2"])
        self.assertEqual([c["how"] for c in seen["A"]], ["launch", "launch"])
        self.assertEqual(seen["A"][1]["pane"], {"TERMINATOR_UUID": "urn:uuid:s2"})
        self.assertEqual(self.d.for_window("✳ Review tests", 0, "A")["session"], "s1")

    def test_each_claude_of_a_window_has_its_own_title(self):
        """(the window's title is only the one in the pane with the keyboard)"""
        folder = claude.PROJECTS / claude.project_slug("/tmp")
        folder.mkdir(parents=True)
        for sid, title in (("s1", "Pricing API"), ("s2", "Review tests")):
            (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": title}) + "\n")
        self.pane("s1", 602, 100.0, started=90.0)
        self.pane("s2", 702, 101.0, started=95.0)
        seen = self.look_all({"A": term("A", "✳ Review tests")}, 102.0, launched={"A": TERM_PID})
        self.assertEqual([c["title"] for c in seen["A"]], ["Pricing API", "Review tests"])

    def test_three_claudes_on_one_workspace(self):
        """A window with two panes, and a terminal of its own you typed into."""
        self.pane("s1", 602, 100.0, started=90.0)
        self.pane("s2", 702, 101.0, started=95.0)
        self.d.note_focus(99.0, "B")
        self.pane("s3", 802, 103.0, started=92.0, term=301, prompt_t=103.0)
        a, b = term("A", "✳ Pricing API"), term("B", "✳ Ask about the CSV format")
        seen = self.look_all({"A": a, "B": b}, 104.0, launched={"A": TERM_PID})
        self.assertEqual((self.sessions(seen, "A"), self.sessions(seen, "B")), (["s1", "s2"], ["s3"]))
        self.assertEqual(seen["B"][0]["how"], "focus")

    def test_two_sessions_in_one_window_you_typed_into(self):
        """A terminal that keeps every window in one process (a Terminator you opened yourself):
        the panes you typed into are told apart by the keyboard."""
        self.d.note_focus(99.0, "A")
        self.pane("s1", 602, 100.0, started=90.0, prompt_t=100.0)
        self.pane("s2", 702, 110.0, started=95.0, prompt_t=110.0)
        seen = self.look_all({"A": term("A", "✳ Claude Code"), "B": term("B", "alex@laptop: ~")}, 111.0)
        self.assertEqual((self.sessions(seen, "A"), seen["B"]), (["s1", "s2"], []))

    def test_a_title_alone_does_not_add_a_second_claude(self):
        """A fork has its parent's title: a window showing it is not where the fork runs."""
        folder = claude.PROJECTS / claude.project_slug("/tmp")
        folder.mkdir(parents=True)
        for sid in ("s1", "s2"):
            (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Fix tests"}) + "\n")
        self.pane("s1", 602, 100.0, started=90.0)
        self.pane("s2", 702, 101.0, started=95.0, term=999)  # in a terminal whose window we do not see
        self.table[999] = (1, "terminator")
        seen = self.look_all({"A": term("A", "✳ Fix tests")}, 102.0, launched={"A": TERM_PID})
        self.assertEqual(self.sessions(seen, "A"), ["s1"])

    def test_one_claude_per_pane(self):
        """Ctrl+Z on one Claude, then another in the same shell: only the one you see."""
        self.pane("s1", 602, 100.0, started=90.0, uuid="urn:uuid:p1")
        self.pane("s2", 702, 120.0, started=110.0, uuid="urn:uuid:p1")
        self.pane("s3", 802, 105.0, started=100.0, uuid="urn:uuid:p2")
        seen = self.look_all({"A": term("A", "✳ X")}, 121.0, launched={"A": TERM_PID})
        self.assertEqual(self.sessions(seen, "A"), ["s3", "s2"])

    def test_a_process_shared_by_two_windows_says_nothing(self):
        """GNOME Terminal and Konsole keep all their windows in one process: on KDE every window
        has its pid, so that pid does not say which window a Claude is in."""
        self.pane("s1", 602, 100.0, started=90.0)
        a, b = term("A", "alex@laptop: ~", pid=TERM_PID), term("B", "alex@laptop: ~", pid=TERM_PID)
        self.assertEqual(self.look_all({"A": a, "B": b}, 101.0), {"A": [], "B": []})
        seen = self.look_all({"A": a}, 102.0)  # one window of its own: that one
        self.assertEqual(self.sessions(seen, "A"), ["s1"])

    def test_a_box_keeps_its_place_through_clear(self):
        """Claudes are in the order their processes started: /clear starts a new session, not a new Claude."""
        self.d = claude.Detector(hook_dir=self.hook_dir, agents=lambda now: ([], now),
                                 started=lambda pid: {602: 50.0, 702: 60.0}.get(pid))
        self.pane("s1", 602, 100.0, started=90.0)
        self.pane("s2", 702, 101.0, started=95.0)
        self.assertEqual(self.sessions(self.look_all({"A": term("A", "✳ X")}, 102.0, {"A": TERM_PID}), "A"), ["s1", "s2"])
        self.hook("s1b", "done", 110.0, pid=602, started=110.0, ancestors=[602, 601, TERM_PID, 1],
                  pane={"TERMINATOR_UUID": "urn:uuid:s1"})  # /clear in the first pane
        seen = self.look_all({"A": term("A", "✳ X")}, 111.0, {"A": TERM_PID})
        self.assertEqual(self.sessions(seen, "A"), ["s1b", "s2"])

    def test_mapping_survives_a_daemon_restart(self):
        a, b = term("A", "✳ Fix tests"), term("B", "✳ Fix tests")
        self.d.note_focus(100.0, "B")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        self.look({"A": a, "B": b}, 102.0)
        self.d = self.detector()  # restarted: no focus history
        seen = self.look({"A": a, "B": b}, 200.0)
        self.assertEqual(seen["B"]["session"], "s1")
        self.assertIsNone(seen["A"])


class HookScriptTest(unittest.TestCase):
    def run_hook(self, home, event):
        env = {**os.environ, "SIDELIT_HOME": home}
        return subprocess.run([sys.executable, "-I", os.path.join(ROOT, "plugin", "hooks", "hook.py")],
                              input=json.dumps(event), text=True, capture_output=True, env=env, timeout=5)

    def test_hook_records_state_and_never_fails(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_hook(home, {"session_id": "x1", "hook_event_name": "Notification",
                                     "notification_type": "permission_prompt", "cwd": "/tmp"})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            rec = json.loads((Path(home) / "claude" / "x1.json").read_text())
            self.assertEqual(rec["state"], "waiting")
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit"})
            self.assertTrue((Path(home) / "claude" / "x1.prompt").exists())
            for bad in ("not json", {"hook_event_name": "Stop"}, {"session_id": "../etc", "hook_event_name": "Stop"}):
                env = {**os.environ, "SIDELIT_HOME": home}
                r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "plugin", "hooks", "hook.py")],
                                   input=bad if isinstance(bad, str) else json.dumps(bad), text=True,
                                   capture_output=True, env=env, timeout=5)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


    def test_the_pane_claude_runs_in_is_recorded(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "SIDELIT_HOME": home, "TERMINATOR_UUID": "urn:uuid:abc", "TMUX_PANE": "%3"}
            env.pop("KITTY_WINDOW_ID", None)
            subprocess.run([sys.executable, "-I", os.path.join(ROOT, "plugin", "hooks", "hook.py")],
                           input=json.dumps({"session_id": "x1", "hook_event_name": "SessionStart"}), text=True,
                           capture_output=True, env=env, timeout=5)
            rec = json.loads((Path(home) / "claude" / "x1.json").read_text())
            self.assertEqual((rec["pane"]["TERMINATOR_UUID"], rec["pane"]["TMUX_PANE"]), ("urn:uuid:abc", "%3"))
            self.assertEqual(claude.pane_id(rec["pane"]), "TMUX_PANE=%3")  # tmux inside Terminator: tmux's pane

    def test_a_background_task_waking_claude_is_not_you_typing(self):
        """(else the terminal that had the keyboard then would be taken for this Claude's)"""
        with tempfile.TemporaryDirectory() as home:
            prompt = Path(home) / "claude" / "x1.prompt"
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit",
                                 "prompt": "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"})
            self.assertFalse(prompt.exists())
            self.assertEqual(json.loads((Path(home) / "claude" / "x1.json").read_text())["state"], "busy")
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit", "prompt": "push it"})
            self.assertTrue(prompt.exists())

    def test_the_turn_that_ended_is_marked_until_the_next_event(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "claude" / "x1.json"
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Stop", "transcript_path": "/t.jsonl",
                                 "last_assistant_message": "Waiting for the deploy."})
            ended = json.loads(path.read_text())["turn_end"]
            self.assertEqual(json.loads(path.read_text())["said"], "Waiting for the deploy.")
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Notification", "notification_type": "idle_prompt"})
            self.assertEqual(json.loads(path.read_text())["turn_end"], ended)  # the idle reminder: same turn
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "PreToolUse"})
            self.assertNotIn("turn_end", json.loads(path.read_text()))
            self.assertNotIn("said", json.loads(path.read_text()))

    def test_the_state_is_the_main_threads_and_subagents_are_counted(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "claude" / "x1.json"
            read = lambda: json.loads(path.read_text())  # noqa: E731
            sub = {"agent_id": "a1", "agent_type": "general-purpose", "cwd": "/repo-sessions/agent"}
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit", "cwd": "/repo"})
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "SubagentStart", **sub})
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Stop", "background_tasks": [
                {"id": "a1", "type": "subagent", "status": "running"}]})
            self.assertEqual((read()["state"], list(read()["agents"])), ("done", ["a1"]))
            # the background agent works on: the main thread is still done, and where it was
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "PreToolUse", **sub})
            self.assertEqual((read()["state"], read()["cwd"]), ("done", "/repo"))
            self.assertIn("turn_end", read())
            # a permission it asks for waits for you, until it is answered
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "PermissionRequest", **sub})
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Notification", "notification_type": "permission_prompt"})
            self.assertEqual(read()["state"], "waiting")
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "PostToolUse", **sub})
            self.assertEqual(read()["state"], "done")
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "SubagentStop", **sub})
            self.assertEqual(read()["agents"], {})

    def test_a_missed_subagent_stop_is_corrected_when_the_turn_ends(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "claude" / "x1.json"
            for a in ("a1", "a2"):
                self.run_hook(home, {"session_id": "x1", "hook_event_name": "SubagentStart", "agent_id": a})
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Stop", "background_tasks": [
                {"id": "a2", "type": "subagent", "status": "running"}, {"id": "b1", "type": "shell", "status": "running"}]})
            self.assertEqual(list(json.loads(path.read_text())["agents"]), ["a2"])

    def test_the_shells_a_turn_left_running_are_kept_with_it(self):
        """(the turn is done all the same: the turn checker asks whether Claude waits on them)"""
        with tempfile.TemporaryDirectory() as home:
            read = lambda: json.loads((Path(home) / "claude" / "x1.json").read_text())  # noqa: E731
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Stop", "background_tasks": [
                {"id": "b1", "type": "shell", "status": "running", "command": "npm run dev"},
                {"id": "m1", "type": "monitor", "status": "running"}]})
            self.assertEqual((read()["state"], read()["shells"]), ("done", 1))
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Notification", "notification_type": "idle_prompt"})
            self.assertEqual((read()["state"], read()["shells"]), ("done", 1))  # (the same turn)
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit",
                                 "prompt": "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"})
            self.assertEqual(read()["state"], "busy")  # the shell ended and woke Claude up
            self.assertNotIn("shells", read())
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "Stop", "background_tasks": [
                {"id": "b1", "type": "shell", "status": "completed"}]})
            self.assertEqual(read()["state"], "done")
            self.assertNotIn("shells", read())
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "UserPromptSubmit", "prompt": "go on"})
            self.run_hook(home, {"session_id": "x1", "hook_event_name": "StopFailure", "error": "rate_limit"})
            self.assertEqual(read()["state"], "done")


THEIRS = {"type": "command", "command": "notify-send done"}
OLD_HOOK = {"type": "command", "command": "/home/alex/.local/bin/sidelit-hook", "timeout": 5}


class PluginTest(unittest.TestCase):
    """`sidelit hooks install|uninstall`: Claude Code's own plugin commands, and the entries Sidelit
    0.5 wrote into settings.json going once the plugin is in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls: list[tuple] = []
        self.answers: dict[str, tuple] = {}
        self.installed = None
        patches = [mock.patch.object(hooks, "SETTINGS", Path(self.tmp.name) / "settings.json"),
                   mock.patch.object(hooks, "run_claude", self.fake_claude),
                   mock.patch.object(hooks, "plugin_state", lambda: self.installed),
                   mock.patch.object(claude, "claude_binary", lambda: "/usr/bin/claude")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_claude(self, *args):
        self.calls.append(args)
        return self.answers.get(args[0] if args[0] != "marketplace" else args[1], (0, {"outcome": "ok"}, ""))

    def settings(self, data=None):
        if data is not None:
            hooks.SETTINGS.write_text(json.dumps(data))
        return json.loads(hooks.SETTINGS.read_text())

    def test_install_uses_claude_codes_commands(self):
        hooks.install()
        self.assertEqual(self.calls, [("marketplace", "add", str(hooks.marketplace_dir()), "--scope", "user"),
                                      ("install", "sidelit@sidelit", "--scope", "user")])
        self.assertTrue((hooks.marketplace_dir() / ".claude-plugin" / "marketplace.json").exists())
        self.assertFalse(hooks.SETTINGS.exists())  # (nothing of ours was in there: not even read into being)

    def test_sidelit_05s_entries_go_once_the_plugin_is_in_and_yours_stay(self):
        before = {"model": "opus", "hooks": {
            "Stop": [{"hooks": [THEIRS]}, {"hooks": [OLD_HOOK]}],
            "PreToolUse": [{"matcher": "*", "hooks": [{**OLD_HOOK, "async": True}]}],
            "SessionEnd": [{"hooks": [OLD_HOOK, THEIRS]}]}}
        self.settings(before)
        self.assertTrue(hooks.present())
        said = hooks.install()
        self.assertTrue(any("removed the hook Sidelit 0.5" in line for line in said), said)
        self.assertEqual(self.settings(), {"model": "opus", "hooks": {"Stop": [{"hooks": [THEIRS]}],
                                                                      "SessionEnd": [{"hooks": [THEIRS]}]}})
        self.assertEqual(json.loads(hooks.SETTINGS.with_name(hooks.BACKUP).read_text()), before)
        self.assertFalse(hooks.present())
        hooks.install()  # (again: nothing more changes)
        self.assertEqual(self.settings()["hooks"]["Stop"], [{"hooks": [THEIRS]}])

    def test_a_plugin_that_does_not_go_in_leaves_the_old_entries_working(self):
        old = {"hooks": {"Stop": [{"hooks": [OLD_HOOK]}]}}
        self.settings(old)
        self.answers["install"] = (1, {"outcome": "failed", "message": "Plugin not found"}, "")
        with self.assertRaisesRegex(RuntimeError, "Plugin not found"):
            hooks.install()
        self.assertEqual(self.settings(), old)

    def test_uninstall(self):
        self.installed = {"id": "sidelit@sidelit", "enabled": True}
        self.settings({"hooks": {"Stop": [{"hooks": [OLD_HOOK]}, {"hooks": [THEIRS]}]}})
        hooks.uninstall()
        self.assertEqual(self.calls, [("uninstall", "sidelit@sidelit", "--scope", "user"),
                                      ("marketplace", "remove", "sidelit")])
        self.assertEqual(self.settings(), {"hooks": {"Stop": [{"hooks": [THEIRS]}]}})
        # (not installed, no marketplace: nothing to say, nothing failed)
        self.installed, self.calls = None, []
        self.answers["remove"] = (1, {"outcome": "failed", "failureCode": "not_configured"}, "")
        self.assertEqual(hooks.uninstall(), [])
        self.assertEqual(self.calls, [("marketplace", "remove", "sidelit")])

    def test_the_plugin_carries_the_events_the_hook_handles(self):
        plugin = Path(ROOT) / "plugin"
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "sidelit")
        self.assertEqual(manifest["version"], (Path(ROOT) / "VERSION").read_text().strip())
        market = json.loads((Path(ROOT) / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual((market["name"], market["plugins"][0]["name"], market["plugins"][0]["source"]),
                         ("sidelit", "sidelit", "./plugin"))
        config = json.loads((plugin / "hooks" / "hooks.json").read_text())
        self.assertEqual(sorted(config["hooks"]), sorted(
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Notification",
             "Stop", "StopFailure", "SubagentStart", "SubagentStop", "SessionEnd"]))
        for event, entries in config["hooks"].items():
            (entry,) = entries
            (hook,) = entry["hooks"]
            self.assertEqual(hook["command"], 'python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py"')
            self.assertEqual(hook.get("async", False), event in ("PreToolUse", "PostToolUse"), event)
            self.assertEqual(entry.get("matcher"), "*" if event in ("PreToolUse", "PostToolUse", "PermissionRequest") else None)
        self.assertEqual(config["modules"], ["./usage.js"])
        self.assertTrue((plugin / "hooks" / "usage.js").exists())


class AgentsPollerTest(unittest.TestCase):
    def test_asks_on_a_thread_at_most_every_few_seconds(self):
        asked, go = [], threading.Event()

        def fetch():
            if asked:
                go.wait(5)
            asked.append(1)
            return [{"sessionId": f"s{len(asked)}", "pid": 5, "status": "idle"}]
        poller = claude.AgentsPoller(fetch=fetch, every=5.0)
        entries, at = poller.poll(100.0)  # the first answer is waited for: a daemon that just started
        self.assertEqual((entries[0]["sessionId"], len(asked)), ("s1", 1))
        self.assertGreater(at, 0)
        self.assertEqual(poller.poll(103.0)[0][0]["sessionId"], "s1")  # (not again yet)
        self.assertEqual(poller.poll(106.0)[0][0]["sessionId"], "s1")  # the next comes on the thread: no waiting here
        self.assertEqual(poller.poll(112.0)[0][0]["sessionId"], "s1")  # (and it is not asked twice at once)
        go.set()
        for _ in range(100):
            if poller.latest[0][0]["sessionId"] == "s2":
                break
            time.sleep(0.01)
        self.assertEqual((poller.poll(110.0)[0][0]["sessionId"], len(asked)), ("s2", 2))

    def test_a_failed_ask_keeps_the_last_answer(self):
        answers = [[{"sessionId": "s1", "pid": 5}], None]
        poller = claude.AgentsPoller(fetch=lambda: answers.pop(0), every=0.0)
        poller._ask()
        poller._ask()
        self.assertEqual(poller.latest[0], [{"sessionId": "s1", "pid": 5}])


if __name__ == "__main__":
    unittest.main()
