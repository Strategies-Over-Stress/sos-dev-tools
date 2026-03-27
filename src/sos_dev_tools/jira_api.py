"""Jira REST API client and Markdown → ADF converter. Zero external dependencies."""

import base64
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _auth_header():
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"


def api(method, path, data=None):
    base_url = os.environ.get("JIRA_BASE_URL", "")
    if not base_url:
        print("Error: JIRA_BASE_URL not set", file=sys.stderr)
        sys.exit(1)

    url = f"{base_url}/rest/api/3{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req) as resp:
            if resp.status == 204:
                return {}
            return json.loads(resp.read())
    except HTTPError as e:
        error_body = e.read().decode()
        print(f"Error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


def get_project_key():
    return os.environ.get("JIRA_PROJECT_KEY", "RICH")


def get_base_url():
    return os.environ.get("JIRA_BASE_URL", "")


# ---------------------------------------------------------------------------
# Markdown → ADF converter
# ---------------------------------------------------------------------------

def _parse_inline(text):
    """Parse inline markdown (**bold**, `code`) into ADF text nodes."""
    nodes = []
    pattern = r"(\*\*(.+?)\*\*|`(.+?)`)"
    last_end = 0

    for match in re.finditer(pattern, text):
        if match.start() > last_end:
            plain = text[last_end : match.start()]
            if plain:
                nodes.append({"type": "text", "text": plain})

        if match.group(2):
            nodes.append({
                "type": "text",
                "text": match.group(2),
                "marks": [{"type": "strong"}],
            })
        elif match.group(3):
            nodes.append({
                "type": "text",
                "text": match.group(3),
                "marks": [{"type": "code"}],
            })

        last_end = match.end()

    if last_end < len(text):
        remaining = text[last_end:]
        if remaining:
            nodes.append({"type": "text", "text": remaining})

    return nodes if nodes else [{"type": "text", "text": text}]


def md_to_adf(text):
    """Convert markdown-formatted text to Atlassian Document Format."""
    if not text:
        return {"version": 1, "type": "doc", "content": []}

    lines = text.split("\n")
    blocks = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            blocks.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _parse_inline(heading_match.group(2)),
            })
            i += 1
            continue

        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                item_text = re.sub(r"^\s*[-*]\s+", "", lines[i])
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline(item_text)}],
                })
                i += 1
            blocks.append({"type": "bulletList", "content": items})
            continue

        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                item_text = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline(item_text)}],
                })
                i += 1
            blocks.append({"type": "orderedList", "attrs": {"order": 1}, "content": items})
            continue

        para_lines = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^#{1,6}\s+", lines[i])
            and not re.match(r"^\s*[-*]\s+", lines[i])
            and not re.match(r"^\s*\d+\.\s+", lines[i])
        ):
            para_lines.append(lines[i])
            i += 1

        if para_lines:
            blocks.append({
                "type": "paragraph",
                "content": _parse_inline(" ".join(para_lines)),
            })

    return {"version": 1, "type": "doc", "content": blocks}
