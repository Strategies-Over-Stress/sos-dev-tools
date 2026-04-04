#!/usr/bin/env python3
"""sos-pm — project management agent orchestrator.

Usage:
    sos-pm start <ticket> [--iteration name]
    sos-pm finish
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .env import load_env

# ---------------------------------------------------------------------------
# Paths (relative to CWD — project root)
# ---------------------------------------------------------------------------

PM_DIR = Path(".pm")
CONFIG_FILE = PM_DIR / "config.json"
ACTIVE_TICKET_FILE = PM_DIR / "active-ticket.json"
INSTRUCTIONS_FILE = PM_DIR / "instructions.md"
LOG_DIR = PM_DIR / "logs"

DEFAULT_CONFIG = {
    "execution": {
        "command": "claude",
        "args": ["--dangerously-skip-permissions", "--print"],
    },
    "jira": {
        "auto_transition": True,
        "in_progress_status": "In Progress",
        "review_status": "In Review",
    },
    "git": {
        "branch_prefix": "feature",
    },
    "checks": {
        "test": None,
        "lint": None,
        "typecheck": None,
    },
    "test_links": {},
    "test_commands": {},
}

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DEV_AGENT_PROMPT = """\
You are a software development agent. Complete the following Jira ticket.

## Ticket
{ticket_context}

## Branch
You are on `{branch}`. Do all work here.

## Rules
1. Stay on branch `{branch}` — do NOT checkout other branches.
2. Commit frequently with clear messages referencing the ticket ID.
3. Do NOT merge into any other branch.
4. Do NOT push to remote.
5. Do NOT force push, reset --hard, or delete branches.
6. Run tests if available.
7. When finished, make a final commit and stop.
{custom_instructions}{review_feedback}\
"""

REVIEW_AGENT_PROMPT = """\
You are a code review agent. Review the work done for this ticket.

## Ticket
{ticket_context}

## Changes
### Commits
{commit_log}

### Diff Stats
{diff_stat}

### Full Diff
{full_diff}

### Quality Check Results
{check_results}

## Instructions
Provide a structured review:
1. **What was done well** — good patterns, clean code, etc.
2. **Concerns** — bugs, code smells, missing edge cases, test gaps
3. **Acceptance criteria met?** — assess each criterion
4. **Recommendation** — APPROVE, REQUEST_CHANGES, or NEEDS_DISCUSSION

Be specific. Reference file names and line numbers when noting concerns.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def git(*args):
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def run_cmd(cmd, check=True, capture=True):
    """Run a command. Returns CompletedProcess."""
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if capture else ""
        print(f"Error running {cmd[0]}: {stderr}", file=sys.stderr)
        sys.exit(1)
    return result


def current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD")


def working_tree_clean():
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD"], capture_output=True, text=True
    )
    return result.returncode == 0


def resolve_ticket(ref):
    ref = ref.strip().upper()
    if re.match(r"^\d+$", ref):
        pk = os.environ.get("JIRA_PROJECT_KEY", "RICH")
        return f"{pk}-{ref}"
    return ref


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError as e:
            print(f"Error: invalid .pm/config.json: {e}", file=sys.stderr)
            sys.exit(1)
    PM_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    print(f"Created {CONFIG_FILE} with defaults — edit to configure checks and test links.")
    return dict(DEFAULT_CONFIG)


def load_active_ticket():
    if not ACTIVE_TICKET_FILE.exists():
        print("Error: no active ticket. Run `sos-pm start <ticket>` first.", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(ACTIVE_TICKET_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: invalid {ACTIVE_TICKET_FILE}: {e}", file=sys.stderr)
        sys.exit(1)


def save_active_ticket(data):
    PM_DIR.mkdir(exist_ok=True)
    ACTIVE_TICKET_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_dir_for(ticket_key, branch_name):
    """Return the log directory for a ticket/branch, creating it if needed."""
    safe_branch = branch_name.replace("/", "-")
    d = LOG_DIR / ticket_key / safe_branch
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_log(ticket_key, branch_name, filename, content):
    d = log_dir_for(ticket_key, branch_name)
    path = d / filename
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Custom instructions
# ---------------------------------------------------------------------------


def load_project_instructions():
    """Load per-project instructions from .pm/instructions.md if it exists."""
    if INSTRUCTIONS_FILE.exists():
        return INSTRUCTIONS_FILE.read_text().strip()
    return ""


def build_instructions(project_instructions="", run_instructions="", iteration_instructions=""):
    """Combine all instruction sources into a single prompt section."""
    parts = []
    if project_instructions:
        parts.append(f"### Project Instructions\n{project_instructions}")
    if run_instructions:
        parts.append(f"### Instructions for This Run\n{run_instructions}")
    if iteration_instructions:
        parts.append(f"### Instructions for This Iteration\n{iteration_instructions}")
    if not parts:
        return ""
    return "\n## Custom Instructions\n" + "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Test links
# ---------------------------------------------------------------------------


def print_test_links(config):
    links = config.get("test_links", {})
    commands = config.get("test_commands", {})
    if not links and not commands:
        return
    print("\n--- Test Your Changes ---")
    for name, cmd in commands.items():
        print(f"  Run:  {cmd}")
    for name, url in links.items():
        print(f"  Open: {url}")


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


def cmd_start(args):
    config = load_config()
    ticket_key = resolve_ticket(args.ticket)

    # Fetch ticket context via sos-jira
    result = run_cmd(["sos-jira", "view", ticket_key])
    ticket_context = result.stdout.strip()
    print(ticket_context)

    # Transition to In Progress
    if config["jira"]["auto_transition"]:
        status = config["jira"]["in_progress_status"]
        run_cmd(["sos-jira", "move", ticket_key, status])
        print(f"{ticket_key} -> {status}")

    # Record parent branch, create iteration branch
    parent_branch = current_branch()
    branch_prefix = config["git"]["branch_prefix"]
    base_branch = f"{branch_prefix}/{ticket_key}"

    cmd = ["sos-feature", "start-iteration", base_branch]
    iteration = args.iteration
    if iteration:
        cmd.append(iteration)
    run_cmd(cmd)
    iteration_branch = current_branch()

    # Derive iteration name from branch if not provided
    if not iteration:
        # sos-feature start-iteration generates a random suffix
        iteration = iteration_branch.removeprefix(f"{base_branch}-")

    # Save active ticket state
    active = {
        "ticket_key": ticket_key,
        "ticket_context": ticket_context,
        "parent_branch": parent_branch,
        "iteration_branch": iteration_branch,
        "iteration": iteration,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "in_progress",
    }
    save_active_ticket(active)

    # Build and launch dev agent
    project_instructions = load_project_instructions()
    run_instructions = args.instructions or ""
    custom = build_instructions(project_instructions, run_instructions)

    prompt = DEV_AGENT_PROMPT.format(
        ticket_context=ticket_context,
        branch=iteration_branch,
        custom_instructions=custom,
        review_feedback="",
    )

    exe = config["execution"]["command"]
    exe_args = config["execution"]["args"]
    print(f"\n--- Launching Dev Agent on {iteration_branch} ---\n")
    subprocess.run([exe] + exe_args + [prompt], stdin=subprocess.DEVNULL)

    # Collect work summary
    commit_log = git("log", "--oneline", f"{parent_branch}..HEAD")
    diff_stat = git("diff", "--stat", f"{parent_branch}...HEAD")

    # Save dev log
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_content = f"# Dev Agent Log — {iteration}\n\n"
    log_content += f"- **Ticket:** {ticket_key}\n"
    log_content += f"- **Branch:** {iteration_branch}\n"
    log_content += f"- **Timestamp:** {timestamp}\n\n"
    log_content += f"## Commits\n```\n{commit_log or '(none)'}\n```\n\n"
    log_content += f"## Files Changed\n```\n{diff_stat or '(none)'}\n```\n"

    log_path = save_log(ticket_key, iteration_branch, f"dev-{iteration}.md", log_content)

    print(f"\n--- Dev Agent Complete ---")
    print(f"  Ticket:  {ticket_key}")
    print(f"  Branch:  {iteration_branch}")
    print(f"  Log:     {log_path}")
    print(f"\nCommits:\n{commit_log or '  (none)'}")
    print(f"\nFiles changed:\n{diff_stat or '  (none)'}")

    print_test_links(config)
    print(f"\nNext: run `sos-pm finish` to review and merge.")


# ---------------------------------------------------------------------------
# cmd_finish
# ---------------------------------------------------------------------------


def run_checks(config):
    """Run quality checks. Returns list of (name, passed, output) tuples."""
    checks = config.get("checks", {})
    results = []
    for name, command in checks.items():
        if command is None:
            continue
        print(f"  Running {name}: {command}")
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        passed = result.returncode == 0
        output = (result.stdout + result.stderr).strip()
        results.append((name, passed, output))
        print(f"    {'PASS' if passed else 'FAIL'}")
    return results


def format_check_results(check_results):
    if not check_results:
        return "(no checks configured)"
    lines = []
    for name, passed, output in check_results:
        status = "PASS" if passed else "FAIL"
        lines.append(f"**{name}**: {status}")
        if not passed and output:
            lines.append(f"```\n{output}\n```")
    return "\n".join(lines)


def review_cycle(active, config, check_results):
    """Run review agent, prompt user. Returns 'merge', 'iterate', or 'abort'."""
    ticket_key = active["ticket_key"]
    parent_branch = active["parent_branch"]
    iteration_branch = active["iteration_branch"]
    iteration = active["iteration"]

    commit_log = git("log", "--oneline", f"{parent_branch}..HEAD")
    diff_stat = git("diff", "--stat", f"{parent_branch}...HEAD")
    full_diff = git("diff", f"{parent_branch}...HEAD")

    # Launch review agent (captured, not streamed)
    review_prompt = REVIEW_AGENT_PROMPT.format(
        ticket_context=active["ticket_context"],
        commit_log=commit_log or "(no commits)",
        diff_stat=diff_stat or "(no changes)",
        full_diff=full_diff or "(no diff)",
        check_results=format_check_results(check_results),
    )

    print(f"\n--- Launching Review Agent ---\n")
    result = subprocess.run(
        ["claude", "--print", review_prompt],
        capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    )
    review_output = result.stdout.strip()
    print(review_output)

    # Save review log
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_content = f"# Review Agent Log — {iteration}\n\n"
    log_content += f"- **Ticket:** {ticket_key}\n"
    log_content += f"- **Branch:** {iteration_branch}\n"
    log_content += f"- **Timestamp:** {timestamp}\n\n"
    log_content += f"## Check Results\n{format_check_results(check_results)}\n\n"
    log_content += f"## Review\n{review_output}\n"

    log_path = save_log(ticket_key, iteration_branch, f"review-{iteration}.md", log_content)
    print(f"\n  Review log: {log_path}")

    # User decides
    print(f"\n--- What next? ---")
    print(f"  [m]erge   — merge into {parent_branch}")
    print(f"  [i]terate — create new iteration to address feedback")
    print(f"  [a]bort   — exit without merging")

    while True:
        answer = input("\nChoice [m/i/a]: ").strip().lower()
        if answer in ("m", "merge"):
            return "merge", review_output
        if answer in ("i", "iterate"):
            return "iterate", review_output
        if answer in ("a", "abort"):
            return "abort", review_output
        print("  Please enter m, i, or a.")


def run_iteration(active, config, review_feedback):
    """Launch a new dev agent iteration with review feedback, return updated active state."""
    ticket_key = active["ticket_key"]
    current_iter_branch = current_branch()

    # Prompt for iteration name and optional instructions
    iteration_name = input("Iteration name (e.g., fixes, round-2): ").strip()
    if not iteration_name:
        iteration_name = "fixes"
    iteration_instructions = input("Additional instructions (or Enter to skip): ").strip()

    # Create new iteration branch off current
    run_cmd(["sos-feature", "start-iteration", current_iter_branch, iteration_name])
    new_branch = current_branch()

    # Update active ticket
    active["iteration_branch"] = new_branch
    active["iteration"] = iteration_name
    save_active_ticket(active)

    # Build prompt with review feedback and custom instructions
    feedback_section = f"\n## Review Feedback from Previous Iteration\nAddress the following concerns:\n\n{review_feedback}\n"

    project_instructions = load_project_instructions()
    custom = build_instructions(project_instructions, iteration_instructions=iteration_instructions)

    prompt = DEV_AGENT_PROMPT.format(
        ticket_context=active["ticket_context"],
        branch=new_branch,
        custom_instructions=custom,
        review_feedback=feedback_section,
    )

    exe = config["execution"]["command"]
    exe_args = config["execution"]["args"]
    print(f"\n--- Launching Dev Agent on {new_branch} ---\n")
    subprocess.run([exe] + exe_args + [prompt], stdin=subprocess.DEVNULL)

    # Collect and save dev log
    commit_log = git("log", "--oneline", f"{current_iter_branch}..HEAD")
    diff_stat = git("diff", "--stat", f"{current_iter_branch}...HEAD")

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_content = f"# Dev Agent Log — {iteration_name}\n\n"
    log_content += f"- **Ticket:** {ticket_key}\n"
    log_content += f"- **Branch:** {new_branch}\n"
    log_content += f"- **Parent:** {current_iter_branch}\n"
    log_content += f"- **Timestamp:** {timestamp}\n\n"
    log_content += f"## Review Feedback Addressed\n{review_feedback}\n\n"
    log_content += f"## Commits\n```\n{commit_log or '(none)'}\n```\n\n"
    log_content += f"## Files Changed\n```\n{diff_stat or '(none)'}\n```\n"

    log_path = save_log(ticket_key, new_branch, f"dev-{iteration_name}.md", log_content)

    print(f"\n--- Dev Agent Complete ---")
    print(f"  Branch:  {new_branch}")
    print(f"  Log:     {log_path}")
    print(f"\nCommits:\n{commit_log or '  (none)'}")
    print(f"\nFiles changed:\n{diff_stat or '  (none)'}")

    print_test_links(config)

    return active


def cmd_finish(args):
    active = load_active_ticket()
    config = load_config()

    # Verify branch
    branch = current_branch()
    expected = active["iteration_branch"]
    if branch != expected:
        print(f"Error: expected branch '{expected}', on '{branch}'", file=sys.stderr)
        print(f"Hint: run `git checkout {expected}` first.", file=sys.stderr)
        sys.exit(1)

    # Verify clean
    if not working_tree_clean():
        print("Error: uncommitted changes. Commit or stash before finishing.", file=sys.stderr)
        sys.exit(1)

    # Run quality checks
    print("--- Quality Checks ---")
    check_results = run_checks(config)

    # Review + iterate loop
    while True:
        choice, review_output = review_cycle(active, config, check_results)

        if choice == "abort":
            print("Aborted. Run `sos-pm finish` when ready.")
            return

        if choice == "merge":
            break

        if choice == "iterate":
            active = run_iteration(active, config, review_output)
            # Re-run checks on new iteration
            print("\n--- Quality Checks ---")
            check_results = run_checks(config)
            # Loop continues → review again

    # Merge
    ticket_key = active["ticket_key"]
    run_cmd(["sos-feature", "merge-iteration"])

    # Transition Jira
    if config["jira"]["auto_transition"]:
        status = config["jira"]["review_status"]
        run_cmd(["sos-jira", "move", ticket_key, status])
        print(f"{ticket_key} -> {status}")

    # Add Jira comment
    commit_log = git("log", "--oneline", f"{active['parent_branch']}..HEAD")
    run_cmd([
        "sos-jira", "comment", ticket_key,
        f"Iteration merged. Commits:\n{commit_log}",
    ])

    # Update state
    active["status"] = "merged"
    active["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_active_ticket(active)

    merged_to = current_branch()
    print(f"\n--- Iteration Complete ---")
    print(f"  Ticket:    {ticket_key}")
    print(f"  Merged to: {merged_to}")
    print(f"\nNext steps:")
    print(f"  sos-pm start {ticket_key} --iteration <name>  — another iteration")
    print(f"  sos-feature pr                                — open a pull request")
    print(f"  sos-jira move {ticket_key} DONE               — mark as done")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    load_env()

    parser = argparse.ArgumentParser(description="sos-pm — project management agent orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="Fetch ticket, create branch, launch dev agent")
    p.add_argument("ticket", help="Jira ticket key or number (e.g., RICH-5 or 5)")
    p.add_argument("--iteration", "-i", default=None,
                   help="Iteration name (default: auto-generated)")
    p.add_argument("--instructions", "-m", default=None,
                   help="Custom instructions for the dev agent (e.g., 'only touch the backend')")
    p.add_argument("--project", "-P", default=None,
                   help="Jira project key (overrides JIRA_PROJECT_KEY)")

    p = sub.add_parser("finish", help="Review, iterate, and merge")
    p.add_argument("--project", "-P", default=None,
                   help="Jira project key (overrides JIRA_PROJECT_KEY)")

    args = parser.parse_args()

    if getattr(args, "project", None):
        os.environ["JIRA_PROJECT_KEY"] = args.project.upper()

    {"start": cmd_start, "finish": cmd_finish}[args.command](args)


if __name__ == "__main__":
    main()
