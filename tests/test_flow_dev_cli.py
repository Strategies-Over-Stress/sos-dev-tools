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

    def test_review_prompt_rereview_has_three_scope_parts(self):
        """Re-review covers: (1) verify prior comments resolved,
        (2) catch regressions from work-N, (3) catch blockers the first
        review missed. The third part was initially excluded under a
        stricter 'prior-comments-only' rule but that suppressed real
        bugs — FX-2 case had 7 legitimate findings that all pre-existed
        but were missed in round 1."""
        p = fd.review_prompt("FOO-1", "42", is_rereview=True)
        self.assertIn("RE-REVIEW", p)
        # Three explicit scope parts
        self.assertIn("Verify prior comments are resolved", p)
        self.assertIn("Catch regressions", p)
        self.assertIn("Catch BLOCKERS the first review missed", p)
        # Anti-churn lever is blocker-vs-preference, NOT prior-only
        self.assertIn("blocker", p.lower())
        self.assertIn("preference", p.lower())
        # Standard blocker contract still threaded in
        self.assertIn("--ticket FOO-1", p)

    def test_review_prompt_rereview_anti_over_engineering_guard(self):
        """Re-review must explicitly filter out over-engineering style
        findings (extract helpers, 'consider using X', speculative
        improvements) — the same guard as first-pass, not a separate
        narrower scope."""
        p = fd.review_prompt("FOO-1", "42", is_rereview=True)
        self.assertIn("do not post", p.lower())
        self.assertIn("extract", p.lower())
        self.assertIn("consider using", p.lower())
        self.assertIn("speculative", p.lower())
        self.assertIn("followups.md", p)

    def test_review_prompt_default_is_full_review(self):
        """Default (is_rereview=False) gets the full quality-sweep
        checklist, not the narrow re-review scope."""
        p = fd.review_prompt("FOO-1", "42")  # default
        self.assertNotIn("RE-REVIEW", p)
        # New prompt uses "Logic bugs" under Correctness category
        self.assertIn("Logic bugs", p)
        # Security is now under its own heading
        self.assertIn("Security", p)

    def test_review_prompt_full_review_has_anti_over_engineering_guard(self):
        """First-pass is the only exhaustive review — but it must explicitly
        warn against AI over-engineering (flagging hypothetical improvements
        as if they were correctness blockers). Re-review can't compensate for
        a review that turned every diff into a 40-comment refactor wishlist."""
        p = fd.review_prompt("FOO-1", "42")
        self.assertIn("Anti-over-engineering", p)
        self.assertIn("Do NOT post", p)
        # Specific examples of what NOT to comment on
        self.assertIn("preference", p.lower())
        self.assertIn("followups.md", p)

    def test_review_prompt_rereview_skips_full_checklist(self):
        """Re-review must NOT include the full-sweep checklist — that
        language is the source of scope expansion."""
        p = fd.review_prompt("FOO-1", "42", is_rereview=True)
        # The full-review checklist header is absent on re-review
        self.assertNotIn("Security concerns", p)
        self.assertNotIn("Dead code, leftover debug", p)

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
        # Don't let one test's port reservations leak into the next.
        fd._RUNTIME_CLAIMED_PORTS.clear()

    def tearDown(self):
        fd.STATE_DIR = self._orig_state
        fd._RUNTIME_CLAIMED_PORTS.clear()
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


class TestPostCardErrorHandling(unittest.TestCase):
    """post_card: crash-proof, but surface real errors to stderr."""

    @patch.object(fd.subprocess, "run", side_effect=FileNotFoundError("sos-inbox"))
    def test_missing_binary_warns_and_continues(self, mock_run):
        with patch.object(fd.sys, "stderr") as mock_err:
            result = fd.post_card("info", "title", ticket="FOO-1")
        self.assertEqual(result, "")
        # Warning written to stderr
        written = "".join(
            (c.args[0] if c.args else "") for c in mock_err.write.call_args_list)
        self.assertIn("sos-inbox not on PATH", written)

    @patch.object(fd.subprocess, "run")
    def test_http_failure_surfaces_stderr_message(self, mock_run):
        err = subprocess.CalledProcessError(1, ["sos-inbox", "info"])
        err.stderr = "HTTP 400 — malformed actions JSON"
        mock_run.side_effect = err
        with patch.object(fd.sys, "stderr") as mock_err:
            result = fd.post_card("info", "title", ticket="FOO-1")
        self.assertEqual(result, "")
        written = "".join(
            (c.args[0] if c.args else "") for c in mock_err.write.call_args_list)
        # Both the exit code and the stderr detail surfaced
        self.assertIn("failed", written)
        self.assertIn("HTTP 400", written)


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

    def _stub_alloc(self, ticket, worktree_path, parent="main"):
        """Patch phase_worktree_alloc so cmd_start doesn't actually alloc."""
        patcher = patch.object(fd, "phase_worktree_alloc", return_value={
            "worktree": str(worktree_path),
            "parent_branch": parent,
            "action": "reused",
            "reason": "test stub",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # Also patch os.chdir so we don't actually change CWD mid-test.
        chdir_patch = patch.object(fd.os, "chdir")
        chdir_patch.start()
        self.addCleanup(chdir_patch.stop)

    def test_approve_verdict_skips_work2(self):
        self._stub_pm_complete("FOO-1")
        self._stub_alloc("FOO-1", Path(self._tmp))

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
            fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after=None, base=None, detach=False, watch=False))

            pm.assert_called_once_with("FOO-1", iteration="first-pass")
            review.assert_called_once()
            work2.assert_not_called()

        self.assertEqual(fd.session_get("FOO-1", "phase"), "awaiting-qa")
        self.assertEqual(fd.session_get("FOO-1", "review_verdict"), "approve")
        # Phase 0 result persisted
        self.assertEqual(fd.session_get("FOO-1", "worktree"), str(Path(self._tmp)))
        self.assertEqual(fd.session_get("FOO-1", "parent_branch"), "main")

        self.assertEqual(post.call_count, 3)
        kinds = [c.args[0] for c in post.call_args_list]
        titles = [c.args[1] for c in post.call_args_list]
        self.assertEqual(kinds, ["info", "info", "action"])
        self.assertEqual(titles[-1], "Ready for QA")

    def test_changes_requested_runs_work2_then_rereviews(self):
        """After work-2, flow-dev runs the reviewer AGAIN to confirm fixes.
        First review returns changes-requested (triggering work-2); second
        review returns approve (confirming work-2 closed the issues) so
        flow advances to QA."""
        self._stub_pm_complete("FOO-1")
        self._stub_alloc("FOO-1", Path(self._tmp))

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
            # First review → changes-requested; re-review after work-2 → approve
            review.side_effect = [
                {"comments": 4, "verdict": "changes-requested"},
                {"comments": 0, "verdict": "approve"},
            ]
            work2.return_value = {"ready": True, "url": "http://localhost:6006"}

            fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after=None, base=None, detach=False, watch=False))
            work2.assert_called_once_with("FOO-1", "42", 4)
            # Two reviews: initial + re-review
            self.assertEqual(review.call_count, 2)

        self.assertEqual(fd.session_get("FOO-1", "preview_url"),
                         "http://localhost:6006")
        self.assertEqual(fd.session_get("FOO-1", "phase"), "awaiting-qa")
        # After re-review approved, verdict is 'approve'
        self.assertEqual(fd.session_get("FOO-1", "review_verdict"), "approve")

    def test_work2_failure_exits_and_posts_action_card(self):
        self._stub_pm_complete("FOO-1")
        self._stub_alloc("FOO-1", Path(self._tmp))

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
                fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after=None, base=None, detach=False, watch=False))
            self.assertNotEqual(cm.exception.code, 0)

            titles = [c.args[1] for c in post.call_args_list]
            self.assertTrue(any("Work 2 failed" in t for t in titles))

    def test_base_flag_passed_to_allocator(self):
        self._stub_pm_complete("FOO-1")
        with patch.object(fd, "phase_worktree_alloc",
                          return_value={"worktree": str(Path(self._tmp)),
                                         "parent_branch": "sbook/epic",
                                         "action": "reused",
                                         "reason": ""}) as alloc, \
             patch.object(fd.os, "chdir"), \
             patch.object(fd, "phase_pm_start",
                          return_value={"pr_url": "https://x/pull/1",
                                         "preview_url": "", "worktree": "x"}), \
             patch.object(fd, "phase_review",
                          return_value={"comments": 0, "verdict": "approve"}), \
             patch.object(fd, "post_card", return_value="id"), \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after=None,
                                 base="sbook/epic", detach=False, watch=False))
            alloc.assert_called_once_with("FOO-1", hint_base="sbook/epic")
        self.assertEqual(fd.session_get("FOO-1", "parent_branch"), "sbook/epic")


class TestCmdStartGating(SessionBase):
    """--pause-after inserts a gate prompt and respects abort."""

    def _stub_pm_complete(self, ticket):
        f = Path(f"/tmp/pm-complete-{ticket}.json")
        f.write_text(json.dumps({"pr_url": "https://x/pull/1", "preview_url": "",
                                  "worktree": "x"}))
        self.addCleanup(lambda: f.unlink() if f.exists() else None)

    def test_pause_after_work1_gates(self):
        self._stub_pm_complete("FOO-1")
        with patch.object(fd, "phase_worktree_alloc",
                          return_value={"worktree": str(Path(self._tmp)),
                                         "parent_branch": "main",
                                         "action": "reused", "reason": ""}), \
             patch.object(fd.os, "chdir"), \
             patch.object(fd, "phase_pm_start",
                          return_value={"pr_url": "https://x/pull/1",
                                         "preview_url": "", "worktree": "x"}), \
             patch.object(fd, "phase_review",
                          return_value={"comments": 0, "verdict": "approve"}), \
             patch.object(fd, "post_card", return_value="card_abc"), \
             patch.object(fd, "prompt_user", return_value="continue") as gate_prompt, \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after="work1", base=None, detach=False, watch=False))
            gate_prompt.assert_called_once()
            self.assertIn("Work 1 done", gate_prompt.call_args[0][0])

    def test_pause_after_work1_abort_exits(self):
        self._stub_pm_complete("FOO-1")
        with patch.object(fd, "phase_worktree_alloc",
                          return_value={"worktree": str(Path(self._tmp)),
                                         "parent_branch": "main",
                                         "action": "reused", "reason": ""}), \
             patch.object(fd.os, "chdir"), \
             patch.object(fd, "phase_pm_start",
                          return_value={"pr_url": "https://x/pull/1",
                                         "preview_url": "", "worktree": "x"}), \
             patch.object(fd, "phase_review") as review, \
             patch.object(fd, "post_card", return_value="card_abc"), \
             patch.object(fd, "prompt_user", return_value="abort"), \
             patch.object(fd, "worktree_root", return_value=Path(self._tmp)):
            with self.assertRaises(SystemExit):
                fd.cmd_start(args_ns(tickets=["FOO-1"], pause_after="work1", base=None, detach=False, watch=False))
            review.assert_not_called()


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
    """cleanup default resets the worktree for reuse; --remove destroys it."""

    def _make_fake_worktree(self, name="wt-1"):
        wt = Path(self._tmp) / name
        (wt / ".pm").mkdir(parents=True)
        # These files should all be dropped by the reset path.
        (wt / ".pm" / "active-ticket.json").write_text('{"status":"merged"}')
        (wt / ".pm" / "work-summary.md").write_text("summary")
        (wt / ".pm" / "review-result.json").write_text("{}")
        (wt / ".pm" / "work-2-result.json").write_text("{}")
        (wt / ".pm" / "failed.json").write_text("{}")
        # This should survive (config carries the port assignment).
        (wt / ".pm" / "config.json").write_text('{"preview":{"port":3001}}')
        return wt

    def test_default_resets_worktree_and_preserves_config(self):
        wt = self._make_fake_worktree("wt-1")
        fd.session_set("FOO-1", phase="merged", worktree=str(wt),
                       parent_branch="main")
        marker = Path("/tmp/pm-complete-FOO-1.json")
        marker.write_text("{}")
        self.addCleanup(lambda: marker.unlink() if marker.exists() else None)

        with patch.object(fd.subprocess, "run") as mock_run:
            # Simulate git commands succeeding
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fd.cmd_cleanup(args_ns(ticket="FOO-1", remove=False))

            # Verify the git reset sequence was attempted
            git_cmds = [c.args[0] for c in mock_run.call_args_list
                        if len(c.args) and len(c.args[0]) >= 2 and c.args[0][0] == "git"]
            # At minimum: fetch, switch, reset, clean
            kinds = [(c[1] if c[1] != "-C" else c[3]) for c in git_cmds]
            self.assertIn("fetch", kinds)
            self.assertIn("switch", kinds)
            self.assertIn("reset", kinds)
            self.assertIn("clean", kinds)

        # Ticket-scoped pm files were dropped
        self.assertFalse((wt / ".pm" / "active-ticket.json").exists())
        self.assertFalse((wt / ".pm" / "work-summary.md").exists())
        self.assertFalse((wt / ".pm" / "review-result.json").exists())
        self.assertFalse((wt / ".pm" / "failed.json").exists())
        # Config preserved
        self.assertTrue((wt / ".pm" / "config.json").exists())
        # Session + marker gone
        self.assertIsNone(fd.session_get("FOO-1"))
        self.assertFalse(marker.exists())

    def test_remove_flag_destroys_worktree(self):
        wt = self._make_fake_worktree("wt-1")
        fd.session_set("FOO-1", phase="merged", worktree=str(wt),
                       parent_branch="main")

        with patch.object(fd.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fd.cmd_cleanup(args_ns(ticket="FOO-1", remove=True))

            # --remove goes through git worktree remove --force
            calls = [c.args[0] for c in mock_run.call_args_list]
            self.assertIn(
                ["git", "worktree", "remove", "--force", str(wt)],
                calls,
            )
            # No reset-for-reuse git commands
            for c in calls:
                self.assertNotIn("reset", c)

    def test_no_session_no_worktree_still_succeeds(self):
        with patch.object(fd.subprocess, "run") as mock_run:
            fd.cmd_cleanup(args_ns(ticket="NOPE-999", remove=False))
            mock_run.assert_not_called()


class TestFanout(SessionBase):
    """Multi-ticket start → one detached tmux runner per ticket."""

    def test_multiple_tickets_spawn_runners(self):
        with patch.object(fd.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(fd.subprocess, "run") as mock_run:
            # has-session checks return 1 (session doesn't exist yet)
            # new-session calls return 0 (success)
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session FOO-1
                MagicMock(returncode=0, stderr=""),  # new-session FOO-1
                MagicMock(returncode=1),  # has-session FOO-2
                MagicMock(returncode=0, stderr=""),  # new-session FOO-2
                MagicMock(returncode=1),  # has-session FOO-3
                MagicMock(returncode=0, stderr=""),  # new-session FOO-3
            ]
            with patch("builtins.print") as mp:
                fd.cmd_start(args_ns(
                    tickets=["FOO-1", "FOO-2", "FOO-3"],
                    pause_after=None, base=None, detach=False, watch=False,
                ))
            # Three new-session calls expected
            new_session_calls = [
                c.args[0] for c in mock_run.call_args_list
                if c.args[0][0] == "tmux" and c.args[0][1] == "new-session"
            ]
            self.assertEqual(len(new_session_calls), 3)
            # Session names are predictable
            printed = "\n".join(str(c.args[0]) if c.args else "" for c in mp.call_args_list)
            for t in ["FOO-1", "FOO-2", "FOO-3"]:
                self.assertIn(f"flow-runner-{t}", printed)

    def test_single_ticket_with_detach_also_fans_out(self):
        with patch.object(fd.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(fd.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session
                MagicMock(returncode=0, stderr=""),  # new-session
            ]
            with patch("builtins.print"):
                fd.cmd_start(args_ns(
                    tickets=["FOO-1"], pause_after=None, base=None, detach=True, watch=False,
                ))
            new_session = [c.args[0] for c in mock_run.call_args_list
                           if c.args[0][0] == "tmux" and c.args[0][1] == "new-session"]
            self.assertEqual(len(new_session), 1)
            self.assertIn("flow-runner-FOO-1", new_session[0])

    def test_existing_session_skipped_with_warning(self):
        with patch.object(fd.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(fd.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # has-session FOO-1 → already exists
                MagicMock(returncode=1),  # has-session FOO-2 → free
                MagicMock(returncode=0, stderr=""),  # new-session FOO-2
            ]
            with patch("builtins.print"):
                fd.cmd_start(args_ns(
                    tickets=["FOO-1", "FOO-2"],
                    pause_after=None, base=None, detach=False, watch=False,
                ))
            # new-session called exactly once (only for FOO-2)
            new_session = [c.args[0] for c in mock_run.call_args_list
                           if c.args[0][0] == "tmux" and c.args[0][1] == "new-session"]
            self.assertEqual(len(new_session), 1)
            self.assertIn("flow-runner-FOO-2", new_session[0])

    def test_no_tmux_fails(self):
        with patch.object(fd.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                fd.cmd_start(args_ns(
                    tickets=["FOO-1", "FOO-2"],
                    pause_after=None, base=None, detach=False, watch=False,
                ))

    def test_single_ticket_without_detach_blocks_not_fanout(self):
        """Sanity: single ticket, no --detach → still goes through blocking path."""
        # We mock phase_worktree_alloc etc. to stubs so the path runs without
        # actually invoking tmux; the key is that _fanout was NOT called.
        with patch.object(fd, "_fanout") as fanout, \
             patch.object(fd, "_run_start_blocking") as blocking:
            fd.cmd_start(args_ns(
                tickets=["FOO-1"], pause_after=None, base=None, detach=False, watch=False,
            ))
            fanout.assert_not_called()
            blocking.assert_called_once()


class TestPhaseWorktreeAlloc(SessionBase):
    """phase_worktree_alloc runs the subagent and reads the result file."""

    def _result_file(self, ticket):
        return Path(f"/tmp/flow-{ticket}-worktree.json")

    def test_happy_path_returns_alloc_dict(self):
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({
            "worktree": self._tmp,
            "parent_branch": "main",
            "action": "created",
            "reason": "no pool yet",
        }))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0) as mock_run:
            out = fd.phase_worktree_alloc("FOO-1")
            mock_run.assert_called_once()
            self.assertEqual(out["worktree"], self._tmp)
            self.assertEqual(out["parent_branch"], "main")
            self.assertEqual(out["action"], "created")

    def test_hint_base_reaches_prompt(self):
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({
            "worktree": self._tmp, "parent_branch": "sbook/epic",
            "action": "reused", "reason": "",
        }))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0) as mock_run:
            fd.phase_worktree_alloc("FOO-1", hint_base="sbook/epic")
            prompt = mock_run.call_args[0][1]
            self.assertIn("sbook/epic", prompt)
            self.assertIn("--base", prompt)

    def test_missing_result_file_exits(self):
        with patch.object(fd, "run_subagent", return_value=0):
            with self.assertRaises(SystemExit):
                fd.phase_worktree_alloc("NOTA-999")

    def test_subagent_reports_failed(self):
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({"failed": "CWD is not a git repo"}))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0):
            with self.assertRaises(SystemExit):
                fd.phase_worktree_alloc("FOO-1")

    def test_invalid_worktree_path_exits(self):
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({
            "worktree": "/does/not/exist",
            "parent_branch": "main", "action": "created", "reason": "",
        }))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0):
            with self.assertRaises(SystemExit):
                fd.phase_worktree_alloc("FOO-1")

    def test_subagent_nonzero_exit_fails(self):
        with patch.object(fd, "run_subagent", return_value=1):
            with self.assertRaises(SystemExit):
                fd.phase_worktree_alloc("FOO-1")

    def test_writes_claim_stub_to_allocated_worktree(self):
        # Simulate a real allocated worktree dir (under tmpdir).
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({
            "worktree": str(wt),
            "parent_branch": "main",
            "action": "created",
            "reason": "fresh pool",
        }))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0):
            fd.phase_worktree_alloc("FOO-1")

        claim = wt / ".pm" / "active-ticket.json"
        self.assertTrue(claim.exists())
        data = json.loads(claim.read_text())
        self.assertEqual(data["ticket_id"], "FOO-1")
        self.assertEqual(data["status"], "claimed")
        self.assertIn("claimed_at", data)

    def test_creates_lock_file(self):
        """Lock file is created at STATE_DIR/alloc.lock so concurrent callers serialize."""
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        result = self._result_file("FOO-1")
        result.write_text(json.dumps({
            "worktree": str(wt), "parent_branch": "main",
            "action": "created", "reason": "",
        }))
        self.addCleanup(lambda: result.unlink() if result.exists() else None)

        with patch.object(fd, "run_subagent", return_value=0):
            fd.phase_worktree_alloc("FOO-1")

        lock_file = Path(self._tmp) / "alloc.lock"
        self.assertTrue(lock_file.exists())


class TestPmStartMissing(SessionBase):
    def test_missing_skill_exits(self):
        with patch.object(fd, "PM_START_SKILL", Path("/does/not/exist")):
            with self.assertRaises(SystemExit):
                fd.phase_pm_start("FOO-1")


class TestPreviewCommand(SessionBase):
    """cmd_preview starts/stops/lists detached preview tmux sessions."""

    def test_resolve_tickets_positional(self):
        args = args_ns(tickets=["FOO-1", "BAR-7"], all=False)
        self.assertEqual(fd._resolve_preview_tickets(args), ["FOO-1", "BAR-7"])

    def test_resolve_tickets_all_reads_session_files(self):
        fd.session_set("FOO-1", phase="awaiting-qa", worktree=str(Path(self._tmp)))
        fd.session_set("BAR-2", phase="merged", worktree=str(Path(self._tmp)))
        fd.session_set("NOWT-3", phase="alloc")  # no worktree → skipped
        args = args_ns(tickets=[], all=True)
        got = fd._resolve_preview_tickets(args)
        self.assertIn("FOO-1", got)
        self.assertIn("BAR-2", got)
        self.assertNotIn("NOWT-3", got)

    def test_preview_config_reads_worktree_json(self):
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "npm run storybook", "cwd": "packages/ui",
                        "port": 6006}
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "npm run storybook")

    def test_preview_config_missing_returns_none(self):
        # No worktree config, no source repo fallback → None
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=None):
            self.assertIsNone(fd._preview_config_for_ticket("FOO-1"))

    def test_preview_config_falls_back_to_source_repo(self):
        # Worktree has NO preview config; source repo DOES. Fallback wins.
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({
            "jira": {"project_key": "X"}  # no preview block
        }))
        source = Path(self._tmp) / "source"
        (source / ".pm").mkdir(parents=True)
        (source / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "npm run storybook"}
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=source):
            cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "npm run storybook")

    def test_preview_config_worktree_wins_over_source(self):
        # When the worktree DOES have preview config, use it, don't fall back.
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "worktree-wins"}
        }))
        source = Path(self._tmp) / "source"
        (source / ".pm").mkdir(parents=True)
        (source / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "source-loses"}
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=source):
            cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "worktree-wins")

    def test_preview_config_all_null_template_treated_as_missing(self):
        # The all-null template that config.json ships with shouldn't count.
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": None, "cwd": None, "services": None}
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=None):
            self.assertIsNone(fd._preview_config_for_ticket("FOO-1"))

    def test_agent_suggested_preview_fallback(self):
        """When no .pm/config.json preview block exists in worktree OR source,
        fall back to .pm/preview-suggested.json written by the dev agent."""
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        # Empty worktree config (no preview), no source config.
        (wt / ".pm" / "config.json").write_text(json.dumps({}))
        (wt / ".pm" / "preview-suggested.json").write_text(json.dumps({
            "command": "npm run dev -w apps/web",
            "cwd": "."
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=None):
            cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "npm run dev -w apps/web")

    def test_operator_config_beats_agent_suggestion(self):
        """Regression: if the worktree has an operator-defined preview block,
        agent-suggested is ignored. Config explicit > agent inference."""
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "operator-wrote-this", "cwd": "."}
        }))
        (wt / ".pm" / "preview-suggested.json").write_text(json.dumps({
            "command": "agent-guessed-this", "cwd": "."
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=None):
            cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "operator-wrote-this")

    def test_source_repo_config_beats_agent_suggestion(self):
        """Source-repo defaults also override agent suggestion."""
        src = Path(self._tmp) / "src"
        (src / ".pm").mkdir(parents=True)
        (src / ".pm" / "config.json").write_text(json.dumps({
            "preview": {"command": "source-default", "cwd": "."}
        }))
        wt = Path(self._tmp) / "wt-1"
        (wt / ".pm").mkdir(parents=True)
        (wt / ".pm" / "config.json").write_text(json.dumps({}))
        (wt / ".pm" / "preview-suggested.json").write_text(json.dumps({
            "command": "agent-guessed", "cwd": "."
        }))
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_source_repo_for_worktree", return_value=src):
            cfg = fd._preview_config_for_ticket("FOO-1")
        self.assertEqual(cfg["command"], "source-default")

    def test_normalize_legacy_config(self):
        # Port in config is IGNORED — runner assigns at runtime.
        # Routes default to a single "Open preview" → / when omitted.
        cfg = {"command": "npm run storybook", "cwd": "packages/ui", "port": 6006}
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(svcs, [{
            "name": "default", "command": "npm run storybook",
            "cwd": "packages/ui", "port": None,
            "routes": [{"label": "Open preview", "path": "/"}],
        }])

    def test_normalize_routes_explicit(self):
        cfg = {
            "command": "npm run dev",
            "cwd": "apps/web",
            "routes": [
                {"label": "Home", "path": "/"},
                {"label": "FX Gallery", "path": "/fx"},
            ],
        }
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(svcs[0]["routes"], [
            {"label": "Home", "path": "/"},
            {"label": "FX Gallery", "path": "/fx"},
        ])

    def test_normalize_routes_adds_leading_slash(self):
        cfg = {
            "command": "npm run dev",
            "routes": [{"label": "Gallery", "path": "fx"}],
        }
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(svcs[0]["routes"], [
            {"label": "Gallery", "path": "/fx"},
        ])

    def test_normalize_routes_drop_empty_label(self):
        cfg = {
            "command": "npm run dev",
            "routes": [
                {"label": "", "path": "/a"},
                {"label": "B", "path": "/b"},
            ],
        }
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(svcs[0]["routes"], [{"label": "B", "path": "/b"}])

    def test_normalize_routes_all_invalid_fallback_to_default(self):
        cfg = {
            "command": "npm run dev",
            "routes": [{"not_a_route": True}, {"label": "", "path": "/x"}],
        }
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(svcs[0]["routes"],
                         [{"label": "Open preview", "path": "/"}])

    def test_normalize_multi_service_config(self):
        cfg = {"services": [
            {"name": "app", "command": "npm run dev", "cwd": "apps/web", "port": 3000},
            {"name": "Storybook", "command": "npm run storybook",
             "cwd": "packages/ui", "port": 6006},
        ]}
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(len(svcs), 2)
        self.assertEqual(svcs[0]["name"], "app")
        self.assertEqual(svcs[1]["name"], "storybook")  # lower-cased
        # port ignored from config — always None
        self.assertIsNone(svcs[0]["port"])
        self.assertIsNone(svcs[1]["port"])

    def test_normalize_drops_services_without_command(self):
        cfg = {"services": [
            {"name": "bad"},  # no command
            {"name": "ok", "command": "npm run dev"},
        ]}
        svcs = fd._normalize_preview_config(cfg)
        self.assertEqual(len(svcs), 1)
        self.assertEqual(svcs[0]["name"], "ok")

    def test_normalize_empty(self):
        self.assertEqual(fd._normalize_preview_config(None), [])
        self.assertEqual(fd._normalize_preview_config({}), [])

    def test_start_preview_no_worktree_errors(self):
        fd.session_set("FOO-1", phase="awaiting-qa")
        results = fd._start_preview_for(
            "FOO-1", [{"name": "default", "command": "npm run dev",
                       "cwd": "", "port": 6006}], wait=False)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["session"])
        self.assertIn("no worktree", results[0]["error"])

    def test_start_preview_port_collision_falls_back(self):
        """Configured port already bound → runner picks next free and warns."""
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        fd.session_set("FOO-1", worktree=str(wt))

        # First call returns True (port 6006 bound), subsequent calls False.
        reachable_calls = [True, False]
        def fake_reachable(port, timeout=1.0):
            if port == 6006:
                return True
            return False

        with patch.object(fd, "_port_reachable", side_effect=fake_reachable), \
             patch.object(fd, "_tmux_session_exists", return_value=False), \
             patch.object(fd, "_next_free_port", return_value=6099), \
             patch.object(fd.subprocess, "run",
                          return_value=MagicMock(returncode=0, stderr="")):
            results = fd._start_preview_for(
                "FOO-1",
                [{"name": "default", "command": "npm run dev",
                  "cwd": "", "port": 6006}],
                wait=False)
        r = results[0]
        # Started successfully on the fallback port
        self.assertEqual(r["url"], "http://localhost:6099")
        self.assertIsNotNone(r["session"])
        # Warning recorded
        self.assertIn("6006", r["error"])
        self.assertIn("6099", r["error"])

    def test_start_preview_spawns_tmux(self):
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        fd.session_set("FOO-1", worktree=str(wt))
        with patch.object(fd, "_port_reachable", return_value=False), \
             patch.object(fd, "_tmux_session_exists", return_value=False), \
             patch.object(fd.subprocess, "run",
                          return_value=MagicMock(returncode=0, stderr="")):
            results = fd._start_preview_for(
                "FOO-1",
                [{"name": "default", "command": "npm run dev",
                  "cwd": "", "port": 6006}],
                wait=False)
        r = results[0]
        self.assertEqual(r["session"], "preview-FOO-1-default")
        self.assertEqual(r["url"], "http://localhost:6006")
        self.assertIsNone(r["error"])
        # Dict-form session state
        self.assertEqual(fd.session_get("FOO-1", "preview_urls"),
                         {"default": "http://localhost:6006"})
        self.assertEqual(fd.session_get("FOO-1", "preview_sessions"),
                         {"default": "preview-FOO-1-default"})
        # Primary preview_url mirrors default
        self.assertEqual(fd.session_get("FOO-1", "preview_url"),
                         "http://localhost:6006")

    def test_sequential_starts_get_distinct_ports(self):
        """Calling _start_preview_for back-to-back within one invocation must
        give each service a different port, even before the previous one binds.
        """
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        fd.session_set("FOO-1", worktree=str(wt))
        fd.session_set("BAR-7", worktree=str(wt))

        # Simulate nothing bound ever — _port_reachable always False.
        # Without the claimed-set, each call would pick 6006.
        with patch.object(fd, "_port_reachable", return_value=False), \
             patch.object(fd, "_tmux_session_exists", return_value=False), \
             patch.object(fd.subprocess, "run",
                          return_value=MagicMock(returncode=0, stderr="")):
            r1 = fd._start_preview_for(
                "FOO-1",
                [{"name": "default", "command": "a", "cwd": "", "port": None}],
                wait=False)
            r2 = fd._start_preview_for(
                "BAR-7",
                [{"name": "default", "command": "b", "cwd": "", "port": None}],
                wait=False)
        p1 = int(r1[0]["url"].rsplit(":", 1)[-1])
        p2 = int(r2[0]["url"].rsplit(":", 1)[-1])
        self.assertNotEqual(p1, p2,
                            f"both tickets got port {p1} — claimed-set leak")

    def test_start_preview_multi_service(self):
        wt = Path(self._tmp) / "wt-1"
        wt.mkdir()
        fd.session_set("FOO-1", worktree=str(wt))
        services = [
            {"name": "app",       "command": "npm run dev",       "cwd": "", "port": 3000},
            {"name": "storybook", "command": "npm run storybook", "cwd": "", "port": 6006},
        ]
        with patch.object(fd, "_port_reachable", return_value=False), \
             patch.object(fd, "_tmux_session_exists", return_value=False), \
             patch.object(fd.subprocess, "run",
                          return_value=MagicMock(returncode=0, stderr="")):
            results = fd._start_preview_for("FOO-1", services, wait=False)
        self.assertEqual(len(results), 2)
        names = {r["name"] for r in results}
        self.assertEqual(names, {"app", "storybook"})
        urls = fd.session_get("FOO-1", "preview_urls")
        self.assertEqual(urls["app"], "http://localhost:3000")
        self.assertEqual(urls["storybook"], "http://localhost:6006")
        sessions = fd.session_get("FOO-1", "preview_sessions")
        self.assertEqual(sessions["app"], "preview-FOO-1-app")
        self.assertEqual(sessions["storybook"], "preview-FOO-1-storybook")

    def test_post_preview_card_is_no_op(self):
        """Deprecated: _post_preview_card used to post an info card with
        Open/Stop buttons after preview start. That pattern is replaced by
        persistent per-route buttons in the sidebar group header (driven by
        `sos-flow-dev previews` + WS broadcast). The function is now a
        no-op, kept for callsite compatibility until full cleanup."""
        results = [
            {"name": "app", "session": "preview-X-app",
             "url": "http://localhost:3000", "error": None},
        ]
        with patch.object(fd, "post_card") as mock_post:
            fd._post_preview_card("X-1", results)
        mock_post.assert_not_called()

    def test_stop_preview_all(self):
        fd.session_set("FOO-1",
                       preview_urls={"app": "http://localhost:3000",
                                     "storybook": "http://localhost:6006"},
                       preview_sessions={"app": "preview-FOO-1-app",
                                         "storybook": "preview-FOO-1-storybook"})
        with patch.object(fd, "_tmux_session_exists", return_value=True), \
             patch.object(fd.subprocess, "run") as mock_run:
            stopped, errors = fd._stop_preview_for("FOO-1")
        self.assertEqual(sorted(stopped), ["app", "storybook"])
        self.assertEqual(errors, [])
        # State cleaned up
        self.assertEqual(fd.session_get("FOO-1", "preview_urls"), {})
        self.assertEqual(fd.session_get("FOO-1", "preview_sessions"), {})
        # Both tmux kills invoked
        killed = [c.args[0] for c in mock_run.call_args_list
                  if "kill-session" in c.args[0]]
        self.assertEqual(len(killed), 2)

    def test_stop_preview_specific_service(self):
        fd.session_set("FOO-1",
                       preview_urls={"app": "http://localhost:3000",
                                     "storybook": "http://localhost:6006"},
                       preview_sessions={"app": "preview-FOO-1-app",
                                         "storybook": "preview-FOO-1-storybook"})
        with patch.object(fd, "_tmux_session_exists", return_value=True), \
             patch.object(fd.subprocess, "run"):
            stopped, errors = fd._stop_preview_for("FOO-1", ["app"])
        self.assertEqual(stopped, ["app"])
        # storybook survives
        self.assertEqual(fd.session_get("FOO-1", "preview_sessions"),
                         {"storybook": "preview-FOO-1-storybook"})
        self.assertEqual(fd.session_get("FOO-1", "preview_urls"),
                         {"storybook": "http://localhost:6006"})

    def test_stop_preview_migrates_legacy_state(self):
        # Legacy single-URL state should be understood and stopped.
        fd.session_set("FOO-1",
                       preview_url="http://localhost:6006",
                       preview_session="preview-FOO-1")
        with patch.object(fd, "_tmux_session_exists", return_value=True), \
             patch.object(fd.subprocess, "run"):
            stopped, errors = fd._stop_preview_for("FOO-1")
        self.assertEqual(stopped, ["default"])
        self.assertEqual(errors, [])

    def test_stop_preview_no_sessions(self):
        fd.session_set("FOO-1", phase="merged")
        stopped, errors = fd._stop_preview_for("FOO-1")
        self.assertEqual(stopped, [])
        self.assertIn("no preview sessions", errors[0])


class TestCmdQaApprove(SessionBase):
    """qa-approve calls gh pr merge directly and verifies mergedAt."""

    def _sess(self, merge_strategy=None, checks=None):
        wt = Path(self._tmp) / "wt"
        (wt / ".pm").mkdir(parents=True)
        cfg = {}
        if merge_strategy:
            cfg["git"] = {"merge_strategy": merge_strategy}
        if checks:
            cfg["checks"] = checks
        (wt / ".pm" / "config.json").write_text(json.dumps(cfg))
        fd.session_set("FOO-1",
                       pr_url="https://github.com/x/y/pull/42",
                       pr_num="42",
                       worktree=str(wt),
                       parent_branch="main")
        return wt

    def test_happy_path_merges_and_verifies(self):
        self._sess(merge_strategy="squash")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "merge" in cmd and "pr" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "view" in cmd and "pr" in cmd:
                return MagicMock(returncode=0,
                                 stdout='{"mergedAt":"2026-04-21T20:00:00Z"}',
                                 stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fd.subprocess, "run", side_effect=fake_run), \
             patch.object(fd, "post_card", return_value="card_x") as mock_post:
            fd.cmd_qa_approve(args_ns(ticket="FOO-1"))

        # gh pr merge was invoked with --squash
        merge_calls = [c for c in calls if isinstance(c, list)
                       and len(c) >= 4 and c[0:3] == ["gh", "pr", "merge"]]
        self.assertEqual(len(merge_calls), 1)
        self.assertIn("--squash", merge_calls[0])
        self.assertIn("--delete-branch", merge_calls[0])
        # Verification call happened
        view_calls = [c for c in calls if isinstance(c, list)
                      and c[0:3] == ["gh", "pr", "view"]]
        self.assertEqual(len(view_calls), 1)
        # Card posted
        mock_post.assert_called_once()
        self.assertEqual(fd.session_get("FOO-1", "phase"), "merged")

    def test_unmerged_refuses_to_post_card(self):
        """If gh pr merge returns 0 but mergedAt is null, refuse to lie."""
        self._sess()

        def fake_run(cmd, **kw):
            if "merge" in cmd and "pr" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "view" in cmd and "pr" in cmd:
                # mergedAt is null — merge didn't actually happen
                return MagicMock(returncode=0, stdout='{"mergedAt":null}',
                                 stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fd.subprocess, "run", side_effect=fake_run), \
             patch.object(fd, "post_card") as mock_post:
            with self.assertRaises(SystemExit):
                fd.cmd_qa_approve(args_ns(ticket="FOO-1"))
        mock_post.assert_not_called()
        # Session NOT marked as merged
        self.assertNotEqual(fd.session_get("FOO-1", "phase"), "merged")

    def test_checks_are_not_run_locally(self):
        """Local lint/test/typecheck from config should NOT gate merge — CI
        owns that via branch protection. Ensure the CLI never shells out to
        the configured check commands."""
        self._sess(checks={"test": "should-never-run",
                           "lint": "echo nope && exit 1"})

        def fake_run(cmd, **kw):
            # If the CLI tried to run the check, its shell=True call would
            # hit exit 1 and abort. With the checks-in-CI design, shell=True
            # commands should never be issued.
            assert not kw.get("shell"), (
                f"CLI ran a shell command ({cmd!r}) — should delegate checks to CI"
            )
            if "merge" in cmd and "pr" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "view" in cmd and "pr" in cmd:
                return MagicMock(returncode=0,
                                 stdout='{"mergedAt":"2026-04-21T20:00:00Z"}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fd.subprocess, "run", side_effect=fake_run), \
             patch.object(fd, "post_card", return_value="card_x"):
            fd.cmd_qa_approve(args_ns(ticket="FOO-1"))

        self.assertEqual(fd.session_get("FOO-1", "phase"), "merged")

    def test_no_pr_num_fails(self):
        fd.session_set("FOO-1", worktree=str(Path(self._tmp)))
        with self.assertRaises(SystemExit):
            fd.cmd_qa_approve(args_ns(ticket="FOO-1"))

    def test_merge_strategy_config(self):
        self._sess(merge_strategy="rebase")
        def fake_run(cmd, **kw):
            if "merge" in cmd and "pr" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "view" in cmd and "pr" in cmd:
                return MagicMock(returncode=0,
                                 stdout='{"mergedAt":"2026-04-21T20:00:00Z"}')
            return MagicMock(returncode=0, stdout="")

        captured_cmd = []
        def capture(cmd, **kw):
            if isinstance(cmd, list) and len(cmd) >= 4 and cmd[0:3] == ["gh", "pr", "merge"]:
                captured_cmd.append(cmd)
            return fake_run(cmd, **kw)

        with patch.object(fd.subprocess, "run", side_effect=capture), \
             patch.object(fd, "post_card"):
            fd.cmd_qa_approve(args_ns(ticket="FOO-1"))
        self.assertIn("--rebase", captured_cmd[0])

    def test_invalid_strategy_fails(self):
        self._sess(merge_strategy="nonsense")
        with self.assertRaises(SystemExit):
            fd.cmd_qa_approve(args_ns(ticket="FOO-1"))


class TestSourceRepoResolution(SessionBase):
    """_resolve_source_repo chain: env → config → CWD git.

    The config pin beats CWD because the ghostty-mini project picker writes
    it as an intentional "this is my active project" declaration — a caller
    sitting in a different repo (e.g. a Claude Code session) must still use
    it, even though its own CWD would otherwise be a valid git repo.
    """

    def test_env_var_wins(self):
        src = Path(self._tmp) / "source"
        src.mkdir()
        with patch.dict("os.environ",
                        {"SOS_FLOW_DEV_SOURCE": str(src)}, clear=False):
            # Also set a conflicting config to make sure env wins.
            fd._save_global_config({"source_repo": "/not/used"})
            result = fd._resolve_source_repo()
        self.assertEqual(result, src.resolve())

    def test_env_var_missing_path_ignored(self):
        with patch.dict("os.environ",
                        {"SOS_FLOW_DEV_SOURCE": "/does/not/exist"}, clear=False), \
             patch.object(fd.subprocess, "run",
                          side_effect=fd.subprocess.CalledProcessError(1, "git")):
            # Falls through to config
            cfg_src = Path(self._tmp) / "cfg-source"
            cfg_src.mkdir()
            fd._save_global_config({"source_repo": str(cfg_src)})
            result = fd._resolve_source_repo()
        self.assertEqual(result, cfg_src.resolve())

    def test_config_beats_cwd_git(self):
        """Regression: the project picker writes source_repo into config;
        a caller whose CWD happens to be a different git repo must still
        honor that pin. (The ghostty-mini project picker cds the user's
        terminal but other callers — background processes, other sessions —
        see only the config.)"""
        cfg_src = Path(self._tmp) / "configured"
        cfg_src.mkdir()
        cwd_src = Path(self._tmp) / "elsewhere"
        cwd_src.mkdir()
        fd._save_global_config({"source_repo": str(cfg_src)})
        os.environ.pop("SOS_FLOW_DEV_SOURCE", None)
        # If the CWD git call were reached, it'd return cwd_src. It must not
        # be reached — config takes precedence, so mock_run should not fire.
        with patch.object(fd.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout=str(cwd_src) + "\n", returncode=0),
            ]
            result = fd._resolve_source_repo()
        self.assertEqual(result, cfg_src.resolve())
        mock_run.assert_not_called()

    def test_cwd_is_source_repo(self):
        src = Path(self._tmp) / "source"
        src.mkdir()
        os.environ.pop("SOS_FLOW_DEV_SOURCE", None)
        with patch.object(fd.subprocess, "run") as mock_run:
            # First call: rev-parse --show-toplevel → returns the source path
            # Second call: rev-parse --git-common-dir → returns ".git" (meaning
            # this IS the main repo, not a worktree)
            mock_run.side_effect = [
                MagicMock(stdout=str(src) + "\n", returncode=0),
                MagicMock(stdout=".git\n", returncode=0),
            ]
            result = fd._resolve_source_repo()
        self.assertEqual(result, src.resolve())

    def test_cwd_is_worktree_walks_up_to_source(self):
        """Even if the operator is inside a worktree, resolve the main repo."""
        src = Path(self._tmp) / "source"
        src.mkdir()
        worktree = Path(self._tmp) / "source" / "claude" / "worktrees" / "wt-1"
        worktree.mkdir(parents=True)
        os.environ.pop("SOS_FLOW_DEV_SOURCE", None)
        # git rev-parse --show-toplevel returns the worktree path
        # git -C <worktree> rev-parse --git-common-dir returns /path/to/source/.git
        with patch.object(fd.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout=str(worktree) + "\n", returncode=0),   # show-toplevel
                MagicMock(stdout=str(src / ".git") + "\n", returncode=0),  # git-common-dir
            ]
            result = fd._resolve_source_repo()
        self.assertEqual(result, src)

    def test_config_fallback_when_cwd_has_no_git(self):
        cfg_src = Path(self._tmp) / "cfg-source"
        cfg_src.mkdir()
        fd._save_global_config({"source_repo": str(cfg_src)})
        os.environ.pop("SOS_FLOW_DEV_SOURCE", None)
        with patch.object(fd.subprocess, "run",
                          side_effect=fd.subprocess.CalledProcessError(128, "git")):
            result = fd._resolve_source_repo()
        self.assertEqual(result, cfg_src.resolve())

    def test_no_resolution_returns_none(self):
        os.environ.pop("SOS_FLOW_DEV_SOURCE", None)
        with patch.object(fd.subprocess, "run",
                          side_effect=fd.subprocess.CalledProcessError(128, "git")):
            self.assertIsNone(fd._resolve_source_repo())


class TestResolveBaseBranch(SessionBase):
    def test_explicit_wins(self):
        fd._save_global_config({"default_base_branch": "main"})
        self.assertEqual(fd._resolve_base_branch("sbook/epic"), "sbook/epic")

    def test_config_when_no_explicit(self):
        fd._save_global_config({"default_base_branch": "sbook/epic"})
        self.assertEqual(fd._resolve_base_branch(None), "sbook/epic")

    def test_none_when_neither(self):
        self.assertIsNone(fd._resolve_base_branch(None))


class TestConfigCommand(SessionBase):
    def test_set_and_get(self):
        src = Path(self._tmp) / "src"
        src.mkdir()
        fd.cmd_config(args_ns(action="set", key="source_repo", value=str(src)))
        got = fd._load_global_config()
        self.assertEqual(got["source_repo"], str(src.resolve()))

    def test_set_rejects_nonexistent_source(self):
        with self.assertRaises(SystemExit):
            fd.cmd_config(args_ns(action="set", key="source_repo",
                                  value="/does/not/exist"))

    def test_set_rejects_unknown_key(self):
        with self.assertRaises(SystemExit):
            fd.cmd_config(args_ns(action="set", key="bogus", value="x"))

    def test_unset_removes(self):
        fd._save_global_config({"default_base_branch": "main"})
        fd.cmd_config(args_ns(action="unset", key="default_base_branch", value=None))
        self.assertNotIn("default_base_branch", fd._load_global_config())


class TestResync(SessionBase):
    """resync re-posts flow-dev cards from session state, idempotently."""

    def test_resync_awaiting_qa_posts_three_cards(self):
        fd.session_set("FOO-1",
                       phase="awaiting-qa",
                       pr_url="https://x/pull/42", pr_num="42",
                       preview_url="",
                       review_verdict="approve", review_comments=0)
        with patch.object(fd, "_existing_card_titles", return_value=set()), \
             patch.object(fd, "post_card", return_value="card_x") as mock_post:
            n = fd._resync_cards_for("FOO-1")
        self.assertEqual(n, 3)
        titles = [c.args[1] for c in mock_post.call_args_list]
        self.assertEqual(titles, [
            "PR #42 opened",
            "Review posted — 0 comments",
            "Ready for QA",
        ])

    def test_resync_skips_existing_cards(self):
        fd.session_set("FOO-1",
                       phase="awaiting-qa",
                       pr_url="https://x/pull/42", pr_num="42",
                       preview_url="",
                       review_verdict="changes-requested", review_comments=3)
        # "PR #42 opened" already in inbox → skip it
        with patch.object(fd, "_existing_card_titles",
                          return_value={"PR #42 opened"}), \
             patch.object(fd, "post_card", return_value="card_x") as mock_post:
            n = fd._resync_cards_for("FOO-1")
        self.assertEqual(n, 2)
        titles = [c.args[1] for c in mock_post.call_args_list]
        self.assertNotIn("PR #42 opened", titles)
        self.assertIn("Review posted — 3 comments", titles)
        self.assertIn("Ready for QA", titles)

    def test_resync_merged_phase_posts_merged_card(self):
        fd.session_set("FOO-1",
                       phase="merged", pr_url="https://x/pull/42",
                       pr_num="42", review_verdict="approve", review_comments=0)
        with patch.object(fd, "_existing_card_titles", return_value=set()), \
             patch.object(fd, "post_card", return_value="card_x") as mock_post:
            fd._resync_cards_for("FOO-1")
        titles = [c.args[1] for c in mock_post.call_args_list]
        self.assertIn("Merged · FOO-1", titles)

    def test_resync_early_phase_posts_nothing(self):
        fd.session_set("FOO-1", phase="alloc", pr_num="")
        with patch.object(fd, "_existing_card_titles", return_value=set()), \
             patch.object(fd, "post_card") as mock_post:
            n = fd._resync_cards_for("FOO-1")
        self.assertEqual(n, 0)
        mock_post.assert_not_called()

    def test_resync_does_not_post_preview_card(self):
        """The 'Preview ready' info-card is deprecated — the sidebar's per-
        route buttons replace it. Resync must no longer post such a card
        even when preview sessions are live."""
        fd.session_set("FOO-1",
                       phase="awaiting-qa", pr_url="https://x/pull/42", pr_num="42",
                       preview_urls={"app": "http://localhost:3001"},
                       preview_sessions={"app": "preview-FOO-1-app"},
                       review_verdict="approve", review_comments=0)
        with patch.object(fd, "_existing_card_titles", return_value=set()), \
             patch.object(fd, "_tmux_session_exists", return_value=True), \
             patch.object(fd, "post_card", return_value="card_x") as mock_post:
            fd._resync_cards_for("FOO-1")
        titles = [c.args[1] for c in mock_post.call_args_list]
        self.assertNotIn("Preview ready · FOO-1", titles)


class TestWatchRendering(SessionBase):
    """cmd_watch renders a table from session state and maps phase → live tmux name."""

    def test_live_session_for_each_phase(self):
        self.assertEqual(fd._live_session_for("FOO-1", "alloc"), "flow-FOO-1-alloc")
        self.assertEqual(fd._live_session_for("FOO-1", "work-1"), "pm-FOO-1")
        self.assertEqual(fd._live_session_for("FOO-1", "review"), "flow-FOO-1-review")
        self.assertEqual(fd._live_session_for("FOO-1", "work-2"), "flow-FOO-1-work2")
        self.assertEqual(fd._live_session_for("FOO-1", "work-3"), "flow-FOO-1-work3")
        self.assertIsNone(fd._live_session_for("FOO-1", "awaiting-qa"))
        self.assertIsNone(fd._live_session_for("FOO-1", "merged"))
        self.assertIsNone(fd._live_session_for("FOO-1", "mystery"))

    def test_render_table_reads_sessions(self):
        fd.session_set("FOO-1", phase="awaiting-qa",
                       pr_url="https://x/pull/42", pr_num="42",
                       preview_url="http://localhost:6006",
                       started_at="2026-04-21T10:00:00Z")
        fd.session_set("BAR-7", phase="review",
                       pr_url="https://x/pull/7", pr_num="7",
                       preview_url="",
                       started_at="2026-04-21T10:05:00Z")
        with patch.object(fd, "_tmux_session_exists", return_value=True):
            rows = fd._render_watch_table()
        self.assertEqual(len(rows), 2)
        tickets = [r[0] for r in rows]
        self.assertIn("FOO-1", tickets)
        self.assertIn("BAR-7", tickets)
        foo = next(r for r in rows if r[0] == "FOO-1")
        # phase=awaiting-qa → no leaf session
        self.assertEqual(foo[2], "—")
        self.assertEqual(foo[4], "#42")
        self.assertEqual(foo[5], "http://localhost:6006")
        bar = next(r for r in rows if r[0] == "BAR-7")
        self.assertEqual(bar[2], "flow-BAR-7-review")

    def test_render_flags_dead_tmux_session(self):
        fd.session_set("FOO-1", phase="review", pr_num="42",
                       started_at="2026-04-21T10:00:00Z")
        with patch.object(fd, "_tmux_session_exists", return_value=False):
            rows = fd._render_watch_table()
        self.assertEqual(rows[0][2], "flow-FOO-1-review (gone)")

    def test_ticket_filter_narrows_rows(self):
        fd.session_set("FOO-1", phase="review", pr_num="1",
                       started_at="2026-04-21T10:00:00Z")
        fd.session_set("BAR-7", phase="review", pr_num="7",
                       started_at="2026-04-21T10:00:00Z")
        with patch.object(fd, "_tmux_session_exists", return_value=True):
            rows = fd._render_watch_table(tickets_filter={"FOO-1"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "FOO-1")

    def test_empty_state_shows_help_line(self):
        with patch("builtins.print") as mp:
            fd._print_watch([], first=True)
        printed = "\n".join(str(c.args[0]) if c.args else "" for c in mp.call_args_list)
        self.assertIn("no active flow-dev sessions", printed)


class TestActivityDiff(unittest.TestCase):
    """Delta detection for the activity watcher."""

    def test_small_additions_listed_individually(self):
        prev = {"a.txt"}
        cur = {"a.txt", "b.txt", "c.txt"}
        with patch.object(fd.subprocess, "run"):
            lines = fd._diff_to_log_lines(prev, cur, 0, 0, Path("/wt"))
        self.assertEqual(sorted(lines), ["+ b.txt", "+ c.txt"])

    def test_bulk_addition_grouped_by_top_dir(self):
        prev = set()
        cur = {f"apps/web/src/app/fx/variants/v{i}.tsx" for i in range(40)}
        with patch.object(fd.subprocess, "run"):
            lines = fd._diff_to_log_lines(prev, cur, 0, 0, Path("/wt"))
        self.assertEqual(len(lines), 1)
        self.assertIn("+40 in apps/", lines[0])
        self.assertIn("(e.g.", lines[0])

    def test_mixed_topdirs_each_grouped(self):
        prev = set()
        cur = set()
        for i in range(10):
            cur.add(f"apps/web/x{i}.ts")
        for i in range(10):
            cur.add(f"packages/ui/y{i}.ts")
        with patch.object(fd.subprocess, "run"):
            lines = fd._diff_to_log_lines(prev, cur, 0, 0, Path("/wt"))
        joined = "\n".join(lines)
        self.assertIn("+10 in apps/", joined)
        self.assertIn("+10 in packages/", joined)

    def test_removals_capped_with_summary(self):
        prev = {f"f{i}.txt" for i in range(10)}
        cur = {"f0.txt"}
        with patch.object(fd.subprocess, "run"):
            lines = fd._diff_to_log_lines(prev, cur, 0, 0, Path("/wt"))
        removal_lines = [l for l in lines if l.startswith("-")]
        # 3 individual removals + "-N more removed" summary
        self.assertEqual(len(removal_lines), 4)
        self.assertTrue(any("6 more removed" in l for l in removal_lines))

    def test_new_commits_fetched_via_git_log(self):
        mock_log = MagicMock(stdout="abc1234 FX-1: scaffolding\ndef5678 FX-1: fix typo\n",
                             returncode=0)
        with patch.object(fd.subprocess, "run", return_value=mock_log):
            lines = fd._diff_to_log_lines(set(), set(), 0, 2, Path("/wt"))
        self.assertEqual(len(lines), 2)
        self.assertIn("● commit abc1234 FX-1: scaffolding", lines[0])

    def test_no_delta_returns_empty(self):
        prev = {"a.txt", "b.txt"}
        with patch.object(fd.subprocess, "run"):
            lines = fd._diff_to_log_lines(prev, prev, 5, 5, Path("/wt"))
        self.assertEqual(lines, [])


class TestActivitySnapshot(unittest.TestCase):
    """git status + rev-list parsing."""

    def test_snapshot_parses_porcelain_flags(self):
        # Typical porcelain output: "XY filename"
        porcelain = (
            " M src/app.js\n"       # modified
            "?? new/file.txt\n"     # untracked
            "A  src/new.js\n"       # staged add
            "D  deleted.js\n"       # deleted
            "R  old.js -> new.js\n" # rename
        )
        def fake_run(cmd, **kw):
            if "status" in cmd:
                return MagicMock(stdout=porcelain)
            if "rev-list" in cmd:
                return MagicMock(stdout="3\n")
            return MagicMock(stdout="")

        with patch.object(fd.subprocess, "run", side_effect=fake_run):
            files, commits = fd._git_snapshot(Path("/wt"), "main")
        self.assertEqual(files,
                         {"src/app.js", "new/file.txt", "src/new.js",
                          "deleted.js", "new.js"})
        self.assertEqual(commits, 3)

    def test_snapshot_handles_git_failure(self):
        with patch.object(fd.subprocess, "run",
                          side_effect=fd.subprocess.SubprocessError("boom")):
            files, commits = fd._git_snapshot(Path("/wt"), "main")
        self.assertEqual(files, set())
        self.assertEqual(commits, 0)

    def test_snapshot_handles_empty_porcelain(self):
        def fake_run(cmd, **kw):
            if "status" in cmd:
                return MagicMock(stdout="")
            if "rev-list" in cmd:
                return MagicMock(stdout="0\n")
            return MagicMock(stdout="")
        with patch.object(fd.subprocess, "run", side_effect=fake_run):
            files, commits = fd._git_snapshot(Path("/wt"), "main")
        self.assertEqual(files, set())
        self.assertEqual(commits, 0)

    def test_snapshot_passes_untracked_files_all(self):
        """Regression: plain `git status --porcelain` collapses an untracked
        dir to a single entry, so the watcher never sees files created
        inside it and fires false silence warnings. Must pass -uall so
        every untracked file lists individually."""
        captured = []
        def fake_run(cmd, **kw):
            captured.append(list(cmd))
            if "status" in cmd:
                return MagicMock(stdout="")
            return MagicMock(stdout="0\n")
        with patch.object(fd.subprocess, "run", side_effect=fake_run):
            fd._git_snapshot(Path("/wt"), "main")
        status_call = next(c for c in captured if "status" in c)
        self.assertIn("--untracked-files=all", status_call)

    def test_snapshot_cumulative_includes_committed_files(self):
        """Regression: when an agent commits a file it drops from
        porcelain. Without cumulative tracking, the watcher would see the
        file COUNT DECREASE when the agent did MORE work — misleading
        progress signal. With phase_start_head passed in, _git_snapshot
        unions the porcelain set with `git diff --name-only <head>..HEAD`
        so the count is monotonic for the life of the phase."""
        def fake_run(cmd, **kw):
            if "status" in cmd:
                # Currently uncommitted: one modified file
                return MagicMock(stdout=" M src/active.ts\n")
            if "diff" in cmd and "--name-only" in cmd:
                # Already-committed in this phase: two files
                return MagicMock(stdout="src/done1.ts\nsrc/done2.ts\n")
            if "rev-list" in cmd:
                return MagicMock(stdout="2\n")
            return MagicMock(stdout="")
        with patch.object(fd.subprocess, "run", side_effect=fake_run):
            files, commits = fd._git_snapshot(
                Path("/wt"), "main", phase_start_head="abc123")
        # Union of uncommitted + committed-this-phase
        self.assertEqual(files, {"src/active.ts", "src/done1.ts", "src/done2.ts"})
        self.assertEqual(commits, 2)

    def test_snapshot_no_phase_start_head_falls_back_to_base(self):
        """When phase_start_head is None (legacy callers), commit count
        falls back to base_ref..HEAD and no diff-based cumulative files
        are added (only porcelain). Preserves old behavior for callers
        that don't pin a phase start."""
        diff_called = []
        def fake_run(cmd, **kw):
            if "status" in cmd:
                return MagicMock(stdout=" M a.ts\n")
            if "diff" in cmd and "--name-only" in cmd:
                diff_called.append(cmd)
                return MagicMock(stdout="")
            if "rev-list" in cmd:
                # Verify base_ref is used, not phase_start_head
                self.assertIn("main..HEAD", " ".join(cmd))
                return MagicMock(stdout="1\n")
            return MagicMock(stdout="")
        with patch.object(fd.subprocess, "run", side_effect=fake_run):
            files, commits = fd._git_snapshot(Path("/wt"), "main", phase_start_head=None)
        # diff --name-only never called when phase_start_head is absent
        self.assertEqual(diff_called, [])
        self.assertEqual(files, {"a.ts"})
        self.assertEqual(commits, 1)


class TestInboxPost(unittest.TestCase):
    """_inbox_post is a thin urllib wrapper — validate payload shape and error
    handling (server offline should not crash the watcher)."""

    def setUp(self):
        # Reset the once-per-process warning flag between tests.
        fd._inbox_post._warned = False

    def test_posts_json_payload(self):
        fake_resp = MagicMock()
        fake_resp.__enter__ = MagicMock(return_value=MagicMock(
            read=lambda: b'{"ok":true,"id":"card_1"}'))
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch.object(fd.urllib.request, "urlopen", return_value=fake_resp):
            with patch("json.load", return_value={"ok": True, "id": "card_1"}):
                resp = fd._inbox_post("/inbox", {"kind": "progress", "title": "t"})
        self.assertEqual(resp, {"ok": True, "id": "card_1"})

    def test_server_offline_returns_none(self):
        with patch.object(fd.urllib.request, "urlopen",
                          side_effect=fd.urllib.error.URLError("nope")):
            resp = fd._inbox_post("/inbox", {"kind": "info"})
        self.assertIsNone(resp)

    def test_connection_error_returns_none(self):
        with patch.object(fd.urllib.request, "urlopen",
                          side_effect=ConnectionError("refused")):
            resp = fd._inbox_post("/inbox", {"kind": "info"})
        self.assertIsNone(resp)


class TestVerifierLoop(SessionBase):
    """Worker/verifier split: worker produces work, verifier judges, retry
    loop pushes back on incomplete verdicts."""

    def setUp(self):
        super().setUp()
        self.wt = Path(self._tmp) / "wt"
        self.wt.mkdir(parents=True)
        (self.wt / ".pm").mkdir(parents=True)

    def _verdict_file(self, phase="pm-start"):
        return Path(f"/tmp/verify-FOO-1-{phase}.json")

    def _stub_verifier(self, verdicts):
        """Patch _run_verifier to return the given verdicts in order."""
        return patch.object(fd, "_run_verifier",
                            side_effect=list(verdicts))

    def test_complete_verdict_returns_deliverable(self):
        """Happy path: verifier says complete, orchestrator writes the
        canonical deliverable file and returns the deliverable dict."""
        worker = MagicMock(return_value=0)
        verdict = {
            "state": "complete",
            "summary": "worker did the thing",
            "deliverable": {"branch": "feature/FOO-1-first-pass",
                            "pr_url": "https://x/pull/1"},
        }
        with self._stub_verifier([verdict]):
            result = fd._run_phase_with_verifier(
                "FOO-1", "pm-start", worker, self.wt)
        worker.assert_called_once()
        self.assertEqual(result["pr_url"], "https://x/pull/1")
        marker = Path("/tmp/pm-complete-FOO-1.json")
        self.assertTrue(marker.exists())
        try: marker.unlink()
        except OSError: pass

    def test_incomplete_verdict_halts_no_retry(self):
        """Retries removed — incomplete verdict halts the phase immediately
        with the verifier's feedback surfaced to the operator."""
        worker = MagicMock(return_value=0)
        incomplete = {
            "state": "incomplete",
            "summary": "only 2 of 6 comments addressed",
            "deliverable": {},
            "feedback": ["Gallery.tsx:42 unaddressed",
                         "manifest.ts:7 unaddressed"],
        }
        with self._stub_verifier([incomplete]), \
             patch.object(fd, "post_card") as mock_post:
            with self.assertRaises(SystemExit):
                fd._run_phase_with_verifier(
                    "FOO-1", "work-2", worker, self.wt)
        # Worker called exactly once — no retry
        worker.assert_called_once()
        # Failure card posted with feedback visible to operator
        mock_post.assert_called()
        title = mock_post.call_args.args[1]
        self.assertIn("incomplete", title)

    def test_failed_verdict_aborts(self):
        worker = MagicMock(return_value=0)
        failed = {
            "state": "failed",
            "summary": "committed to wrong branch",
            "deliverable": {"error": "on parent branch, should be feature/FOO-1"},
        }
        with self._stub_verifier([failed]), \
             patch.object(fd, "post_card"):
            with self.assertRaises(SystemExit):
                fd._run_phase_with_verifier("FOO-1", "work-2", worker, self.wt)
        worker.assert_called_once()

    def test_verifier_crash_treated_as_failure(self):
        worker = MagicMock(return_value=0)
        with self._stub_verifier([None]):
            with self.assertRaises(SystemExit):
                fd._run_phase_with_verifier(
                    "FOO-1", "pm-start", worker, self.wt)
        worker.assert_called_once()

    def test_worker_rc_nonzero_does_not_abort_before_verifier(self):
        """New behavior: rc!=0 does not short-circuit. Verifier runs from
        state; a crashed worker that still committed real work can pass."""
        worker = MagicMock(return_value=1)
        verdict = {
            "state": "complete",
            "summary": "worker crashed but commits landed",
            "deliverable": {"ready": True, "url": ""},
        }
        with self._stub_verifier([verdict]):
            result = fd._run_phase_with_verifier(
                "FOO-1", "work-2", worker, self.wt)
        worker.assert_called_once()
        # Verifier got called despite rc=1
        self.assertEqual(result["ready"], True)


class TestVerdictSchemaValidation(unittest.TestCase):
    """_validate_verdict_schema rejects malformed verifier output."""

    def test_valid_minimal_verdict(self):
        v = {"state": "complete", "summary": "ok", "deliverable": {}}
        ok, err = fd._validate_verdict_schema(v)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_missing_state(self):
        v = {"summary": "x", "deliverable": {}}
        ok, err = fd._validate_verdict_schema(v)
        self.assertFalse(ok)
        self.assertIn("state", err)

    def test_invalid_state_value(self):
        v = {"state": "done", "summary": "x", "deliverable": {}}
        ok, err = fd._validate_verdict_schema(v)
        self.assertFalse(ok)
        self.assertIn("invalid state", err)

    def test_deliverable_must_be_object(self):
        v = {"state": "complete", "summary": "x", "deliverable": "not-a-dict"}
        ok, err = fd._validate_verdict_schema(v)
        self.assertFalse(ok)
        self.assertIn("deliverable", err)

    def test_feedback_must_be_list_when_present(self):
        v = {"state": "incomplete", "summary": "x", "deliverable": {},
             "feedback": "should be a list"}
        ok, err = fd._validate_verdict_schema(v)
        self.assertFalse(ok)
        self.assertIn("feedback", err)

    def test_non_dict_rejected(self):
        ok, err = fd._validate_verdict_schema("hello")
        self.assertFalse(ok)


class TestVerifierPromptShape(SessionBase):
    """verifier_prompt contains the phase description, success criteria,
    and evidence-source guidance."""

    def test_prompt_includes_phase_description(self):
        p = fd.verifier_prompt("FOO-1", "pm-start", Path("/tmp/wt"))
        self.assertIn("pm-start initializes", p)
        self.assertIn("feature branch", p.lower())
        self.assertIn("verdict", p.lower())
        self.assertIn("gh pr", p)

    def test_prompt_includes_deliverable_schema(self):
        p = fd.verifier_prompt("FOO-1", "pm-start", Path("/tmp/wt"))
        self.assertIn("completed_at", p)
        self.assertIn("pr_url", p)

    def test_prompt_no_production_language(self):
        p = fd.verifier_prompt("FOO-1", "work-2", Path("/tmp/wt"))
        self.assertIn("You do NOT produce", p)

    def test_no_retry_language_in_prompt(self):
        """Retries removed — prompt should NOT mention attempts or prior
        feedback anymore."""
        p = fd.verifier_prompt("FOO-1", "pm-start", Path("/tmp/wt"))
        self.assertNotIn("Prior feedback", p)
        self.assertNotIn("attempt", p.lower())

    def test_review_phase_has_no_verifier_spec(self):
        """Review verifier was removed — its absence from PHASE_SPECS is
        the signal that review is not verifier-gated."""
        self.assertNotIn("review", fd.PHASE_SPECS)


class TestDeliverableSynthesis(SessionBase):
    """When a subagent commits real work but exits without writing its
    deliverable file, _run_subagent_with_watcher should synthesize the
    deliverable from git state rather than flag the phase as failed."""

    def setUp(self):
        super().setUp()
        self.wt = Path(self._tmp) / "wt"
        (self.wt / ".pm").mkdir(parents=True)
        self.deliverable = self.wt / ".pm" / "work-3-result.json"

    def _patch_subagent(self, rc=0):
        """Stub out everything except the synthesis logic."""
        return patch.multiple(
            fd,
            run_subagent=MagicMock(return_value=rc),
            spawn_watcher=MagicMock(return_value=MagicMock()),
            stop_watcher=MagicMock(),
        )

    def _stub_head(self, before, after):
        """HEAD returns `before` on first call, `after` on second."""
        def fake(cmd, **kw):
            if "rev-parse" in cmd:
                return MagicMock(stdout=(before if fake.calls == 0 else after) + "\n",
                                 returncode=0)
            return MagicMock(stdout="", returncode=0)
        fake.calls = 0
        def wrapped(cmd, **kw):
            v = fake(cmd, **kw)
            fake.calls += 1
            return v
        return wrapped

    def test_explicit_deliverable_trusted_no_synthesis(self):
        """If the agent wrote the file, use it verbatim — never call synth."""
        self.deliverable.write_text('{"ready": true, "url": "http://x"}')
        synth = MagicMock(return_value={"ready": True, "url": "synth"})
        with self._patch_subagent(rc=0):
            rc, missing = fd._run_subagent_with_watcher(
                "FOO-1", "work-3", "sess", "prompt",
                self.deliverable, synthesize_fn=synth)
        self.assertEqual(rc, 0)
        self.assertFalse(missing)
        synth.assert_not_called()
        self.assertNotIn("_synthesized", json.loads(self.deliverable.read_text()))

    def test_synthesis_kicks_in_when_commits_landed(self):
        """Missing file + HEAD moved → synthesize + write, treat as success."""
        synth = lambda: {"ready": True, "url": "http://preview:6008"}
        with self._patch_subagent(rc=0), \
             patch.object(fd.subprocess, "run",
                          side_effect=self._stub_head("a" * 40, "b" * 40)):
            rc, missing = fd._run_subagent_with_watcher(
                "FOO-1", "work-3", "sess", "prompt",
                self.deliverable, synthesize_fn=synth)
        self.assertEqual(rc, 0)
        self.assertFalse(missing)
        # Written synthesized result carries provenance fields
        data = json.loads(self.deliverable.read_text())
        self.assertTrue(data.get("_synthesized"))
        self.assertEqual(data["_commits_since"], "a" * 7)
        self.assertEqual(data["url"], "http://preview:6008")

    def test_no_synthesis_when_head_unchanged(self):
        """Missing file + HEAD didn't move → real failure."""
        synth = MagicMock(return_value={"ready": True})
        same = "c" * 40
        with self._patch_subagent(rc=0), \
             patch.object(fd.subprocess, "run",
                          side_effect=self._stub_head(same, same)):
            rc, missing = fd._run_subagent_with_watcher(
                "FOO-1", "work-3", "sess", "prompt",
                self.deliverable, synthesize_fn=synth)
        self.assertTrue(missing)
        self.assertFalse(self.deliverable.exists())
        synth.assert_not_called()

    def test_rc_nonzero_skips_synthesis(self):
        """Subagent exited non-zero → real failure, even if HEAD moved."""
        synth = MagicMock()
        with self._patch_subagent(rc=1), \
             patch.object(fd.subprocess, "run",
                          side_effect=self._stub_head("a" * 40, "b" * 40)):
            rc, missing = fd._run_subagent_with_watcher(
                "FOO-1", "work-3", "sess", "prompt",
                self.deliverable, synthesize_fn=synth)
        self.assertEqual(rc, 1)
        synth.assert_not_called()

    def test_synthesize_fn_none_requires_explicit_deliverable(self):
        """Phases without a synthesizer (e.g. review) still require the file."""
        with self._patch_subagent(rc=0), \
             patch.object(fd.subprocess, "run",
                          side_effect=self._stub_head("a" * 40, "b" * 40)):
            rc, missing = fd._run_subagent_with_watcher(
                "FOO-1", "review", "sess", "prompt",
                self.deliverable, synthesize_fn=None)
        self.assertTrue(missing)


if __name__ == "__main__":
    unittest.main()
