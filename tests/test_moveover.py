"""Taking over from the old name, on a made-up home: what moves, what goes, what is swapped."""
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import chrome, hooks, moveover  # noqa: E402
from sidelit.moveover import OLD, MoveOver, Places  # noqa: E402

THEIRS = {"type": "command", "command": "notify-send done"}
MINE = {"type": "command", "command": "~/bin/log-session-end"}


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class FakeSystem:
    """systemctl and kill, recorded: the units stop on `stop`, a process goes on SIGTERM."""

    def __init__(self, proc: Path, active=("active", "inactive")):
        self.proc, self.active, self.calls, self.signals = proc, active, [], []

    def systemctl(self, *args):
        self.calls.append(args)
        if args[0] == "stop":
            self.active = ("inactive", "inactive")
        return "\n".join(self.active) + "\n" if args[0] == "is-active" else ""

    def kill(self, pid, sig):
        if not (self.proc / str(pid)).exists():
            raise ProcessLookupError(pid)
        if sig:
            self.signals.append((pid, sig))
            shutil.rmtree(self.proc / str(pid))


def compositor_started(proc: Path, seconds_ago: float):
    """A cosmic-comp in a fake /proc that started this long ago."""
    clk = os.sysconf("SC_CLK_TCK")
    write(proc / "uptime", "1000.00 0\n")
    write(proc / "900/comm", "cosmic-comp\n")
    write(proc / "900/stat", "900 (cosmic-comp) " + " ".join(["S"] + ["0"] * 18 + [str(int((1000 - seconds_ago) * clk))]))


class OldInstall(unittest.TestCase):
    """A home that ran the old name from a checkout, with Chrome and the hook set up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a COSMIC session, as its fake /proc has (whatever desktop runs the tests)
        env = mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1"})
        env.start()
        self.addCleanup(env.stop)
        root = Path(self.tmp.name)
        self.home = home = root / "home" / "alex"
        run = root / "run"
        self.places = Places(home=home, config_home=home / ".config", state_home=home / ".local/state",
                             data_home=home / ".local/share", cache_home=home / ".cache", runtime=run,
                             new_config=home / ".config/sidelit", new_state=home / ".local/state/sidelit",
                             new_runtime=run / "sidelit", system_autostart=root / "etc/xdg/autostart",
                             system_bin=root / "usr/bin", proc=root / "proc")
        old = home / ".config" / OLD
        write(old / "layout.json", json.dumps({"version": 2, "workspaces": [{"id": "w1", "slots": [
            {"id": "s1", "kind": "terminal", "state": {"command": "jobs open j1 || claude-workspace open --app x"}}]}]}))
        write(old / "config.json", json.dumps({"width": 300, "browser": "~/.local/bin/claude-workspace-open"}))
        write(old / "sources" / "jobs.json",
              json.dumps({"name": "Jobs", "terminal": "jobs open {id} || claude-workspace open --app {url}"}))
        write(old / "usage-cache.json", "{}")
        write(home / ".local/state" / OLD / "journal.jsonl", '{"t": 1, "kind": "calm"}\n')
        write(home / ".local/state" / OLD / "open.log", "old\n")
        write(home / ".local/state/sidelit/open.log", "new\n")      # sidelit open ran before the move
        write(run / OLD / "claude" / "s1.json", '{"session": "s1"}')
        write(run / OLD / "restored", "123:456\n")
        write(run / OLD / "daemon.sock")
        write(run / OLD / "session.env", 'WAYLAND_DISPLAY="wayland-1"\n')
        for unit in moveover.OLD_UNITS:
            write(home / ".config/systemd/user" / unit, "[Service]\n")
        write(home / ".config/autostart" / f"{OLD}.desktop", "[Desktop Entry]\n")
        write(home / ".local/share/applications" / f"{OLD}.desktop", "[Desktop Entry]\n")
        checkout = home / ".local/share" / OLD
        (checkout / ".git").mkdir(parents=True)
        (home / ".local/bin").mkdir(parents=True)
        for name in moveover.OLD_BINS:
            write(checkout / "bin" / name, "#!/bin/sh\n")
            (home / ".local/bin" / name).symlink_to(checkout / "bin" / name)
        old_hook = {"type": "command", "command": f"{home}/.local/bin/{OLD}-hook"}
        self.settings = write(home / ".claude/settings.json", json.dumps({"model": "opus", "hooks": {
            "Stop": [{"hooks": [THEIRS]}, {"hooks": [old_hook]}],
            "PreToolUse": [{"matcher": "*", "hooks": [{**old_hook, "async": True}]}],
            "SessionEnd": [{"hooks": [old_hook, MINE]}]}}))  # one of yours in the old hook's entry
        write(home / ".config/google-chrome/NativeMessagingHosts" / f"{moveover.OLD_CHROME_HOST}.json", "{}")
        write(home / ".zshrc", f"export BROWSER=~/.local/bin/{OLD}-open\n")
        write(root / "proc/4242/cmdline", "python3\0-m\0claude_workspace\0daemon\0")
        write(root / "proc/4343/cmdline", "python3\0-m\0sidelit\0daemon\0")
        write(root / "proc/self/cmdline", "x\0")
        self.sys = FakeSystem(root / "proc")
        self.claude_calls, self.claude_fails = [], False

        def claude(*args):  # Claude Code's plugin commands
            self.claude_calls.append(args)
            return (1, {"outcome": "failed", "message": "network down"}, "") if self.claude_fails \
                else (0, {"outcome": "ok"}, "")
        patches = [mock.patch.object(hooks, "SETTINGS", self.settings),
                   mock.patch.object(hooks, "run_claude", claude),
                   mock.patch.object(hooks, "hook_command", lambda: "/usr/bin/sidelit-hook"),
                   mock.patch.object(chrome, "host_command", lambda: "/usr/bin/sidelit-chrome-host"),
                   mock.patch.object(moveover, "opener_command", lambda: "/usr/bin/sidelit-open")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def move(self, **kw) -> MoveOver:
        m = MoveOver(self.places, systemctl=self.sys.systemctl, kill=self.sys.kill, **kw)
        self.ok = m.run()
        return m

    def test_finds_the_old_daemon_by_its_command_line(self):
        self.assertEqual(moveover.old_processes(self.places.proc), [4242])

    def test_takes_over_once(self):
        m = MoveOver(self.places, systemctl=self.sys.systemctl, kill=self.sys.kill)
        self.assertTrue(m.traces())
        self.assertIsNone(m.done_before())
        m.run()
        self.assertEqual(m.failed, [])
        self.assertIsNotNone(MoveOver(self.places).done_before())

    def test_the_layout_config_sources_and_journal_move(self):
        self.move()
        self.assertTrue(self.ok)
        new = self.places.new_config
        self.assertEqual(json.loads((new / "layout.json").read_text())["version"], 2)
        self.assertTrue((new / "usage-cache.json").exists())
        self.assertFalse((self.home / ".config" / OLD).exists())
        self.assertIn("calm", (self.places.new_state / "journal.jsonl").read_text())
        self.assertIn('"moved_over"', (self.places.new_state / "journal.jsonl").read_text())

    def test_the_old_command_in_your_files_becomes_ours(self):
        self.move()
        cfg = json.loads((self.places.new_config / "config.json").read_text())
        self.assertEqual(cfg["browser"], "~/.local/bin/sidelit-open")
        src = json.loads((self.places.new_config / "sources/jobs.json").read_text())
        self.assertEqual(src["terminal"], "jobs open {id} || sidelit open --app {url}")
        layout = json.loads((self.places.new_config / "layout.json").read_text())
        self.assertEqual(layout["workspaces"][0]["slots"][0]["state"]["command"], "jobs open j1 || sidelit open --app x")

    def test_nothing_sidelit_has_is_overwritten(self):
        m = self.move()
        self.assertEqual((self.places.new_state / "open.log").read_text(), "new\n")
        self.assertEqual((self.home / ".local/state" / OLD / "open.log").read_text(), "old\n")
        self.assertTrue(any("open.log" in line for line in m.left))

    def test_the_runtime_records_move_and_the_old_processes_leave_nothing(self):
        self.move()
        self.assertTrue((self.places.new_runtime / "claude/s1.json").exists())
        self.assertEqual((self.places.new_runtime / "restored").read_text(), "123:456\n")
        self.assertFalse((self.places.new_runtime / "session.env").exists())
        self.assertFalse((self.places.runtime / OLD).exists())

    def test_the_old_one_is_stopped_and_cannot_start_again(self):
        m = self.move()
        self.assertIn(("stop", *moveover.OLD_UNITS), self.sys.calls)
        self.assertIn(("daemon-reload",), self.sys.calls)
        self.assertEqual(self.sys.signals, [(4242, signal.SIGTERM)])
        for f in self.places.launchers + self.places.links:
            if f.name not in (moveover.OLD_HOOK, moveover.OLD_OPENER):
                self.assertFalse(f.exists() or f.is_symlink(), f)
        self.assertTrue(any("old checkout" in line for line in m.left))

    def test_links_still_open_from_what_names_the_old_opener(self):
        m = self.move()
        link = self.home / ".local/bin" / moveover.OLD_OPENER
        self.assertEqual(os.readlink(link), "/usr/bin/sidelit-open")
        self.assertTrue(any(line.startswith(f"~/.local/bin/{moveover.OLD_OPENER} now runs") for line in m.left), m.left)

    def test_an_old_start_racing_this_one_is_stopped_too(self):
        def late_start(*args):  # the old autostart entry started it again during the move
            if args[:1] == ("stop",) and not (self.places.proc / "4545").exists():
                write(self.places.proc / "4545/cmdline", "python3\0-m\0claude_workspace\0sidebar\0")
            return FakeSystem.systemctl(self.sys, *args)
        self.sys.systemctl = late_start
        self.move()
        self.assertEqual(sorted(self.sys.signals), [(4242, signal.SIGTERM), (4545, signal.SIGTERM)])

    def test_sessions_already_running_keep_reporting_until_the_next_login(self):
        self.move()
        link = self.home / ".local/bin" / moveover.OLD_HOOK
        self.assertEqual(os.readlink(link), "/usr/bin/sidelit-hook")
        record = {**MoveOver(self.places).done_before(), "at": time.time() - 600}  # moved ten minutes ago
        compositor_started(self.places.proc, seconds_ago=900)  # in the same login
        self.assertIsNone(MoveOver(self.places).end_bridge(record))
        self.assertTrue(link.is_symlink())
        compositor_started(self.places.proc, seconds_ago=60)  # a login since
        self.assertEqual(MoveOver(self.places).end_bridge(record), str(link))
        self.assertFalse(link.is_symlink())

    def test_an_old_folder_that_is_a_link_moves_as_the_link(self):
        dotfiles = Path(self.tmp.name) / "dotfiles/claude-workspace"
        shutil.move(str(self.home / ".config" / OLD), str(dotfiles))
        (self.home / ".config" / OLD).symlink_to(dotfiles)
        self.move()
        self.assertEqual(os.path.realpath(self.places.new_config), str(dotfiles))
        self.assertIn("sidelit open", (self.places.new_config / "sources/jobs.json").read_text())
        self.assertFalse((self.home / ".config" / OLD).is_symlink())

    def test_the_clone_that_runs_sidelit_now_is_not_called_old(self):
        write(self.home / ".local/share" / OLD / "bin/sidelit", "#!/bin/sh\n")  # pulled to the new code
        m = self.move()
        self.assertFalse(any("old checkout" in line for line in m.left))

    def test_the_hook_gives_way_to_the_plugin_and_yours_stay(self):
        before = self.settings.read_text()
        self.move()
        self.assertEqual([c[:2] for c in self.claude_calls], [("marketplace", "add"), ("install", "sidelit@sidelit")])
        data = json.loads(self.settings.read_text())
        self.assertEqual(data, {"model": "opus", "hooks": {"Stop": [{"hooks": [THEIRS]}],
                                                           "SessionEnd": [{"hooks": [MINE]}]}})
        self.assertEqual(self.settings.with_name(hooks.BACKUP).read_text(), before)

    def test_the_old_hook_stays_until_the_plugin_is_in(self):
        before = self.settings.read_text()
        self.claude_fails = True
        m = self.move()
        self.assertFalse(self.ok)  # tried again at the next start
        self.assertTrue(any("network down" in line for line in m.failed), m.failed)
        self.assertEqual(self.settings.read_text(), before)
        self.claude_fails = False
        self.move()
        self.assertTrue(self.ok)
        self.assertNotIn(OLD, self.settings.read_text())

    def test_a_settings_file_in_your_dotfiles_stays_a_link_and_private(self):
        real = write(Path(self.tmp.name) / "dotfiles/claude-settings.json", self.settings.read_text())
        real.chmod(0o600)
        self.settings.unlink()
        self.settings.symlink_to(real)
        self.move()
        self.assertTrue(self.ok)
        self.assertTrue(self.settings.is_symlink())
        self.assertNotIn(OLD, real.read_text())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_a_settings_file_in_a_shape_it_does_not_expect_keeps_its_shape(self):
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": 5}], "SessionEnd": None,
                                                       "PreToolUse": [{"hooks": [{"command": f"/x/{OLD}-hook"}]}]}}))
        m = MoveOver(self.places, systemctl=self.sys.systemctl, kill=self.sys.kill)
        self.assertTrue(m.traces())
        self.assertTrue(m.run(), m.failed)
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {"Stop": [{"hooks": 5}], "SessionEnd": None}})

    def test_a_settings_file_that_is_not_an_object_is_left_alone(self):
        self.settings.write_text("[]")
        m = self.move()
        self.assertEqual(self.settings.read_text(), "[]")
        self.assertTrue((self.places.new_config / "layout.json").exists())  # the rest went on

    def test_a_retry_still_says_what_the_failed_try_left_for_you(self):
        self.claude_fails = True
        first = self.move()
        self.assertFalse(self.ok)
        self.assertTrue(any("chrome://extensions" in line for line in first.left))
        self.claude_fails = False  # (Claude Code could be reached again)
        second = self.move()
        self.assertTrue(self.ok)
        record = MoveOver(self.places).done_before()
        for m in (second, record):
            left = m.left if isinstance(m, MoveOver) else m["left"]
            self.assertTrue(any("chrome://extensions" in line for line in left), left)
            self.assertTrue(any("old checkout" in line for line in left), left)
        self.assertTrue(any("removed" in line for line in record["did"]))
        self.assertFalse((self.places.new_config / moveover.PARTIAL).exists())

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only folder anyway (CI's container)")
    def test_a_settings_link_it_cannot_write_through_is_left_and_named(self):
        store = Path(self.tmp.name) / "nix/store"
        real = write(store / "settings.json", self.settings.read_text())
        self.settings.unlink()
        self.settings.symlink_to(real)
        store.chmod(0o555)
        try:
            m = self.move()
        finally:
            store.chmod(0o755)
        self.assertFalse(self.ok)
        self.assertTrue(any("cannot be written" in line for line in m.failed), m.failed)
        self.assertTrue(self.settings.is_symlink())

    def test_the_old_chrome_host_stays_until_ours_is_registered(self):
        old = self.home / ".config/google-chrome/NativeMessagingHosts" / f"{moveover.OLD_CHROME_HOST}.json"
        with mock.patch.object(chrome, "install_host", side_effect=OSError("read-only")):
            self.move()
        self.assertFalse(self.ok)
        self.assertTrue(old.exists())  # (so the next start tries again)

    def test_the_chrome_host_is_replaced(self):
        m = self.move()
        hosts = self.home / ".config/google-chrome/NativeMessagingHosts"
        self.assertFalse((hosts / f"{moveover.OLD_CHROME_HOST}.json").exists())
        manifest = json.loads((hosts / f"{chrome.HOST_NAME}.json").read_text())
        self.assertEqual(manifest["path"], "/usr/bin/sidelit-chrome-host")
        self.assertTrue(any("chrome://extensions" in line for line in m.left))

    def test_says_what_still_names_the_old_command(self):
        m = self.move()
        self.assertTrue(any(line.startswith("~/.zshrc") for line in m.left), m.left)

    def test_a_dry_run_changes_nothing(self):
        before = sorted(str(p) for p in Path(self.tmp.name).rglob("*"))
        m = self.move(dry=True)
        self.assertTrue(m.did)
        self.assertEqual(sorted(str(p) for p in Path(self.tmp.name).rglob("*")), before)
        self.assertEqual([c for c in self.sys.calls if c[0] != "is-active"], [])
        self.assertEqual(self.sys.signals, [])

    def test_a_test_instance_copies_the_folders_and_touches_nothing_else(self):
        settings = self.settings.read_text()
        self.move(test=True)
        self.assertTrue(self.ok)
        new = self.places.new_config
        self.assertTrue((new / "layout.json").exists())
        self.assertIn("sidelit open", (new / "sources/jobs.json").read_text())
        self.assertTrue((self.places.new_runtime / "claude/s1.json").exists())
        self.assertFalse((self.places.new_runtime / "daemon.sock").exists())
        # the old install as it was: running, started, hooked, its files untouched
        self.assertIn("claude-workspace open", (self.home / ".config" / OLD / "sources/jobs.json").read_text())
        self.assertTrue((self.places.runtime / OLD / "daemon.sock").exists())
        self.assertEqual(self.settings.read_text(), settings)
        self.assertTrue(all(link.is_symlink() for link in self.places.links))
        self.assertTrue(all(f.exists() for f in self.places.launchers))
        self.assertTrue(all(f.exists() for f in self.places.chrome_hosts if f.parent.parent.name == "google-chrome"))
        self.assertEqual((self.sys.calls, self.sys.signals), ([], []))

    def test_a_test_instance_never_writes_through_a_link_into_the_old_install(self):
        old = self.home / ".config" / OLD
        dotfiles = Path(self.tmp.name) / "dotfiles/sources"
        shutil.move(str(old / "sources"), str(dotfiles))
        (old / "sources").symlink_to(dotfiles)
        self.move(test=True)
        self.assertTrue(self.ok)
        self.assertIn("sidelit open", (self.places.new_config / "sources/jobs.json").read_text())
        self.assertFalse((self.places.new_config / "sources").is_symlink())
        self.assertIn("claude-workspace open", (dotfiles / "jobs.json").read_text())

    def test_an_old_package_is_hidden_and_named(self):
        write(self.places.system_autostart / f"{OLD}.desktop", "[Desktop Entry]\nExec=claude-workspace start\n")
        write(self.places.system_bin / OLD, "#!/bin/sh\n")
        m = self.move()
        hide = self.home / ".config/autostart" / f"{OLD}.desktop"
        self.assertIn("Hidden=true", hide.read_text())
        self.assertTrue(any("apt remove" in line for line in m.left))


class FreshMachine(unittest.TestCase):
    def test_nothing_to_take_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            places = Places(home=home, config_home=home / ".config", state_home=home / ".local/state",
                            data_home=home / ".local/share", cache_home=home / ".cache", runtime=home / "run",
                            new_config=home / ".config/sidelit", new_state=home / ".local/state/sidelit",
                            new_runtime=home / "run/sidelit", system_autostart=home / "etc",
                            system_bin=home / "bin", proc=home / "proc")
            with mock.patch.object(hooks, "SETTINGS", home / ".claude/settings.json"):
                self.assertEqual(MoveOver(places).traces(), [])


if __name__ == "__main__":
    unittest.main()
