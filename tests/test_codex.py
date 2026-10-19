"""Codex behind the agent interface: its events through the hook, which codex process shows a
session, its plugin through Codex's own commands, the turn check, usage limits. The events and
answers below were recorded from codex-cli 0.160.0 (tests/codex_live.py runs the real thing)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import agents, codex, detect, turncheck  # noqa: E402
from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core.model import Slot, Win  # noqa: E402

HOOK = os.path.join(ROOT, "plugin", "hooks", "hook.py")
SID = "01a14f42-2e6b-7170-bdd7-850e5e55278d"
TURN = "01a14f41-44eb-7aa2-a8c6-573d59ef7a30"
COMMON = {"session_id": SID, "transcript_path": f"/home/alex/.codex/sessions/2026/10/18/rollout-2026-10-18T15-44-59-{SID}.jsonl",
          "cwd": "/home/alex/src/pricing", "model": "gpt-5.5-codex"}
# what Codex sent its hooks, event by event (plus the common fields)
EVENTS = [
    {"hook_event_name": "SessionStart", "permission_mode": "default", "source": "startup"},
    {"hook_event_name": "UserPromptSubmit", "turn_id": TURN, "permission_mode": "default", "prompt": "run the tests"},
    {"hook_event_name": "PreToolUse", "turn_id": TURN, "permission_mode": "default", "tool_name": "Bash",
     "tool_input": {"command": "npm test"}, "tool_use_id": "call_4"},
    {"hook_event_name": "PostToolUse", "turn_id": TURN, "permission_mode": "default", "tool_name": "Bash",
     "tool_input": {"command": "npm test"}, "tool_response": "12 passing\n", "tool_use_id": "call_4"},
    {"hook_event_name": "Stop", "turn_id": TURN, "permission_mode": "default", "stop_hook_active": False,
     "last_assistant_message": "All 12 tests pass."},
]
# `codex exec --json`, a turn
EXEC_OUT = [
    {"type": "thread.started", "thread_id": "01a14f3e-a84b-7772-881c-6e59267409fc"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Model metadata for `x` not found."}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                        "text": '{"verdict": "waiting", "on": "CI pipeline"}'}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
]
# account/rateLimits/read (the shape Codex's app-server documents)
RATE_LIMITS = {"rateLimits": {"limitId": "codex", "limitName": None,
                              "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1792350000},
                              "secondary": {"usedPercent": 61, "windowDurationMins": 10080, "resetsAt": 1792700000},
                              "rateLimitReachedType": None, "planType": "pro"},
               "rateLimitsByLimitId": {"codex": {"limitId": "codex"}}}


class HookTest(unittest.TestCase):
    """plugin/hooks/hook.py, called the way Codex calls it: `hook.py codex`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.path = Path(self.home) / "codex" / f"{SID}.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, event, parent=None):
        """The hook run by a process called codex, as Codex's own (`parent` its arguments), or by us."""
        env = {**os.environ, "SIDELIT_HOME": self.home, "TERMINATOR_UUID": "urn:uuid:server"}
        payload = json.dumps({**COMMON, **event})
        if parent is None:
            r = subprocess.run([sys.executable, "-I", HOOK, "codex"], input=payload, text=True,
                               capture_output=True, env=env, timeout=10)
        else:
            # a process named codex (a link to python) that runs the hook as its child
            fake = Path(self.home) / "bin" / "codex"
            fake.parent.mkdir(exist_ok=True)
            if not fake.exists():
                fake.symlink_to(sys.executable)
            code = "import subprocess, sys; sys.exit(subprocess.run([sys.argv[1], '-I', sys.argv[2], 'codex']).returncode)"
            r = subprocess.run([str(fake), "-I", "-c", code, sys.executable, HOOK, *parent], input=payload, text=True,
                               capture_output=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        return json.loads(self.path.read_text())

    def test_a_turn_as_codex_reports_it(self):
        states = [self.run_hook(e)["state"] for e in EVENTS]
        self.assertEqual(states, ["done", "busy", "busy", "busy", "done"])
        rec = json.loads(self.path.read_text())
        self.assertEqual((rec["model"], rec["asked"], rec["tools"], rec["said"], rec["first_prompt"]),
                         ("gpt-5.5-codex", "run the tests", ["Bash: npm test"], "All 12 tests pass.", "run the tests"))
        self.assertIn("turn_end", rec)
        self.assertTrue((Path(self.home) / "codex" / f"{SID}.prompt").exists())  # you typed it
        # the next prompt starts a new turn: its tools are its own, the title stays the first ask
        rec = self.run_hook({"hook_event_name": "UserPromptSubmit", "turn_id": "t2", "prompt": "and the linter"})
        self.assertEqual((rec["asked"], rec["tools"], rec["first_prompt"]), ("and the linter", [], "run the tests"))
        self.assertNotIn("turn_end", rec)

    def test_a_tool_hook_finishing_after_its_turn_ended_changes_nothing(self):
        """(they run in the background: the turn's end can be written first, seen live)"""
        for e in EVENTS:
            self.run_hook(e)
        late = self.run_hook(EVENTS[3])  # PostToolUse of the turn that is over
        self.assertEqual((late["state"], late["event"], late["tools"]), ("done", "Stop", ["Bash: npm test"]))
        self.assertIn("turn_end", late)
        # the next turn's own tool events count again
        self.run_hook({"hook_event_name": "UserPromptSubmit", "turn_id": "t2", "prompt": "again"})
        self.assertEqual(self.run_hook({**EVENTS[2], "turn_id": "t2"})["state"], "busy")

    def test_permission_interrupt_and_the_end(self):
        self.run_hook(EVENTS[1])
        self.assertEqual(self.run_hook({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                        "tool_input": {"command": "rm -rf build", "description": None}})["state"], "waiting")
        self.assertEqual(self.run_hook({"hook_event_name": "Interrupt", "turn_id": TURN})["state"], "done")
        self.assertEqual(self.run_hook({"hook_event_name": "SessionEnd", "reason": "other"})["state"], "ended")

    def test_subagents_are_counted_and_their_permission_waits(self):
        self.run_hook(EVENTS[1])
        sub = {"agent_id": "a1", "agent_type": "worker"}
        self.assertEqual(list(self.run_hook({"hook_event_name": "SubagentStart", **sub})["agents"]), ["a1"])
        self.assertEqual(self.run_hook({"hook_event_name": "PermissionRequest", **sub})["state"], "waiting")
        self.assertEqual(self.run_hook({"hook_event_name": "PostToolUse", **sub})["state"], "busy")
        self.assertEqual(self.run_hook({"hook_event_name": "SubagentStop", **sub})["agents"], {})

    def test_a_session_of_its_own_process_knows_it(self):
        rec = self.run_hook(EVENTS[0], parent=[])
        self.assertTrue(rec["pid"])
        self.assertEqual(rec["ancestors"][0], rec["pid"])  # (here codex runs the hook itself, not a shell)
        self.assertEqual(rec["pane"], {"TERMINATOR_UUID": "urn:uuid:server"})
        self.assertNotIn("server", rec)

    def test_codexs_shared_server_cannot_say_whose_terminal(self):
        """Its environment and ancestry are the server's: the daemon finds the session's codex."""
        rec = self.run_hook(EVENTS[0], parent=["app-server", "--listen", "unix://"])
        self.assertEqual(rec["pid"], 0)
        self.assertTrue(rec["server"])
        self.assertNotIn("ancestors", rec)
        self.assertNotIn("pane", rec)

    def test_an_agent_the_hook_does_not_know_is_ignored(self):
        env = {**os.environ, "SIDELIT_HOME": self.home}
        r = subprocess.run([sys.executable, "-I", HOOK, "nosuch"], input=json.dumps({**COMMON, **EVENTS[0]}),
                           text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout, os.listdir(self.home)), (0, "", []))


TUI_A, TUI_B, SERVER, SHELL_A, TERM_A, SHELL_B, TERM_B = 700, 710, 650, 699, 698, 709, 708


class ResolveTest(unittest.TestCase):
    """Which codex process shows a session the shared server reported."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "codex"
        self.dir.mkdir()
        self.table = {TUI_A: (SHELL_A, "codex"), SHELL_A: (TERM_A, "zsh"), TERM_A: (1, "terminator"),
                      TUI_B: (SHELL_B, "codex"), SHELL_B: (TERM_B, "zsh"), TERM_B: (1, "terminator"),
                      SERVER: (1, "codex")}
        self.args = {TUI_A: ["codex"], TUI_B: ["codex"], SERVER: ["codex", "app-server", "--listen", "unix://"]}
        self.folder = {TUI_A: "/src/pricing", TUI_B: "/src/pricing", SERVER: "/home/alex"}
        self.started = {TUI_A: 100.0, TUI_B: 200.0, SERVER: 90.0}
        patches = [mock.patch.object(codex, "cmdline", lambda p: self.args.get(p, [])),
                   mock.patch.object(agents, "cmdline", lambda p: self.args.get(p, [])),
                   mock.patch.object(codex, "cwd_of", lambda p: self.folder.get(p, "")),
                   mock.patch.object(codex, "in_terminal", lambda p: p != SERVER),
                   mock.patch.object(agents, "pane_of", lambda p: {})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.agent = self.new_agent()

    def new_agent(self):
        return codex.Codex(hook_dir=self.dir, started=lambda p: self.started.get(p))

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, sid, started, cwd="/src/pricing"):
        return {"session": sid, "pid": 0, "server": SERVER, "cwd": cwd, "started": started, "t": started}

    def resolve(self, *recs, agent=None):
        recs = [dict(r) for r in recs]
        (agent or self.agent).resolve(recs, self.table)
        return [r.get("pid") or None for r in recs]

    def test_the_codex_started_for_it(self):
        self.args[TUI_B] = ["codex", "resume", "s1"]
        self.assertEqual(self.resolve(self.rec("s1", 50.0)), [TUI_B])

    def test_the_codex_in_its_folder_that_started_last_before_it(self):
        a, b = self.rec("a", 101.0), self.rec("b", 202.0)
        self.assertEqual(self.resolve(a, b), [TUI_A, TUI_B])
        recs = [dict(a)]
        self.agent.resolve(recs, self.table)
        self.assertEqual(recs[0]["ancestors"], [TUI_A, SHELL_A, TERM_A])  # (its terminal: launch evidence)
        self.assertEqual(self.resolve(self.rec("elsewhere", 101.0, cwd="/src/other")), [None])

    def test_one_whose_codex_quit_is_gone(self):
        """The server keeps it a while, without a word: it is not the next codex's in that folder."""
        self.resolve(self.rec("b", 202.0))
        del self.table[TUI_B]
        self.assertEqual(self.resolve(self.rec("b", 202.0)), [None])
        self.table[TUI_B + 1] = (SHELL_B, "codex")  # a new codex there, later
        self.args[TUI_B + 1], self.folder[TUI_B + 1], self.started[TUI_B + 1] = ["codex"], "/src/pricing", 300.0
        self.assertEqual(self.resolve(self.rec("b", 202.0), self.rec("c", 301.0)), [None, TUI_B + 1])
        # a daemon restart remembers the codex that quit
        self.assertEqual(self.resolve(self.rec("b", 202.0), agent=self.new_agent()), [None])

    def test_a_session_from_before_any_codex_there_has_none(self):
        self.assertEqual(self.resolve(self.rec("old", 10.0)), [None])

    def test_the_hooks_own_word_stands(self):
        recs = [{"session": "x", "pid": TUI_A, "cwd": "/src/pricing", "started": 1.0}]
        self.agent.resolve(recs, self.table)
        self.assertEqual(recs[0]["pid"], TUI_A)

    def test_detector_shows_it_in_its_terminal(self):
        (self.dir / "a.json").write_text(json.dumps({**self.rec("a", 101.0), "state": "busy", "first_prompt": "fix pricing",
                                                     "model": "gpt-5.5-codex"}))
        with mock.patch.object(detect, "in_terminal", lambda p: p != SERVER):
            d = detect.Detector(agents=[self.agent], started=lambda p: self.started.get(p),
                                windows=Path(self.tmp.name) / "windows.json")
            d.refresh(300.0, force=True, table=self.table)
            win = Win(id="W", app="terminator", title="⠸ pricing", space=1, output="eDP-1")
            d.assign({"W": win}, {"W": TERM_A})
            got = d.claudes_for(win.title, 0, "W")
        self.assertEqual([(c["session"], c["agent"], c["title"], c["status"], c["how"]) for c in got],
                         [("a", "codex", "fix pricing", "busy", "launch")])


class FocusTest(unittest.TestCase):
    """A Codex terminal's title is its project: the terminal you typed into is the one when its
    title says this session's project."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "codex"
        self.dir.mkdir()
        self.repo = Path(self.tmp.name) / "pricing"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "api").mkdir()
        self.agent = codex.Codex(hook_dir=self.dir)
        self.table = {800: (799, "codex"), 799: (798, "zsh"), 798: (1, "terminator")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_terminal_you_typed_into(self):
        rec = {"session": "s1", "pid": 800, "ancestors": [799, 800, 798, 1], "cwd": str(self.repo / "api"),
               "state": "busy", "t": 101.0, "started": 90.0}
        (self.dir / "s1.json").write_text(json.dumps(rec))
        (self.dir / "s1.prompt").write_text("101.0")
        with mock.patch.object(detect, "in_terminal", lambda p: True):
            d = detect.Detector(agents=[self.agent], started=lambda p: None, windows=Path(self.tmp.name) / "w.json")
            d.note_focus(100.0, "B")
            d.refresh(102.0, force=True, table=self.table)
            wins = {"A": Win(id="A", app="terminator", title="alex@laptop: ~", space=1, output=None),
                    "B": Win(id="B", app="terminator", title="pricing", space=1, output=None)}
            d.assign(wins, {})
            self.assertEqual([c["how"] for c in d.claudes_for("pricing", 0, "B")], ["focus"])
            self.assertEqual(d.claudes_for("alex@laptop: ~", 0, "A"), [])

    def test_project_names(self):
        self.assertEqual(codex.project_name(str(self.repo / "api")), "pricing")  # (its git checkout's folder)
        self.assertEqual(codex.project_name(self.tmp.name), os.path.basename(self.tmp.name))
        self.assertTrue(self.agent.owns_title("⠸ pricing", str(self.repo / "api")))
        self.assertTrue(self.agent.owns_title("pricing", str(self.repo)))
        self.assertFalse(self.agent.owns_title("alex@laptop: ~/src/pricing", str(self.repo)))


class FakeProc:
    def __init__(self, lines):
        r, w = os.pipe()
        os.write(w, "".join(json.dumps(x) + "\n" for x in lines).encode())
        os.close(w)
        self.stdout = os.fdopen(r, "rb")
        self.stdin = open(os.devnull, "wb")
        self.sidelit_system = "SYSTEM"


class TurnCheckTest(unittest.TestCase):
    def test_the_turn_from_what_the_hook_kept(self):
        rec = {"asked": "run the tests", "tools": ["Bash: npm test", "Bash: gh pr checks --watch"],
               "said": "Pushed; waiting for CI."}
        turn = codex.Codex().last_turn(rec)
        self.assertEqual(turn["events"], [("tool", "Bash: npm test"), ("tool", "Bash: gh pr checks --watch"),
                                          ("Codex", "Pushed; waiting for CI.")])
        self.assertIn("[Codex] Pushed; waiting for CI.", turncheck.describe(turn))
        self.assertIsNone(codex.Codex().last_turn({"asked": "x"}))

    def test_codex_exec_answers(self):
        proc = FakeProc(EXEC_OUT)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(turncheck.parse(codex.Codex().ask_judge(proc, "turn", 5)), ("waiting", "CI pipeline"))
        proc = FakeProc(EXEC_OUT[:3] + [{"type": "turn.failed", "error": {"message": "usage limit reached"}}])
        self.addCleanup(proc.stdout.close)
        with self.assertRaisesRegex(RuntimeError, "usage limit"):
            codex.Codex().ask_judge(proc, "turn", 5)

    def test_the_judge_runs_headless_with_nothing_of_yours(self):
        with mock.patch.object(codex, "codex_binary", lambda: "/usr/bin/codex"), \
                mock.patch.object(codex.subprocess, "Popen") as popen:
            codex.Codex().spawn_judge("gpt-5.5-mini", "SYSTEM", "/run/sidelit")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:3], ["/usr/bin/codex", "exec", "--ephemeral"])
        for flag in ("--json", "hooks", "plugins", "shell_tool", "mcp_servers={}", "read-only"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-4:], ["-m", "gpt-5.5-mini", "-C", "/run/sidelit"])

    def test_the_judge_reads_about_codex(self):
        self.assertIn("several Codex sessions", turncheck.prompt_for(codex.Codex()))
        self.assertIs(turncheck.prompt_for(agents.get("claude")), turncheck.PROMPT)


class UsageTest(unittest.TestCase):
    def test_the_bars_from_the_app_server(self):
        got = codex.usage_from(RATE_LIMITS, now=1792340000)
        self.assertEqual([(x["kind"], x["percentUsed"]) for x in got["limits"]], [("five_hour", 25.0), ("seven_day", 61.0)])
        self.assertTrue(got["limits"][0]["resetsAt"].endswith("Z"))
        from datetime import datetime
        self.assertEqual(datetime.fromisoformat(got["limits"][0]["resetsAt"].replace("Z", "+00:00")).timestamp(), 1792350000)
        odd = {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 60}, "secondary": None}}
        self.assertEqual(codex.usage_from(odd)["limits"], [{"kind": "1_hour", "percentUsed": 5.0, "resetsAt": None}])
        for nothing in ({}, {"rateLimits": {}}, {"rateLimits": {"primary": None}}, None):
            self.assertIsNone(codex.usage_from(nothing))

    def test_no_bars_without_an_account(self):
        class Refusing:
            def __init__(self):
                raise RuntimeError("codex account authentication required to read rate limits")
        self.assertIsNone(codex.Codex(server=Refusing).usage())


class AppServerTest(unittest.TestCase):
    """The JSON-RPC client, against a stand-in `codex app-server` that answers as Codex does."""
    SERVER = r'''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        print(json.dumps({"method": "remoteControl/status/changed", "params": {}}))
        out = {"id": msg["id"], "result": {"userAgent": "x", "codexHome": "/x"}}
    elif msg["method"] == "account/rateLimits/read":
        out = {"id": msg["id"], "error": {"code": -32600, "message": "codex account authentication required to read rate limits"}}
    else:
        out = {"id": msg["id"], "result": {"thread": {"id": msg["params"]["threadId"], "name": "Billing fix", "preview": "fix it"}}}
    print(json.dumps(out), flush=True)
'''

    def test_a_visit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            fake.write_text(self.SERVER)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            with codex.AppServer(binary=str(fake), timeout=10) as s:
                self.assertEqual(s.call("thread/read", {"threadId": "t1"})["thread"]["name"], "Billing fix")
                with self.assertRaisesRegex(RuntimeError, "authentication required"):
                    s.call("account/rateLimits/read", {})
            # names, asked for on a thread of their own
            agent = codex.Codex(server=lambda: codex.AppServer(binary=str(fake), timeout=10))
            self.assertEqual(agent.title("t1", "/", {"first_prompt": "fix it please"}), "fix it please")
            agent.watch(["t1"])
            for _ in range(100):
                if agent.title("t1", "/", {}) == "Billing fix":
                    break
                time.sleep(0.05)
            self.assertEqual(agent.title("t1", "/", {}), "Billing fix")


class NamesTest(unittest.TestCase):
    def test_codex_is_asked_once_a_minute_even_when_it_cannot_say(self):
        """(a session with nothing saved yet, an app-server that does not start: not every step)"""
        started = []

        def server():
            started.append(1)
            raise RuntimeError("codex app-server did not start")
        agent = codex.Codex(server=server)
        for _ in range(5):
            agent.watch(["s1"])
            for _ in range(100):
                if not agent._asking:
                    break
                time.sleep(0.01)
        self.assertEqual(len(started), 1)
        agent.watch(["s1", "s2"])  # a new one is asked at once
        for _ in range(100):
            if not agent._asking:
                break
            time.sleep(0.01)
        self.assertEqual(len(started), 2)
        later = time.time() + codex.NAMES_EVERY_S + 1
        with mock.patch.object(codex.time, "time", lambda: later):
            agent.watch(["s1", "s2"])  # a minute later: again
        for _ in range(100):
            if not agent._asking:
                break
            time.sleep(0.01)
        self.assertEqual(len(started), 3)


class PluginTest(unittest.TestCase):
    """`sidelit hooks install|uninstall|status` for Codex: its own plugin commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls, self.market, self.installed = [], None, False
        from sidelit import config
        patches = [mock.patch.object(codex, "run_codex", self.fake_codex),
                   mock.patch.object(codex, "codex_binary", lambda: "/usr/bin/codex"),
                   mock.patch.object(codex, "CODEX_HOME", Path(self.tmp.name) / "codex"),
                   mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name) / "config")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_codex(self, *args, json_out=True):
        self.calls.append(args)
        if args == ("marketplace", "list"):
            return 0, {"marketplaces": [{"name": "sidelit", "root": self.market}] if self.market else []}, ""
        if args[:2] == ("marketplace", "add"):
            self.market = args[2]
            return 0, {"marketplaceName": "sidelit", "installedRoot": args[2]}, ""
        if args[:2] == ("marketplace", "remove"):
            self.market = None
        if args[0] == "add":
            self.installed = True
            return 0, {"pluginId": "sidelit@sidelit", "installedPath": str(codex.cached_plugin())}, ""
        if args[0] == "remove":
            self.installed = False
        if args[0] == "list":
            return 0, {"installed": [{"pluginId": "sidelit@sidelit", "enabled": True}] if self.installed else []}, ""
        return 0, {}, ""

    def test_install_uses_codexs_commands_and_says_to_trust_the_hooks(self):
        said = codex.Codex().install()
        folder = str(codex.plugin_dir().parent)
        self.assertEqual(self.calls, [("marketplace", "list"), ("marketplace", "add", folder), ("add", "sidelit@sidelit")])
        self.assertIn("installed sidelit@sidelit in Codex", said[-1])
        (hint,) = codex.Codex().after_install  # (`sidelit hooks install` says it once it is in: one line)
        self.assertIn("/hooks", hint)
        # a marketplace added from elsewhere (an older checkout) comes from here now
        self.market, self.calls = "/old/checkout", []
        codex.Codex().install()
        self.assertEqual(self.calls[:3], [("marketplace", "list"), ("marketplace", "remove", "sidelit"),
                                          ("marketplace", "add", folder)])

    def test_uninstall(self):
        codex.Codex().install()
        self.calls = []
        codex.Codex().uninstall()
        self.assertEqual(self.calls, [("list",), ("remove", "sidelit@sidelit"), ("marketplace", "list"),
                                      ("marketplace", "remove", "sidelit")])
        self.calls = []
        self.assertEqual(codex.Codex().uninstall(), [])  # (nothing there: nothing said, nothing failed)

    def test_status_says_whether_you_trusted_the_hooks(self):
        self.assertEqual(codex.Codex().status()[0], False)
        codex.Codex().install()

        def server(trust):
            class Listing:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

                def call(self, method, params=None):
                    return {"data": [{"hooks": [{"pluginId": "sidelit@sidelit", "trustStatus": trust},
                                                {"pluginId": "other@x", "trustStatus": "untrusted"}]}]}
            return Listing
        self.copy_plugin()
        ok, said = codex.Codex(server=server("untrusted")).status()
        self.assertFalse(ok)
        self.assertTrue(any("/hooks" in line for line in said))
        self.assertEqual(codex.Codex(server=server("trusted")).status()[0], True)

    def copy_plugin(self):
        for name in codex.PLUGIN_FILES:
            dest = codex.cached_plugin() / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((codex.plugin_dir() / name).read_bytes())

    def test_sidelit_start_puts_the_plugin_in_once(self):
        """(a Sidelit from before Codex: the update's first start does it; taken out, it stays out)"""
        said = codex.Codex().upgrade()
        self.assertIn(("add", "sidelit@sidelit"), self.calls)
        self.assertIn("/hooks", said[-1])
        codex.Codex().uninstall()
        self.calls = []
        self.assertEqual(codex.Codex().upgrade(), [])
        self.assertEqual(self.calls, [])

    def test_sidelit_start_refreshes_codexs_copy_of_the_plugin(self):
        self.copy_plugin()
        self.assertEqual(codex.Codex().upgrade(), [])  # (the same: nothing to do)
        (codex.cached_plugin() / "hooks" / "hook.py").write_text("# an older Sidelit's\n")
        said = codex.Codex().upgrade()
        self.assertTrue(said and ("add", "sidelit@sidelit") in self.calls, said)

    def test_the_plugin_carries_codexs_events_only(self):
        plugin = Path(ROOT) / "plugin"
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual((manifest["name"], manifest["hooks"]), ("sidelit", "./hooks/codex.json"))
        config = json.loads((plugin / "hooks" / "codex.json").read_text())
        self.assertEqual(sorted(config["hooks"]), sorted(
            ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop",
             "Interrupt", "SubagentStart", "SubagentStop", "SessionEnd"]))  # (no Notification: Codex has none)
        for event, entries in config["hooks"].items():
            (entry,) = entries
            (hook,) = entry["hooks"]
            # (never exit 2, which blocks a turn's end: Codex would go on and on)
            self.assertEqual(hook["command"], 'python3 -I "${PLUGIN_ROOT}/hooks/hook.py" codex || true')
            self.assertEqual(hook.get("async", False), event in ("PreToolUse", "PostToolUse"), event)
            self.assertLessEqual(hook["timeout"], 3 if event in ("SessionEnd", "Interrupt") else 5)


class LayoutTest(unittest.TestCase):
    """A terminal running Codex is remembered as Codex's, and comes back as Codex."""

    def test_capture_and_launch(self):
        slot = Slot(id="s", kind="terminal", app="terminator")
        ctx = ad.Ctx(resume_command=lambda sid, cwd, agent=None: f"{agent} resume {sid}")
        win = Win(id="W", app="terminator", title="⠸ pricing", space=1, output=None)
        ctx.claude = {"W": {"session": SID, "agent": "codex", "title": "fix pricing", "cwd": "/src/pricing"}}
        terminal = ad.TerminalAdapter()
        self.assertTrue(terminal.capture(slot, win, ctx, True, 100.0))
        self.assertEqual(slot.state["agent"], "codex")
        self.assertEqual(slot.claudes[0]["agent"], "codex")
        self.assertEqual(terminal.launch(slot, ctx)["command"], f"codex resume {SID}")
        self.assertEqual(terminal.describe(slot), "Codex 'fix pricing'")
        # Codex's idle title is its project: it still runs there
        ctx.claude = {}
        terminal.capture(slot, Win(id="W", app="terminator", title="pricing", space=1, output=None), ctx, True, 200.0)
        self.assertNotIn("no_claude_since", slot.state)
        # a Claude Code session takes the terminal's place: no agent kept (Claude Code's is the default)
        ctx.claude = {"W": {"session": "c1", "agent": "claude", "title": "Docs", "cwd": "/src/pricing"}}
        terminal.capture(slot, win, ctx, True, 300.0)
        self.assertNotIn("agent", slot.state)

    def test_registry(self):
        self.assertEqual([a.name for a in agents.every()], ["claude", "codex", "opencode"])
        self.assertEqual(agents.get(None).name, "claude")
        self.assertEqual(agents.get("codex").mark(True), agents.get("codex").mark(False))
        self.assertEqual((agents.get("claude").mark(False), agents.get("claude").mark(True)), ("✳", "◐"))
        self.assertNotIn("✳", agents.get("codex").marks)  # (Claude's glyph is Claude's only)


if __name__ == "__main__":
    unittest.main()
