#!/usr/bin/env python3
"""Tests for claude_print_cli — env stripping + cmd construction.

All tests mock os.execvpe; no real claude process is spawned.

Usage:
    python -m unittest tests.test_claude_print_cli -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
