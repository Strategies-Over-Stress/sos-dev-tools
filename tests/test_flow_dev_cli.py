#!/usr/bin/env python3
"""Tests for flow_dev_cli — orchestration control flow.

All subagent launches, sos-inbox calls, git calls, and filesystem writes are
mocked. No claude subprocess, no tmux, no actual workflow.

Usage:
    python -m unittest tests.test_flow_dev_cli -v
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sos_dev_tools import flow_dev_cli as fd


def args_ns(**kw):
    n = argparse.Namespace()
    for k, v in kw.items():
        setattr(n, k, v)
    return n


class TestExtractPrNum(unittest.TestCase):
    def test_github_url(self):
        self.assertEqual(
            fd.extract_pr_num("https://github.com/x/y/pull/42"), "42")

    def test_trailing_suffix(self):
        self.assertEqual(
            fd.extract_pr_num("https://github.com/x/y/pull/123#issuecomment-9"),
            "123")

    def test_empty(self):
        self.assertEqual(fd.extract_pr_num(""), "")
        self.assertEqual(fd.extract_pr_num(None), "")

    def test_no_pull_in_url(self):
        self.assertEqual(fd.extract_pr_num("https://example.com/foo"), "")


class TestPromptTemplates(unittest.TestCase):
    """Every prompt must include the blocker contract with the ticket substituted."""

    def test_review_prompt_has_blocker(self):
        p = fd.review_prompt("FOO-1", "42")
        self.assertIn("Review PR #42", p)
        self.assertIn("FOO-1", p)
        self.assertIn("sos-inbox prompt", p)
        self.assertIn(".pm/review-result.json", p)
        # Blocker contract substitution
        self.assertIn("--ticket FOO-1", p)
        self.assertNotIn("<TICKET>", p)

    def test_work2_prompt_has_blocker(self):
        p = fd.work2_prompt("BAR-7", "99", 3)
        self.assertIn("PR #99 has 3 review comments", p)
        self.assertIn(".pm/work-2-result.json", p)
        self.assertIn("sos-inbox prompt", p)
        self.assertIn("--ticket BAR-7", p)

    def test_work3_prompt_includes_reason(self):
        p = fd.work3_prompt("FOO-1", "42", "breaks on Safari")
        self.assertIn("breaks on Safari", p)
        self.assertIn(".pm/work-3-result.json", p)

    def test_work3_prompt_handles_empty_reason(self):
        p = fd.work3_prompt("FOO-1", "42", "")
        self.assertIn("sos-inbox replies", p)
        self.assertIn("FOO-1", p)


class TestQaCardActions(unittest.TestCase):
    def test_with_preview_and_pr(self):
        actions = fd.qa_card_actions("FOO-1", "https://x/pull/42",
                                     "http://localhost:6006")
        labels = [a["label"] for a in actions]
        self.assertIn("Open preview", labels)
        self.assertIn("Open PR", labels)
        self.assertIn("Approve & merge", labels)
        self.assertIn("Request changes", labels)
        # Approve button injects the right command
        approve = next(a for a in actions if a["label"] == "Approve & merge")
        self.assertEqual(approve["kind"], "inject")
        self.assertIn("sos-flow-dev qa-approve FOO-1", approve["text"])
        self.assertTrue(approve["execute"])
        # Reject is non-executing so user can append a reason
        reject = next(a for a in actions if a["label"] == "Request changes")
        self.assertFalse(reject["execute"])

    def test_without_preview(self):
        actions = fd.qa_card_actions("FOO-1", "https://x/pull/42", "")
        labels = [a["label"] for a in actions]
        self.assertNotIn("Open preview", labels)
        self.assertIn("Open PR", labels)

    def test_without_pr_or_preview(self):
        actions = fd.qa_card_actions("FOO-1", "", "")
        labels = [a["label"] for a in actions]
        self.assertNotIn("Open preview", labels)
        self.assertNotIn("Open PR", labels)
        self.assertIn("Approve & merge", labels)


class SessionBase(unittest.TestCase):
    """Tests that touch session state use a tmpdir-scoped STATE_DIR."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_state = fd.STATE_DIR
        fd.STATE_DIR = Path(self._tmp)

    def tearDown(self):
        fd.STATE_DIR = self._orig_state
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestSessionState(SessionBase):
    def test_set_get_roundtrip(self):
        fd.session_set("FOO-1", phase="review", pr_url="https://x/pull/42")
        self.assertEqual(fd.session_get("FOO-1", "phase"), "review")
        self.assertEqual(fd.session_get("FOO-1", "pr_url"), "https://x/pull/42")
        # ticket is automatically set on init
        self.assertEqual(fd.session_get("FOO-1", "ticket"), "FOO-1")

    def test_set_merges_not_replaces(self):
        fd.session_set("FOO-1", phase="review", pr_url="https://x/pull/42")
        fd.session_set("FOO-1", phase="work-2")
        self.assertEqual(fd.session_get("FOO-1", "phase"), "work-2")
        self.assertEqual(fd.session_get("FOO-1", "pr_url"), "https://x/pull/42")

    def test_rm(self):
        fd.session_set("FOO-1", phase="review")
        fd.session_rm("FOO-1")
        self.assertIsNone(fd.session_get("FOO-1"))

    def test_get_missing(self):
        self.assertIsNone(fd.session_get("NOPE-999"))


class TestPostCardSilentOnMissing(unittest.TestCase):
    """post_card must no-op silently when sos-inbox itself is missing."""

    @patch.object(fd.subprocess, "run", side_effect=FileNotFoundError("sos-inbox"))
    def test_no_inbox_binary_doesnt_crash(self, mock_run):
        result = fd.post_card("info", "title", ticket="FOO-1")
        self.assertEqual(result, "")

    @patch.object(fd.subprocess, "run",
                  side_effect=subprocess.CalledProcessError(1, "sos-inbox"))
    def test_inbox_error_doesnt_crash(self, mock_run):
        result = fd.post_card("info", "title", ticket="FOO-1")
        self.assertEqual(result, "")


class TestCmdStartHappyPath(SessionBase):
    """start command — approve verdict skips work-2 and goes straight to QA."""

    def _stub_pm_complete(self, ticket):
        """Write a /tmp/pm-complete-TICKET.json so read_pm_complete works."""
        f = Path(f"/tmp/pm-complete-{ticket}.json")
        f.write_text(json.dumps({
            "ticket": ticket,
            "pr_url": f"https://github.com/owner/repo/pull/42",
            "preview_url": "http://localhost:3142",
            "worktree": "repo",
        }))
        self.addCleanup(lambda: f.unlink() if f.exists() else None)

    def test_approve_verdict_skips_work2(self):
        self._stub_pm_complete("FOO-1")

        # Mock every side-effect
        with patch.object(fd, "phase_pm_start") as pm, \
             patch.object(fd, "phase_review") as review, \
             patch.object(fd, "phase_work2") as work2, \
             patch.object(fd, "post_card", return_value="card_abc") as post, \
             patch.object(fd, "worktree_root",
                          return_value=Path(self._tmp)):
            pm.return_value = {
                "pr_url": "https://github.com/owner/repo/pull/42",
                "preview_url": "http://localhost:3142",
                "worktree": "repo",
            }
            review.return_value = {"comments": 0, "verdict": "approve"}
            fd.cmd_start(args_ns(ticket="FOO-1", pause_after=None))

            pm.assert_called_once_with("FOO-1")
            review.assert_called_once()
            work2.assert_not_called()  # verdict=approve → skip

        # Session ended at awaiting-qa
        self.assertEqual(fd.session_get("FOO-1", "phase"), "awaiting-qa")
        self.assertEqual(fd.session_get("FOO-1", "review_verdict"), "approve")

        # Three cards posted: PR opened, Review posted, Ready for QA
        self.assertEqual(post.call_count, 3)
        kinds = [c.args[0] for c in post.call_args_list]
        titles = [c.args[1] for c in post.call_args_list]
        self.assertEqual(kinds, ["info", "info", "action"])
        self.assertEqual(titles[-1], "Ready for QA")

    def test_changes_requested_runs_work2(self):
        self._stub_pm_complete("FOO-1")

        with patch.object(fd, "phase_pm_start") as pm, \
             patch.object(fd, "phase_review") as review, \
             patch.object(fd, "phase_work2") as work2, \
             patch.object(fd, "post_card", return_value="card_abc"), \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            pm.return_value = {
                "pr_url": "https://github.com/owner/repo/pull/42",
                "preview_url": "",
                "worktree": "repo",
            }
            review.return_value = {"comments": 4, "verdict": "changes-requested"}
            work2.return_value = {"ready": True, "url": "http://localhost:6006"}

            fd.cmd_start(args_ns(ticket="FOO-1", pause_after=None))
            work2.assert_called_once_with("FOO-1", "42", 4)

        # preview_url in session was updated from work-2's output
        self.assertEqual(fd.session_get("FOO-1", "preview_url"),
                         "http://localhost:6006")
        self.assertEqual(fd.session_get("FOO-1", "phase"), "awaiting-qa")

    def test_work2_failure_exits_and_posts_action_card(self):
        self._stub_pm_complete("FOO-1")

        with patch.object(fd, "phase_pm_start") as pm, \
             patch.object(fd, "phase_review") as review, \
             patch.object(fd, "phase_work2") as work2, \
             patch.object(fd, "post_card", return_value="card_abc") as post, \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            pm.return_value = {"pr_url": "https://github.com/x/y/pull/42",
                               "preview_url": "", "worktree": "y"}
            review.return_value = {"comments": 1, "verdict": "changes-requested"}
            work2.return_value = {"failed": "tests red after fix"}

            with self.assertRaises(SystemExit) as cm:
                fd.cmd_start(args_ns(ticket="FOO-1", pause_after=None))
            self.assertNotEqual(cm.exception.code, 0)

            # An action card was posted announcing the failure
            titles = [c.args[1] for c in post.call_args_list]
            self.assertTrue(any("Work 2 failed" in t for t in titles))


class TestCmdStartGating(SessionBase):
    """--pause-after inserts a gate prompt and respects abort."""

    def _stub_pm_complete(self, ticket):
        f = Path(f"/tmp/pm-complete-{ticket}.json")
        f.write_text(json.dumps({"pr_url": "https://x/pull/1", "preview_url": "",
                                  "worktree": "x"}))
        self.addCleanup(lambda: f.unlink() if f.exists() else None)

    def test_pause_after_work1_gates(self):
        self._stub_pm_complete("FOO-1")
        with patch.object(fd, "phase_pm_start",
                          return_value={"pr_url": "https://x/pull/1",
                                         "preview_url": "", "worktree": "x"}), \
             patch.object(fd, "phase_review",
                          return_value={"comments": 0, "verdict": "approve"}), \
             patch.object(fd, "post_card", return_value="card_abc"), \
             patch.object(fd, "prompt_user", return_value="continue") as gate_prompt, \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            fd.cmd_start(args_ns(ticket="FOO-1", pause_after="work1"))
            gate_prompt.assert_called_once()
            # Gate question includes the phase name
            self.assertIn("Work 1 done", gate_prompt.call_args[0][0])

    def test_pause_after_work1_abort_exits(self):
        self._stub_pm_complete("FOO-1")
        with patch.object(fd, "phase_pm_start",
                          return_value={"pr_url": "https://x/pull/1",
                                         "preview_url": "", "worktree": "x"}), \
             patch.object(fd, "phase_review") as review, \
             patch.object(fd, "post_card", return_value="card_abc"), \
             patch.object(fd, "prompt_user", return_value="abort"), \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            with self.assertRaises(SystemExit):
                fd.cmd_start(args_ns(ticket="FOO-1", pause_after="work1"))
            review.assert_not_called()  # abort short-circuits


class TestCmdStatus(SessionBase):
    def test_empty(self):
        with patch("builtins.print") as mp:
            fd.cmd_status(args_ns(ticket=None))
        mp.assert_called_with("(no active flow-dev sessions)")

    def test_single_ticket_prints_json(self):
        fd.session_set("FOO-1", phase="awaiting-qa", pr_url="https://x")
        with patch("builtins.print") as mp:
            fd.cmd_status(args_ns(ticket="FOO-1"))
        out = mp.call_args[0][0]
        parsed = json.loads(out)
        self.assertEqual(parsed["phase"], "awaiting-qa")

    def test_missing_ticket_fails(self):
        with self.assertRaises(SystemExit):
            fd.cmd_status(args_ns(ticket="NOPE"))

    def test_list_all(self):
        fd.session_set("FOO-1", phase="awaiting-qa",
                       pr_url="https://x/1", preview_url="")
        fd.session_set("BAR-2", phase="review", pr_url="https://x/2",
                       preview_url="http://localhost:3001")
        with patch("builtins.print") as mp:
            fd.cmd_status(args_ns(ticket=None))
        lines = [c.args[0] for c in mp.call_args_list]
        self.assertTrue(any("FOO-1" in l for l in lines))
        self.assertTrue(any("BAR-2" in l for l in lines))


class TestCmdCleanup(SessionBase):
    def test_rm_session_and_tmp(self):
        fd.session_set("FOO-1", phase="merged", worktree="/does/not/exist")
        marker = Path("/tmp/pm-complete-FOO-1.json")
        marker.write_text("{}")
        self.addCleanup(lambda: marker.unlink() if marker.exists() else None)

        with patch.object(fd.subprocess, "run") as mock_run:
            fd.cmd_cleanup(args_ns(ticket="FOO-1"))
            # git worktree remove was attempted
            mock_run.assert_called_with(
                ["git", "worktree", "remove", "--force", "/does/not/exist"],
                check=False)

        self.assertIsNone(fd.session_get("FOO-1"))
        self.assertFalse(marker.exists())

    def test_no_session_no_worktree_still_succeeds(self):
        with patch.object(fd.subprocess, "run") as mock_run:
            # Should not raise
            fd.cmd_cleanup(args_ns(ticket="NOPE-999"))
            mock_run.assert_not_called()


class TestPmStartMissing(SessionBase):
    def test_missing_skill_exits(self):
        with patch.object(fd, "PM_START_SKILL", Path("/does/not/exist")):
            with self.assertRaises(SystemExit):
                fd.phase_pm_start("FOO-1")


if __name__ == "__main__":
    unittest.main()
