"""Jira REST API client and Markdown → ADF converter. Zero external dependencies."""

import base64
import json
import os
import re
import sys
import time
from pathlib import Path
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


def create_project(key, name, project_type="software", template="scrum"):
    """Create a new Jira project.

    Args:
        key: Project key (e.g. "PILOT") — uppercase, 2-10 chars
        name: Project name (e.g. "Pilot Development")
        project_type: "software", "business", or "service_desk"
        template: "scrum", "kanban", or "basic"
    """
    # Get the current user's account ID to set as lead
    myself = api("GET", "/../myself")  # /rest/api/3/../myself = /rest/api/2/myself workaround
    lead_account_id = myself.get("accountId", "")

    # Map template to Jira's project template key
    template_keys = {
        "scrum": "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic",
        "kanban": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
        "basic": "com.pyxis.greenhopper.jira:gh-simplified-basic",
    }
    template_key = template_keys.get(template, template_keys["scrum"])

    # Map project type
    type_keys = {
        "software": "software",
        "business": "business",
        "service_desk": "service_desk",
    }

    data = {
        "key": key.upper(),
        "name": name,
        "projectTypeKey": type_keys.get(project_type, "software"),
        "projectTemplateKey": template_key,
        "leadAccountId": lead_account_id,
    }

    result = api("POST", "/project", data)
    return result


def get_base_url():
    return os.environ.get("JIRA_BASE_URL", "")


# ---------------------------------------------------------------------------
# Auto-discovery — issue types and transitions from the Jira API
# ---------------------------------------------------------------------------

_issue_type_cache: dict | None = None
_transition_cache: dict | None = None
_CACHE_TTL = 86400  # 24 hours


def _cache_file() -> Path:
    """Cache file lives next to the nearest .env (project root)."""
    from .env import find_env
    env_path = find_env()
    if env_path:
        return env_path.parent / ".jira-cache.json"
    return Path.cwd() / ".jira-cache.json"


def _load_disk_cache() -> dict | None:
    """Load cached issue types from disk if fresh. Returns dict or None."""
    path = _cache_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        base_url = os.environ.get("JIRA_BASE_URL", "")
        entry = data.get(base_url)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > _CACHE_TTL:
            return None
        return entry["types"]
    except (json.JSONDecodeError, KeyError):
        return None


def _save_disk_cache(types: dict) -> None:
    """Persist issue types to disk, keyed by Jira instance URL."""
    path = _cache_file()
    base_url = os.environ.get("JIRA_BASE_URL", "")
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    data[base_url] = {"types": types, "ts": time.time()}
    path.write_text(json.dumps(data, indent=2))


def get_issue_types() -> dict[str, str]:
    """Discover issue type name → id mapping for the current project.

    Priority: env overrides → disk cache → Jira API.
    """
    global _issue_type_cache
    if _issue_type_cache is not None:
        return _issue_type_cache

    # Check .env overrides
    env_types = {}
    for name in ("task", "epic", "subtask", "story", "bug"):
        env_key = f"JIRA_ISSUE_TYPE_{name.upper()}"
        val = os.environ.get(env_key)
        if val:
            env_types[name] = val
    if env_types:
        _issue_type_cache = env_types
        return _issue_type_cache

    # Check disk cache
    cached = _load_disk_cache()
    if cached:
        _issue_type_cache = cached
        return _issue_type_cache

    # Auto-discover from Jira API
    pk = get_project_key()
    try:
        data = api("GET", f"/project/{pk}/statuses")
        types = {}
        for issuetype in data:
            name = issuetype["name"].lower().replace(" ", "")
            types[name] = issuetype["id"] if "id" in issuetype else str(issuetype.get("id", ""))

        # Also try createmeta for more reliable type IDs
        meta = api("GET", f"/issue/createmeta?projectKeys={pk}&expand=projects.issuetypes")
        for project in meta.get("projects", []):
            for it in project.get("issuetypes", []):
                name = it["name"].lower().replace(" ", "")
                types[name] = str(it["id"])

        _issue_type_cache = types
        _save_disk_cache(types)
        return _issue_type_cache
    except SystemExit:
        pass

    # Last resort: try to get types from /issuetype
    try:
        all_types = api("GET", "/issuetype")
        types = {}
        for it in all_types:
            name = it["name"].lower().replace(" ", "")
            types[name] = str(it["id"])
        _issue_type_cache = types
        _save_disk_cache(types)
        return _issue_type_cache
    except SystemExit:
        print("Error: could not discover issue types. Set JIRA_ISSUE_TYPE_TASK etc. in .env", file=sys.stderr)
        sys.exit(1)


def get_issue_type_id(name: str) -> str:
    """Get the issue type ID for a given name (case-insensitive)."""
    name = name.lower().replace(" ", "")
    types = get_issue_types()
    if name in types:
        return types[name]
    # Try fuzzy match
    for key, val in types.items():
        if name in key or key in name:
            return val
    available = ", ".join(sorted(types.keys()))
    print(f"Error: issue type '{name}' not found. Available: {available}", file=sys.stderr)
    sys.exit(1)


def get_transitions(ticket_key: str) -> dict[str, str]:
    """Discover available transitions for a ticket. Returns name → id mapping."""
    global _transition_cache
    if _transition_cache is not None:
        return _transition_cache

    # Check .env overrides
    env_transitions = {}
    for name, env_key in [
        ("TO DO", "JIRA_TRANSITION_TODO"),
        ("IN PROGRESS", "JIRA_TRANSITION_IN_PROGRESS"),
        ("IN REVIEW", "JIRA_TRANSITION_IN_REVIEW"),
        ("DONE", "JIRA_TRANSITION_DONE"),
    ]:
        val = os.environ.get(env_key)
        if val:
            env_transitions[name] = val
    if env_transitions:
        _transition_cache = env_transitions
        return _transition_cache

    # Auto-discover from Jira API
    data = api("GET", f"/issue/{ticket_key}/transitions")
    transitions = {}
    for t in data.get("transitions", []):
        name = t["to"]["name"].upper()
        transitions[name] = str(t["id"])
        # Also store the transition name itself for flexibility
        t_name = t["name"].upper()
        if t_name not in transitions:
            transitions[t_name] = str(t["id"])

    _transition_cache = transitions
    return _transition_cache


def transition_ticket(ticket_key: str, status: str) -> bool:
    """Transition a ticket to a new status. Returns True if successful."""
    status = status.upper()
    transitions = get_transitions(ticket_key)

    # Exact match
    if status in transitions:
        api("POST", f"/issue/{ticket_key}/transitions", {"transition": {"id": transitions[status]}})
        return True

    # Fuzzy match
    for name, tid in transitions.items():
        if status in name or name in status:
            api("POST", f"/issue/{ticket_key}/transitions", {"transition": {"id": tid}})
            return True

    available = ", ".join(f'"{k}"' for k in transitions.keys())
    print(f"Error: status '{status}' not found. Available: {available}", file=sys.stderr)
    return False


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
