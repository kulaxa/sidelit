"""Which window shows which Claude, and what it is doing: hook events, focus, launches, titles."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        # no real ~/.claude in here
        self._saved = (claude.SESSIONS, claude.STATUSLINE_STATE, claude.PROJECTS)
        claude.SESSIONS, claude.STATUSLINE_STATE, claude.PROJECTS = base / "s", base / "sl", base / "p"
        claude.SESSIONS.mkdir()
        self.table = {CLAUDE_PID: (SHELL_PID, "claude"), SHELL_PID: (TERM_PID, "zsh"), TERM_PID: (1, "terminator"),
                      501: (401, "claude"), 401: (301, "zsh"), 301: (1, "terminator")}
        self.headless: set[int] = set()
        self._in_terminal = claude.in_terminal
        claude.in_terminal = lambda pid: pid not in self.headless
        self.d = claude.Detector(hook_dir=self.hook_dir)

    def tearDown(self):
        claude.SESSIONS, claude.STATUSLINE_STATE, claude.PROJECTS = self._saved
        claude.in_terminal = self._in_terminal
        self.tmp.cleanup()

    def hook(self, sid, state, t, pid=CLAUDE_PID, prompt_t=None, **extra):
        rec = {"session": sid, "state": state, "t": t, "pid": pid, "cwd": "/tmp",
               "ancestors": [pid, pid - 100, pid - 200, 1], "started": extra.pop("started", t), **extra}
        (self.hook_dir / f"{sid}.json").write_text(json.dumps(rec))
        if prompt_t is not None:
            (self.hook_dir / f"{sid}.prompt").write_text(repr(prompt_t))

    def claude_file(self, sid, pid, status, name=None, since=None):
        (claude.SESSIONS / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "/tmp",
                                                                "status": status, "name": name,
                                                                "statusUpdatedAt": (since or 0) * 1000}))

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
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        self.claude_file("s1", CLAUDE_PID, "shell")  # a background shell still runs
        self.assertEqual(self.look({"A": a}, 106.0)["A"]["status"], "shell")

    def test_a_background_task_waking_claude_up_is_busy(self):
        """No hook event until it uses a tool or stops: Claude's own file says it works again."""
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        self.claude_file("s1", CLAUDE_PID, "busy", since=104.0)  # (the turn that just ended)
        self.assertEqual(self.look({"A": a}, 106.0)["A"]["status"], "done")
        self.claude_file("s1", CLAUDE_PID, "busy", since=120.0)  # the pipeline watcher reported
        self.assertEqual(self.look({"A": a}, 121.0)["A"]["status"], "busy")

    def test_the_session_file_alone_counts_for_five_minutes(self):
        a = term("A", "✳ Fix tests")
        self.d.note_focus(100.0, "A")
        self.hook("s1", "done", 105.0, prompt_t=100.5)
        self.claude_file("s1", CLAUDE_PID, "busy", since=120.0)
        self.assertEqual(self.look({"A": a}, 120.0 + claude.WOKEN_MAX_S + 1)["A"]["status"], "done")

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
        self.claude_file("checker", 777, "idle")
        self.claude_file("s9", CLAUDE_PID, "busy", name="Deploy")
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
        self.claude_file("s9", CLAUDE_PID, "busy", name="Deploy")
        seen = self.look({"A": term("A", "⠂ Deploy")}, 100.0)
        self.assertEqual(seen["A"]["session"], "s9")
        self.assertEqual(seen["A"]["status"], "busy")
        self.assertFalse(seen["A"]["hooked"])

    def test_mapping_survives_a_daemon_restart(self):
        a, b = term("A", "✳ Fix tests"), term("B", "✳ Fix tests")
        self.d.note_focus(100.0, "B")
        self.hook("s1", "busy", 101.0, prompt_t=101.0)
        self.look({"A": a, "B": b}, 102.0)
        self.d = claude.Detector(hook_dir=self.hook_dir)  # restarted: no focus history
        seen = self.look({"A": a, "B": b}, 200.0)
        self.assertEqual(seen["B"]["session"], "s1")
        self.assertIsNone(seen["A"])


class HookScriptTest(unittest.TestCase):
    def run_hook(self, home, event):
        env = {**os.environ, "SIDELIT_HOME": home}
        return subprocess.run([sys.executable, "-I", os.path.join(ROOT, "sidelit", "hook.py")],
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
                r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "sidelit", "hook.py")],
                                   input=bad if isinstance(bad, str) else json.dumps(bad), text=True,
                                   capture_output=True, env=env, timeout=5)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


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


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = hooks.SETTINGS, hooks.hook_command
        hooks.SETTINGS = Path(self.tmp.name) / "settings.json"
        hooks.hook_command = lambda: "/usr/bin/sidelit-hook"

    def tearDown(self):
        hooks.SETTINGS, hooks.hook_command = self._saved
        self.tmp.cleanup()

    def test_install_keeps_other_hooks_and_is_idempotent(self):
        theirs = {"type": "command", "command": "notify-send done"}
        hooks.SETTINGS.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [theirs]}]}}))
        hooks.install()
        hooks.install()
        data = json.loads(hooks.SETTINGS.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"], [theirs])
        self.assertTrue(data["hooks"]["PreToolUse"][0]["hooks"][0]["async"])
        self.assertEqual(sorted(hooks.installed()), sorted(hooks.EVENTS))
        self.assertTrue(hooks.SETTINGS.with_name("settings.json.before-sidelit").exists())
        hooks.uninstall()
        data = json.loads(hooks.SETTINGS.read_text())
        self.assertEqual(data["hooks"], {"Stop": [{"hooks": [theirs]}]})


if __name__ == "__main__":
    unittest.main()
