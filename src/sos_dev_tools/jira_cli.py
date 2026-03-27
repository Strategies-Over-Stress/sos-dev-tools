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
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from .env import load_env
from .jira_api import api, md_to_adf, get_project_key, get_base_url


ISSUE_TYPES = {"task": "10122", "epic": "10123", "subtask": "10124"}
TRANSITIONS = {"TO DO": "11", "IN PROGRESS": "21", "IN REVIEW": "2", "DONE": "31"}


def cmd_create(args):
    fields = {
        "project": {"key": get_project_key()},
        "issuetype": {"id": ISSUE_TYPES[args.type]},
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
    if status not in TRANSITIONS:
        print(f"Error: unknown status. Valid: {', '.join(TRANSITIONS)}", file=sys.stderr)
        sys.exit(1)
    api("POST", f"/issue/{ticket}/transitions", {"transition": {"id": TRANSITIONS[status]}})
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


def main():
    load_env()

    parser = argparse.ArgumentParser(description="sos-jira — Jira ticket management")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create")
    p.add_argument("--summary", "-s", required=True)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--file", "-f", default=None)
    p.add_argument("--type", "-t", default="task", choices=ISSUE_TYPES.keys())
    p.add_argument("--parent", "-p", default=None)

    p = sub.add_parser("edit")
    p.add_argument("ticket")
    p.add_argument("--summary", "-s", default=None)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--file", "-f", default=None)

    p = sub.add_parser("move")
    p.add_argument("ticket")
    p.add_argument("status")

    p = sub.add_parser("view")
    p.add_argument("ticket")

    p = sub.add_parser("list")
    p.add_argument("--status", default=None)
    p.add_argument("--type", default=None)

    p = sub.add_parser("comment")
    p.add_argument("ticket")
    p.add_argument("text")

    p = sub.add_parser("delete")
    p.add_argument("ticket")

    args = parser.parse_args()
    {
        "create": cmd_create, "edit": cmd_edit, "move": cmd_move,
        "view": cmd_view, "list": cmd_list, "comment": cmd_comment,
        "delete": cmd_delete,
    }[args.command](args)


if __name__ == "__main__":
    main()
