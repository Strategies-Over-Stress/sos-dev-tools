#!/usr/bin/env python3
"""Tests for inbox_cli — payload construction, POST behavior, error handling.

All tests are dry-run: network I/O is mocked via patching ``urllib.request.urlopen``.
No ghostty-mini server needed.

Usage:
    python -m unittest tests.test_inbox_cli -v
"""

import argparse
import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from sos_dev_tools import inbox_cli


def make_args(**kw):
    """Build an argparse.Namespace with only the provided keys."""
    ns = argparse.Namespace()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _mock_response(body):
    """Mock urllib.request.urlopen context-manager return value."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestInboxBase(unittest.TestCase):
    """inbox_base honors GHOSTTY_MINI_URL with sane default."""

    @patch.dict("os.environ", {}, clear=False)
    def test_default_when_unset(self):
        inbox_cli.os.environ.pop("GHOSTTY_MINI_URL", None)
        self.assertEqual(inbox_cli.inbox_base(), "http://localhost:3030")

    @patch.dict("os.environ", {"GHOSTTY_MINI_URL": "http://example:1234/"})
    def test_env_override_strips_trailing_slash(self):
        self.assertEqual(inbox_cli.inbox_base(), "http://example:1234")

    @patch.dict("os.environ", {"GHOSTTY_MINI_URL": "http://host:9"})
    def test_env_override_no_slash(self):
        self.assertEqual(inbox_cli.inbox_base(), "http://host:9")


class TestBuildCard(unittest.TestCase):
    """build_card translates argparse.Namespace → inbox JSON payload."""

    def test_info_minimal(self):
        args = make_args(title="Hello", ticket=None, url=None, ctx=None)
        card = inbox_cli.build_card("info", args)
        self.assertEqual(card, {"kind": "info", "title": "Hello"})

    def test_action_full(self):
        args = make_args(
            title="Ready for QA", ticket="FOO-123",
            url="http://localhost:3142", ctx="dev server up",
            actions='[{"label":"Open","kind":"openUrl"}]',
        )
        card = inbox_cli.build_card("action", args)
        self.assertEqual(card["kind"], "action")
        self.assertEqual(card["ticket"], "FOO-123")
        self.assertEqual(card["url"], "http://localhost:3142")
        self.assertEqual(card["ctx"], "dev server up")
        self.assertEqual(card["actions"], [{"label": "Open", "kind": "openUrl"}])

    def test_empty_fields_omitted(self):
        # Empty string should be treated the same as missing — don't include key.
        args = make_args(title="X", ticket="", url="", ctx="")
        card = inbox_cli.build_card("info", args)
        self.assertEqual(card, {"kind": "info", "title": "X"})

    def test_no_ticket_produces_general_group(self):
        # Cards without a ticket land in the "General" group on the server.
        args = make_args(title="Standalone", ticket=None, url="https://x", ctx=None)
        card = inbox_cli.build_card("info", args)
        self.assertNotIn("ticket", card)
        self.assertEqual(card["url"], "https://x")

    def test_actions_must_be_valid_json(self):
        args = make_args(title="X", ticket=None, url=None, ctx=None,
                         actions="not json at all")
        with self.assertRaises(SystemExit) as cm:
            inbox_cli.build_card("action", args)
        self.assertEqual(cm.exception.code, 2)

    def test_actions_must_be_array(self):
        args = make_args(title="X", ticket=None, url=None, ctx=None,
                         actions='{"not":"an array"}')
        with self.assertRaises(SystemExit) as cm:
            inbox_cli.build_card("action", args)
        self.assertEqual(cm.exception.code, 2)

    def test_actions_complex_button(self):
        actions_json = json.dumps([
            {"label": "Open PR", "kind": "openUrl", "url": "https://github.com/x/y/pull/1"},
            {"label": "Approve", "kind": "inject", "text": "/flow-dev qa-approve\n", "execute": True},
            {"label": "Request", "kind": "inject", "text": "/flow-dev qa-reject ", "execute": False},
        ])
        args = make_args(title="Gate", ticket="FOO-1", url=None, ctx=None,
                         actions=actions_json)
        card = inbox_cli.build_card("action", args)
        self.assertEqual(len(card["actions"]), 3)
        self.assertTrue(card["actions"][1]["execute"])
        self.assertFalse(card["actions"][2]["execute"])


class TestPost(unittest.TestCase):
    """_post sends correct HTTP request and handles outcomes."""

    @patch.object(inbox_cli.urllib.request, "urlopen")
    def test_posts_correct_body_and_headers(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True, "id": "card_abc"})
        result = inbox_cli._post("/inbox", {"kind": "info", "title": "x"})
        self.assertEqual(result, {"ok": True, "id": "card_abc"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode("utf-8")),
                         {"kind": "info", "title": "x"})

    @patch.object(inbox_cli.urllib.request, "urlopen")
    def test_posts_to_configured_base_url(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True})
        with patch.dict("os.environ", {"GHOSTTY_MINI_URL": "http://remote:9000"}):
            inbox_cli._post("/inbox", {"title": "x"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://remote:9000/inbox")

    @patch.object(inbox_cli.urllib.request, "urlopen",
                  side_effect=inbox_cli.urllib.error.URLError("Connection refused"))
    def test_silently_returns_none_on_url_error(self, mock_urlopen):
        # A missing sidebar must never block the flow.
        self.assertIsNone(inbox_cli._post("/inbox", {"title": "x"}))

    @patch.object(inbox_cli.urllib.request, "urlopen", side_effect=TimeoutError())
    def test_silently_returns_none_on_timeout(self, mock_urlopen):
        self.assertIsNone(inbox_cli._post("/inbox", {"title": "x"}))

    @patch.object(inbox_cli.urllib.request, "urlopen", side_effect=ConnectionRefusedError())
    def test_silently_returns_none_on_connection_refused(self, mock_urlopen):
        self.assertIsNone(inbox_cli._post("/inbox", {"title": "x"}))

    @patch.object(inbox_cli.urllib.request, "urlopen")
    def test_http_error_exits_with_stderr(self, mock_urlopen):
        # A 4xx/5xx from the server IS a real bug — payload shape wrong, etc. —
        # and should surface rather than be swallowed.
        err = inbox_cli.urllib.error.HTTPError(
            url="http://x/inbox", code=400, msg="Bad Request",
            hdrs=None, fp=BytesIO(b'{"error":"missing title"}'),
        )
        mock_urlopen.side_effect = err
        with self.assertRaises(SystemExit) as cm:
            inbox_cli._post("/inbox", {"title": ""})
        self.assertEqual(cm.exception.code, 1)


class TestCmdPost(unittest.TestCase):
    """cmd_post integrates build_card + _post and prints the returned id."""

    @patch.object(inbox_cli, "_post", return_value={"ok": True, "id": "card_abc"})
    def test_prints_id_on_success(self, mock_post):
        args = make_args(title="X", ticket="FOO-1", url=None, ctx=None, actions=None)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_post("info", args)
        mock_print.assert_called_with("card_abc")
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["kind"], "info")
        self.assertEqual(payload["ticket"], "FOO-1")

    @patch.object(inbox_cli, "_post", return_value=None)
    def test_silent_on_unreachable(self, mock_post):
        # No output, no exit — the CLI just returns 0.
        args = make_args(title="X", ticket=None, url=None, ctx=None, actions=None)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_post("info", args)
        mock_print.assert_not_called()


class TestCmdStatus(unittest.TestCase):
    """cmd_status reports connection + card count, or exits on unreachable."""

    @patch.object(inbox_cli, "_get", return_value={"connected": 2, "count": 5})
    def test_connected(self, mock_get):
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_status(None)
        mock_print.assert_called_with("connected=2 cards=5")

    @patch.object(inbox_cli, "_get", return_value={"connected": 0, "count": 0})
    def test_zero_everything_is_still_ok(self, mock_get):
        # Server is running, just no tabs and no cards — exit 0.
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_status(None)
        mock_print.assert_called_with("connected=0 cards=0")

    @patch.object(inbox_cli, "_get", return_value=None)
    def test_disconnected_exits(self, mock_get):
        with self.assertRaises(SystemExit) as cm:
            with patch("builtins.print"):
                inbox_cli.cmd_status(None)
        self.assertEqual(cm.exception.code, 1)


class TestDelete(unittest.TestCase):
    """_delete sends a DELETE request and handles outcomes."""

    @patch.object(inbox_cli.urllib.request, "urlopen")
    def test_sends_delete_method(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True, "id": "card_abc"})
        result = inbox_cli._delete("/inbox/card_abc")
        self.assertEqual(result, {"ok": True, "id": "card_abc"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "DELETE")

    @patch.object(inbox_cli.urllib.request, "urlopen",
                  side_effect=inbox_cli.urllib.error.URLError("refused"))
    def test_silently_returns_none_on_unreachable(self, mock_urlopen):
        self.assertIsNone(inbox_cli._delete("/inbox/card_abc"))

    @patch.object(inbox_cli.urllib.request, "urlopen")
    def test_404_surfaces_as_exit(self, mock_urlopen):
        # 404 "no card with that id" IS a real condition worth reporting —
        # distinct from "server unreachable".
        err = inbox_cli.urllib.error.HTTPError(
            url="http://x/inbox/nope", code=404, msg="Not Found",
            hdrs=None, fp=BytesIO(b'{"error":"no card with that id"}'),
        )
        mock_urlopen.side_effect = err
        with self.assertRaises(SystemExit) as cm:
            inbox_cli._delete("/inbox/nope")
        self.assertEqual(cm.exception.code, 1)


class TestCmdList(unittest.TestCase):
    """cmd_list formats card lists and supports --ticket filtering + --json."""

    SAMPLE = {
        "count": 3,
        "cards": [
            {"id": "card_a", "kind": "info",   "ticket": "FOO-1",
             "title": "PR opened", "url": "https://x/1", "ctx": "ci",  "ts": 1},
            {"id": "card_b", "kind": "action", "ticket": "FOO-1",
             "title": "QA ready",  "url": "http://localhost:3142",    "ts": 2},
            {"id": "card_c", "kind": "info",   "ticket": "BAR-7",
             "title": "Deploy",    "url": "https://x/2",               "ts": 3},
        ],
    }

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_plain_output_lists_all_cards(self, mock_get):
        args = make_args(ticket=None, json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_list(args)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("card_a", printed)
        self.assertIn("card_b", printed)
        self.assertIn("card_c", printed)
        self.assertIn("PR opened", printed)

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_ticket_filter_drops_others(self, mock_get):
        args = make_args(ticket="FOO-1", json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_list(args)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("card_a", printed)
        self.assertIn("card_b", printed)
        self.assertNotIn("card_c", printed)

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_json_mode_prints_valid_json(self, mock_get):
        args = make_args(ticket=None, json=True)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_list(args)
        output = mock_print.call_args[0][0]
        parsed = json.loads(output)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["id"], "card_a")

    @patch.object(inbox_cli, "_get", return_value={"count": 0, "cards": []})
    def test_empty_inbox_message(self, mock_get):
        args = make_args(ticket=None, json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_list(args)
        mock_print.assert_called_with("(inbox is empty)")

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_ticket_filter_empty_result_message(self, mock_get):
        args = make_args(ticket="ZZ-999", json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_list(args)
        mock_print.assert_called_with("(no cards for ZZ-999)")

    @patch.object(inbox_cli, "_get", return_value=None)
    def test_list_exits_on_unreachable(self, mock_get):
        args = make_args(ticket=None, json=False)
        with self.assertRaises(SystemExit) as cm:
            inbox_cli.cmd_list(args)
        self.assertEqual(cm.exception.code, 1)


class TestCmdRemove(unittest.TestCase):
    """cmd_remove deletes a single card by id."""

    @patch.object(inbox_cli, "_delete", return_value={"ok": True, "id": "card_abc"})
    def test_prints_removal(self, mock_delete):
        args = make_args(card_id="card_abc")
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_remove(args)
        mock_delete.assert_called_with("/inbox/card_abc")
        mock_print.assert_called_with("removed card_abc")

    @patch.object(inbox_cli, "_delete", return_value=None)
    def test_exits_on_unreachable(self, mock_delete):
        args = make_args(card_id="card_abc")
        with self.assertRaises(SystemExit) as cm:
            with patch("builtins.print"):
                inbox_cli.cmd_remove(args)
        self.assertEqual(cm.exception.code, 1)


class TestCmdClear(unittest.TestCase):
    """cmd_clear wipes everything OR filters to a ticket group."""

    @patch.object(inbox_cli, "_delete", return_value={"ok": True, "removed": 7})
    def test_clear_all(self, mock_delete):
        args = make_args(ticket=None)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_clear(args)
        mock_delete.assert_called_once_with("/inbox")
        mock_print.assert_called_with("cleared 7 card(s)")

    @patch.object(inbox_cli, "_get",
                  return_value={"cards": [
                      {"id": "card_a", "ticket": "FOO-1"},
                      {"id": "card_b", "ticket": "FOO-1"},
                      {"id": "card_c", "ticket": "OTHER"},
                  ]})
    @patch.object(inbox_cli, "_delete", return_value={"ok": True})
    def test_clear_by_ticket_removes_only_matching(self, mock_delete, mock_get):
        args = make_args(ticket="FOO-1")
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_clear(args)
        # Two matching cards → two DELETE calls.
        deleted_paths = [c.args[0] for c in mock_delete.call_args_list]
        self.assertIn("/inbox/card_a", deleted_paths)
        self.assertIn("/inbox/card_b", deleted_paths)
        self.assertNotIn("/inbox/card_c", deleted_paths)
        self.assertEqual(len(deleted_paths), 2)
        mock_print.assert_called_with("removed 2 card(s) from FOO-1")

    @patch.object(inbox_cli, "_get",
                  return_value={"cards": [{"id": "card_a", "ticket": "OTHER"}]})
    @patch.object(inbox_cli, "_delete")
    def test_clear_by_ticket_no_match(self, mock_delete, mock_get):
        args = make_args(ticket="ZZ-9")
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_clear(args)
        mock_delete.assert_not_called()
        mock_print.assert_called_with("(no cards for ZZ-9)")

    @patch.object(inbox_cli, "_delete", return_value=None)
    def test_clear_all_exits_on_unreachable(self, mock_delete):
        args = make_args(ticket=None)
        with self.assertRaises(SystemExit) as cm:
            with patch("builtins.print"):
                inbox_cli.cmd_clear(args)
        self.assertEqual(cm.exception.code, 1)


class TestCmdReply(unittest.TestCase):
    """cmd_reply POSTs text to /inbox/:id/reply."""

    @patch.object(inbox_cli, "_post", return_value={"ok": True})
    def test_posts_to_correct_endpoint(self, mock_post):
        args = make_args(card_id="card_abc", text="it broke on Safari")
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_reply(args)
        mock_post.assert_called_with("/inbox/card_abc/reply", {"text": "it broke on Safari"})
        mock_print.assert_called_with("replied to card_abc")

    @patch.object(inbox_cli, "_post", return_value=None)
    def test_unreachable_exits(self, mock_post):
        args = make_args(card_id="card_abc", text="hi")
        with self.assertRaises(SystemExit) as cm:
            with patch("builtins.print"):
                inbox_cli.cmd_reply(args)
        self.assertEqual(cm.exception.code, 1)


class TestCmdReplies(unittest.TestCase):
    """cmd_replies lists the reply thread on a card."""

    SAMPLE = {"replies": [
        {"text": "hello", "ts": 1700000000000},
        {"text": "follow-up", "ts": 1700000060000},
    ]}

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_text_output(self, mock_get):
        args = make_args(card_id="card_abc", json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_replies(args)
        lines = [c.args[0] for c in mock_print.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertIn("hello", lines[0])
        self.assertIn("follow-up", lines[1])

    @patch.object(inbox_cli, "_get", return_value=SAMPLE)
    def test_json_output(self, mock_get):
        args = make_args(card_id="card_abc", json=True)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_replies(args)
        parsed = json.loads(mock_print.call_args[0][0])
        self.assertEqual(parsed, self.SAMPLE["replies"])

    @patch.object(inbox_cli, "_get", return_value={"replies": []})
    def test_empty_thread_message(self, mock_get):
        args = make_args(card_id="card_abc", json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_replies(args)
        mock_print.assert_called_with("(no replies)")


class TestCmdWait(unittest.TestCase):
    """cmd_wait long-polls and returns the first reply."""

    @patch.object(inbox_cli, "_get", return_value={"reply": {"text": "approved", "ts": 123}})
    def test_immediate_reply(self, mock_get):
        args = make_args(card_id="card_abc", timeout=60, since=0, json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_wait(args)
        mock_print.assert_called_with("approved")
        # Verify it built the correct URL path including since + timeout_ms.
        called_path = mock_get.call_args[0][0]
        self.assertIn("/inbox/card_abc/wait?since=0&timeout_ms=", called_path)

    @patch.object(inbox_cli, "_get", return_value={"reply": {"text": "hi", "ts": 42}})
    def test_json_output_prints_full_reply(self, mock_get):
        args = make_args(card_id="card_abc", timeout=60, since=0, json=True)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_wait(args)
        parsed = json.loads(mock_print.call_args[0][0])
        self.assertEqual(parsed, {"text": "hi", "ts": 42})

    @patch.object(inbox_cli, "_get")
    def test_retries_on_timeout_then_succeeds(self, mock_get):
        # First poll times out, second returns a reply.
        mock_get.side_effect = [
            {"timeout": True},
            {"reply": {"text": "done waiting", "ts": 456}},
        ]
        args = make_args(card_id="card_abc", timeout=60, since=0, json=False)
        with patch("builtins.print") as mock_print:
            inbox_cli.cmd_wait(args)
        self.assertEqual(mock_get.call_count, 2)
        mock_print.assert_called_with("done waiting")

    @patch.object(inbox_cli, "_get", return_value=None)
    def test_unreachable_exits(self, mock_get):
        args = make_args(card_id="card_abc", timeout=60, since=0, json=False)
        with self.assertRaises(SystemExit) as cm:
            with patch("builtins.print"):
                inbox_cli.cmd_wait(args)
        self.assertEqual(cm.exception.code, 1)

    def test_deadline_already_passed_exits(self):
        # Zero timeout means deadline is essentially now — loop should detect
        # and exit with code 2 before even issuing a request.
        args = make_args(card_id="card_abc", timeout=0, since=0, json=False)
        with patch.object(inbox_cli, "_get") as mock_get:
            with self.assertRaises(SystemExit) as cm:
                with patch("builtins.print"):
                    inbox_cli.cmd_wait(args)
            self.assertEqual(cm.exception.code, 2)
            mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
