"""`sidelit new` and `sidelit terminal`: what each sends the daemon."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidelit import __main__ as cli  # noqa: E402
from sidelit import ipc  # noqa: E402


class Start(unittest.TestCase):
    def run_cli(self, *args):
        with mock.patch.object(ipc, "call", return_value="wN") as call, \
             mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_shell="/usr/bin/zsh")), \
             mock.patch.dict(os.environ, {"SIDELIT_CALLER_CWD": "/home/me/src"}), \
             mock.patch("builtins.print"):
            cli._start(args[0], list(args[1:]))
        return call.call_args.args[0], call.call_args.kwargs

    def test_options_before_the_command_are_options_after_a_workspace_too(self):
        method, params = self.run_cli("terminal", "w1", "--cwd", "app", "--no-focus", "--", "claude", "--x")
        self.assertEqual(method, "terminal")
        self.assertEqual(params, {"ws": "w1", "cwd": "/home/me/src/app", "focus": False,
                                  "command": "/usr/bin/zsh -i -c 'claude --x; exec /usr/bin/zsh -i'"})

    def test_a_workspace_by_number(self):
        self.assertEqual(self.run_cli("terminal", "3")[1], {"space": 3, "focus": True})

    def test_new_with_a_name_and_an_agent(self):
        method, params = self.run_cli("new", "--name", "fix", "--agent", "claude", "--cwd", "/abs")
        self.assertEqual((method, params), ("new", {"name": "fix", "agent": "claude", "cwd": "/abs",
                                                    "focus": True}))

    def test_the_command_keeps_its_quotes_through_the_shell(self):
        command = self.run_cli("new", "--", "claude \"$(cat '/b r.md')\"")[1]["command"]
        self.assertEqual(command, "/usr/bin/zsh -i -c 'claude \"$(cat '\"'\"'/b r.md'\"'\"')\"; exec /usr/bin/zsh -i'")


if __name__ == "__main__":
    unittest.main()
