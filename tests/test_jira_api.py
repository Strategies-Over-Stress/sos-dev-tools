#!/usr/bin/env python3
"""Tests for jira_api — issue type discovery, disk caching, case handling, transitions.

All tests are dry-run: API calls and disk I/O are mocked. No Jira instance needed.

Usage:
    python -m unittest tests.test_jira_api -v
    python tests/test_jira_api.py
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sos_dev_tools import jira_api


# Simulated Jira API responses

# /project/{pk}/statuses returns issue types with status arrays
FAKE_PROJECT_STATUSES = [
    {"id": "10001", "name": "Task", "statuses": []},
    {"id": "10002", "name": "Epic", "statuses": []},
    {"id": "10003", "name": "Subtask", "statuses": []},
    {"id": "10004", "name": "Story", "statuses": []},
    {"id": "10005", "name": "Bug", "statuses": []},
]

# /issue/createmeta response
FAKE_CREATEMETA = {
    "projects": [{
        "issuetypes": [
            {"id": "10001", "name": "Task"},
            {"id": "10002", "name": "Epic"},
            {"id": "10003", "name": "Subtask"},
            {"id": "10004", "name": "Story"},
            {"id": "10005", "name": "Bug"},
        ]
    }]
}

# /issuetype fallback response
FAKE_ISSUE_TYPES_RESPONSE = [
    {"id": "10001", "name": "Task"},
    {"id": "10002", "name": "Epic"},
    {"id": "10003", "name": "Subtask"},
    {"id": "10004", "name": "Story"},
    {"id": "10005", "name": "Bug"},
]


def api_side_effect(*args, **kwargs):
    """Mock api() that returns appropriate responses per endpoint."""
    method, path = args[0], args[1]
    if "/project/" in path and "/statuses" in path:
        return FAKE_PROJECT_STATUSES
    if "createmeta" in path:
        return FAKE_CREATEMETA
    if path == "/issuetype":
        return FAKE_ISSUE_TYPES_RESPONSE
    return {}

FAKE_TRANSITIONS_RESPONSE = {
    "transitions": [
        {"id": "11", "name": "To Do", "to": {"name": "TO DO"}},
        {"id": "21", "name": "Start Progress", "to": {"name": "IN PROGRESS"}},
        {"id": "2", "name": "Review", "to": {"name": "IN REVIEW"}},
        {"id": "31", "name": "Done", "to": {"name": "DONE"}},
    ]
}


class TestGetIssueTypeId(unittest.TestCase):
    """get_issue_type_id should be case-insensitive and handle fuzzy matches."""

    def setUp(self):
        jira_api._issue_type_cache = {}

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_lowercase_input(self, mock_api, mock_save, mock_load):
        self.assertEqual(jira_api.get_issue_type_id("task"), "10001")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_uppercase_input(self, mock_api, mock_save, mock_load):
        self.assertEqual(jira_api.get_issue_type_id("Task"), "10001")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_allcaps_input(self, mock_api, mock_save, mock_load):
        self.assertEqual(jira_api.get_issue_type_id("STORY"), "10004")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_mixed_case_input(self, mock_api, mock_save, mock_load):
        self.assertEqual(jira_api.get_issue_type_id("Epic"), "10002")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_unknown_type_exits(self, mock_api, mock_save, mock_load):
        with self.assertRaises(SystemExit):
            jira_api.get_issue_type_id("nonexistent")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_fuzzy_match(self, mock_api, mock_save, mock_load):
        self.assertEqual(jira_api.get_issue_type_id("sub"), "10003")


class TestIssueTypeDiscovery(unittest.TestCase):
    """get_issue_types should discover types from API and normalize names."""

    def setUp(self):
        jira_api._issue_type_cache = {}

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_discovers_all_types(self, mock_api, mock_save, mock_load):
        types = jira_api.get_issue_types()
        self.assertEqual(types["task"], "10001")
        self.assertEqual(types["epic"], "10002")
        self.assertEqual(types["subtask"], "10003")
        self.assertEqual(types["story"], "10004")
        self.assertEqual(types["bug"], "10005")

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_keys_are_lowercase(self, mock_api, mock_save, mock_load):
        types = jira_api.get_issue_types()
        for key in types:
            self.assertEqual(key, key.lower())

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_api_not_called_after_memory_cache(self, mock_api, mock_save, mock_load):
        jira_api.get_issue_types()
        call_count = mock_api.call_count
        jira_api.get_issue_types()
        jira_api.get_issue_types()
        # No additional API calls after first discovery
        self.assertEqual(mock_api.call_count, call_count)

    @patch.object(jira_api, "_load_disk_cache", return_value=None)
    @patch.object(jira_api, "_save_disk_cache")
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_saves_to_disk_after_discovery(self, mock_api, mock_save, mock_load):
        types = jira_api.get_issue_types()
        mock_save.assert_called_once_with(types)

    @patch.dict(os.environ, {
        "JIRA_ISSUE_TYPE_TASK": "99999",
        "JIRA_ISSUE_TYPE_STORY": "88888",
    })
    @patch.object(jira_api, "api")
    def test_env_overrides_skip_api(self, mock_api):
        types = jira_api.get_issue_types()
        self.assertEqual(types["task"], "99999")
        self.assertEqual(types["story"], "88888")
        mock_api.assert_not_called()


class TestDiskCache(unittest.TestCase):
    """Disk cache should persist types per-instance and respect TTL."""

    def setUp(self):
        jira_api._issue_type_cache = {}
        self._tmpdir = tempfile.mkdtemp()
        self._cache_path = Path(self._tmpdir) / ".jira-cache.json"
        # Patch _cache_file to return our temp path
        self._patcher = patch.object(jira_api, "_cache_file", return_value=self._cache_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_returns_none_when_no_file(self):
        self.assertIsNone(jira_api._load_disk_cache())

    @patch.dict(os.environ, {"JIRA_BASE_URL": "https://test.atlassian.net"})
    def test_save_then_load(self):
        types = {"task": "10001", "story": "10004"}
        jira_api._save_disk_cache(types)
        loaded = jira_api._load_disk_cache()
        self.assertEqual(loaded, types)

    @patch.dict(os.environ, {"JIRA_BASE_URL": "https://test.atlassian.net"})
    def test_load_returns_none_when_expired(self):
        types = {"task": "10001"}
        jira_api._save_disk_cache(types)
        # Backdate the timestamp
        data = json.loads(self._cache_path.read_text())
        cache_key = jira_api._cache_key()
        data[cache_key]["ts"] = time.time() - jira_api._CACHE_TTL - 1
        self._cache_path.write_text(json.dumps(data))
        self.assertIsNone(jira_api._load_disk_cache())

    def test_multiple_instances_coexist(self):
        with patch.dict(os.environ, {"JIRA_BASE_URL": "https://a.atlassian.net"}):
            key_a = jira_api._cache_key()
            jira_api._save_disk_cache({"task": "10001"})
        with patch.dict(os.environ, {"JIRA_BASE_URL": "https://b.atlassian.net"}):
            key_b = jira_api._cache_key()
            jira_api._save_disk_cache({"task": "20001", "story": "20004"})

        data = json.loads(self._cache_path.read_text())
        self.assertIn(key_a, data)
        self.assertIn(key_b, data)
        self.assertEqual(data[key_a]["types"]["task"], "10001")
        self.assertEqual(data[key_b]["types"]["task"], "20001")

    def test_corrupted_file_returns_none(self):
        self._cache_path.write_text("not json{{{")
        self.assertIsNone(jira_api._load_disk_cache())

    @patch.dict(os.environ, {"JIRA_BASE_URL": "https://test.atlassian.net"})
    @patch.object(jira_api, "api", side_effect=api_side_effect)
    def test_discovery_populates_disk_cache(self, mock_api):
        types = jira_api.get_issue_types()
        self.assertTrue(self._cache_path.exists())
        loaded = jira_api._load_disk_cache()
        self.assertEqual(loaded, types)

    @patch.dict(os.environ, {"JIRA_BASE_URL": "https://test.atlassian.net"})
    @patch.object(jira_api, "api")
    def test_fresh_cache_skips_api(self, mock_api):
        jira_api._save_disk_cache({"task": "10001", "story": "10004"})
        types = jira_api.get_issue_types()
        self.assertEqual(types["task"], "10001")
        self.assertEqual(types["story"], "10004")
        mock_api.assert_not_called()


class TestTransitionTicket(unittest.TestCase):
    """transition_ticket should discover transitions and match case-insensitively."""

    def setUp(self):
        jira_api._transition_cache = {}

    @patch.object(jira_api, "api")
    def test_transition_calls_api(self, mock_api):
        mock_api.side_effect = [FAKE_TRANSITIONS_RESPONSE, {}]
        jira_api.transition_ticket("RICH-1", "IN PROGRESS")
        mock_api.assert_any_call("GET", "/issue/RICH-1/transitions")
        mock_api.assert_any_call("POST", "/issue/RICH-1/transitions", {
            "transition": {"id": "21"},
        })

    @patch.object(jira_api, "api")
    def test_transition_case_insensitive(self, mock_api):
        mock_api.side_effect = [FAKE_TRANSITIONS_RESPONSE, {}]
        result = jira_api.transition_ticket("RICH-1", "done")
        self.assertTrue(result)

    @patch.object(jira_api, "api", return_value=FAKE_TRANSITIONS_RESPONSE)
    def test_invalid_transition_returns_false(self, mock_api):
        jira_api._transition_cache = {}
        result = jira_api.transition_ticket("RICH-1", "INVALID")
        self.assertFalse(result)


class TestMdToAdf(unittest.TestCase):
    """md_to_adf should convert markdown to Atlassian Document Format."""

    def test_empty_string(self):
        result = jira_api.md_to_adf("")
        self.assertEqual(result, {"version": 1, "type": "doc", "content": []})

    def test_plain_paragraph(self):
        result = jira_api.md_to_adf("Hello world")
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "paragraph")
        self.assertEqual(result["content"][0]["content"][0]["text"], "Hello world")

    def test_heading(self):
        result = jira_api.md_to_adf("## My Heading")
        block = result["content"][0]
        self.assertEqual(block["type"], "heading")
        self.assertEqual(block["attrs"]["level"], 2)

    def test_bold_inline(self):
        result = jira_api.md_to_adf("This is **bold** text")
        nodes = result["content"][0]["content"]
        bold_node = [n for n in nodes if n.get("marks")]
        self.assertEqual(len(bold_node), 1)
        self.assertEqual(bold_node[0]["text"], "bold")
        self.assertEqual(bold_node[0]["marks"][0]["type"], "strong")

    def test_bullet_list(self):
        result = jira_api.md_to_adf("- item one\n- item two")
        block = result["content"][0]
        self.assertEqual(block["type"], "bulletList")
        self.assertEqual(len(block["content"]), 2)

    def test_ordered_list(self):
        result = jira_api.md_to_adf("1. first\n2. second\n3. third")
        block = result["content"][0]
        self.assertEqual(block["type"], "orderedList")
        self.assertEqual(len(block["content"]), 3)


class TestArgparseLowercase(unittest.TestCase):
    """The -t flag should accept any casing and lowercase it."""

    def test_str_lower_normalizes(self):
        self.assertEqual(str.lower("Story"), "story")
        self.assertEqual(str.lower("TASK"), "task")
        self.assertEqual(str.lower("Epic"), "epic")
        self.assertEqual(str.lower("BUG"), "bug")
        self.assertEqual(str.lower("SubTask"), "subtask")


if __name__ == "__main__":
    unittest.main()
