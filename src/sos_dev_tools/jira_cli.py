#!/usr/bin/env python3
"""sos-jira — Jira ticket management CLI.

Usage:
    sos-jira create -s "Title" [-d "description"] [--file desc.md] [-t task|epic|subtask] [-p PARENT]
    sos-jira edit TICKET [-s "New title"] [-d "New desc"] [--file desc.md]
    sos-jira move TICKET "IN PROGRESS"
    sos-jira view TICKET
    sos-jira list [--status "To Do"] [--type task]
    sos-jira comment TICKET "text"
    sos-jira delete TICKET
    sos-jira sync ops.json
    sos-jira create-project -k KEY -n "Name" [-t software|business|service_desk] [--template scrum|kanban|basic]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from .env import load_env
from .jira_api import api, agile_api, md_to_adf, get_project_key, set_project_key, get_base_url, get_issue_type_id, transition_ticket, create_project, get_status_name, move_issues_to_project, ensure_dev_workflow_scheme, assign_workflow_scheme, DEV_SCHEME_NAME, DEV_WORKFLOW_NAME, DEV_PIPELINE


def cmd_create(args):
    fields = {
        "project": {"key": get_project_key()},
        "issuetype": {"id": get_issue_type_id(args.type)},
        "summary": args.summary,
    }
    desc_text = Path(args.file).read_text() if args.file else args.description
    if desc_text:
        fields["description"] = md_to_adf(desc_text)
    if args.parent:
        fields["parent"] = {"key": args.parent}

    result = api("POST", "/issue", {"fields": fields})
    key = result["key"]
    print(f"Created {key} — {get_base_url()}/browse/{key}")


def cmd_edit(args):
    ticket = args.ticket.upper()
    fields = {}
    if args.summary:
        fields["summary"] = args.summary
    desc_text = Path(args.file).read_text() if args.file else args.description
    if desc_text:
        fields["description"] = md_to_adf(desc_text)
    if not fields:
        print("Error: provide --summary, --description, or --file", file=sys.stderr)
        sys.exit(1)
    api("PUT", f"/issue/{ticket}", {"fields": fields})
    print(f"Updated {ticket}")


def cmd_move(args):
    ticket = args.ticket.upper()
    status = args.status.upper()
    if transition_ticket(ticket, status):
        print(f"{ticket} → {status}")


def cmd_move_project(args):
    """Re-home one or more issues into another project (re-keys them, preserving
    history/comments) via Jira Cloud's bulk-move. Async under the hood."""
    tickets = [t.upper() for t in args.tickets]
    type_map = {t: args.type for t in tickets} if args.type else None
    task, remap = move_issues_to_project(
        tickets, args.to_project, type_map=type_map, notify=args.notify
    )
    status = task.get("status")
    if status != "COMPLETE":
        msg = task.get("message") or task.get("result") or ""
        print(f"Error: bulk move did not complete (status: {status}). {msg}", file=sys.stderr)
        sys.exit(1)
    dest = args.to_project.upper()
    for old in tickets:
        print(f"  {old} → {remap.get(old, '?')}")
    print(f"\n{len(tickets)} issue(s) moved to {dest}")


def cmd_view(args):
    ticket = args.ticket.upper()
    issue = api("GET", f"/issue/{ticket}")
    f = issue["fields"]
    print(f"  Key:     {issue['key']}")
    print(f"  Summary: {f['summary']}")
    print(f"  Status:  {f['status']['name']}")
    print(f"  Type:    {f['issuetype']['name']}")
    if f.get("parent"):
        print(f"  Parent:  {f['parent']['key']}")
    print(f"  URL:     {get_base_url()}/browse/{issue['key']}")

    desc = f.get("description")
    if desc and desc.get("content"):
        print("  ---")
        for block in desc["content"]:
            if block["type"] == "paragraph":
                text = "".join(n.get("text", "") for n in block.get("content", []))
                print(f"  {text}")
            elif block["type"] == "heading":
                text = "".join(n.get("text", "") for n in block.get("content", []))
                print(f"\n  ## {text}")
            elif block["type"] in ("orderedList", "bulletList"):
                for i, item in enumerate(block.get("content", []), 1):
                    for p in item.get("content", []):
                        text = "".join(n.get("text", "") for n in p.get("content", []))
                        prefix = f"  {i}." if block["type"] == "orderedList" else "  -"
                        print(f"  {prefix} {text}")


def cmd_list(args):
    pk = get_project_key()

    # Validate project exists — api() exits on 404
    api("GET", f"/project/{pk}")

    jql_parts = [f"project = {pk}"]
    if args.status:
        jql_parts.append(f'status = "{args.status}"')
    if args.type:
        jql_parts.append(f'issuetype = "{args.type}"')
    jql = " AND ".join(jql_parts) + " ORDER BY created DESC"

    result = api("POST", "/search/jql", {"jql": jql, "maxResults": 50, "fields": ["summary", "status", "issuetype", "parent"]})
    issues = result.get("issues", [])
    if not issues:
        print("No issues found.")
        return
    for issue in issues:
        f = issue["fields"]
        parent = f' (parent: {f["parent"]["key"]})' if f.get("parent") else ""
        print(f'  {issue["key"]}  [{f["status"]["name"]}]  {f["summary"]}{parent}')
    print(f"\n  {len(issues)} issue(s)")


def cmd_comment(args):
    ticket = args.ticket.upper()
    api("POST", f"/issue/{ticket}/comment", {"body": md_to_adf(args.text)})
    print(f"Comment added to {ticket}")


def cmd_delete(args):
    ticket = args.ticket.upper()
    api("DELETE", f"/issue/{ticket}")
    print(f"Deleted {ticket}")


def cmd_sync(args):
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    try:
        operations = json.loads(file_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(operations, list):
        print("Error: JSON must be an array of operations", file=sys.stderr)
        sys.exit(1)

    total = len(operations)
    succeeded = []
    failed = []

    for i, op in enumerate(operations):
        action = op.get("action", "")
        try:
            if action == "create":
                fields = {
                    "project": {"key": op.get("project", get_project_key())},
                    "issuetype": {"id": get_issue_type_id(op.get("type", "task"))},
                    "summary": op["summary"],
                }
                if op.get("description"):
                    fields["description"] = md_to_adf(op["description"])
                if op.get("parent"):
                    fields["parent"] = {"key": op["parent"]}
                result = api("POST", "/issue", {"fields": fields})
                succeeded.append(f"create {result['key']}")

            elif action == "update":
                ticket = op["ticket"].upper()
                fields = {}
                if "summary" in op:
                    fields["summary"] = op["summary"]
                if "description" in op:
                    fields["description"] = md_to_adf(op["description"])
                if not fields:
                    failed.append((f"update {ticket}", "no fields to update"))
                    continue
                api("PUT", f"/issue/{ticket}", {"fields": fields})
                succeeded.append(f"update {ticket}")

            elif action == "delete":
                ticket = op["ticket"].upper()
                api("DELETE", f"/issue/{ticket}")
                succeeded.append(f"delete {ticket}")

            elif action == "create-project":
                result = create_project(
                    key=op["key"],
                    name=op["name"],
                    project_type=op.get("type", "software"),
                    template=op.get("template", "scrum"),
                )
                succeeded.append(f"create-project {result.get('key', op['key'].upper())}")

            else:
                failed.append((f"op #{i + 1}", f"unknown action '{action}'"))

        except SystemExit:
            label = op.get("ticket", op.get("key", op.get("summary", f"op #{i + 1}")))
            failed.append((f"{action} {label}", "API error (see above)"))
        except KeyError as e:
            failed.append((f"op #{i + 1}", f"missing field {e}"))

    ok = len(succeeded)
    print(f"\n{ok}/{total} operations completed successfully")

    if failed:
        print(f"\nSucceeded ({ok}):")
        for s in succeeded:
            print(f"  {s}")
        print(f"\nFailed ({len(failed)}):")
        for label, reason in failed:
            print(f"  {label} -- {reason}")


def cmd_create_project(args):
    result = create_project(
        key=args.key,
        name=args.name,
        project_type=args.type,
        template=args.template,
    )
    project_id = result.get("id", "")
    key = result.get("key", args.key.upper())
    print(f"Created project {key} (id: {project_id}) — {get_base_url()}/projects/{key}")


def cmd_provision_dev_workflow(args):
    """Idempotently (re)provision the canonical dev workflow scheme, and
    optionally assign it to an existing project with --assign KEY."""
    pipeline = " → ".join(n for n, _ in DEV_PIPELINE)
    print(f"Provisioning '{DEV_SCHEME_NAME}' → {pipeline}")
    scheme_id = ensure_dev_workflow_scheme()
    print(f"  scheme id: {scheme_id}  (workflow: '{DEV_WORKFLOW_NAME}')")
    if args.assign:
        key = args.assign.upper()
        proj = api("GET", f"/project/{key}")
        if assign_workflow_scheme(proj["id"], scheme_id):
            print(f"  ✓ assigned to {key}")
        else:
            sys.exit(1)
    else:
        print("  (new projects created via `sos-jira create-project` get this "
              "automatically; use --assign KEY to apply to an existing project)")


def cmd_promote(args):
    """Move tickets from backlog to the 'ready' status (e.g., Selected for Development)."""
    target_status = get_status_name("ready")
    tickets = [t.upper() for t in args.tickets]

    for ticket in tickets:
        transition_ticket(ticket, target_status.upper())
        print(f"  {ticket} → {target_status}")

    print(f"\n{len(tickets)} ticket(s) promoted to '{target_status}'")


def _get_board_id():
    """Find the board for the current project."""
    project_key = get_project_key()
    data = agile_api("GET", f"/board?projectKeyOrId={project_key}")
    boards = data.get("values", [])
    if not boards:
        print(f"Error: no board found for project {project_key}", file=sys.stderr)
        sys.exit(1)
    return boards[0]["id"]


def _get_active_sprint(board_id):
    """Get the active sprint for a board."""
    data = agile_api("GET", f"/board/{board_id}/sprint?state=active")
    sprints = data.get("values", [])
    if not sprints:
        return None
    return sprints[0]


def cmd_sprint(args):
    board_id = _get_board_id()

    if args.sprint_action == "list":
        data = agile_api("GET", f"/board/{board_id}/sprint?state=active,future")
        sprints = data.get("values", [])
        if not sprints:
            print("No active or future sprints found.")
            return
        for s in sprints:
            print(f"  {s['id']}  [{s['state'].upper()}]  {s['name']}")

    elif args.sprint_action == "active":
        sprint = _get_active_sprint(board_id)
        if not sprint:
            print("No active sprint.")
            return
        print(f"  {sprint['id']}  {sprint['name']}  ({sprint['state']})")
        # List issues in this sprint
        issues = agile_api("GET", f"/sprint/{sprint['id']}/issue")
        for issue in issues.get("issues", []):
            key = issue["key"]
            summary = issue["fields"]["summary"]
            status = issue["fields"]["status"]["name"]
            print(f"    {key}  [{status}]  {summary}")

    elif args.sprint_action == "move":
        if not args.tickets:
            print("Error: provide ticket IDs to move", file=sys.stderr)
            sys.exit(1)

        # Find target sprint
        sprint = _get_active_sprint(board_id)
        if not sprint:
            # Try future sprints
            data = agile_api("GET", f"/board/{board_id}/sprint?state=future")
            future = data.get("values", [])
            if future:
                sprint = future[0]
            else:
                print("Error: no active or future sprint found.", file=sys.stderr)
                sys.exit(1)

        issue_keys = [t.upper() for t in args.tickets]
        agile_api("POST", f"/sprint/{sprint['id']}/issue", {"issues": issue_keys})
        print(f"Moved {len(issue_keys)} issue(s) to sprint '{sprint['name']}':")
        for k in issue_keys:
            print(f"  {k}")


def main():
    load_env()

    parser = argparse.ArgumentParser(description="sos-jira — Jira ticket management")
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared --project flag for all subcommands that operate within a project
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--project", "-P", default=None,
                      help="Jira project key (overrides JIRA_PROJECT_KEY env var)")

    p = sub.add_parser("create", parents=[proj])
    p.add_argument("--summary", "-s", required=True)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--file", "-f", default=None)
    p.add_argument("--type", "-t", default="task", type=str.lower)
    p.add_argument("--parent", "-p", default=None)

    p = sub.add_parser("edit", parents=[proj])
    p.add_argument("ticket")
    p.add_argument("--summary", "-s", default=None)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser("move", parents=[proj])
    p.add_argument("ticket")
    p.add_argument("status")

    p = sub.add_parser("view", parents=[proj])
    p.add_argument("ticket")

    p = sub.add_parser("list", parents=[proj])
    p.add_argument("--status", default=None)
    p.add_argument("--type", default=None)

    p = sub.add_parser("comment", parents=[proj])
    p.add_argument("ticket")
    p.add_argument("text")

    p = sub.add_parser("delete", parents=[proj])
    p.add_argument("ticket")

    sync_epilog = """\
JSON schema — the file must contain an array of operation objects. Each object
must have an "action" field; other fields depend on the action.

Supported actions:

  create            Create a new issue.
    summary         (required) Issue title.
    type            (optional) "task" | "epic" | "story" | "bug" | "subtask".
                    Defaults to "task".
    description     (optional) Markdown; auto-converted to Atlassian Document
                    Format. Headings, bold, code, bullet/ordered lists supported.
    parent          (optional) Parent ticket key (e.g. "PROJ-1"). Used for
                    epic linking or subtask parents.
    project         (optional) Project key override. Defaults to --project/-P
                    or $JIRA_PROJECT_KEY.

  update            Update an existing issue.
    ticket          (required) Ticket key to update.
    summary         (optional) New title.
    description     (optional) New description (Markdown → ADF).

  delete            Delete an existing issue.
    ticket          (required) Ticket key to delete.

  create-project    Provision a new Jira project. Automatically assigned the
                    canonical dev workflow scheme (BACKLOG → READY FOR DEV →
                    IN PROGRESS → IN REVIEW → IN QA → DONE); set
                    JIRA_SKIP_DEV_WORKFLOW=1 to opt out.
    key             (required) Project key (uppercase, 2-10 chars).
    name            (required) Human-readable project name.

  provision-dev-workflow  (Re)provision the canonical dev workflow scheme
                    (idempotent). --assign KEY also applies it to an existing
                    project.
    type            (optional) "software" | "business" | "service_desk".
                    Defaults to "software".
    template        (optional) "scrum" | "kanban" | "basic".
                    Defaults to "scrum".

Example ops.json:

  [
    {
      "action": "create",
      "type": "epic",
      "summary": "Q2 platform work",
      "description": "# Goals\\n\\n- Ship X\\n- Retire Y"
    },
    {
      "action": "create",
      "type": "task",
      "parent": "PROJ-1",
      "summary": "Extract shared Reveal component",
      "description": "Move the scroll-reveal wrapper into @sos/ui."
    },
    {
      "action": "update",
      "ticket": "PROJ-42",
      "summary": "Refined title"
    },
    {
      "action": "delete",
      "ticket": "PROJ-99"
    }
  ]

The command processes operations in order, continuing past individual failures
and reporting a summary at the end (succeeded vs. failed). A non-existent file,
malformed JSON, or a non-array root is a hard failure before any API calls.
"""
    p = sub.add_parser(
        "sync",
        parents=[proj],
        help="Batch create/update/delete tickets from a JSON operations file",
        description=(
            "Batch-apply Jira ticket operations (create, update, delete, "
            "create-project) from a JSON file. Designed for bulk backlog "
            "authoring and programmatic ticket generation."
        ),
        epilog=sync_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", help="Path to JSON file containing an array of operation objects (see below)")

    p = sub.add_parser(
        "move-project",
        help="Re-home issue(s) into another project (re-keys them, keeps history)",
        description=(
            "Move one or more issues into a different project via Jira Cloud's "
            "bulk-move. This RE-KEYS the issues (e.g. INFRA-108 → WEGUUD-71) while "
            "preserving their history, comments, and links — unlike a create+delete. "
            "Each issue keeps its own type unless --type forces one."
        ),
    )
    p.add_argument("tickets", nargs="+", help="Issue key(s) to move")
    p.add_argument("--to-project", "-T", required=True, help="Destination project key (e.g. WEGUUD)")
    p.add_argument("--type", "-t", default=None, help="Force a target issue type for all tickets (default: keep each issue's own type)")
    p.add_argument("--notify", action="store_true", help="Send bulk-move notifications (default: off)")

    p = sub.add_parser("promote", parents=[proj])
    p.add_argument("tickets", nargs="+", help="Ticket IDs to promote from backlog to ready")

    p = sub.add_parser("sprint", parents=[proj])
    p.add_argument("sprint_action", choices=["list", "active", "move"])
    p.add_argument("tickets", nargs="*", help="Ticket IDs to move (for 'move' action)")

    p = sub.add_parser("create-project")
    p.add_argument("--key", "-k", required=True, help="Project key (e.g. PILOT) — uppercase, 2-10 chars")
    p.add_argument("--name", "-n", required=True, help="Project name (e.g. Pilot Development)")
    p.add_argument("--type", "-t", default="software", choices=["software", "business", "service_desk"])
    p.add_argument("--template", default="scrum", choices=["scrum", "kanban", "basic"])

    p = sub.add_parser("provision-dev-workflow",
                       help="Idempotently (re)provision the canonical BACKLOG→READY FOR DEV→IN PROGRESS→IN REVIEW→IN QA→DONE scheme")
    p.add_argument("--assign", metavar="KEY", default=None,
                   help="Also assign the scheme to an existing project (e.g. --assign INFRA)")

    args = parser.parse_args()

    # Apply project override before dispatching
    if getattr(args, "project", None):
        set_project_key(args.project)

    {
        "create": cmd_create, "edit": cmd_edit, "move": cmd_move,
        "move-project": cmd_move_project,
        "view": cmd_view, "list": cmd_list, "comment": cmd_comment,
        "delete": cmd_delete, "sync": cmd_sync, "promote": cmd_promote,
        "sprint": cmd_sprint, "create-project": cmd_create_project,
        "provision-dev-workflow": cmd_provision_dev_workflow,
    }[args.command](args)


if __name__ == "__main__":
    main()
