"""The one-line install's update of a copy (`get.sh pull`, also the sidebar's update button), between
made-up repositories at one address: a fast-forward as ever, and a copy following a repository
that began its history anew (as when Sidelit went public), unless it has something of its own."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class UpdateCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sidelit-get-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = self.tmp / "home"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.layout = home / ".config" / "sidelit" / "layout.json"
        self.layout.parent.mkdir(parents=True)
        self.layout.write_text('{"workspaces": ["kept"]}')
        # the old repository, with a release after the copy's and a branch of its own
        self.address = self.tmp / "sidelit.git"
        work = self.repo("old", {"VERSION": "0.5.0\n", "bin/sidelit": "#!/bin/sh\n", "README.md": "old\n"})
        self.git(work, "branch", "job/old-work")
        self.git(self.tmp, "clone", "--quiet", "--bare", str(work), str(self.address))
        self.copy = self.tmp / "home" / ".local" / "share" / "sidelit"
        self.git(self.tmp, "clone", "--quiet", str(self.address), str(self.copy))
        self.before = self.pulled = self.head(self.copy)

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def head(self, repo: Path, ref: str = "HEAD") -> str:
        return self.git(repo, "rev-parse", ref)

    def repo(self, name: str, files: dict) -> Path:
        work = self.tmp / name
        work.mkdir()
        self.git(work, "init", "--quiet", "-b", "main")
        self.commit(work, files)
        return work

    def commit(self, work: Path, files: dict):
        for path, text in files.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_text(text)
        self.git(work, "add", "-A")
        self.git(work, "commit", "--quiet", "-m", "a release")

    def begin_anew(self) -> str:
        """The address gets a new repository, one commit, no history shared with the old one
        (which goes to another address); returns its main."""
        self.address.rename(self.tmp / "sidelit-private.git")
        work = self.repo("new", {"VERSION": "0.6.0\n", "bin/sidelit": "#!/bin/sh\n", "README.md": "public\n"})
        self.git(self.tmp, "clone", "--quiet", "--bare", str(work), str(self.address))
        return self.head(work)

    def pull(self) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / "get.sh"), "pull", str(self.copy)], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_an_ordinary_update_is_a_fast_forward(self):
        work = self.tmp / "old"
        self.commit(work, {"VERSION": "0.5.1\n"})
        self.git(work, "push", "--quiet", str(self.address), "main")
        run = self.pull()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.head(self.copy), self.head(work))
        self.assertNotIn("anew", run.stdout)

    def test_a_copy_follows_a_repository_that_began_anew(self):
        new = self.begin_anew()
        run = self.pull()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("began its history anew", run.stdout)
        self.assertEqual(self.head(self.copy), new)
        self.assertEqual((self.copy / "VERSION").read_text(), "0.6.0\n")
        self.assertEqual(self.git(self.copy, "status", "--porcelain"), "")
        # still on main, following origin's main, and the old repository's branches are gone
        self.assertEqual(self.git(self.copy, "rev-parse", "--abbrev-ref", "@{upstream}"), "origin/main")
        self.assertEqual(self.git(self.copy, "branch", "-r", "--list", "origin/job/*"), "")
        self.assertEqual(self.layout.read_text(), '{"workspaces": ["kept"]}')
        # and from then on, ordinary updates again
        self.assertEqual(self.pull().returncode, 0)
        self.assertEqual(self.head(self.copy), new)

    def assert_left_as_it_is(self, run: subprocess.CompletedProcess):
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("has changes of its own", run.stderr)
        self.assertEqual(self.head(self.copy), self.before)
        self.assertEqual(self.head(self.copy, "origin/main"), self.pulled)

    def test_a_copy_with_a_file_changed_by_hand_is_left_as_it_is(self):
        self.begin_anew()
        (self.copy / "README.md").write_text("my notes\n")
        self.assert_left_as_it_is(self.pull())
        self.assertEqual((self.copy / "README.md").read_text(), "my notes\n")

    def test_a_new_file_counts_as_a_change(self):
        self.begin_anew()
        (self.copy / "notes.txt").write_text("mine\n")
        self.assert_left_as_it_is(self.pull())

    def test_once_tidied_up_the_next_try_follows(self):
        # (the refused try already fetched the new history: what was there before still counts)
        new = self.begin_anew()
        (self.copy / "notes.txt").write_text("mine\n")
        self.assert_left_as_it_is(self.pull())
        (self.copy / "notes.txt").unlink()
        run = self.pull()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.head(self.copy), new)

    def test_after_an_older_update_button_the_line_still_follows(self):
        # (a Sidelit from before this ran `git pull --ff-only`: its fetch moved origin/main onto
        # the new history before the pull refused; the copy still has nothing of its own)
        new = self.begin_anew()
        old_button = subprocess.run(["git", "pull", "--ff-only", "--quiet"], cwd=self.copy, env=self.env,
                                    capture_output=True, text=True)
        self.assertNotEqual(old_button.returncode, 0)
        self.assertEqual(self.head(self.copy, "origin/main"), new)
        run = self.pull()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.head(self.copy), new)

    def test_a_copy_with_a_commit_of_its_own_is_left_as_it_is(self):
        self.begin_anew()
        self.commit(self.copy, {"README.md": "my change\n"})
        self.before = self.head(self.copy)
        self.assert_left_as_it_is(self.pull())
        self.assert_left_as_it_is(self.pull())


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class LoginLine(unittest.TestCase):
    """Whether a compositor's config starts Sidelit, and by its path: a bare `sidelit` is found
    only where the compositor has ~/.local/bin on its PATH (Hyprland from greetd on Arch has not)."""

    def check(self, function: str, config: str) -> bool:
        folder = Path(tempfile.mkdtemp(prefix="sidelit-conf-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "config").write_text(config)
        # get.sh's functions, without its last line (which runs the install)
        script = 'eval "$(sed \'$d\' "$1")"; "$2" "$3"'
        return subprocess.run(["bash", "-c", script, "bash", str(ROOT / "get.sh"), function, str(folder)],
                              capture_output=True).returncode == 0

    def test_each_compositors_line(self):
        for config, starts, by_path in [
            ("exec-once = sidelit start\n", True, False),
            ("exec-once = ~/.local/bin/sidelit start\n", True, True),
            ('hl.on("hyprland.start", function () hl.exec_cmd("sidelit start") end)\n', True, False),
            ('hl.on("hyprland.start", function () hl.exec_cmd("~/.local/bin/sidelit start") end)\n', True, True),
            ('spawn-at-startup "sidelit" "start"\n', True, False),
            ('spawn-at-startup "/home/me/.local/bin/sidelit" "start"\n', True, True),
            ("exec sidelit start\n", True, False),
            ("exec $HOME/.local/bin/sidelit start\n", True, True),
            ("# exec-once = ~/.local/bin/sidelit start\n", False, False),
            ('-- hl.exec_cmd("~/.local/bin/sidelit start")\n', False, False),
            ("exec-once = waybar\n", False, False),
        ]:
            with self.subTest(config=config):
                self.assertEqual(self.check("starts_sidelit", config), starts)
                self.assertEqual(self.check("starts_sidelit_by_path", config), by_path)


if __name__ == "__main__":
    unittest.main()
