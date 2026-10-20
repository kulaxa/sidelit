"""Which folder a terminal is in: what its title says (a prompt's, or the folder alone), else
what its shell's process says."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import procs  # noqa: E402
from sidelit.core import titles  # noqa: E402

HOME = os.path.expanduser("~")


class FromTheTitle(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="sidelit title ")  # (a space in it, too)
        self.addCleanup(os.rmdir, self.folder)

    def test_a_prompt(self):
        self.assertEqual(titles.cwd_from_prompt_title(f"alex@laptop: {self.folder}"), self.folder)
        self.assertEqual(titles.cwd_from_prompt_title("alex@laptop:~"), HOME)
        self.assertEqual(titles.cwd_from_prompt_title(f"alex@laptop: {HOME} (main)"), HOME)

    def test_the_folder_alone(self):
        """kitty's and Ghostty's shell integration, and fish, title a window with its folder."""
        self.assertEqual(titles.cwd_from_prompt_title(self.folder), self.folder)
        self.assertEqual(titles.cwd_from_prompt_title("~"), HOME)

    def test_anything_else_says_nothing(self):
        for title in ("kitty", "vim notes.txt", "/usr/bin/top", "~/no/such/folder", "", "✳ Fix tests"):
            with self.subTest(title=title):
                self.assertIsNone(titles.cwd_from_prompt_title(title))


@unittest.skipUnless(os.path.isdir("/proc/self/task"), "needs Linux's /proc")
class FromTheShell(unittest.TestCase):
    """A made-up terminal: a process that starts shells (or not) in folders, and waits."""

    PROGRAMS = {"sh": ["sh", "-c", "sleep 30; true"],  # (`; true`: sh does not become the sleep)
                "python": [sys.executable, "-c", "import time; time.sleep(30)"]}

    def terminal(self, *children: tuple[str, str]) -> int:
        """Its pid; `children`: (program, folder) each, started by it."""
        starts = "".join(f"subprocess.Popen({self.PROGRAMS[prog]!r}, cwd={folder!r}); " for prog, folder in children)
        code = f"import subprocess, sys, time; {starts}print('up', flush=True); time.sleep(30)"
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.addCleanup(lambda: (os.killpg(proc.pid, 9), proc.wait(), proc.stdout.close()))
        proc.stdout.readline()  # (said once each has started: Popen returns when it runs)
        return proc.pid

    @staticmethod
    def shell_cwd(pid: int) -> str:
        return procs.shell_cwd(pid, procs.proc_table())

    def folder(self) -> str:
        path = tempfile.mkdtemp(prefix="sidelit-shell-")
        self.addCleanup(os.rmdir, path)
        return path

    def test_its_shells_folder(self):
        here = self.folder()
        self.assertEqual(self.shell_cwd(self.terminal(("sh", here))), here)

    def test_tabs_in_one_folder(self):
        here = self.folder()
        self.assertEqual(self.shell_cwd(self.terminal(("sh", here), ("sh", here))), here)

    def test_tabs_in_different_folders_say_nothing(self):
        self.assertEqual(self.shell_cwd(self.terminal(("sh", self.folder()), ("sh", self.folder()))), "")

    def test_a_program_that_is_no_shell_says_nothing(self):
        self.assertEqual(self.shell_cwd(self.terminal(("python", self.folder()))), "")

    def test_nothing_running_says_nothing(self):
        self.assertEqual(self.shell_cwd(self.terminal()), "")
        self.assertEqual(self.shell_cwd(999999999), "")


if __name__ == "__main__":
    unittest.main()
