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
from .jira_api import api, md_to_adf, get_project_key, set_project_key, get_base_url, get_issue_type_id, transition_ticket, create_project


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

  create-project    Provision a new Jira project.
    key             (required) Project key (uppercase, 2-10 chars).
    name            (required) Human-readable project name.
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

    p = sub.add_parser("create-project")
    p.add_argument("--key", "-k", required=True, help="Project key (e.g. PILOT) — uppercase, 2-10 chars")
    p.add_argument("--name", "-n", required=True, help="Project name (e.g. Pilot Development)")
    p.add_argument("--type", "-t", default="software", choices=["software", "business", "service_desk"])
    p.add_argument("--template", default="scrum", choices=["scrum", "kanban", "basic"])

    args = parser.parse_args()

    # Apply project override before dispatching
    if getattr(args, "project", None):
        set_project_key(args.project)

    {
        "create": cmd_create, "edit": cmd_edit, "move": cmd_move,
        "view": cmd_view, "list": cmd_list, "comment": cmd_comment,
        "delete": cmd_delete, "sync": cmd_sync, "create-project": cmd_create_project,
    }[args.command](args)


if __name__ == "__main__":
    main()
