"""Chrome through the extension: which window is which, profiles, the native host."""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sidelit import chrome  # noqa: E402
from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core import engine as eng  # noqa: E402
from sidelit.core.engine import Engine  # noqa: E402
from sidelit.core.model import Layout, Win  # noqa: E402
from fake import Clock, FakeDesktop, run  # noqa: E402


def tab(url, title, active=False, pinned=False):
    return {"url": url, "title": title, "active": active, "pinned": pinned}


def ext_window(wid, tabs, focused=False, type_="normal"):
    return {"id": wid, "type": type_, "focused": focused, "tabs": tabs}


def top(wid, title, focused=False):
    return Win(id=wid, app="google-chrome", title=title + " - Google Chrome", space=1, output="eDP-1", focused=focused)


class Pairing(unittest.TestCase):
    def setUp(self):
        self.p = chrome.Pairer()

    def test_a_unique_title_pairs(self):
        reported = [("c1", ext_window(1, [tab("https://a", "Inbox", True), tab("https://b", "Docs")])),
                    ("c1", ext_window(2, [tab("https://c", "GitLab", True)]))]
        found = self.p.update([top("A", "GitLab"), top("B", "Inbox")], reported, 100.0)
        self.assertEqual(found["A"][1]["id"], 2)
        self.assertEqual(found["B"][1]["id"], 1)

    def test_windows_with_the_same_title_are_never_guessed(self):
        """Three windows on a "Example" tab: a guess recorded one window's tabs under another."""
        reported = [("c1", ext_window(i, [tab(f"https://x/{i}", "Example", True)])) for i in (1, 2, 3)]
        tops = [top(w, "Example") for w in "ABC"]
        for t in range(10):
            self.assertEqual(self.p.update(tops, reported, 100.0 + t), {})

    def test_a_focus_chrome_confirms_pairs_them(self):
        reported = [("c1", ext_window(1, [tab("https://x/1", "Example", True)])),
                    ("c1", ext_window(2, [tab("https://x/2", "Example", True)], focused=True))]
        tops = [top("A", "Example"), top("B", "Example", focused=True)]
        self.p.update(tops, reported, 100.0, {"c1": 99.0})
        # Chrome's focus flag is from before B got the keyboard: not trusted yet
        self.assertEqual(self.p.update(tops, reported, 101.0, {"c1": 100.5}), {})
        self.assertTrue(self.p.update(tops, reported, 103.0, {"c1": 100.5}) == {} and self.p.wants_report(103.0))
        found = self.p.update(tops, reported, 104.0, {"c1": 103.5})  # a fresh report, focus held
        self.assertEqual(found["B"][1]["id"], 2)
        # and A, the only "Example" left on both sides, pairs by elimination
        self.assertEqual(found["A"][1]["id"], 1)

    def test_a_window_the_extension_opened_pairs_with_the_one_that_appeared(self):
        old = ("c1", ext_window(1, [tab("https://x/1", "Example", True)]))
        self.p.update([top("A", "Example")], [old], 100.0)
        new = ("c1", ext_window(7, [tab("https://x/7", "Example", True), tab("", "")]))
        self.p.note_opened("c1", 7, 110.0)
        found = self.p.update([top("A", "Example"), top("N", "Example")], [old, new], 110.5)
        self.assertEqual(found["N"][1]["id"], 7)
        self.assertEqual(found["A"][1]["id"], 1)

    def test_a_pair_whose_titles_keep_disagreeing_is_dropped(self):
        reported = [("c1", ext_window(1, [tab("https://a", "Inbox", True)]))]
        self.p.update([top("A", "Inbox")], reported, 100.0)
        reported[0][1]["tabs"] = [tab("https://b", "Docs", True)]
        self.assertIn("A", self.p.update([top("A", "Inbox")], reported, 101.0))   # a title update in flight
        self.assertEqual(self.p.update([top("A", "Inbox")], reported, 107.0), {})

    def test_app_windows_are_left_to_the_web_app_adapter(self):
        reported = [("c1", ext_window(1, [tab("https://discord.com/app", "Discord", True)], type_="app"))]
        self.assertEqual(self.p.update([top("A", "Discord")], reported, 100.0), {})

class Tabs(unittest.TestCase):
    def test_tabs_worth_keeping(self):
        tabs = [tab("chrome://newtab/", "New Tab"), tab("https://x", "X", True), tab("about:blank", "")]
        self.assertEqual([t["url"] for t in chrome.clean_tabs(tabs)], ["https://x"])
        self.assertAlmostEqual(chrome.overlap([tab("https://a", ""), tab("https://b", "")],
                                              [tab("https://b", ""), tab("https://c", "")]), 1 / 3)


class Profiles(unittest.TestCase):
    def test_the_profile_is_found_by_what_the_extension_stored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for prof, inst in (("Default", "cAAAA"), ("Profile 1", "cBBBB")):
                store = root / "google-chrome" / prof / "Local Extension Settings" / chrome.EXTENSION_ID
                store.mkdir(parents=True)
                (store / "000003.log").write_bytes(b"\x00\x01instance\x07\"" + inst.encode() + b"\"\x00")
                (root / "google-chrome" / prof / "Preferences").write_text(
                    json.dumps({"session": {"restore_on_startup": 1 if prof == "Profile 1" else 5}}))
            self.assertEqual(chrome.find_profile("cBBBB", root), ("google-chrome", "Profile 1"))
            self.assertIsNone(chrome.find_profile("cZZZZ", root))
            self.assertEqual(chrome.restores_itself(root), ["google-chrome/Profile 1"])
            done = chrome.install_host("/usr/bin/sidelit-chrome-host", root)
            manifest = json.loads(done[0].read_text())
            self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{chrome.EXTENSION_ID}/"])
            self.assertEqual(chrome.uninstall_host(root), done)

    def test_the_manifest_key_gives_the_extension_id_we_register(self):
        import base64
        import hashlib
        manifest = json.loads(Path(ROOT, "chrome-extension", "manifest.json").read_text())
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in digest), chrome.EXTENSION_ID)


class NativeHost(unittest.TestCase):
    def test_relays_both_ways(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "SIDELIT_HOME": home}
            from sidelit import chromehost
            os.environ["SIDELIT_HOME"] = home
            try:
                path = chromehost.socket_path()
            finally:
                del os.environ["SIDELIT_HOME"]
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)
            got = []

            def daemon():
                conn, _ = srv.accept()
                f = conn.makefile("rb")
                got.append(json.loads(f.readline()))
                conn.sendall((json.dumps({"event": "chrome", "data": {"cmd": "open", "req": 1, "tabs": []}}) + "\n").encode())
                for line in f:
                    got.append(json.loads(line))
                    if got[-1]["params"].get("type") == "opened":
                        break
                conn.close()

            t = threading.Thread(target=daemon, daemon=True)
            t.start()
            proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "sidelit", "chromehost.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
            try:
                proc.stdin.write(chromehost.encode({"type": "hello", "instance": "c1"}))
                proc.stdin.flush()
                while True:  # (a fresh report is asked for on connecting)
                    size = struct.unpack("<I", proc.stdout.read(4))[0]
                    cmd = json.loads(proc.stdout.read(size))
                    if cmd["cmd"] != "snapshot":
                        break
                self.assertEqual(cmd["cmd"], "open")
                proc.stdin.write(chromehost.encode({"type": "opened", "req": 1, "window": 9}))
                proc.stdin.flush()
                t.join(5)
                self.assertEqual(got[0]["method"], "chrome")
                self.assertEqual([g["params"]["type"] for g in got], ["hello", "opened"])  # the hello once
                self.assertEqual(got[1]["params"]["window"], 9)
            finally:
                proc.stdin.close()
                self.assertEqual(proc.wait(5), 0)
                srv.close()


class ChromeDesk(unittest.TestCase):
    TABS = [tab("https://git/mr/1", "MR 1", True), tab("https://docs/x", "Docs", pinned=True)]

    def setUp(self):
        self.desk = FakeDesktop(spaces=3)
        self.clock = Clock()
        self.layout = Layout()
        self.ctx = ad.Ctx()
        self.log = []

    def engine(self):
        self.e = Engine(self.layout, self.desk, ctx=self.ctx, now=self.clock.t,
                        journal=lambda k, **f: self.log.append((k, f)))

    def advance(self, s):
        run(self.e, self.desk, self.clock, s)

    def chrome_window(self, space, tabs, ext_id=1):
        wid = self.desk.open("google-chrome", next(t["title"] for t in tabs if t["active"]) + " - Google Chrome", space)
        self.desk.browser[wid] = {"instance": "c1", "window": ext_id, "tabs": tabs, "profile": ["google-chrome", "Profile 1"]}
        return wid


class Restoring(ChromeDesk):

    def test_a_window_remembers_its_tabs_and_comes_back_with_them(self):
        self.chrome_window(2, self.TABS)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slot = self.layout.ws(self.e.owner[2]).slots[0]
        self.assertEqual([t["url"] for t in slot.state["tabs"]], ["https://git/mr/1", "https://docs/x"])
        self.assertEqual(slot.state["profile"], ["google-chrome", "Profile 1"])
        # next login: Chrome is not running and restores nothing itself
        self.desk.windows.clear()
        self.desk.browser.clear()
        self.layout = Layout.from_json(self.layout.to_json())
        self.engine()
        self.ctx.browser_online = set()
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(8)
        spec = next(l for l in self.desk.launched if l["type"] == "chrome_window")
        self.assertEqual(spec["tabs"], slot.state["tabs"])
        # the browser starts with a blank window first: that is not the restored one
        blank = self.desk.open("google-chrome", "New Tab - Google Chrome", 1)
        self.desk.browser[blank] = {"instance": "c1", "window": 5, "tabs": [tab("chrome://newtab/", "New Tab", True)]}
        self.advance(2)
        self.assertNotIn(blank, self.e.bind)
        # ...until the extension fills it with the tabs
        self.desk.browser[blank]["tabs"] = self.TABS
        self.desk.windows[blank]["title"] = "MR 1 - Google Chrome"
        self.advance(3)
        restored = self.layout.ws(self.e.owner[self.desk.index_of(self.layout.workspaces[0].ids[0])]).slots[0]
        self.assertEqual(self.e.bind.get(blank), restored.id)
        self.advance(20)
        self.assertEqual(self.desk.windows[blank]["space"], self.e.primary[self.layout.workspaces[0].id].index)

    def test_the_restore_moves_on_as_soon_as_a_window_is_back(self):
        """A window recognised by its tabs before its launch claimed it does not hold the
        next one up for the whole launch wait."""
        one = [tab("https://a/1", "One", True)]
        two = [tab("https://b/2", "Two", True)]
        self.chrome_window(2, one, ext_id=1)
        self.chrome_window(2, two, ext_id=2)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        self.desk.windows.clear()
        self.desk.browser.clear()
        self.ctx.browser_online = {"c1"}
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(6)
        self.assertEqual(len(self.desk.launched), 1)
        # the first window came back, but opened a moment before the launch looked for it
        w = self.chrome_window(2, [dict(t) for t in self.desk.launched[0]["tabs"]], ext_id=3)
        self.e.first_seen[w] = self.clock.t - 30
        self.advance(3)
        self.assertEqual(len(self.desk.launched), 2, "the second window waited for the first launch to time out")
        self.assertEqual(len(self.e.restore.planned_slots), 2)

    def test_tabs_that_have_not_loaded_do_not_shrink_what_is_remembered(self):
        """A window just reopened: its background tabs come with no URL until they load."""
        wid = self.chrome_window(2, self.TABS)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slot = self.layout.ws(self.e.owner[2]).slots[0]
        self.desk.browser[wid]["tabs"] = [self.TABS[0], {"url": "", "title": "", "active": False, "pinned": False}]
        self.advance(3)
        self.assertEqual(len(slot.state["tabs"]), 2)
        self.assertEqual(slot.state["tabs"][1]["url"], "https://docs/x")
        # once it has loaded, a real change is recorded
        self.desk.browser[wid]["tabs"] = [self.TABS[0]]
        self.advance(3)
        self.assertEqual([t["url"] for t in slot.state["tabs"]], ["https://git/mr/1"])

    def test_a_window_is_recognised_by_its_tabs_not_its_title(self):
        self.chrome_window(2, self.TABS)
        other = [tab("https://mail", "MR 1", True)]  # same title, different window
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slot = self.layout.ws(self.e.owner[2]).slots[0]
        adapter = ad.adapter_of(slot)
        win = Win(id="x", app="google-chrome", title="MR 1 - Google Chrome", space=2, output="eDP-1")
        self.ctx.browser = {"x": {"tabs": other}}
        self.assertEqual(adapter.score(win, slot, self.ctx), 0.0)
        self.ctx.browser = {"x": {"tabs": self.TABS}}
        self.assertEqual(adapter.score(win, slot, self.ctx), 3.0)


class Closing(ChromeDesk):
    """Whether you closed a Chrome window or it crashed, when the compositor cannot tell: the
    window did not have the keyboard (closed from the overview, or while the sidebar had it)."""

    def two_windows(self):
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        self.chrome_window(2, [tab("https://mail", "Inbox", True)], ext_id=1)
        w = self.chrome_window(2, self.TABS, ext_id=2)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        return w, self.e.bind[w]

    def reopened(self):
        return [l for l in self.desk.launched if l.get("type") == "chrome_window"]

    def test_a_window_chrome_closed_while_it_keeps_running_is_yours(self):
        w, sid = self.two_windows()
        self.desk.crash(w)  # (our last look saw it without the keyboard)
        self.advance(eng.DELETE_GRACE_S + 20)
        self.assertEqual(self.reopened(), [])
        self.assertIsNone(self.layout.find(sid)[1])
        self.assertIn("the browser closed it", next(f["why"] for k, f in self.log if k == "closed"))

    def test_the_report_may_come_a_moment_after_the_window_went(self):
        w, sid = self.two_windows()
        self.desk.reports = {"c1": {1, 2}}  # still the report from before the close
        self.desk.crash(w)
        self.advance(1)
        self.assertIsNotNone(self.layout.find(sid)[1])
        self.desk.reports = None
        self.advance(eng.DELETE_GRACE_S + 20)
        self.assertEqual(self.reopened(), [])
        self.assertIsNone(self.layout.find(sid)[1])

    def test_chrome_going_along_with_its_window_is_a_crash(self):
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        w = self.chrome_window(2, self.TABS)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        sid = self.e.bind[w]
        self.desk.reports = {"c1": {1}}  # a crashed Chrome reports nothing more
        self.desk.crash(w)
        self.advance(0.5)
        self.desk.extensions = set()  # its native host's pipe closes a moment later
        self.desk.reports = None
        self.ctx.browser_online = set()
        self.advance(eng.BROWSER_GONE_S + eng.RELAUNCH_AFTER_S + 10)
        self.assertEqual([l["slot"] for l in self.reopened()], [sid])
        self.assertIn("taken as a crash", next(f["why"] for k, f in self.log if k == "unbound"))

    def test_the_blank_window_chrome_starts_with_is_not_yours_while_one_is_reopened(self):
        """After a crash while you work, the extension fills the blank window Chrome opens as it
        starts with the first window's tabs. Taken as a window you opened, the tabs came twice:
        once there, and once more when the launch found no window of its own."""
        self.desk.open("terminator", "shell", 1)
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        w = self.chrome_window(2, self.TABS)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        sid = self.e.bind[w]
        self.desk.reports = {"c1": {1}}
        self.desk.crash(w)
        self.advance(0.5)
        self.desk.extensions = set()
        self.desk.reports = None
        self.ctx.browser_online = set()
        self.advance(eng.BROWSER_GONE_S + eng.RELAUNCH_AFTER_S + 6)
        self.assertEqual([l["slot"] for l in self.reopened()], [sid])
        # Chrome starts where you are, with a blank window
        blank = self.desk.open("google-chrome", "New Tab - Google Chrome", 1)
        self.desk.browser[blank] = {"instance": "c1", "window": 5, "tabs": [tab("chrome://newtab/", "New Tab", True)]}
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        self.advance(3)
        self.assertNotIn(blank, self.e.bind)
        # ...which the extension fills with the tabs
        self.desk.browser[blank]["tabs"] = self.TABS
        self.desk.windows[blank]["title"] = "MR 1 - Google Chrome"
        self.advance(3)
        self.assertEqual(self.e.bind.get(blank), sid)
        self.advance(eng.LAUNCH_WAIT_S + 60)
        self.assertEqual([l["slot"] for l in self.reopened()], [sid])
        self.assertEqual(self.desk.windows[blank]["space"], 2)

    def test_a_window_chrome_still_reports_is_a_crash(self):
        """The compositor lost the window but Chrome says it has it: nothing says you closed it."""
        w, sid = self.two_windows()
        self.desk.reports = {"c1": {1, 2}}
        self.desk.crash(w)
        self.advance(eng.RELAUNCH_AFTER_S + 10)
        self.assertEqual([l["slot"] for l in self.reopened()], [sid])

    def test_chromes_relaunch_button_brings_every_window_back_home(self):
        """Settings > Relaunch (2026-10-01): Chrome closes its windows one by one, each getting the
        keyboard as the one before goes, its extension drops, and Chrome reopens them all on the
        workspace you are on. They were taken as closed by you (and forgotten) or as new windows
        of that workspace, and the ones taken as crashed were started a second time."""
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        tabs = {i: [tab(f"https://site/{i}/a", f"Page {i}", True), tab(f"https://site/{i}/b", f"More {i}")]
                for i in (1, 2, 3)}
        old = {i: self.chrome_window(i, tabs[i], ext_id=i) for i in (1, 2, 3)}
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slots = {i: self.e.bind[old[i]] for i in (1, 2, 3)}
        homes = {i: self.layout.find(slots[i])[0].id for i in (1, 2, 3)}
        self.log.clear()
        self.desk.active = 1
        self.desk.close(old[3])  # the one you clicked Relaunch in
        self.advance(1)
        self.desk.close(old[2])  # the keyboard passed to it as the first went
        self.advance(0.5)
        self.desk.extensions = set()
        self.ctx.browser_online = set()
        self.desk.crash(old[1])
        self.advance(3)
        # Chrome is back: every window on the workspace you are on, tabs and all
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        new = {i: self.chrome_window(1, tabs[i], ext_id=10 + i) for i in (1, 2, 3)}
        self.advance(eng.BROWSER_GONE_S + eng.RELAUNCH_AFTER_S + 20)
        for i in (1, 2, 3):
            self.assertEqual(self.e.bind.get(new[i]), slots[i])
            self.assertEqual(self.desk.windows[new[i]]["space"], self.e.primary[homes[i]].index)
        self.assertEqual(self.reopened(), [])
        self.assertNotIn("slot_created", [k for k, _ in self.log])
        self.assertNotIn("slot_deleted", [k for k, _ in self.log])

    def quit_chrome(self, how, gaps=(2.0,)):
        """Close the windows one after another, `gaps` apart; with the last one Chrome quits and
        its extension drops. `how` is what Chrome then wrote down about that exit."""
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        self.ctx.browser_exit = lambda inst: how
        wins = [self.chrome_window(2, [tab(f"https://site/{i}", f"Page {i}", True)], ext_id=i)
                for i in range(len(gaps) + 1)]
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slots = [self.e.bind[w] for w in wins]
        for w, gap in zip(wins, gaps):
            self.desk.close(w)
            self.advance(gap)
        self.desk.close(wins[-1])
        self.desk.extensions = set()
        self.ctx.browser_online = set()
        self.advance(eng.BROWSER_GONE_S + eng.RELAUNCH_AFTER_S + eng.DELETE_GRACE_S + 30)
        return slots

    def test_closing_chromes_last_window_is_you_closing_it(self):
        """2026-10-03 19:33: two windows closed two seconds apart, the second the last, so Chrome
        quit. Taken as Chrome quitting on you, both came back."""
        slots = self.quit_chrome("normal")
        self.assertEqual(self.reopened(), [])
        self.assertEqual([self.layout.find(s)[1] for s in slots], [None, None])

    def test_chrome_quit_with_its_windows_open_keeps_them_but_does_not_reopen_them(self):
        slots = self.quit_chrome("normal", gaps=(0.0, 0.0))
        self.assertEqual(self.reopened(), [])
        self.assertTrue(all(self.layout.find(s)[1] is not None for s in slots))

    def test_windows_closed_fast_one_of_them_said_closed_by_chrome_are_yours(self):
        """2026-10-03 19:46: one window closed from the overview (Chrome said it closed it), the
        last half a second later, then COSMIC Settings. Within QUIT_GAP_S, the two were taken as
        Chrome quit with them open, and Settings, closed while Chrome's going disturbed the
        desktop, was started again."""
        self.desk.extensions = {"c1"}
        self.ctx.browser_online = {"c1"}
        self.ctx.browser_exit = lambda inst: "normal"
        wins = [self.chrome_window(2, [tab(f"https://site/{i}", f"Page {i}", True)], ext_id=i) for i in (1, 2)]
        settings = self.desk.open("com.system76.CosmicSettings", "Wi-Fi - COSMIC Settings", 2)
        self.engine()
        self.advance(eng.STARTUP_S + 3)
        slots = [self.e.bind[w] for w in wins + [settings]]
        self.desk.crash(wins[0])
        self.advance(0.5)
        self.desk.close(wins[1])
        self.desk.extensions = set()
        self.ctx.browser_online = set()
        self.advance(1.5)
        self.desk.close(settings)
        self.advance(eng.MASS_GRACE_S + 10)  # (three windows in two seconds: held in case it is a logout)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual([self.layout.find(s)[1] for s in slots], [None, None, None])

    def test_chrome_that_crashed_brings_its_windows_back(self):
        slots = self.quit_chrome("crashed", gaps=(0.0,))
        self.assertEqual({l["slot"] for l in self.reopened()}, set(slots))  # (again: the fake never opens them)

    def test_without_the_extension_the_keyboard_decides(self):
        w, sid = self.two_windows()
        self.desk.extensions = set()
        self.ctx.browser_online = set()
        self.desk.crash(w)
        self.advance(eng.BROWSER_GONE_S + eng.RELAUNCH_AFTER_S + 10)
        self.assertEqual([l["slot"] for l in self.reopened()], [sid])


if __name__ == "__main__":
    unittest.main()
