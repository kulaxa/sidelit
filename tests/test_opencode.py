"""OpenCode behind the agent interface: Sidelit's plugin for it (run under Node with events
recorded from OpenCode 1.19.3 and 2.0.32), its install, titles, which terminal shows an OpenCode 2
session, resume and fork, the turn check. tests/opencode_live.py runs the real thing."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import agents, detect, opencode, turncheck  # noqa: E402
from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core.model import Slot, Win  # noqa: E402

PLUGIN = Path(ROOT) / "plugin" / "opencode" / "sidelit.js"
NODE = shutil.which("node")
SID, MSG_U, MSG_A = "ses_eafc1a41bffeOhWMlgIXE32Ldn", "msg_1503e5bef001SYOqCkEtCEPYN6", "msg_1503e5bfa0017xabCXdfP9YaOZ"
INFO = {"id": SID, "slug": "playful-star", "version": "1.19.3", "projectID": "global", "directory": "/src/pricing",
        "path": "src/pricing", "title": "New session - 2026-10-18T18:20:26.468Z"}


def ev(kind, **props):
    return {"type": kind, "properties": props}


# a turn as OpenCode 1.19.3 told its plugins (trimmed to what Sidelit reads; the order is theirs)
TURN = [
    ev("session.created", sessionID=SID, info=INFO),
    ev("message.updated", sessionID=SID, info={"id": MSG_U, "role": "user", "sessionID": SID,
                                               "model": {"providerID": "anthropic", "modelID": "claude-sonnet-5-5"}}),
    ev("message.part.updated", sessionID=SID, part={"type": "text", "text": "fix the pricing test", "messageID": MSG_U,
                                                     "sessionID": SID, "id": "prt_u1"}, time=1792347626487),
    ev("session.status", sessionID=SID, status={"type": "busy"}),
    ev("message.updated", sessionID=SID, info={"id": MSG_A, "parentID": MSG_U, "role": "assistant", "mode": "build",
                                               "modelID": "claude-sonnet-5-5", "providerID": "anthropic", "sessionID": SID}),
    ev("session.updated", sessionID=SID, info={**INFO, "title": "Fix the pricing test"}),
    {"tool": {"input": {"tool": "bash", "sessionID": SID, "callID": "call_5"},
              "args": {"command": "npm test -- pricing", "description": "run the test"}}},
    ev("message.part.updated", sessionID=SID, part={"id": "prt_a1", "messageID": MSG_A, "sessionID": SID, "type": "text",
                                                     "text": "", "time": {"start": 1}}, time=1),
    ev("message.part.delta", sessionID=SID, messageID=MSG_A, partID="prt_a1", field="text", delta="All"),
    ev("message.part.updated", sessionID=SID, part={"id": "prt_a1", "messageID": MSG_A, "sessionID": SID, "type": "text",
                                                     "text": "All 12 pricing tests pass.", "time": {"start": 1, "end": 2}}, time=2),
    ev("session.status", sessionID=SID, status={"type": "busy"}),
    ev("session.status", sessionID=SID, status={"type": "idle"}),
    ev("session.idle", sessionID=SID),
]

# a turn as OpenCode 2.0.32 told its plugins (the same trimming); every copy of the plugin hears it
SID2, AT2 = "ses_eaf975f69ffe2AQr5RYFibwnnj", {"directory": "/src/pricing"}


def ev2(n, kind, located=True, **data):
    return {"id": f"evt_{n:04d}", "type": kind, "data": data, **({"location": AT2} if located else {})}


TURN2 = [
    ev2(1, "session.created", sessionID=SID2, projectID="6a900e40", location=AT2, subpath="", slug="tidy-forest",
        agent="build", model={"id": "kimi-k2.5", "providerID": "moonshot"}, version="2.0.32"),
    ev2(2, "session.inbox.enqueued", sessionID=SID2, inboxID="msg_u1",
        item={"type": "user", "payload": {"text": "fix the pricing test", "files": [], "agents": []}, "delivery": "steer"}),
    ev2(3, "session.execution.started", located=False, sessionID=SID2),
    ev2(4, "session.inbox.delivered", sessionID=SID2, inboxID="msg_u1"),
    ev2(5, "session.step.started", sessionID=SID2, assistantMessageID="msg_a1", agent="build",
        model={"id": "kimi-k2.5", "providerID": "moonshot", "variant": "default"}, started=1792350396839),
    {"tool": {"id": "call_5", "tool": "shell", "sessionID": SID2, "agent": "build", "messageID": "msg_a1",
              "input": {"command": "npm test -- pricing"}}},
    ev2(6, "session.tool.called", sessionID=SID2, assistantMessageID="msg_a1", id="call_5",
        input={"command": "npm test -- pricing"}, executed=False),
    ev2(7, "session.step.started", sessionID=SID2, assistantMessageID="msg_a2", agent="build",
        model={"id": "kimi-k2.5", "providerID": "moonshot", "variant": "default"}, started=1792350397000),
    ev2(8, "session.text.delta", sessionID=SID2, assistantMessageID="msg_a2", ordinal=0, delta="All"),
    ev2(9, "session.text.ended", sessionID=SID2, assistantMessageID="msg_a2", ordinal=0, text="All 12 pricing tests pass."),
    ev2(10, "session.text.ended", sessionID=SID2, assistantMessageID="msg_a2", ordinal=1, text="Shall I push?"),
    ev2(11, "session.execution.succeeded", located=False, sessionID=SID2),
    ev2(12, "session.renamed", sessionID=SID2, title="Fix the pricing test"),
    ev2(13, "session.viewed", located=False, sessionID=SID2, idle=1792350396900),
]


@unittest.skipIf(NODE is None, "needs Node")
class PluginTest(unittest.TestCase):
    """plugin/opencode/sidelit.js, as OpenCode runs it: its `event` and `tool.execute.before` hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "sidelit"
        mod = Path(self.tmp.name) / "plugin"  # (a folder of its own that says its .js files are modules)
        mod.mkdir()
        (mod / "package.json").write_text('{"type": "module"}')
        shutil.copy(PLUGIN, mod / "sidelit.js")
        self.module = mod / "sidelit.js"

    def tearDown(self):
        self.tmp.cleanup()

    # OpenCode 1 calls the default export's server() and hands its hooks each event (1.19.3 calls
    # its setup() too, with none of what OpenCode 2's has)
    V1 = ("import { readFileSync } from 'fs';"
          "const mod = await import(process.argv[1]);"
          "const hooks = await mod.default.server({ directory: '/src/pricing' });"
          "await mod.default.setup({ options: {}, agent: {}, aisdk: {}, catalog: {}, command: {}, integration: {},"
          "                          plugin: {}, reference: {}, skill: {} });"
          "for (const e of JSON.parse(readFileSync(0, 'utf8'))) {"
          "  if (e.tool) await hooks['tool.execute.before'](e.tool.input, { args: e.tool.args });"
          "  else await hooks.event({ event: e }); }")
    # OpenCode 2 loads the file once per folder it works in (`copies`), calls each copy's setup(),
    # and tells every copy every event of its server
    V2 = ("import { readFileSync } from 'fs';"
          "import { pathToFileURL } from 'url';"
          "const items = JSON.parse(readFileSync(0, 'utf8'));"
          "const copies = Number(process.argv[2]);"
          "const runs = [];"
          "for (let n = 0; n < copies; n++) {"
          "  const mod = await import(pathToFileURL(process.argv[1]).href + '?copy=' + n);"
          "  let toolHook = null; let done; runs.push(new Promise((r) => (done = r)));"
          "  const ctx = { location: { directory: n ? '/home/alex' : '/src/pricing' },"
          "    event: { subscribe: () => (async function* () {"
          "      await new Promise((r) => setTimeout(r, 10));"
          "      for (const e of items) { if (e.tool) { if (toolHook) await toolHook(e.tool) } else yield e;"
          "        await new Promise((r) => setTimeout(r, 1)) }"
          "      done() })() },"
          "    tool: { hook: async (name, fn) => { if (name === 'execute.before') toolHook = fn } } };"
          "  const stop = await mod.default.setup(ctx);"
          "  runs.push(runs.pop().then(() => stop())); }"
          "await Promise.all(runs);")

    def run_plugin(self, events, env=None, args=(), copies=0):
        """Every event through one loaded plugin, in order (OpenCode 2's way with `copies`); the
        records it wrote."""
        mine = {k: v for k, v in os.environ.items() if k not in detect.PANE_KEYS + ("KITTY_LISTEN_ON", "TMUX")}
        r = subprocess.run([NODE, "--input-type=module", "-e", self.V2 if copies else self.V1, str(self.module),
                            *([str(copies)] if copies else args)], input=json.dumps(events),
                           text=True, capture_output=True, timeout=30,
                           env={**mine, "SIDELIT_HOME": str(self.home), "TMUX_PANE": "%7", **(env or {})})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        folder = self.home / "opencode"
        return {p.stem: json.loads(p.read_text()) for p in folder.glob("*.json")} if folder.exists() else {}

    def test_a_turn(self):
        rec = self.run_plugin(TURN)[SID]
        self.assertEqual((rec["state"], rec["title"], rec["model"], rec["cwd"]),
                         ("done", "Fix the pricing test", "claude-sonnet-5-5", "/src/pricing"))
        self.assertEqual((rec["asked"], rec["tools"], rec["said"], rec["first_prompt"]),
                         ("fix the pricing test", ["bash: npm test -- pricing"], "All 12 pricing tests pass.",
                          "fix the pricing test"))
        self.assertIn("turn_end", rec)
        self.assertEqual(rec["pane"], {"TMUX_PANE": "%7"})
        self.assertTrue(rec["pid"] and rec["ancestors"][0] == rec["pid"])  # (OpenCode's own process)
        self.assertTrue((self.home / "opencode" / f"{SID}.prompt").exists())  # you typed it
        # working again: the turn's end and last words are past
        rec = self.run_plugin(TURN + [ev("session.status", sessionID=SID, status={"type": "busy"})])[SID]
        self.assertEqual(rec["state"], "busy")
        self.assertNotIn("turn_end", rec)
        self.assertNotIn("said", rec)

    def test_permissions_and_questions_wait_for_you(self):
        start = TURN[:4]
        for asked, replied in (("permission.asked", "permission.replied"), ("question.asked", "question.replied")):
            recs = self.run_plugin(start + [ev(asked, sessionID=SID, id="per_1")])
            self.assertEqual(recs[SID]["state"], "waiting", asked)
            recs = self.run_plugin(start + [ev(asked, sessionID=SID, id="per_1"),
                                            ev(replied, sessionID=SID, requestID="per_1", reply="once")])
            self.assertEqual(recs[SID]["state"], "busy", replied)

    def test_a_subagent_is_its_parents(self):
        child = "ses_child"
        recs = self.run_plugin(TURN[:4] + [
            ev("session.created", sessionID=child, info={"id": child, "parentID": SID, "directory": "/src/pricing",
                                                         "title": "Explore the tests (@explore subagent)"}),
            ev("session.status", sessionID=child, status={"type": "busy"})])
        self.assertEqual(list(recs), [SID])  # (no box of its own)
        self.assertEqual(list(recs[SID]["agents"]), [child])
        recs = self.run_plugin(TURN[:4] + [
            ev("session.created", sessionID=child, info={"id": child, "parentID": SID}),
            ev("session.status", sessionID=child, status={"type": "busy"}),
            ev("permission.asked", sessionID=child, id="per_2")])
        self.assertEqual(recs[SID]["state"], "waiting")  # it asks you all the same
        recs = self.run_plugin(TURN[:4] + [
            ev("session.created", sessionID=child, info={"id": child, "parentID": SID}),
            ev("session.status", sessionID=child, status={"type": "busy"}),
            ev("permission.asked", sessionID=child, id="per_2"),
            ev("permission.replied", sessionID=child, requestID="per_2", reply="once"),
            ev("session.status", sessionID=child, status={"type": "idle"})])
        self.assertEqual((recs[SID]["state"], recs[SID]["agents"]), ("busy", {}))

    def test_an_opencode_2_turn(self):
        """OpenCode 2 runs the plugin in its server, once per folder: each event counts once, and
        the record says it comes from the server (no process of a terminal: opencode.py finds it)"""
        for copies in (1, 2):
            shutil.rmtree(self.home, ignore_errors=True)
            rec = self.run_plugin(TURN2, copies=copies)[SID2]
            self.assertEqual((rec["state"], rec["title"], rec["model"], rec["cwd"]),
                             ("done", "Fix the pricing test", "kimi-k2.5", "/src/pricing"), copies)
            self.assertEqual((rec["asked"], rec["tools"], rec["said"], rec["first_prompt"]),
                             ("fix the pricing test", ["shell: npm test -- pricing"],
                              "All 12 pricing tests pass.\nShall I push?", "fix the pricing test"), copies)
            self.assertEqual((rec["server"], rec["pid"]), (True, 0))
            self.assertNotIn("pane", rec)  # (the server's terminal, if any, is not the session's)
            self.assertNotIn("ancestors", rec)
            self.assertIn("turn_end", rec)
            self.assertTrue((self.home / "opencode" / f"{SID2}.prompt").exists())
        rec = self.run_plugin(TURN2 + [ev2(20, "session.execution.started", located=False, sessionID=SID2)], copies=2)[SID2]
        self.assertEqual(rec["state"], "busy")
        self.assertNotIn("said", rec)

    def test_a_turn_that_says_nothing_has_no_last_words(self):
        """(stopped, or failed, before it said anything: not the turn before's words)"""
        msg = "msg_u2"
        rec = self.run_plugin(TURN + [
            ev("message.updated", sessionID=SID, info={"id": msg, "role": "user", "sessionID": SID}),
            ev("message.part.updated", sessionID=SID, part={"type": "text", "text": "now deploy", "messageID": msg,
                                                             "sessionID": SID, "id": "prt_u2"}),
            ev("session.status", sessionID=SID, status={"type": "busy"}),
            ev("session.status", sessionID=SID, status={"type": "idle"})])[SID]
        self.assertEqual((rec["state"], rec["asked"], rec.get("said")), ("done", "now deploy", None))
        rec = self.run_plugin(TURN2 + [
            ev2(60, "session.inbox.enqueued", sessionID=SID2, inboxID="msg_u2",
                item={"type": "user", "payload": {"text": "now deploy"}, "delivery": "steer"}),
            ev2(61, "session.execution.started", located=False, sessionID=SID2),
            ev2(62, "session.execution.interrupted", located=False, sessionID=SID2, reason="user")], copies=2)[SID2]
        self.assertEqual((rec["state"], rec["asked"], rec.get("said")), ("done", "now deploy", None))

    def test_opencode_2_asks_you(self):
        start = TURN2[:5]
        asked = ev2(30, "permission.asked", id="per_1", sessionID=SID2, action="shell", resources=["rm -f x"],
                    save=["rm *"], source={"type": "tool", "messageID": "msg_a1", "id": "call_5"})
        replied = ev2(31, "permission.replied", sessionID=SID2, requestID="per_1", reply="once")
        form = ev2(32, "form.created", form={"id": "frm_1", "sessionID": SID2, "title": "Questions",
                                              "metadata": {"kind": "question"}, "fields": []})
        answered = ev2(33, "form.replied", id="frm_1", sessionID=SID2, answer={"q0": "Alpha"})
        self.assertEqual(self.run_plugin(start + [asked], copies=2)[SID2]["state"], "waiting")
        self.assertEqual(self.run_plugin(start + [asked, replied], copies=2)[SID2]["state"], "busy")
        self.assertEqual(self.run_plugin(start + [form], copies=2)[SID2]["state"], "waiting")
        self.assertEqual(self.run_plugin(start + [form, answered], copies=2)[SID2]["state"], "busy")
        for n, end in enumerate(("session.execution.interrupted", "session.execution.failed")):
            rec = self.run_plugin(start + [ev2(40 + n, end, located=False, sessionID=SID2)], copies=2)[SID2]
            self.assertEqual(rec["state"], "done", end)

    def test_an_opencode_2_subagent_is_its_parents(self):
        child = "ses_child2"
        recs = self.run_plugin(TURN2[:5] + [
            ev2(50, "session.created", sessionID=child, parentID=SID2, location=AT2, slug="x", version="2.0.32",
                title="Explore the tests (@explore subagent)"),
            ev2(51, "session.execution.started", located=False, sessionID=child)], copies=2)
        self.assertEqual(list(recs), [SID2])
        self.assertEqual(list(recs[SID2]["agents"]), [child])
        # (a fork names its source as parentID too, but is a session of its own)
        recs = self.run_plugin(TURN2 + [ev2(52, "session.forked", sessionID="ses_fork", parentID=SID2),
                                        ev2(53, "session.execution.started", located=False, sessionID="ses_fork")],
                               copies=1)
        self.assertEqual(sorted(recs), sorted([SID2, "ses_fork"]))

    def test_what_it_does_not_need_writes_nothing(self):
        self.assertEqual(self.run_plugin([ev("message.part.delta", sessionID=SID, delta="x"),
                                          ev("session.diff", sessionID=SID, diff=[]), {"type": "plugin.added"},
                                          {"bogus": True}, ev("session.status")]), {})

    def test_a_session_opencode_was_started_for_is_there_at_once(self):
        """`opencode -s <id>` (a restore after a login) says nothing until you type: it is on screen"""
        self.run_plugin(TURN)
        rec = self.run_plugin([], args=["-s", SID])[SID]
        self.assertEqual((rec["event"], rec["state"], rec["title"], rec["cwd"]), ("opened", "done", "Fix the pricing test",
                                                                                  "/src/pricing"))
        self.assertEqual(self.run_plugin([], args=["-s", "ses_new", "--fork"]).get("ses_new"), None)
        self.assertEqual(self.run_plugin([], args=["run", "-s", "ses_new"]).get("ses_new"), None)

    def test_a_deleted_session_ended(self):
        self.assertEqual(self.run_plugin(TURN + [ev("session.deleted", sessionID=SID, info=INFO)])[SID]["state"], "ended")


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from sidelit import config
        patches = [mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg")}),
                   mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name) / "sidelit-config"),
                   mock.patch.object(opencode, "opencode_binary", lambda: "/usr/bin/opencode")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.agent = opencode.OpenCode()

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_is_a_file_in_opencodes_plugin_folder(self):
        dest = Path(self.tmp.name) / "xdg" / "opencode" / "plugins" / "sidelit.js"
        self.assertEqual(self.agent.status()[0], False)
        self.agent.install()
        self.assertEqual(dest.read_bytes(), PLUGIN.read_bytes())
        self.assertEqual(self.agent.status(), (True, [f"Sidelit's OpenCode plugin is installed ({dest})"]))
        dest.write_text(PLUGIN.read_text().replace("SAID_MAX = 6000", "SAID_MAX = 5000"))  # an older Sidelit's
        self.assertFalse(self.agent.status()[0])
        self.assertTrue(self.agent.upgrade())
        self.assertEqual(dest.read_bytes(), PLUGIN.read_bytes())
        self.assertEqual(self.agent.uninstall(), [f"took Sidelit's plugin out of OpenCode's plugin folder ({dest})"])
        self.assertFalse(dest.exists())
        self.assertEqual(self.agent.upgrade(), [])  # (taken out: it stays out)
        self.assertEqual(self.agent.uninstall(), [])

    def test_sidelit_start_puts_it_in_once(self):
        said = self.agent.upgrade()
        self.assertTrue(said and opencode.installed_copy().exists(), said)
        self.assertIn("start it again", said[-1])

    def test_a_file_of_that_name_that_is_not_ours_is_left_alone(self):
        dest = opencode.installed_copy()
        dest.parent.mkdir(parents=True)
        dest.write_text("// someone else's\n")
        with self.assertRaisesRegex(RuntimeError, "not Sidelit's"):
            self.agent.install()
        self.assertEqual((self.agent.uninstall(), dest.read_text()), ([], "// someone else's\n"))

    def test_titles(self):
        a = self.agent
        self.assertEqual(a.window_key("OC | Fix the pricing test"), "Fix the pricing test")
        self.assertEqual(a.window_key("OpenCode"), "")  # (its home screen: no session yet)
        self.assertIsNone(a.window_key("alex@laptop: ~"))
        self.assertTrue(a.owns_title("OC | x") and a.owns_title("OpenCode") and not a.owns_title("✳ Claude"))
        self.assertTrue(a.unmatched_title("OC | x") and not a.unmatched_title("OpenCode"))
        self.assertEqual(a.title("s", "/", {"title": "Fix it", "first_prompt": "fix"}), "Fix it")
        self.assertEqual(a.title("s", "/", {"first_prompt": "fix"}), "fix")
        self.assertEqual(a.model("s", {"model": "claude-sonnet-5-5"}), "Sonnet 5.5")
        self.assertEqual(a.model("s", {"model": "gpt-5.5-codex"}), "GPT-5.5 Codex")
        self.assertEqual([opencode.pretty_model(m) for m in ("kimi-k2.5", "qwen3-coder", "glm-4.6", "gpt-oss-120b")],
                         ["Kimi K2.5", "Qwen3 Coder", "GLM 4.6", "GPT-oss 120b"])
        self.assertIsNone(a.model("s", {}))

    def test_a_long_title_as_its_terminal_cuts_it(self):
        long = "Move the pricing tiers to the new billing API and keep the old ones"
        self.assertEqual(self.agent.title_key("s", {"title": long}), long[:37] + "…")
        # (JavaScript counts an emoji as two)
        self.assertEqual(self.agent.title_key("s", {"title": "🚀 " + long}), "🚀 " + long[:34] + "…")
        self.assertEqual(self.agent.title_key("s", {"title": "🚀" * 21}), "🚀" * 18 + "…")
        self.assertEqual(self.agent.title_key("s", {"title": "Fix it"}), "Fix it")
        self.assertIsNone(self.agent.title_key("s", {}))

    def test_resume_and_fork(self):
        self.assertIn("opencode -s ses_1 || opencode", self.agent.resume_command("ses_1", "/src"))
        fork = self.agent.fork_command("ses_1", "/src")  # (Sidelit's own, in the new terminal: fork_here)
        self.assertIn(f'env PYTHONPATH="{ROOT}" "{sys.executable}" -m sidelit opencode-fork ses_1', fork)
        self.assertNotIn("'", fork.split("-c '", 1)[1].rsplit("'", 1)[0])  # (it goes in single quotes)
        self.assertIsNone(self.agent.fork_command("ses_1; rm -rf ~", "/src"))
        self.assertIsNone(self.agent.resume_command("$(true)", "/src"))

    def test_the_fork_in_its_terminal(self):
        """OpenCode 1 forks itself; OpenCode 2's server forks, and the copy opens; a fork that
        fails says so and a new session starts"""
        def fork(version, api_out="", api_err=""):
            ran, calls = [], []
            run = lambda argv, **kw: (calls.append(argv), mock.Mock(stdout=api_out, stderr=api_err))[1]  # noqa: E731

            def execv(path, argv):  # (it becomes that opencode: nothing after it runs)
                ran.append(argv)
                raise SystemExit
            with mock.patch.object(opencode, "version_of", lambda binary=None: version), \
                    mock.patch.object(opencode.shutil, "which", lambda name: "/usr/bin/opencode"), \
                    mock.patch.object(opencode.subprocess, "run", run), \
                    mock.patch.object(opencode.os, "execv", execv), \
                    redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                opencode.fork_here("ses_1")
            return ran[0], calls, err.getvalue()
        self.assertEqual(fork(1)[:2], (["/usr/bin/opencode", "-s", "ses_1", "--fork"], []))
        made = json.dumps({"data": {"id": "ses_copy", "fork": {"sessionID": "ses_1"}, "title": "x (fork #1)"}})
        argv, calls, _ = fork(2, made)
        self.assertEqual(calls, [["/usr/bin/opencode", "api", "POST", "/api/session/ses_1/fork", "-d", "{}"]])
        self.assertEqual(argv, ["/usr/bin/opencode", "-s", "ses_copy"])
        argv, _, said = fork(2, "", "Session.ForkEmptyError")
        self.assertEqual(argv, ["/usr/bin/opencode"])
        self.assertIn("could not fork that session (Session.ForkEmptyError)", said)
        # (one that will not say which it is: OpenCode 2's way, else 1's)
        self.assertEqual(fork(None, made)[0], ["/usr/bin/opencode", "-s", "ses_copy"])
        self.assertEqual(fork(None, "", "unknown command")[0], ["/usr/bin/opencode", "-s", "ses_1", "--fork"])

    def test_which_opencode_it_is(self):
        for said, want in (("1.19.3\n", 1), ("opencode v2.0.32\n", 2), ("", None)):
            opencode._MAJOR.clear()
            with mock.patch.object(opencode.os, "stat", lambda p: mock.Mock(st_mtime=1.0)), \
                    mock.patch.object(opencode.subprocess, "run", lambda *a, **kw: mock.Mock(stdout=said)):
                self.assertEqual(opencode.version_of("/usr/bin/opencode"), want, said)
                self.assertEqual(opencode.major("/usr/bin/opencode"), want or 1, said)
        self.assertEqual(opencode._MAJOR, {})  # (one that did not say is asked again next time)
        opencode._MAJOR.clear()

    def test_no_usage_bars(self):
        self.assertIsNone(self.agent.usage_file)
        self.assertFalse(self.agent.polls_usage)

    def test_the_mark_is_its_own(self):
        marks = {a.name: set(a.marks) for a in agents.every()}
        self.assertTrue(marks["opencode"].isdisjoint(marks["claude"] | marks["codex"]))


class FakeProc:
    def __init__(self, lines):
        r, w = os.pipe()
        os.write(w, "".join(json.dumps(x) + "\n" for x in lines).encode())
        os.close(w)
        self.stdout = os.fdopen(r, "rb")
        self.stdin = open(os.devnull, "wb")
        self.sidelit_system = "SYSTEM"
        self.sidelit_binary = "/usr/bin/opencode"
        self.sidelit_title = "Sidelit turn check 1a2b3c4d"
        self.sidelit_cwd = "/run/sidelit"

    def wait(self, timeout=None):
        return 0

    def forgotten(self):
        """(the check's session is deleted on a thread of its own)"""
        self.sidelit_forgetting.join(10)


RUN_OUT = [  # `opencode run --format json`, a turn (1.19.3)
    {"type": "step_start", "timestamp": 1792347598744, "sessionID": "ses_judge",
     "part": {"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_judge", "type": "step-start"}},
    {"type": "text", "timestamp": 1792347598765, "sessionID": "ses_judge",
     "part": {"id": "prt_2", "messageID": "msg_1", "sessionID": "ses_judge", "type": "text",
              "text": '{"verdict": "waiting", "on": "CI pipeline"}', "time": {"start": 1, "end": 2}}},
    {"type": "step_finish", "timestamp": 1792347598765, "sessionID": "ses_judge",
     "part": {"id": "prt_3", "reason": "stop", "messageID": "msg_1", "sessionID": "ses_judge", "type": "step-finish"}},
]


class TurnCheckTest(unittest.TestCase):
    def test_opencode_run_answers_and_its_session_goes(self):
        deleted = []
        with mock.patch.object(opencode.subprocess, "run", lambda argv, **kw: deleted.append(argv)):
            proc = FakeProc(RUN_OUT)
            self.addCleanup(proc.stdout.close)
            reply = opencode.OpenCode().ask_judge(proc, "turn", 5)
            proc.forgotten()
        self.assertEqual(turncheck.parse(reply), ("waiting", "CI pipeline"))
        self.assertEqual(deleted, [["/usr/bin/opencode", "session", "delete", "--pure", "ses_judge"]])

    def test_a_check_that_said_nothing_is_found_by_its_title(self):
        """(no answer in time, so no word of its session's id: its own title finds it)"""
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            listed = [{"id": "ses_other", "title": "Sidelit turn check 99999999"},
                      {"id": "ses_mine", "title": "Sidelit turn check 1a2b3c4d"}, {"id": "ses_yours", "title": "Fix it"}]
            return mock.Mock(stdout=json.dumps(listed))
        with mock.patch.object(opencode.subprocess, "run", run):
            proc = FakeProc([])
            self.addCleanup(proc.stdout.close)
            with self.assertRaises(EOFError):
                opencode.OpenCode().ask_judge(proc, "turn", 5)
            proc.forgotten()
        self.assertEqual(ran, [["/usr/bin/opencode", "session", "list", "--pure", "--format", "json"],
                               ["/usr/bin/opencode", "session", "delete", "--pure", "ses_mine"]])

    def test_an_error_is_an_error(self):
        with mock.patch.object(opencode.subprocess, "run", lambda argv, **kw: None):
            proc = FakeProc(RUN_OUT[:1] + [{"type": "error", "sessionID": "ses_judge",
                                            "error": {"name": "APIError", "data": {"message": "rate limited"}}}])
            self.addCleanup(proc.stdout.close)
            with self.assertRaisesRegex(RuntimeError, "rate limited"):
                opencode.OpenCode().ask_judge(proc, "turn", 5)
            proc.forgotten()

    def test_the_judge_has_no_tools_and_no_plugins(self):
        with mock.patch.object(opencode, "opencode_binary", lambda: "/usr/bin/opencode"), \
                mock.patch.object(opencode.subprocess, "Popen") as popen:
            opencode.OpenCode().spawn_judge("anthropic/claude-haiku-5-5", "SYSTEM", "/run/sidelit")
        argv, kw = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(argv[:6], ["/usr/bin/opencode", "run", "--format", "json", "--pure", "--title"])
        self.assertRegex(argv[6], r"^Sidelit turn check [0-9a-f]{8}$")  # (each its own)
        self.assertEqual(argv[-2:], ["-m", "anthropic/claude-haiku-5-5"])
        self.assertEqual(json.loads(kw["env"]["OPENCODE_CONFIG_CONTENT"]),
                         {"permission": {"*": "deny"}, "tools": {"*": False}})

    def test_opencode_2s_judge_runs_on_a_server_of_its_own(self):
        """(no --pure there: its plugins off but OpenCode's own, every permission refused, and
        never your background server, also not to delete the check's session)"""
        with mock.patch.object(opencode, "opencode_binary", lambda: "/usr/bin/opencode"), \
                mock.patch.object(opencode, "major", lambda binary=None: 2), \
                mock.patch.object(opencode.subprocess, "Popen") as popen:
            opencode.OpenCode().spawn_judge(None, "SYSTEM", "/run/sidelit")
        argv, kw = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(argv[:6], ["/usr/bin/opencode", "run", "--format", "json", "--standalone", "--title"])
        self.assertEqual(len(argv), 7)
        self.assertEqual(json.loads(kw["env"]["OPENCODE_CONFIG_CONTENT"]),
                         {"permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
                          "plugins": ["-*", "opencode.*"]})
        deleted = []
        with mock.patch.object(opencode, "major", lambda binary=None: 2), \
                mock.patch.object(opencode.subprocess, "run", lambda argv, **kw: deleted.append(argv)):
            proc = FakeProc(RUN_OUT)
            self.addCleanup(proc.stdout.close)
            opencode.OpenCode().ask_judge(proc, "turn", 5)
            proc.forgotten()
        self.assertEqual(deleted, [["/usr/bin/opencode", "session", "delete", "--standalone", "ses_judge"]])

    def test_opencode_2s_errors(self):
        with mock.patch.object(opencode.subprocess, "run", lambda argv, **kw: None):
            proc = FakeProc([{"type": "error", "timestamp": 1, "sessionID": "",
                              "error": {"type": "unknown", "message": "Model not found: x/y"}}])
            self.addCleanup(proc.stdout.close)
            with self.assertRaisesRegex(RuntimeError, "Model not found"):
                opencode.OpenCode().ask_judge(proc, "turn", 5)
            proc.forgotten()

    def test_the_turn_from_what_the_plugin_kept(self):
        turn = opencode.OpenCode().last_turn({"asked": "deploy it", "tools": ["bash: ./deploy.sh"],
                                              "said": "Deploying; I'll report when it is live."})
        self.assertIn("[OpenCode] Deploying", turncheck.describe(turn))
        self.assertIn("several OpenCode sessions", turncheck.prompt_for(opencode.OpenCode()))


TUI_A, TUI_B, TUI_C, RUN, SERVICE, SHELL, TERM = 501, 502, 503, 504, 590, 400, 300


class TerminalsTest(unittest.TestCase):
    """OpenCode 2: its background server writes the records; which opencode in a terminal shows
    each session is found from the processes (agents.TerminalClients)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "opencode"
        self.dir.mkdir()
        self.table = {TUI_A: (SHELL, "opencode"), TUI_B: (SHELL, "opencode"), TUI_C: (SHELL, "opencode"),
                      RUN: (SHELL, "opencode"), SERVICE: (1, "opencode"), SHELL: (TERM, "zsh"), TERM: (1, "foot")}
        self.args = {TUI_A: ["opencode"], TUI_B: ["opencode", "-s", "ses_old"], TUI_C: ["opencode", "--session=ses_back"],
                     RUN: ["opencode", "run", "check the logs"], SERVICE: ["opencode", "serve", "--service"]}
        self.folder = {TUI_A: "/src/pricing", TUI_B: "/src/billing", TUI_C: "/src/docs", RUN: "/src/logs",
                       SERVICE: "/home/alex"}
        self.started = {TUI_A: 100.0, TUI_B: 200.0, TUI_C: 50.0, RUN: 300.0, SERVICE: 90.0}
        xdg = Path(self.tmp.name) / "xdg"
        patches = [mock.patch.object(opencode, "cmdline", lambda p: self.args.get(p, [])),
                   mock.patch.object(agents, "cmdline", lambda p: self.args.get(p, [])),
                   mock.patch.object(opencode, "cwd_of", lambda p: self.folder.get(p, "")),
                   mock.patch.object(opencode, "in_terminal", lambda p: p != SERVICE),
                   mock.patch.object(agents, "pane_of", lambda p: {"KITTY_WINDOW_ID": str(p)}),
                   mock.patch.object(opencode.os.path, "realpath", lambda p: p),
                   mock.patch.object(agents.os.path, "realpath", lambda p: p),
                   mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (xdg / "opencode" / "plugins").mkdir(parents=True)
        shutil.copy(PLUGIN, xdg / "opencode" / "plugins" / "sidelit.js")
        self.agent = opencode.OpenCode(hook_dir=self.dir, started=lambda p: self.started.get(p))

    def tearDown(self):
        self.tmp.cleanup()

    def server_rec(self, sid, cwd, started):
        return {"session": sid, "server": True, "pid": 0, "cwd": cwd, "started": started, "t": started, "state": "busy"}

    def test_each_session_gets_its_opencode(self):
        recs = [self.server_rec("ses_new", "/src/pricing", 150.0),  # opened in the opencode there
                self.server_rec("ses_old", "/src/billing", 20.0),   # the one started for it (-s)
                self.server_rec("ses_log", "/src/logs", 301.0),     # an `opencode run`'s
                self.server_rec("ses_early", "/src/pricing", 40.0),  # older than any opencode there: none
                {"session": "ses_v1", "pid": 777, "cwd": "/src/pricing", "started": 160.0}]  # (OpenCode 1's own word)
        self.agent.resolve(recs, self.table)
        self.assertEqual([r["pid"] for r in recs], [TUI_A, TUI_B, RUN, 0, 777])
        self.assertEqual(recs[0]["ancestors"], [TUI_A, SHELL, TERM])
        self.assertEqual(recs[0]["pane"], {"KITTY_WINDOW_ID": str(TUI_A)})
        self.assertNotIn(SERVICE, self.agent.terminals.live)  # (the server shows nothing)

    def test_an_opencode_started_for_a_session_shows_it_at_once(self):
        """(as a restore after a login has it: OpenCode 2 says nothing of it until you type)"""
        self.agent.resolve([], self.table)
        rec = json.loads((self.dir / "ses_back.json").read_text())
        self.assertEqual((rec["session"], rec["state"], rec["server"], rec["cwd"], rec["started"]),
                         ("ses_back", "done", True, "/src/docs", 50.0))
        self.assertTrue((self.dir / "ses_old.json").exists())
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")), ["ses_back.json", "ses_old.json"])
        # what the plugin wrote stands
        (self.dir / "ses_back.json").write_text(json.dumps({**rec, "state": "busy", "title": "Docs"}))
        self.agent.resolve([], self.table)
        self.assertEqual(json.loads((self.dir / "ses_back.json").read_text())["state"], "busy")
        # once per opencode: a session deleted in it (its record ended, then tidied away) stays gone
        (self.dir / "ses_back.json").unlink()
        self.agent.resolve([], self.table)
        self.assertFalse((self.dir / "ses_back.json").exists())
        # one just started: OpenCode 1's plugin may say so itself any moment
        self.started[TUI_C] = time.time()
        self.agent.terminals.seen = None
        self.agent._opened.clear()
        self.agent.resolve([], self.table)
        self.assertFalse((self.dir / "ses_back.json").exists())

    def test_without_the_plugin_nothing_is_written(self):
        opencode.installed_copy().unlink()
        self.agent.resolve([], self.table)
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_the_detector_shows_it_in_its_terminal(self):
        (self.dir / "ses_new.json").write_text(json.dumps(self.server_rec("ses_new", "/src/pricing", 150.0)))
        with mock.patch.object(detect, "in_terminal", lambda p: p != SERVICE):
            d = detect.Detector(agents=[self.agent], started=lambda p: self.started.get(p),
                                windows=Path(self.tmp.name) / "w.json")
            d.refresh(time.time(), force=True, table=self.table)
        self.assertEqual((d.hooked["ses_new"]["pid"], d.hooked["ses_new"]["windowless"]), (TUI_A, False))


class WindowTest(unittest.TestCase):
    """Its terminal's title is the session's: "OC | <title>"."""

    def test_the_terminal_you_typed_into(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "opencode"
            folder.mkdir()
            table = {800: (799, "opencode"), 799: (798, "zsh"), 798: (1, "foot")}
            (folder / "s1.json").write_text(json.dumps({"session": "s1", "pid": 800, "ancestors": [800, 799, 798],
                                                        "cwd": "/src/pricing", "state": "busy", "t": 101.0,
                                                        "started": 90.0, "title": "Fix the pricing test"}))
            (folder / "s1.prompt").write_text("101.0")
            with mock.patch.object(detect, "in_terminal", lambda p: True):
                d = detect.Detector(agents=[opencode.OpenCode(hook_dir=folder)], started=lambda p: None,
                                    windows=Path(tmp) / "w.json")
                d.note_focus(100.0, "B")
                d.refresh(102.0, force=True, table=table)
                wins = {"A": Win(id="A", app="foot", title="alex@laptop: ~", space=1, output=None),
                        "B": Win(id="B", app="foot", title="OC | Fix the pricing test", space=1, output=None)}
                d.assign(wins, {})
                got = d.claudes_for(wins["B"].title, 0, "B")
            self.assertEqual([(c["agent"], c["title"], c["status"], c["how"]) for c in got],
                             [("opencode", "Fix the pricing test", "busy", "focus")])
            self.assertEqual(d.claudes_for(wins["A"].title, 0, "A"), [])

    def test_restore_resumes_it_with_opencode(self):
        slot = Slot(id="s", kind="terminal", app="foot")
        ctx = ad.Ctx(resume_command=lambda sid, cwd, agent=None: agents.get(agent).resume_command(sid, cwd))
        ctx.claude = {"W": {"session": "ses_1", "agent": "opencode", "title": "Fix it", "cwd": "/src/pricing"}}
        terminal = ad.TerminalAdapter()
        terminal.capture(slot, Win(id="W", app="foot", title="OC | Fix it", space=1, output=None), ctx, True, 1.0)
        self.assertEqual(slot.state["agent"], "opencode")
        self.assertIn("opencode -s ses_1", terminal.launch(slot, ctx)["command"])
        self.assertEqual(terminal.describe(slot), "OpenCode 'Fix it'")


if __name__ == "__main__":
    unittest.main()
