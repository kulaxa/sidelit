"""Other sources: config files, feed validation, the poller's timing and failures, and the
report of this desktop's Claudes back to them, over real HTTP to a fake receiver."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sidelit import sources  # noqa: E402
from sidelit.tabstate import Attention  # noqa: E402


def write(folder: Path, name: str, data):
    (folder / name).write_text(data if isinstance(data, str) else json.dumps(data))


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_reads_sources_in_file_order_with_defaults(self):
        write(self.dir, "b.json", {"name": "Jobs", "url": "http://127.0.0.1:1/feed", "token_file": "~/t", "every": 1})
        write(self.dir, "a.json", {"url": "https://example.test/feed"})
        write(self.dir, "notes.txt", "ignored")
        got = sources.load_sources(self.dir)
        self.assertEqual([s["key"] for s in got], ["a", "b"])
        self.assertEqual(got[0]["name"], "a")
        self.assertEqual(got[0]["every"], 10)
        self.assertEqual(got[1]["every"], 3)  # never faster than 3 s
        self.assertEqual(got[1]["token_file"], "~/t")
        self.assertIsNone(got[1]["report"])
        self.assertEqual((got[1]["only"], got[1]["workspaces"]), ({}, False))

    def test_only_and_workspaces(self):
        write(self.dir, "a.json", {"url": "https://example.test/feed", "workspaces": True,
                                   "only": {"here": True, "computer": "Work laptop", "odd": [1]}})
        write(self.dir, "b.json", {"url": "https://example.test/feed", "workspaces": "yes", "only": "here"})
        got = {s["key"]: s for s in sources.load_sources(self.dir)}
        self.assertEqual(got["a"]["only"], {"here": True, "computer": "Work laptop"})  # (a list is no value)
        self.assertTrue(got["a"]["workspaces"])
        self.assertEqual((got["b"]["only"], got["b"]["workspaces"]), ({}, False))

    def test_a_report_url_is_kept_only_when_it_is_http(self):
        write(self.dir, "a.json", {"url": "https://example.test/feed", "report": "https://example.test/report"})
        write(self.dir, "b.json", {"url": "https://example.test/feed", "report": "file:///tmp/x"})
        got = {s["key"]: s for s in sources.load_sources(self.dir)}
        self.assertEqual(got["a"]["report"], "https://example.test/report")
        self.assertIsNone(got["b"]["report"])

    def test_broken_or_unsafe_configs_become_errors_not_crashes(self):
        write(self.dir, "bad.json", "{not json")
        write(self.dir, "file.json", {"url": "file:///etc/passwd"})
        got = {s["key"]: s for s in sources.load_sources(self.dir)}
        self.assertIn("unreadable", got["bad"]["error"])
        self.assertIn("http(s)", got["file"]["error"])
        self.assertIsNone(got["file"]["url"])

    def test_missing_folder_means_no_sources(self):
        self.assertEqual(sources.load_sources(self.dir / "nope"), [])


class Feed(unittest.TestCase):
    def test_keeps_well_formed_items_and_orders_what_needs_you_first(self):
        items = sources.parse_feed({"items": [
            {"id": "1", "title": "Old done", "state": "done", "updated": 100, "url": "https://x.test/1"},
            {"id": "2", "title": "Needs you", "state": "waiting", "updated": 50, "detail": "May it push?"},
            {"id": "3", "title": "Busy", "state": "working", "updated": 200},
            {"title": ""},                                   # no title: dropped
            "nonsense",                                      # not an object: dropped
            {"title": "Odd state", "state": "exploded", "url": "javascript:alert(1)"},
        ]})
        self.assertEqual([i["title"] for i in items], ["Needs you", "Busy", "Old done", "Odd state"])
        odd = items[-1]
        self.assertEqual(odd["state"], "idle")
        self.assertIsNone(odd["url"])                        # only http(s) links are ever opened
        self.assertEqual(items[0]["detail"], "May it push?")

    def test_only_keeps_the_items_whose_fields_match(self):
        feed = {"items": [{"id": "1", "title": "Here", "here": True, "folder": "src/app"},
                          {"id": "2", "title": "Elsewhere", "here": False, "computer": "Laptop"},
                          {"id": "3", "title": "Says nothing"}]}
        self.assertEqual([i["id"] for i in sources.parse_feed(feed, {"here": True})], ["1"])
        self.assertEqual([i["id"] for i in sources.parse_feed(feed, {"computer": None})], ["1", "3"])
        self.assertEqual(len(sources.parse_feed(feed)), 3)
        self.assertEqual(sources.parse_feed(feed, {"here": True})[0]["folder"], "src/app")

    def test_anything_else_is_an_empty_feed(self):
        self.assertEqual(sources.parse_feed(None), [])
        self.assertEqual(sources.parse_feed({"items": "x"}), [])

    def test_caps_the_number_of_items(self):
        many = {"items": [{"title": f"t{i}", "updated": i} for i in range(50)]}
        self.assertEqual(len(sources.parse_feed(many)), sources.MAX_ITEMS)


class ItemCommand(unittest.TestCase):
    def test_the_command_is_yours_and_only_a_plain_id_goes_in(self):
        src = {"terminal": "my-tool open {id}"}
        self.assertEqual(sources.item_command(src, "job-1a2b3c4d"), "my-tool open job-1a2b3c4d")
        for bad in ("a b", "x;rm -rf ~", "$(id)", "'", "", "x" * 121):
            self.assertIsNone(sources.item_command(src, bad), bad)
        self.assertIsNone(sources.item_command({}, "job-1"))
        self.assertIsNone(sources.item_command(None, "job-1"))

    def test_the_link_goes_in_only_when_it_is_plain(self):
        src = {"terminal": "my-tool open {id} || sidelit open --app {url}"}
        self.assertEqual(sources.item_command(src, "j1", "https://jobs.example.test/s/j1"),
                         "my-tool open j1 || sidelit open --app https://jobs.example.test/s/j1")
        for bad in ("https://x.test/?a=1&b=2", "https://x.test/$(id)", "https://x.test/a b", "javascript:x", None):
            self.assertIsNone(sources.item_command(src, "j1", bad), bad)  # (its page opens instead)

    def test_a_command_that_would_break_out_of_its_quotes_is_not_taken(self):
        d = Path(tempfile.mkdtemp())
        write(d, "a.json", {"url": "https://example.test/feed", "terminal": "my-tool open {id}"})
        write(d, "b.json", {"url": "https://example.test/feed", "terminal": "echo 'hi' {id}"})
        got = {s["key"]: s for s in sources.load_sources(d)}
        self.assertEqual(got["a"]["terminal"], "my-tool open {id}")
        self.assertIsNone(got["b"]["terminal"])


class Wanting(unittest.TestCase):
    def item(self, i, state="working"):
        return {"id": i, "title": i, "state": state, "detail": "", "url": None, "updated": 0, "folder": None}

    def test_items_working_or_waiting_get_their_workspace_once(self):
        results = [{"key": "jobs", "workspaces": True,
                    "items": [self.item("a"), self.item("b", "waiting"), self.item("c", "done"), self.item("d")]},
                   {"key": "board", "workspaces": False, "items": [self.item("x")]}]
        got = sources.wanting_workspaces(results, bound={("jobs", "d")}, asked={("jobs", "b")})
        self.assertEqual([(k, i["id"]) for k, i in got], [("jobs", "a")])


class Polling(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        write(self.dir, "jobs.json", {"name": "Jobs", "url": "http://127.0.0.1:1/feed", "every": 10})
        self.now = 1000.0
        self.calls = 0
        self.fail = False

        def fetcher(src):
            self.calls += 1
            if self.fail:
                raise OSError("connection refused")
            return [{"id": "a", "title": "A", "state": "working", "detail": "", "url": None, "updated": 0}]
        self.poller = sources.Poller(self.dir, fetcher=fetcher, clock=lambda: self.now)

    def test_fetches_only_when_due(self):
        self.poller.poll()
        self.now += 5
        self.poller.poll()
        self.assertEqual(self.calls, 1)
        self.now += 6
        self.poller.poll()
        self.assertEqual(self.calls, 2)

    def test_a_failing_source_keeps_its_last_items_and_says_why(self):
        self.poller.poll()
        self.fail = True
        self.now += 11
        res = self.poller.poll()[0]
        self.assertEqual(res["items"][0]["title"], "A")
        self.assertIn("can't reach it", res["error"])

    def test_a_removed_config_disappears(self):
        self.poller.poll()
        (self.dir / "jobs.json").unlink()
        self.assertEqual(self.poller.poll(), [])


def tab(key, state=None, title="Fix tests", active=False, session=None, on="", item=None):
    """A tab as the daemon's view has it; state None: no Claude on it."""
    c = {"session": session or f"sess-{key}", "title": title, "state": state, "on": on, "model": "claude-x"} \
        if state else None
    return {"key": key, "active": active, "claude": c, "item": item, "last_title": ""}


class ClaudeItems(unittest.TestCase):
    def setUp(self):
        self.attention = Attention()

    def items(self, *tabs):
        return sources.claude_items(list(tabs), self.attention)

    def test_the_tab_colour_picks_the_state(self):
        got = self.items(tab("a", "busy"), tab("b", "checking"), tab("c", "shell"), tab("d", "external", on="CI pipeline"),
                         tab("e", "waiting"), tab("f", "done"), tab("g"))
        self.assertEqual([(i["id"], i["state"]) for i in got], [
            ("sess-a", "working"), ("sess-b", "working"), ("sess-c", "working"), ("sess-d", "working"),
            ("sess-e", "waiting"), ("sess-f", "done")])
        self.assertEqual(got[3]["detail"], "waiting on CI pipeline")   # the note, never the model
        self.assertEqual(got[2]["detail"], "shell still running")
        self.assertEqual(got[5]["detail"], "")
        self.assertNotIn("url", got[0])

    def test_a_claude_that_hands_back_while_you_are_elsewhere_waits_until_you_go_there(self):
        self.items(tab("a", "busy"))
        self.assertEqual(self.items(tab("a", "checking"))[0]["state"], "working")
        self.assertEqual(self.items(tab("a", "done"))[0]["state"], "waiting")   # the turn check: it needs you
        self.assertEqual(self.items(tab("a", "done"))[0]["state"], "waiting")
        self.assertEqual(self.items(tab("a", "done", active=True))[0]["state"], "done")
        self.assertEqual(self.items(tab("a", "done"))[0]["state"], "done")

    def test_one_that_ends_while_you_watch_or_only_waits_does_not(self):
        self.items(tab("a", "busy", active=True), tab("b", "busy"))
        got = self.items(tab("a", "done", active=True), tab("b", "external", on="prod deploy"))
        self.assertEqual([i["state"] for i in got], ["done", "working"])

    def test_tabs_opened_for_a_source_item_are_left_out(self):
        got = self.items(tab("a", "busy", item={"source": "jobs", "id": "j1"}), tab("b", "busy"))
        self.assertEqual([i["id"] for i in got], ["sess-b"])


class Receiver:
    """A fake source on 127.0.0.1: the feed and the report door, behind a token (None: open)."""

    def __init__(self, token="s3cret"):
        self.posts, self.token, self.status = [], token, 200
        recv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self):
                if recv.token is not None and self.headers.get("Authorization") != f"Bearer {recv.token}":
                    self._answer(401, {"error": "not allowed"})
                    return False
                return True

            def do_GET(self):
                if self._allowed():
                    self._answer(200, {"name": "Jobs", "items": [
                        {"id": "j1", "title": "Port the login screen", "state": "waiting", "detail": "Allow",
                         "url": "https://jobs.example.test/job/j1", "updated": 1791016000}]})

            def do_POST(self):
                if not self._allowed():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                recv.posts.append((self.headers.get("Content-Type"), body))
                self._answer(recv.status, {"ok": recv.status == 200, "items": len(body["items"])})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.recv = Receiver()
        self.addCleanup(self.recv.close)
        (self.dir / "token").write_text("s3cret\n")
        self.source = {"key": "jobs", "name": "Jobs", "url": self.recv.url + "/feed", "report": self.recv.url + "/report",
                       "token_file": str(self.dir / "token"), "every": 10}

    def test_the_feed_comes_with_the_token(self):
        items = sources.fetch(self.source)
        self.assertEqual([(i["id"], i["state"], i["detail"]) for i in items], [("j1", "waiting", "Allow")])

    def test_a_report_goes_out_as_json_with_the_token(self):
        items = [{"id": "s1", "title": "Fix tests", "state": "working", "detail": "", "updated": 1791016000}]
        sources.post(self.source, items)
        self.assertEqual(self.recv.posts, [("application/json", {"items": items})])

    def test_a_new_token_works_without_a_restart(self):
        self.recv.token = "rotated"
        with self.assertRaises(Exception):
            sources.post(self.source, [])
        (self.dir / "token").write_text("rotated")
        sources.post(self.source, [])
        self.assertEqual(len(self.recv.posts), 1)

    def test_a_refusal_is_an_error(self):
        self.recv.status = 500
        with self.assertRaises(Exception):
            sources.post(self.source, [])


class Reporting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.recv = Receiver(token=None)
        self.addCleanup(self.recv.close)
        write(self.dir, "jobs.json", {"name": "Jobs", "url": self.recv.url + "/feed",
                                      "report": self.recv.url + "/report", "every": 10})
        write(self.dir, "quiet.json", {"name": "Feed only", "url": self.recv.url + "/feed"})
        self.now = 1000.0
        self.logged = []
        # POSTs run on the caller's thread here, so each tick has sent before it returns
        self.r = sources.Reporter(self.dir, clock=lambda: self.now, spawn=lambda fn: fn(), log=self.logged.append)

    def sent(self):
        return [[(i["id"], i["state"]) for i in body["items"]] for _, body in self.recv.posts]

    def later(self, seconds):
        self.now += seconds
        self.r.tick()

    def test_reports_at_once_then_every_so_often(self):
        self.r.update([tab("a", "busy")])
        self.r.tick()
        self.assertEqual(self.sent(), [[("sess-a", "working")]])  # only the source that asks for it
        self.later(5)
        self.assertEqual(len(self.recv.posts), 1)
        self.later(5)
        self.assertEqual(len(self.recv.posts), 2)

    def test_a_change_goes_out_once_it_has_settled(self):
        self.r.update([tab("a", "busy")])
        self.r.tick()
        self.now += 1
        self.r.update([tab("a", "waiting")])
        self.later(1)
        self.assertEqual(len(self.recv.posts), 1)
        self.later(1)
        self.assertEqual(self.sent()[-1], [("sess-a", "waiting")])

    def test_updated_is_when_what_it_shows_last_changed(self):
        self.r.update([tab("a", "busy")])
        self.now += 30
        self.r.update([tab("a", "busy", title="Fix tests")])
        self.r.update([tab("a", "busy"), tab("b", "done")])
        self.assertEqual([i["updated"] for i in self.r.items], [1000, 1030])
        self.now += 5
        self.r.update([tab("a", "busy", title="Fix the flaky tests"), tab("b", "done")])
        self.assertEqual([i["updated"] for i in self.r.items], [1035, 1030])

    def test_a_dead_receiver_is_logged_once_and_tried_again_later(self):
        self.recv.status = 500
        self.r.update([tab("a", "busy")])
        self.r.tick()
        self.now += 1
        self.r.update([tab("a", "waiting")])
        self.later(3)
        self.assertEqual(len(self.recv.posts), 1)  # a change does not hammer it
        self.later(7)
        self.assertEqual(len(self.recv.posts), 2)
        self.assertEqual(len(self.logged), 1)
        self.assertIn("report to Jobs failed", self.logged[0])
        self.recv.status = 200
        self.later(10)
        self.assertEqual(self.sent()[-1], [("sess-a", "waiting")])
        self.assertIn("works again", self.logged[-1])

    def test_one_report_at_a_time(self):
        later = []
        self.r.spawn = later.append
        self.r.update([tab("a", "busy")])
        self.r.tick()
        self.later(20)
        self.assertEqual(len(later), 1)  # the first has not come back yet
        later[0]()
        self.later(1)
        self.assertEqual(len(later), 2)

    def test_an_unreachable_receiver_never_raises(self):
        write(self.dir, "jobs.json", {"name": "Jobs", "url": "http://127.0.0.1:9/feed", "report": "http://127.0.0.1:9/r"})
        self.r._loaded = float("-inf")
        self.r.update([tab("a", "busy")])
        self.r.tick()
        self.assertIn("report to Jobs failed", self.logged[0])


if __name__ == "__main__":
    unittest.main()
