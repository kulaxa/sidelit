"""Before a tab turns yellow: what the model reads of a turn, and what its answer shows."""
import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import turncheck  # noqa: E402


def user(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def assistant(*blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, **extra}


def text(t):
    return {"type": "text", "text": t}


def bash(cmd, background=False):
    return {"type": "tool_use", "name": "Bash", "input": {"command": cmd, "run_in_background": background}}


class TurnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, *entries):
        with open(self.path, "w") as fh:
            fh.write("\n".join(json.dumps(e) for e in entries) + "\n")
        return turncheck.last_turn(self.path)

    def test_the_last_turn_from_your_request_on(self):
        turn = self.transcript(
            user("an earlier request"), assistant(text("an earlier answer")),
            user("push it and watch CI"),
            assistant(text("Pushing."), bash("git push")),
            user([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also bump the version",
                                                  "origin": {"kind": "human"}}},
            assistant(bash("glab ci status --live", background=True)),
            assistant(text("a subagent's words"), isSidechain=True),
            user("<task-notification>\n<status>completed</status>\n<summary>Pipeline 12 passed</summary>\n</task-notification>"),
            assistant(text("Pipeline 12 passed.")))
        self.assertEqual(turn["asked"], "push it and watch CI")
        self.assertEqual(turn["events"], [
            ("Claude", "Pushing."), ("tool", "Bash: git push"), ("you, during the turn", "also bump the version"),
            ("tool", "Bash (in the background): glab ci status --live"),
            ("background task", "completed Pipeline 12 passed"), ("Claude", "Pipeline 12 passed.")])
        message = turncheck.describe(turn, shell=True)
        self.assertIn("[Claude] Pipeline 12 passed.", message)
        self.assertIn("background shell", message)

    def test_a_task_notification_queued_mid_turn_is_not_you(self):
        turn = self.transcript(
            user("deploy"), assistant(text("Deploying.")),
            {"type": "attachment", "attachment": {"type": "queued_command",
                                                  "prompt": "<task-notification><status>failed</status></task-notification>"}})
        self.assertEqual(turn["events"][-1], ("background task", "failed"))

    def test_the_last_words_the_transcript_does_not_have_yet(self):
        """Claude Code writes the transcript a moment after the Stop hook: the hook's copy of
        Claude's last message is what the turn ends with."""
        entries = [user("push the configs"), assistant(text("Pushed. Checking the logs:"), bash("kubectl logs"))]
        turn = self.transcript(*entries)
        with open(self.path, "w") as fh:
            fh.write("\n".join(json.dumps(e) for e in entries) + "\n")
        turn = turncheck.last_turn(self.path, said="I'll add the service once the deploy lands.")
        self.assertEqual(turn["events"][-1], ("Claude", "I'll add the service once the deploy lands."))
        self.assertEqual(turncheck.last_turn("", said="Done.")["events"], [("Claude", "Done.")])

    def test_last_words_already_written_are_not_doubled(self):
        turn = self.transcript(user("go"), assistant(bash("make")), assistant(text("Built.")), assistant(text("All green.")))
        turn = turncheck.last_turn(self.path, said="Built.\n\nAll green.")
        self.assertEqual([t for who, t in turn["events"] if who == "Claude"], ["Built.", "All green."])

    def test_what_can_wake_claude_up(self):
        waits = [user("push it"), assistant(bash("git push")), assistant(text("I'll check the pipeline later."))]
        self.assertFalse(self.transcript(*waits)["wakes"])
        watched = [user("push it"), assistant({"type": "tool_use", "name": "Monitor", "input": {"command": "watch.py"}}),
                   assistant(text("Watching it."))]
        self.assertTrue(self.transcript(*watched)["wakes"])
        self.assertTrue(self.transcript(user("push it"), assistant(bash("glab ci status --live", background=True)))["wakes"])
        woken = [user("push it"), assistant(text("Pushed.")),
                 user("<task-notification><summary>Monitor event</summary></task-notification>"), assistant(text("3 of 7."))]
        self.assertTrue(self.transcript(*woken)["wakes"])

    def test_nothing_to_judge(self):
        self.assertIsNone(self.transcript(user("hello")))
        self.assertIsNone(turncheck.last_turn(os.path.join(self.tmp.name, "missing.jsonl")))

    def test_a_long_turn_keeps_its_end(self):
        turn = self.transcript(user("go"), *[assistant(text(f"step {i} " + "x" * 500)) for i in range(40)])
        self.assertTrue(turn["cut"])
        self.assertTrue(turn["events"][-1][1].startswith("step 39"))
        self.assertLessEqual(sum(len(t) for _, t in turn["events"]), turncheck.TURN_MAX)


class ParseTest(unittest.TestCase):
    def test_answers(self):
        self.assertEqual(turncheck.parse('{"verdict": "input"}'), ("input", ""))
        self.assertEqual(turncheck.parse('Sure: {"verdict": "waiting", "on": "CI  pipeline"}'), ("waiting", "CI pipeline"))
        self.assertEqual(turncheck.parse('{"verdict": "input", "on": "junk"}'), ("input", ""))
        for bad in ("", "input", '{"verdict": "maybe"}', "{not json}", "[]"):
            self.assertIsNone(turncheck.parse(bad), bad)


class FakeChecker:
    def __init__(self):
        self.results = queue.Queue()
        self.asked = []
        self.warmed = 0

    def warm(self):
        self.warmed += 1

    def check(self, key, transcript, shell=False, said=None):
        self.asked.append((key, transcript, shell))
        self.said = said

    def answer(self, verdict, on="", wakes=True):
        key = self.asked[-1][0]
        self.results.put((key, verdict, on, None, 1.5, wakes))


class TurnsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "turns.json"
        self.checker = FakeChecker()
        self.turns = turncheck.Turns(self.checker, self.file)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def rec(turn_end=100.0, state="done"):
        return {"state": state, "turn_end": turn_end, "transcript": "/t.jsonl"}

    def test_a_claude_that_only_waits_stays_orange(self):
        rec = dict(self.rec(), said="Waiting for CI.")
        self.assertEqual(self.turns.state("s1", rec, "done", False, 101.0), ("checking", ""))
        self.assertEqual(self.checker.asked, [(("s1", 100.0), "/t.jsonl", False)])
        self.assertEqual(self.checker.said, "Waiting for CI.")
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 102.0), ("checking", ""))
        self.assertEqual(len(self.checker.asked), 1)  # asked once per turn
        self.checker.answer("waiting", "CI pipeline")
        self.assertEqual(self.turns.collect()[0]["verdict"], "waiting")
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 103.0), ("external", "CI pipeline"))
        self.assertEqual(self.turns.state("s1", self.rec(), "done", True, 104.0), ("external", "CI pipeline"))

    def test_a_claude_that_needs_you_turns_yellow(self):
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        self.checker.answer("input")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 103.0), ("done", ""))

    def test_a_turn_you_watched_end_is_not_checked(self):
        self.assertEqual(self.turns.state("s1", self.rec(), "done", True, 101.0), ("done", ""))
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 130.0), ("done", ""))  # you left
        self.assertEqual(self.checker.asked, [])

    def test_an_old_turn_is_not_checked(self):
        """(the daemon just started, or a session from before the upgrade)"""
        now = 100.0 + turncheck.TURN_FRESH_S + 1
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, now), ("done", ""))
        self.assertEqual(self.checker.asked, [])

    def test_an_answer_that_never_comes_is_yellow(self):
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        late = 101.0 + turncheck.CHECKING_MAX_S + 1
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, late), ("done", ""))
        self.checker.answer("waiting", "CI pipeline")  # too late: stays yellow
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, late + 1), ("done", ""))

    def test_each_turn_is_checked_again(self):
        self.turns.state("s1", self.rec(100.0), "done", False, 101.0)
        self.checker.answer("waiting", "CI pipeline")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(100.0, "busy"), "busy", False, 150.0), ("busy", ""))
        self.assertEqual(self.checker.warmed, 1)  # working: a process is started ahead
        self.assertEqual(self.turns.state("s1", self.rec(200.0), "done", False, 201.0), ("checking", ""))
        self.checker.answer("input")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(200.0), "done", False, 203.0), ("done", ""))

    def test_waiting_on_a_shell_that_ended_without_waking_claude_needs_you(self):
        self.turns.state("s1", self.rec(), "shell", False, 101.0)
        self.assertTrue(self.checker.asked[0][2])
        self.checker.answer("waiting", "test run")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "shell", False, 103.0), ("external", "test run"))
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 200.0), ("external", "test run"))
        after = 200.0 + turncheck.SHELL_GRACE_S + 1
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, after), ("done", ""))

    def test_commands_left_running_are_checked_even_on_the_tab_you_look_at(self):
        self.assertEqual(self.turns.state("s1", self.rec(), "shell", True, 101.0), ("checking", ""))
        self.checker.answer("waiting", "sandbox deploy")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "shell", True, 103.0), ("external", "sandbox deploy"))

    def test_a_shell_claude_does_not_wait_for_is_not_orange(self):
        """(a dev server it left running: it is done, and needs you)"""
        self.turns.state("s1", self.rec(), "shell", False, 101.0)
        self.checker.answer("input")
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "shell", False, 103.0), ("done", ""))

    def test_a_turn_seen_before_its_shell_showed_up_is_checked(self):
        self.assertEqual(self.turns.state("s1", self.rec(), "done", True, 101.0), ("done", ""))
        self.assertEqual(self.turns.state("s1", self.rec(), "shell", True, 102.0), ("checking", ""))
        self.assertEqual(len(self.checker.asked), 1)

    def test_waiting_with_nothing_to_wake_it_turns_yellow_in_the_end(self):
        said = []
        self.turns.journal = lambda kind, **f: said.append(f)
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        self.checker.answer("waiting", "CI pipeline", wakes=False)  # "I'll check the pipeline later"
        self.turns.collect()
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, 1000.0), ("external", "CI pipeline"))
        late = 100.0 + turncheck.STRANDED_S + 1
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, late), ("done", ""))
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, late + 1), ("done", ""))
        self.assertEqual(len(said), 1)  # journalled once
        self.assertIn("nothing to wake it", said[0]["error"])

    def test_waiting_on_a_watcher_stays_orange(self):
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        self.checker.answer("waiting", "CI pipeline", wakes=True)
        self.turns.collect()
        late = 100.0 + turncheck.STRANDED_S + 1
        self.assertEqual(self.turns.state("s1", self.rec(), "done", False, late), ("external", "CI pipeline"))

    def test_a_lost_answer_is_journalled(self):
        said = []
        self.turns.journal = lambda kind, **f: said.append(f)
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        self.turns.state("s1", self.rec(), "done", False, 101.0 + turncheck.CHECKING_MAX_S + 1)
        self.assertEqual([f["verdict"] for f in said], ["input"])
        self.assertIn("no answer", said[0]["error"])

    def test_verdicts_survive_a_daemon_restart(self):
        self.turns.state("s1", self.rec(), "done", False, 101.0)
        self.checker.answer("waiting", "prod deploy")
        self.turns.collect()
        self.turns.state("s2", self.rec(), "done", False, 102.0)  # still checking when it stops
        self.turns.save()
        again = turncheck.Turns(FakeChecker(), self.file)
        self.assertEqual(again.state("s1", self.rec(), "done", False, 110.0), ("external", "prod deploy"))
        self.assertEqual(again.state("s2", self.rec(), "done", False, 110.0), ("checking", ""))
        self.assertEqual(len(again.checker.asked), 1)  # s2 asked again, s1 not

    def test_permission_prompts_and_plain_claudes_are_left_alone(self):
        self.assertEqual(self.turns.state("s1", self.rec(state="waiting"), "waiting", False, 101.0), ("waiting", ""))
        self.assertEqual(self.turns.state("s2", None, "done", False, 101.0), ("done", ""))
        self.assertEqual(self.turns.state("s3", {"state": "done"}, "done", False, 101.0), ("done", ""))
        self.assertEqual(self.checker.asked, [])


class ParallelTest(unittest.TestCase):
    def test_claudes_stopping_together_are_checked_at_once(self):
        """(one at a time, each waiting for its claude to start, the last ones ran out the clock)"""
        import time

        class Slow(turncheck.Checker):
            def _spawn(self):
                return None

            def _check(self, transcript, shell, said=None):
                time.sleep(0.5)
                return "input", "", None, False

        checker = Slow(lambda: None)
        started = time.time()
        for i in range(4):
            checker.check(i, "")
        got = sorted(checker.results.get(timeout=5)[0] for _ in range(4))
        self.assertEqual(got, [0, 1, 2, 3])
        self.assertLess(time.time() - started, 1.5)
        checker.stop()


if __name__ == "__main__":
    unittest.main()
