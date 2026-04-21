#!/usr/bin/env python3
"""Tests for claude_print_cli — env stripping + cmd construction.

All tests mock os.execvpe; no real claude process is spawned.

Usage:
    python -m unittest tests.test_claude_print_cli -v
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sos_dev_tools import claude_print_cli


class TestStripEnv(unittest.TestCase):
    """stripped_env removes exactly the four load-bearing variables."""

    def test_strips_all_four(self):
        source = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "/x",
            "CLAUDE_CODE_EXECPATH": "/y",
            "ANTHROPIC_API_KEY": "sk-fake",
            "PATH": "/usr/bin",
            "HOME": "/Users/x",
        }
        result = claude_print_cli.stripped_env(source)
        self.assertNotIn("CLAUDECODE", result)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", result)
        self.assertNotIn("CLAUDE_CODE_EXECPATH", result)
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertEqual(result["PATH"], "/usr/bin")
        self.assertEqual(result["HOME"], "/Users/x")

    def test_no_vars_to_strip_is_identity(self):
        source = {"PATH": "/usr/bin", "HOME": "/Users/x"}
        result = claude_print_cli.stripped_env(source)
        self.assertEqual(result, source)

    def test_strip_list_is_stable(self):
        # Guard against accidental edits to the strip list — each name is
        # load-bearing and removing any one re-introduces the credit-balance bug.
        expected = {
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_EXECPATH",
            "ANTHROPIC_API_KEY",
        }
        self.assertEqual(set(claude_print_cli.STRIP_ENV), expected)


class TestBuildCmd(unittest.TestCase):
    """build_cmd constructs the claude argv correctly."""

    def test_minimal(self):
        cmd = claude_print_cli.build_cmd("hello", [])
        self.assertEqual(cmd, [
            "claude", "--dangerously-skip-permissions", "--print", "hello",
        ])

    def test_extra_args_before_prompt(self):
        cmd = claude_print_cli.build_cmd(
            "hello", ["--model", "claude-opus-4-7"])
        self.assertEqual(cmd, [
            "claude", "--dangerously-skip-permissions", "--print",
            "--model", "claude-opus-4-7", "hello",
        ])

    def test_leading_double_dash_stripped(self):
        # argparse passes `--` through as the first REMAINDER arg; we drop it
        # so the caller can cleanly separate their flags without claude seeing
        # the literal "--".
        cmd = claude_print_cli.build_cmd("hi", ["--", "--extra", "val"])
        self.assertEqual(cmd, [
            "claude", "--dangerously-skip-permissions", "--print",
            "--extra", "val", "hi",
        ])

    def test_no_default_args(self):
        cmd = claude_print_cli.build_cmd("hi", [], include_defaults=False)
        self.assertEqual(cmd, ["claude", "hi"])

    def test_prompt_with_spaces_stays_single_arg(self):
        cmd = claude_print_cli.build_cmd("prompt with spaces", [])
        self.assertEqual(cmd[-1], "prompt with spaces")


class TestMain(unittest.TestCase):
    """main() dispatches to os.execvpe with correct cmd + env."""

    @patch.object(claude_print_cli.os, "execvpe")
    def test_positional_prompt(self, mock_exec):
        with patch.object(claude_print_cli.sys, "argv", ["sos-claude-print", "do work"]):
            claude_print_cli.main()
        args = mock_exec.call_args[0]
        self.assertEqual(args[0], "claude")
        self.assertEqual(args[1][-1], "do work")
        env = args[2]
        for stripped in claude_print_cli.STRIP_ENV:
            self.assertNotIn(stripped, env)

    @patch.object(claude_print_cli.os, "execvpe")
    def test_file_prompt(self, mock_exec):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("do from file")
            path = f.name
        try:
            with patch.object(claude_print_cli.sys, "argv",
                              ["sos-claude-print", "--file", path]):
                claude_print_cli.main()
        finally:
            os.unlink(path)
        args = mock_exec.call_args[0]
        self.assertEqual(args[1][-1], "do from file")

    @patch.object(claude_print_cli.os, "execvpe")
    def test_file_not_found_exits_2(self, mock_exec):
        with patch.object(claude_print_cli.sys, "argv",
                          ["sos-claude-print", "--file", "/does/not/exist.md"]):
            with self.assertRaises(SystemExit) as cm:
                claude_print_cli.main()
            self.assertEqual(cm.exception.code, 2)
        mock_exec.assert_not_called()

    @patch.object(claude_print_cli.os, "execvpe", side_effect=FileNotFoundError())
    def test_claude_not_on_path_exits_127(self, mock_exec):
        with patch.object(claude_print_cli.sys, "argv", ["sos-claude-print", "hi"]):
            with self.assertRaises(SystemExit) as cm:
                claude_print_cli.main()
            self.assertEqual(cm.exception.code, 127)

    def test_missing_prompt_and_file_errors(self):
        with patch.object(claude_print_cli.sys, "argv", ["sos-claude-print"]):
            with self.assertRaises(SystemExit):
                claude_print_cli.main()

    @patch.object(claude_print_cli.os, "execvpe")
    def test_no_default_args_flag(self, mock_exec):
        with patch.object(claude_print_cli.sys, "argv",
                          ["sos-claude-print", "--no-default-args", "hi"]):
            claude_print_cli.main()
        cmd = mock_exec.call_args[0][1]
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--print", cmd)

    @patch.object(claude_print_cli.os, "execvpe")
    def test_extra_args_passthrough(self, mock_exec):
        with patch.object(claude_print_cli.sys, "argv", [
            "sos-claude-print", "hi", "--", "--model", "claude-opus-4-7",
        ]):
            claude_print_cli.main()
        cmd = mock_exec.call_args[0][1]
        self.assertIn("--model", cmd)
        self.assertIn("claude-opus-4-7", cmd)


class TestTmuxMode(unittest.TestCase):
    """--tmux flag dispatches to run_in_tmux instead of execvpe."""

    @patch.object(claude_print_cli.shutil, "which", return_value=None)
    def test_tmux_missing_exits_127(self, mock_which):
        with self.assertRaises(SystemExit) as cm:
            claude_print_cli.run_in_tmux("sess", ["claude", "--print", "hi"])
        self.assertEqual(cm.exception.code, 127)

    @patch.object(claude_print_cli.shutil, "which", return_value="/usr/bin/tmux")
    @patch.object(claude_print_cli.time, "sleep")  # skip real sleeps
    @patch.object(claude_print_cli.subprocess, "check_call")
    @patch.object(claude_print_cli.subprocess, "run")
    @patch.object(claude_print_cli.tempfile, "mktemp")
    def test_happy_path(self, mock_mktemp, mock_run, mock_check_call, mock_sleep, mock_which):
        # Simulate: session created, poll sees session alive, then dead.
        # Exit file contains "0".
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0")
            exit_path = f.name
        mock_mktemp.return_value = exit_path
        # has-session: first call returncode=0 (alive), second returncode=1 (dead)
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        try:
            result = claude_print_cli.run_in_tmux("my-session", ["claude", "--print", "hi"])
            self.assertEqual(result, 0)
            # Verify tmux new-session was invoked with the expected skeleton.
            new_session_cmd = mock_check_call.call_args[0][0]
            self.assertEqual(new_session_cmd[:3], ["tmux", "new-session", "-d"])
            self.assertIn("-s", new_session_cmd)
            self.assertIn("my-session", new_session_cmd)
            # Wrapper script should include the unset + the exit file.
            wrapper = new_session_cmd[-1]
            for v in claude_print_cli.STRIP_ENV:
                self.assertIn(v, wrapper)
            self.assertIn(exit_path, wrapper)
        finally:
            if os.path.exists(exit_path):
                os.unlink(exit_path)

    @patch.object(claude_print_cli.shutil, "which", return_value="/usr/bin/tmux")
    @patch.object(claude_print_cli.time, "sleep")
    @patch.object(claude_print_cli.subprocess, "check_call")
    @patch.object(claude_print_cli.subprocess, "run")
    @patch.object(claude_print_cli.tempfile, "mktemp")
    def test_propagates_nonzero_exit(self, mock_mktemp, mock_run, mock_check_call, mock_sleep, mock_which):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("42")
            exit_path = f.name
        mock_mktemp.return_value = exit_path
        mock_run.side_effect = [MagicMock(returncode=1)]  # session already dead on first check
        try:
            result = claude_print_cli.run_in_tmux("sess", ["claude", "hi"])
            self.assertEqual(result, 42)
        finally:
            if os.path.exists(exit_path):
                os.unlink(exit_path)

    @patch.object(claude_print_cli.shutil, "which", return_value="/usr/bin/tmux")
    @patch.object(claude_print_cli.time, "sleep")
    @patch.object(claude_print_cli.subprocess, "check_call")
    @patch.object(claude_print_cli.subprocess, "run")
    @patch.object(claude_print_cli.tempfile, "mktemp")
    def test_missing_exit_file_treated_as_failure(self, mock_mktemp, mock_run, mock_check_call, mock_sleep, mock_which):
        # Session dies without writing the exit file (e.g., killed from outside).
        mock_mktemp.return_value = "/tmp/definitely-does-not-exist-zzzz.exit"
        mock_run.side_effect = [MagicMock(returncode=1)]
        result = claude_print_cli.run_in_tmux("sess", ["claude", "hi"])
        self.assertEqual(result, 1)

    @patch.object(claude_print_cli.shutil, "which", return_value="/usr/bin/tmux")
    @patch.object(claude_print_cli.subprocess, "check_call",
                  side_effect=subprocess.CalledProcessError(1, "tmux"))
    def test_tmux_new_session_failure_exits_1(self, mock_check_call, mock_which):
        with self.assertRaises(SystemExit) as cm:
            claude_print_cli.run_in_tmux("sess", ["claude", "hi"])
        self.assertEqual(cm.exception.code, 1)

    @patch.object(claude_print_cli, "run_in_tmux", return_value=7)
    def test_main_dispatches_to_tmux_when_flag_set(self, mock_run_tmux):
        with patch.object(claude_print_cli.sys, "argv", [
            "sos-claude-print", "--tmux", "my-session", "hello",
        ]):
            with self.assertRaises(SystemExit) as cm:
                claude_print_cli.main()
            self.assertEqual(cm.exception.code, 7)
        mock_run_tmux.assert_called_once()
        session, cmd = mock_run_tmux.call_args[0]
        self.assertEqual(session, "my-session")
        self.assertEqual(cmd[-1], "hello")


if __name__ == "__main__":
    unittest.main()
