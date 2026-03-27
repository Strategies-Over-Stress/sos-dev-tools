#!/usr/bin/env python3
"""sos-feature — Git + Jira feature branch lifecycle.

Usage:
    sos-feature create "Title" [-d "desc"] [-f file.md] [-t task] [-p PARENT]
    sos-feature start TICKET
    sos-feature switch TICKET
    sos-feature pr [--title "..."] [--body "..."]
    sos-feature status
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .env import load_env
from .jira_api import api, md_to_adf, get_project_key, get_base_url, get_issue_type_id, transition_ticket


def git(*args):
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def gh(*args):
    result = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD")


def resolve_ticket(ref):
    ref = ref.strip().upper()
    if re.match(r"^\d+$", ref):
        return f"{get_project_key()}-{ref}"
    return ref


def ticket_from_branch(branch=None):
    branch = branch or current_branch()
    match = re.match(rf"feature/({get_project_key()}-\d+)", branch)
    return match.group(1) if match else None


def slugify(text):
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")[:50]


def transition(ticket_key, status):
    transition_ticket(ticket_key, status)


def cmd_create(args):
    desc_text = Path(args.file).read_text() if args.file else args.description

    fields = {
        "project": {"key": get_project_key()},
        "issuetype": {"id": get_issue_type_id(args.type)},
        "summary": args.summary,
    }
    if desc_text:
        fields["description"] = md_to_adf(desc_text)
    if args.parent:
        fields["parent"] = {"key": args.parent}

    result = api("POST", "/issue", {"fields": fields})
    ticket_key = result["key"]
    print(f"Created {ticket_key} — {get_base_url()}/browse/{ticket_key}")

    slug = slugify(args.summary)
    branch_name = f"feature/{ticket_key}-{slug}"
    git("branch", branch_name)
    print(f"Branch created: {branch_name}")
    print(f"{ticket_key} → TO DO")


def cmd_start(args):
    ticket_key = resolve_ticket(args.ticket)
    all_branches = git("branch", "--list", f"feature/{ticket_key}-*").strip()
    if not all_branches:
        print(f"Error: no branch found for {ticket_key}", file=sys.stderr)
        sys.exit(1)
    branch_name = all_branches.strip().lstrip("* ").split("\n")[0].strip()
    git("checkout", branch_name)
    print(f"Checked out {branch_name}")
    transition(ticket_key, "IN PROGRESS")
    print(f"{ticket_key} → IN PROGRESS")


def cmd_switch(args):
    ticket_key = resolve_ticket(args.ticket)
    all_branches = git("branch", "--list", f"feature/{ticket_key}-*").strip()
    if not all_branches:
        print(f"Error: no branch found for {ticket_key}", file=sys.stderr)
        sys.exit(1)
    branch_name = all_branches.strip().lstrip("* ").split("\n")[0].strip()
    git("checkout", branch_name)
    print(f"Checked out {branch_name}")


def cmd_pr(args):
    branch = current_branch()
    ticket_key = ticket_from_branch(branch)
    if not ticket_key:
        print(f"Error: branch '{branch}' doesn't match feature/TICKET-* pattern", file=sys.stderr)
        sys.exit(1)

    issue = api("GET", f"/issue/{ticket_key}")
    summary = issue["fields"]["summary"]
    ticket_url = f"{get_base_url()}/browse/{ticket_key}"

    git("push", "-u", "origin", branch)
    print(f"Pushed {branch}")

    pr_title = args.title or f"[{ticket_key}] {summary}"
    pr_body = args.body or f"## Summary\n\nResolves [{ticket_key}]({ticket_url})\n\n## Test plan\n\n- [ ] Verify changes locally\n- [ ] Review in staging"

    pr_url = gh("pr", "create", "--title", pr_title, "--body", pr_body, "--base", "main")
    print(f"PR created — {pr_url}")

    api("POST", f"/issue/{ticket_key}/comment", {"body": md_to_adf(f"PR opened: {pr_url}")})
    transition(ticket_key, "IN REVIEW")
    print(f"{ticket_key} → IN REVIEW")


def cmd_status(args):
    branch = current_branch()
    ticket_key = ticket_from_branch(branch)
    if not ticket_key:
        print(f"  Branch:  {branch}")
        print(f"  Ticket:  (none — not a feature branch)")
        return
    issue = api("GET", f"/issue/{ticket_key}")
    f = issue["fields"]
    print(f"  Branch:  {branch}")
    print(f"  Ticket:  {ticket_key}")
    print(f"  Summary: {f['summary']}")
    print(f"  Status:  {f['status']['name']}")
    print(f"  URL:     {get_base_url()}/browse/{ticket_key}")


def main():
    load_env()

    parser = argparse.ArgumentParser(description="sos-feature — Git + Jira lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create")
    p.add_argument("summary")
    p.add_argument("-d", "--description", default=None)
    p.add_argument("-f", "--file", default=None)
    p.add_argument("-t", "--type", default="task")
    p.add_argument("-p", "--parent", default=None)

    p = sub.add_parser("start")
    p.add_argument("ticket")

    p = sub.add_parser("switch")
    p.add_argument("ticket")

    p = sub.add_parser("pr")
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)

    sub.add_parser("status")

    args = parser.parse_args()
    {"create": cmd_create, "start": cmd_start, "switch": cmd_switch, "pr": cmd_pr, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
