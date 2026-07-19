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


def agile_api(method, path, data=None):
    """Call the Jira Agile REST API (/rest/agile/1.0)."""
    base_url = os.environ.get("JIRA_BASE_URL", "")
    if not base_url:
        print("Error: JIRA_BASE_URL not set", file=sys.stderr)
        sys.exit(1)

    url = f"{base_url}/rest/agile/1.0{path}"
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
    config = load_jira_config()
    if config and config.get("project_key"):
        return config["project_key"]
    return os.environ.get("JIRA_PROJECT_KEY", "RICH")


def set_project_key(key: str):
    """Override the project key for this session."""
    os.environ["JIRA_PROJECT_KEY"] = key.upper()


def load_jira_config():
    """Load .jira.json from the current directory or parents."""
    cwd = Path.cwd()
    for d in [cwd] + list(cwd.parents):
        config_path = d / ".jira.json"
        if config_path.exists():
            try:
                return json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
    return None


# Default alias → status name map for the canonical SOS dev pipeline. A repo's
# .jira.json "statuses" block overrides any of these per-project.
_DEFAULT_STATUS_ALIASES = {
    "backlog": "BACKLOG",
    "ready": "READY FOR DEV",
    "in_progress": "IN PROGRESS",
    "in_review": "IN REVIEW",
    "in_qa": "IN QA",
    "done": "DONE",
}


def get_status_name(alias):
    """Resolve a status alias (backlog, ready, in_progress, in_review, in_qa, done)
    to the actual Jira status name. A repo's .jira.json overrides the defaults."""
    config = load_jira_config()
    if config and "statuses" in config:
        statuses = config["statuses"]
        if alias in statuses:
            return statuses[alias]
    return _DEFAULT_STATUS_ALIASES.get(alias, alias)


def create_project(key, name, project_type="software", template="scrum"):
    """Create a new Jira project.

    Args:
        key: Project key (e.g. "PILOT") — uppercase, 2-10 chars
        name: Project name (e.g. "Pilot Development")
        project_type: "software", "business", or "service_desk"
        template: "scrum", "kanban", or "basic"
    """
    # Get the current user's account ID to set as lead
    myself = api("GET", "/myself")
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

    # Guarantee the review + QA pipeline on every new project by assigning the
    # shared canonical workflow scheme (idempotently provisioned). A brand-new
    # project has no issues, so the association applies cleanly and synchronously.
    # Opt out with JIRA_SKIP_DEV_WORKFLOW=1. Never fail creation over this.
    if os.environ.get("JIRA_SKIP_DEV_WORKFLOW") != "1" and result.get("id"):
        try:
            scheme_id = ensure_dev_workflow_scheme()
            if assign_workflow_scheme(result["id"], scheme_id):
                print(f"  ✓ applied '{DEV_SCHEME_NAME}' "
                      f"({' → '.join(n for n, _ in DEV_PIPELINE)})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — scheme is best-effort
            print(f"Warning: dev workflow scheme not applied to {data['key']}: {e}",
                  file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# Canonical dev workflow scheme
# ---------------------------------------------------------------------------
# Every project created via create_project() is assigned one shared workflow
# scheme so the review + QA stages are guaranteed and statuses never drift
# project-to-project. Pipeline (open / any-to-any transitions):
#   BACKLOG → READY FOR DEV → IN PROGRESS → IN REVIEW → IN QA → DONE

DEV_WORKFLOW_NAME = "SOS Dev Workflow"
DEV_SCHEME_NAME = "SOS Dev Workflow Scheme"
# (status name, status category) in pipeline order
DEV_PIPELINE = [
    ("BACKLOG", "TODO"),
    ("READY FOR DEV", "TODO"),
    ("IN PROGRESS", "IN_PROGRESS"),
    ("IN REVIEW", "IN_PROGRESS"),
    ("IN QA", "IN_PROGRESS"),
    ("DONE", "DONE"),
]


def _api_raw(method, path, data=None):
    """Like api() but returns (status_code, parsed_body) and never sys.exit()s —
    provisioning must inspect validation/error bodies rather than abort."""
    base_url = os.environ.get("JIRA_BASE_URL", "")
    if not base_url:
        return 0, {"error": "JIRA_BASE_URL not set"}
    url = f"{base_url}/rest/api/3{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def _ensure_dev_statuses():
    """Find-or-create the pipeline statuses globally. Returns {name: id}.

    Reuses any existing global status whose name matches case-insensitively so
    cross-project reporting stays coherent; only genuinely-missing statuses are
    created."""
    existing, start = {}, 0
    while True:
        _, d = _api_raw("GET", f"/statuses/search?maxResults=200&startAt={start}")
        for s in d.get("values", []):
            existing[s["name"].lower()] = s
        if d.get("isLast", True):
            break
        start += 200
    ids = {}
    for name, cat in DEV_PIPELINE:
        hit = existing.get(name.lower())
        if hit:
            ids[name] = hit["id"]
            continue
        st, d = _api_raw("POST", "/statuses", {
            "scope": {"type": "GLOBAL"},
            "statuses": [{"name": name, "statusCategory": cat,
                          "description": f"{name} (SOS dev pipeline)"}],
        })
        if st not in (200, 201):
            raise RuntimeError(f"could not create status {name!r}: {st} {d}")
        ids[name] = d[0]["id"] if isinstance(d, list) else d["statuses"][0]["id"]
    return ids


def _ensure_dev_workflow(status_ids):
    """Find-or-create the shared workflow. Returns the workflow name."""
    import uuid

    _, d = _api_raw("POST", "/workflows", {"workflowNames": [DEV_WORKFLOW_NAME]})
    if any(w.get("name") == DEV_WORKFLOW_NAME for w in d.get("workflows", [])):
        return DEV_WORKFLOW_NAME

    # The create API links its statuses/transitions by a client-generated UUID
    # (`statusReference`); existing global statuses are bound via their real id.
    order = [n for n, _ in DEV_PIPELINE]
    ref = {n: str(uuid.uuid4()) for n in order}
    transitions = [{"id": "1", "name": "Create", "type": "INITIAL",
                    "toStatusReference": ref["BACKLOG"]}]
    for i, n in enumerate(order):
        transitions.append({"id": str((i + 1) * 10 + 1), "name": n.title(),
                            "type": "GLOBAL", "toStatusReference": ref[n]})
    payload = {
        "scope": {"type": "GLOBAL"},
        "statuses": [{"id": status_ids[n], "statusReference": ref[n],
                      "name": n, "statusCategory": cat} for n, cat in DEV_PIPELINE],
        "workflows": [{
            "name": DEV_WORKFLOW_NAME,
            "description": ("Canonical SOS dev pipeline "
                           "(Backlog -> Ready for Dev -> In Progress -> In Review "
                           "-> In QA -> Done); open transitions."),
            "statuses": [{"statusReference": ref[n],
                          "layout": {"x": float(i * 180), "y": 0.0}}
                         for i, n in enumerate(order)],
            "transitions": transitions,
        }],
    }
    st, d = _api_raw("POST", "/workflows/create", payload)
    if st not in (200, 201):
        raise RuntimeError(f"could not create workflow: {st} {d}")
    return DEV_WORKFLOW_NAME


def _ensure_dev_scheme(workflow_name):
    """Find-or-create the workflow scheme. Returns its id."""
    _, d = _api_raw("GET", "/workflowscheme?maxResults=200")
    for s in d.get("values", []):
        if s.get("name") == DEV_SCHEME_NAME:
            return s["id"]
    st, d = _api_raw("POST", "/workflowscheme", {
        "name": DEV_SCHEME_NAME,
        "description": "All issue types use SOS Dev Workflow.",
        "defaultWorkflow": workflow_name,
    })
    if st not in (200, 201):
        raise RuntimeError(f"could not create workflow scheme: {st} {d}")
    return d["id"]


def ensure_dev_workflow_scheme():
    """Idempotently provision the canonical dev workflow scheme; return its id.

    Set JIRA_DEV_WORKFLOW_SCHEME_ID to short-circuit the lookup/provisioning
    (e.g. a different Jira instance with a pre-built scheme)."""
    override = os.environ.get("JIRA_DEV_WORKFLOW_SCHEME_ID")
    if override:
        return override
    status_ids = _ensure_dev_statuses()
    workflow_name = _ensure_dev_workflow(status_ids)
    return _ensure_dev_scheme(workflow_name)


def assign_workflow_scheme(project_id, scheme_id):
    """Assign a workflow scheme to a project. Non-fatal — warns and returns False
    on failure. On a new (issue-less) project the change applies synchronously."""
    st, d = _api_raw("PUT", "/workflowscheme/project",
                     {"workflowSchemeId": str(scheme_id), "projectId": str(project_id)})
    if st not in (200, 204):
        print(f"Warning: could not assign workflow scheme {scheme_id} to project "
              f"{project_id}: {st} {d}", file=sys.stderr)
        return False
    return True


def get_base_url():
    return os.environ.get("JIRA_BASE_URL", "")


# ---------------------------------------------------------------------------
# Auto-discovery — issue types and transitions from the Jira API
# ---------------------------------------------------------------------------

_issue_type_cache: dict[str, dict[str, str]] = {}   # project_key → {type_name → id}
_transition_cache: dict[str, dict[str, str]] = {}   # ticket_key → {status_name → id}
_CACHE_TTL = 86400  # 24 hours


def _cache_file() -> Path:
    """Cache file lives next to the nearest .env (project root)."""
    from .env import find_env
    env_path = find_env()
    if env_path:
        return env_path.parent / ".jira-cache.json"
    return Path.cwd() / ".jira-cache.json"


def _cache_key() -> str:
    """Cache key combines instance URL and project key so each project has its own entry."""
    base_url = os.environ.get("JIRA_BASE_URL", "")
    return f"{base_url}:{get_project_key()}"


def _load_disk_cache() -> dict | None:
    """Load cached issue types from disk if fresh. Returns dict or None."""
    path = _cache_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        entry = data.get(_cache_key())
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > _CACHE_TTL:
            return None
        return entry["types"]
    except (json.JSONDecodeError, KeyError):
        return None


def _save_disk_cache(types: dict) -> None:
    """Persist issue types to disk, keyed by Jira instance URL + project key."""
    path = _cache_file()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    data[_cache_key()] = {"types": types, "ts": time.time()}
    path.write_text(json.dumps(data, indent=2))


def get_issue_types() -> dict[str, str]:
    """Discover issue type name → id mapping for the current project.

    Priority: env overrides → in-memory cache → disk cache → Jira API.
    """
    pk = get_project_key()

    # Check in-memory cache (keyed by project)
    if pk in _issue_type_cache:
        return _issue_type_cache[pk]

    # Check .env overrides
    env_types = {}
    for name in ("task", "epic", "subtask", "story", "bug"):
        env_key = f"JIRA_ISSUE_TYPE_{name.upper()}"
        val = os.environ.get(env_key)
        if val:
            env_types[name] = val
    if env_types:
        _issue_type_cache[pk] = env_types
        return env_types

    # Check disk cache
    cached = _load_disk_cache()
    if cached:
        _issue_type_cache[pk] = cached
        return cached

    # Auto-discover from Jira API
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

        _issue_type_cache[pk] = types
        _save_disk_cache(types)
        return types
    except SystemExit:
        pass

    # Last resort: try to get types from /issuetype
    try:
        all_types = api("GET", "/issuetype")
        types = {}
        for it in all_types:
            name = it["name"].lower().replace(" ", "")
            types[name] = str(it["id"])
        _issue_type_cache[pk] = types
        _save_disk_cache(types)
        return types
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
    # Check in-memory cache (keyed by ticket)
    if ticket_key in _transition_cache:
        return _transition_cache[ticket_key]

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
        _transition_cache[ticket_key] = env_transitions
        return env_transitions

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

    _transition_cache[ticket_key] = transitions
    return transitions


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
# Cross-project move (re-home) — Jira Cloud bulk-move API
# ---------------------------------------------------------------------------

def get_project_id(project_key: str) -> str:
    """Resolve a project key to its numeric id."""
    return str(api("GET", f"/project/{project_key.upper()}")["id"])


def get_issue_type_id_for_project(project_key: str, type_name: str) -> str:
    """Resolve an issue type NAME → id within a SPECIFIC project (not the session
    default) — needed when moving into a different project than the CLI's default.
    """
    meta = api("GET", f"/issue/createmeta?projectKeys={project_key.upper()}&expand=projects.issuetypes")
    norm = type_name.lower().replace(" ", "")
    for project in meta.get("projects", []):
        for it in project.get("issuetypes", []):
            if it["name"].lower().replace(" ", "") == norm:
                return str(it["id"])
    available = ", ".join(
        it["name"] for p in meta.get("projects", []) for it in p.get("issuetypes", [])
    )
    print(f"Error: issue type '{type_name}' not found in project {project_key}. "
          f"Available: {available}", file=sys.stderr)
    sys.exit(1)


def _poll_task(task_id: str, timeout: int = 180, interval: int = 2) -> dict:
    """Poll a Jira async task until it reaches a terminal state. Returns the task JSON
    (status TIMEOUT if it never settles within `timeout` seconds)."""
    waited = 0
    while waited < timeout:
        task = api("GET", f"/task/{task_id}")
        if task.get("status") in ("COMPLETE", "FAILED", "CANCELLED", "DEAD"):
            return task
        time.sleep(interval)
        waited += interval
    return {"status": "TIMEOUT"}


def move_issues_to_project(issue_keys, target_project_key, type_map=None, notify=False):
    """Re-home issues into another project via Jira Cloud's bulk-move endpoint.

    Unlike a create-and-delete, this RE-KEYS the existing issues (e.g. INFRA-108 →
    WEGUUD-71) while preserving their history, comments, and links. The move is
    async: it submits the bulk-move, polls the task to completion, then reports the
    new key for each issue (the old key resolves to the new one after the move).

    Args:
        issue_keys: source issue keys.
        target_project_key: destination project key.
        type_map: optional {ISSUE_KEY: target_type_name}. When absent, each issue
            keeps its own type name, mapped to the target project's type id.
        notify: send bulk-move notifications (default False).

    Returns:
        (task_json, {old_key: new_key}).
    """
    target_project_key = target_project_key.upper()
    project_id = get_project_id(target_project_key)
    type_map = {k.upper(): v for k, v in (type_map or {}).items()}

    # Group issues by their resolved target issue-type id — the bulk-move payload
    # keys each source group by "<targetProjectId>,<targetIssueTypeId>".
    groups: dict[str, list[str]] = {}
    type_id_by_name: dict[str, str] = {}
    for key in issue_keys:
        key = key.upper()
        type_name = type_map.get(key)
        if not type_name:
            type_name = api("GET", f"/issue/{key}?fields=issuetype")["fields"]["issuetype"]["name"]
        if type_name not in type_id_by_name:
            type_id_by_name[type_name] = get_issue_type_id_for_project(target_project_key, type_name)
        groups.setdefault(type_id_by_name[type_name], []).append(key)

    mapping = {
        f"{project_id},{type_id}": {
            "inferClassificationDefaults": True,
            "inferFieldDefaults": True,
            "inferStatusDefaults": True,
            "inferSubtaskTypeDefault": True,
            "issueIdsOrKeys": keys,
        }
        for type_id, keys in groups.items()
    }

    resp = api("POST", "/bulk/issues/move", {
        "sendBulkNotification": bool(notify),
        "targetToSourcesMapping": mapping,
    })
    task_id = resp.get("taskId")
    if not task_id:
        return resp, {}

    task = _poll_task(task_id)
    remap: dict[str, str] = {}
    if task.get("status") == "COMPLETE":
        for key in issue_keys:
            info = api("GET", f"/issue/{key.upper()}?fields=key")
            remap[key.upper()] = info.get("key", key.upper())
    return task, remap


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
