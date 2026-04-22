#!/usr/bin/env python3
"""sos-flow-dev — deterministic ticket workflow orchestrator.

Runs Work 1 (pm-start) → Review → Work 2 → QA gate as sequential phases.
Each phase launches a Claude subagent inside a detached tmux session (via
sos-claude-print --tmux). The orchestrator itself is pure Python/shell — no
LLM in the control flow, so it cannot pause mid-flow or get tricked by a
recap heuristic.

Every phase writes a result JSON file. The orchestrator reads the file and
branches mechanically. Contrast with the `/flow-dev` Claude Code skill, which
is prompt-driven and therefore probabilistic at phase boundaries.

Usage:
    sos-flow-dev start TICKET [--pause-after work1|review|work2]
    sos-flow-dev review TICKET                  # re-run just the review phase
    sos-flow-dev work2 TICKET [--comments N]    # re-run just work-2
    sos-flow-dev qa-approve TICKET              # merge via /pm-finish
    sos-flow-dev qa-reject TICKET [reason]      # kick off work-3
    sos-flow-dev status [TICKET]
    sos-flow-dev cleanup TICKET
"""

import argparse
import datetime
import fcntl
import glob as _glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("GHOSTTY_MINI_STATE", str(Path.home() / ".ghostty-mini")))


def _global_config_path():
    # Computed each call so tests can monkey-patch STATE_DIR.
    return STATE_DIR / "config.json"
PM_START_SKILL = Path(os.environ.get(
    "SOS_FLOW_DEV_PM_START",
    str(Path.home() / ".claude" / "commands" / "pm-start.md"),
))
PM_FINISH_SKILL = Path(os.environ.get(
    "SOS_FLOW_DEV_PM_FINISH",
    str(Path.home() / ".claude" / "commands" / "pm-finish.md"),
))


# ─── Small output helpers ──────────────────────────────────────────────────

def step(msg):
    print(f"▶ {msg}", flush=True)


def check(msg):
    print(f"✓ {msg}", flush=True)


def fail(msg, exit_code=1):
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    sys.exit(exit_code)


# ─── Subprocess + state helpers ────────────────────────────────────────────

def run_capture(cmd, **kw):
    kw.setdefault("check", True)
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw).stdout.strip()


def post_card(kind, title, ticket=None, url=None, ctx=None, actions=None):
    """Post an info/action card via sos-inbox; return card id (or '' on error).

    Network-unreachable failures (server down) silently no-op — sos-inbox
    already returns 0 in that case so nothing to catch here. But sos-inbox
    exits non-zero on *real* failures (validation errors, HTTP 4xx/5xx,
    malformed actions JSON) — surface those to stderr rather than swallowing
    silently, so users notice when cards fail to post for real reasons.
    """
    cmd = ["sos-inbox", kind, title]
    if ticket: cmd += ["--ticket", ticket]
    if url:    cmd += ["--url", url]
    if ctx:    cmd += ["--ctx", ctx]
    if actions: cmd += ["--actions", json.dumps(actions)]
    try:
        return run_capture(cmd)
    except FileNotFoundError:
        # sos-inbox not installed — keep the flow going but warn once.
        print("post_card: sos-inbox not on PATH — cards won't post",
              file=sys.stderr)
        return ""
    except subprocess.CalledProcessError as e:
        stderr = (getattr(e, "stderr", None) or "").strip()
        detail = f" — {stderr}" if stderr else ""
        print(f"post_card: sos-inbox {kind} failed (exit {e.returncode}){detail}",
              file=sys.stderr)
        return ""


def prompt_user(title, ticket=None, ctx=None, actions=None, timeout=3600):
    """Post an action card, block until reply lands, return the reply text."""
    cmd = ["sos-inbox", "prompt", title, "--timeout", str(timeout)]
    if ticket: cmd += ["--ticket", ticket]
    if ctx:    cmd += ["--ctx", ctx]
    if actions: cmd += ["--actions", json.dumps(actions)]
    return run_capture(cmd)


def run_subagent(session, prompt_text):
    """Launch `claude --print` inside tmux via sos-claude-print. Blocks. Returns rc."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     prefix=f"sos-flow-dev-{session}-") as f:
        f.write(prompt_text)
        path = f.name
    print(f"  🧰 session '{session}' · attach: tmux attach -t {session}", flush=True)
    try:
        return subprocess.run(
            ["sos-claude-print", "--tmux", session, "--file", path],
        ).returncode
    finally:
        try: os.unlink(path)
        except OSError: pass


def worktree_root():
    try:
        return Path(run_capture(["git", "rev-parse", "--show-toplevel"]))
    except subprocess.CalledProcessError:
        fail("not inside a git worktree")


def session_file(ticket):
    return STATE_DIR / "sessions" / f"{ticket}.json"


def session_get(ticket, key=None):
    f = session_file(ticket)
    if not f.exists(): return None if key else None
    data = json.loads(f.read_text())
    return data if key is None else data.get(key)


def session_set(ticket, **kw):
    d = STATE_DIR / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    f = session_file(ticket)
    data = json.loads(f.read_text()) if f.exists() else {"ticket": ticket}
    data.update(kw)
    f.write_text(json.dumps(data, indent=2))


def session_rm(ticket):
    f = session_file(ticket)
    if f.exists(): f.unlink()


def read_pm_complete(ticket):
    f = Path(f"/tmp/pm-complete-{ticket}.json")
    if not f.exists():
        fail(f"missing {f} — did pm-start run?")
    return json.loads(f.read_text())


def extract_pr_num(pr_url):
    if not pr_url: return ""
    m = re.search(r"/pull/(\d+)", pr_url)
    return m.group(1) if m else ""


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Prompt templates ──────────────────────────────────────────────────────

BLOCKER_CONTRACT = """
## Blocker contract — ask, don't guess

If you hit an ambiguity you cannot resolve from the ticket + existing codebase,
STOP and ask via the ghostty-mini inbox. Your subprocess stays alive while you
wait; block on `sos-inbox prompt`:

    REPLY=$(sos-inbox prompt "Your question here" \\
        --ticket <TICKET> \\
        --ctx "brief context on why you are asking" \\
        --actions '[
          {"label":"Option A", "kind":"reply", "text":"use A"},
          {"label":"Option B", "kind":"reply", "text":"use B"},
          {"label":"Explain",  "kind":"reply", "text":""}
        ]' \\
        --timeout 1800)

Then branch on $REPLY. Do NOT guess. Do NOT leave TODOs. Do NOT exit on
ambiguity — exiting leaves half-done work. Batch related questions into
one prompt; prefer button-based options when enumerable, include a freeform
"Explain" option (empty text) so the human can write something else.
"""


def _blocker_for(ticket):
    return BLOCKER_CONTRACT.replace("<TICKET>", ticket)


def worktree_alloc_prompt(ticket, source_repo, hint_base=None):
    """Phase-0 subagent: decide where to do the work, write the answer to a file.

    `source_repo` is the resolved main git checkout — do not rely on CWD to
    find it. The operator may have invoked this from inside a worktree or
    from an unrelated directory; the orchestrator has already figured it out.
    """
    base_hint = (
        f"The base branch is `{hint_base}` (resolved from --base flag or "
        f"~/.ghostty-mini/config.json `default_base_branch`). Use it.\n"
        if hint_base else
        "No base branch was resolved. Infer from context: the branch currently "
        "checked out in the source repo, `git.default_base` in .pm/config.json, "
        "or recent flow-dev sessions at $HOME/.ghostty-mini/sessions/*.json. "
        "If you can't resolve confidently, ASK via `sos-inbox prompt`.\n"
    )
    return f"""You are allocating a git worktree for ticket {ticket}. The work
happens in that worktree; the downstream phases (pm-start, Review, Work 2, QA)
expect to run inside whatever directory you pick.

SOURCE REPO: {source_repo}
POOL DIR: {source_repo}/claude/worktrees/  (may not exist yet)
RESULT FILE: /tmp/flow-{ticket}-worktree.json  ← write your decision here

All git operations below should use `git -C {source_repo}` (or cd into the
source first). Do not rely on the caller's CWD — they might have invoked
this from anywhere.

{base_hint}
## Steps

1. **Confirm the source repo.**
   - `git -C {source_repo} rev-parse --show-toplevel` must succeed.
   - If not, write `{{"failed": "source repo {source_repo} is not a git repo"}}`
     and exit 0.

2. **Inspect the pool** at `{source_repo}/claude/worktrees/`.
   - If the directory doesn't exist, note that and skip to step 4 (create).
   - For each subdirectory, use `git worktree list --porcelain` (run from CWD)
     to confirm it's a registered worktree.
   - For each worktree, determine AVAILABILITY. A worktree is AVAILABLE if:
     * `.pm/active-ticket.json` is missing, OR
     * its `status` is `"merged"`, OR
     * its `completed_at` is older than 7 days (stale).

3. **If an available pool worktree exists**, pick the lowest-numbered one
   (e.g. prefer `wt-1` over `wt-5`). Reset it for reuse:
       git -C <wt> fetch origin <parent_branch>
       git -C <wt> switch <parent_branch>
       git -C <wt> reset --hard origin/<parent_branch>
       git -C <wt> clean -fd
       # Remove ticket-specific pm files but keep .pm/config.json intact
       rm -f <wt>/.pm/active-ticket.json <wt>/.pm/work-summary.md \\
             <wt>/.pm/dev-agent-instructions.md <wt>/.pm/*-result.json \\
             <wt>/.pm/failed.json <wt>/.pm/preview.log
   Then set `action: "reused"` in the result.

4. **Otherwise, create a new worktree.**
   - Ensure `{source_repo}/claude/worktrees/` exists (`mkdir -p`).
   - Pick the lowest unused integer N for `wt-N` (scan existing pool).
   - Create:  `git -C {source_repo} worktree add claude/worktrees/wt-N <parent_branch>`
   - If the source repo has a `.pm/config.json`, copy it into the new
     worktree's `.pm/` dir **verbatim — do NOT modify ports or any other
     field**. Ports are a runtime concern handled by `sos-flow-dev preview`,
     which picks free ports from its pool (6006-6099) at start time. Copying
     the config unchanged means every worktree inherits the same
     commands/services without the allocator needing to coordinate port
     assignments.
   - Set `action: "created"`.

5. **Write the result file** at `/tmp/flow-{ticket}-worktree.json`:
   ```json
   {{
     "worktree": "<absolute path to the chosen worktree>",
     "parent_branch": "<branch name>",
     "action": "reused" | "created",
     "reason": "<one-sentence rationale>",
     "preview_port": <int or null>
   }}
   ```
   Exit 0 after writing.

## When to use the inbox

If you hit genuine ambiguity — especially around parent branch when it's not
obvious — ask the human via `sos-inbox prompt`. Examples:

- CWD has no clear parent (detached HEAD, or on a non-standard branch).
- You found both `main` and `sbook/epic` as plausible parents and the ticket
  key doesn't hint either way.
- An existing worktree's state is confusing (has a branch checked out but no
  active-ticket.json — was work done manually?).

Never guess silently on the parent branch. Cost of asking: 30 seconds. Cost
of forking from the wrong branch: hours of rework downstream.

{_blocker_for(ticket)}
"""


def review_prompt(ticket, pr_num):
    return f"""You are a senior code reviewer. Review PR #{pr_num} in this repo.

Ticket context: read `.pm/active-ticket.json` for summary + acceptance criteria.

## Steps

1. Read the PR diff:          `gh pr diff {pr_num}`
2. Read the PR description:   `gh pr view {pr_num}`
3. Check for:
   - Logic bugs and edge cases
   - Security concerns
   - Test coverage gaps
   - Deviation from acceptance criteria
   - Style inconsistencies (per the repo's existing conventions)
   - Dead code, leftover debug prints, stray TODOs
4. For each concrete issue, post an inline review comment:
      gh pr review {pr_num} --comment -F <feedback-file>
5. When done, write `.pm/review-result.json` in the worktree root:
      {{"comments": N, "verdict": "changes-requested" | "approve"}}
   Do NOT print the JSON to stdout — the orchestrator reads the file.

{_blocker_for(ticket)}
"""


def work2_prompt(ticket, pr_num, comments_n):
    return f"""You are the implementation agent. PR #{pr_num} has {comments_n} review comments to address.

## Your task

1. Read every review comment:
      gh pr view {pr_num} --json reviews,comments
      gh api repos/:owner/:repo/pulls/{pr_num}/comments
2. Address every concrete issue. Commit each fix with a clear message referencing
   the ticket (e.g. `{ticket}: address review feedback on <topic>`).
3. Push after each logical group of commits.
4. Resolve review threads where your fix addresses them.
5. If a preview dev server is configured (see `.pm/config.json` preview.port), use
   it to verify in-flight. If none is configured but one is useful, start one —
   e.g. `cd packages/ui && PORT=<port> npm run storybook &` — and record the URL
   in the result file below.
6. When done, write `.pm/work-2-result.json` in the worktree root:
      {{"ready": true, "url": "<preview_url or empty string>"}}
   or on failure:
      {{"failed": "<reason>"}}

{_blocker_for(ticket)}
"""


def work3_prompt(ticket, pr_num, reason):
    feedback = reason.strip() if reason else (
        "(no reason string supplied; read the reply thread on the most recent "
        "'Ready for QA' card — use `sos-inbox list --ticket " + ticket + "` "
        "to find its id and `sos-inbox replies <id>`)"
    )
    return f"""QA rejected PR #{pr_num} for {ticket}. Feedback from the reviewer:

    {feedback}

## Your task

1. Understand the feedback. If vague, consult the reply thread via sos-inbox.
2. Address the feedback in the worktree. Commit and push.
3. Keep the preview server running.
4. When done, write `.pm/work-3-result.json`:
      {{"ready": true, "url": "<preview_url>"}}
   or on failure:
      {{"failed": "<reason>"}}

{_blocker_for(ticket)}
"""


# ─── Phase runners (the mechanical parts) ──────────────────────────────────

def phase_worktree_alloc(ticket, hint_base=None):
    """Phase 0 — AI subagent picks or creates a worktree; return its path + parent.

    Writes /tmp/flow-<TICKET>-worktree.json with {worktree, parent_branch,
    action, reason, preview_port}.

    Source-repo resolution is CWD-independent: the operator can invoke
    sos-flow-dev from any directory (the main repo, a worktree inside it,
    or somewhere else entirely) as long as either:
      - CWD is a git repo related to the work (→ walks up to find main), OR
      - ~/.ghostty-mini/config.json has `source_repo` set.

    Serializes concurrent callers via flock on $GHOSTTY_MINI_STATE/alloc.lock.
    Before releasing the lock, writes a stub .pm/active-ticket.json into the
    chosen worktree so the next allocator sees it as busy.
    """
    step(f"Phase 0/5 — allocating worktree for {ticket}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    source_repo = _resolve_source_repo()
    if source_repo is None:
        fail(
            "cannot determine source repo. Either:\n"
            "  1. cd into the source repo (or any of its worktrees) and retry, OR\n"
            f"  2. write {_global_config_path()} with\n"
            '       {"source_repo": "/absolute/path/to/source/repo"}'
        )
    base = _resolve_base_branch(hint_base)

    lock_path = STATE_DIR / "alloc.lock"
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        prompt = worktree_alloc_prompt(ticket, str(source_repo), base)
        rc = run_subagent(f"flow-{ticket}-alloc", prompt)
        if rc != 0:
            fail(f"worktree-alloc subagent exited {rc}")
        result_file = Path(f"/tmp/flow-{ticket}-worktree.json")
        if not result_file.exists():
            fail(f"worktree-alloc did not write {result_file} — "
                 f"check `tmux attach -t flow-{ticket}-alloc` for last state")
        data = json.loads(result_file.read_text())
        if "failed" in data:
            fail(f"worktree-alloc failed: {data['failed']}")
        wt = data.get("worktree")
        if not wt or not Path(wt).is_dir():
            fail(f"worktree-alloc reported invalid path: {wt}")
        # Claim the worktree under the lock so concurrent allocators see it
        # as busy even before pm-start runs.
        pm_dir = Path(wt) / ".pm"
        pm_dir.mkdir(parents=True, exist_ok=True)
        (pm_dir / "active-ticket.json").write_text(json.dumps({
            "ticket_id": ticket,
            "status": "claimed",
            "claimed_at": now_iso(),
        }, indent=2))
    return data  # {worktree, parent_branch, action, reason, preview_port?}


_FLOW_DEV_PREFIX_TEMPLATE = """## Invocation context — READ FIRST

You are being invoked as Phase 1/5 ("work-1") of `sos-flow-dev` for ticket
{ticket}. This is NOT a standalone pm-start — flow-dev has already:

- Allocated the worktree you're running in (see `.pm/active-ticket.json`).
- Written session state at `~/.ghostty-mini/sessions/{ticket}.json` with
  `phase: "work-1"`.
- Spawned YOU inside tmux session `flow-{ticket}-work1`.

The session file, the `.pm/active-ticket.json` "claimed" marker, and the
`flow-{ticket}-work1` tmux session you may discover via `tmux ls` are ALL
YOUR OWN context — not a separate racing process. Do not refuse to run on
the basis of "another agent is already working on this ticket". Do not
call `sos-flow-dev cleanup`. Do not try to attach to `flow-{ticket}-work1`
(you are already in it).

Just execute the pm-start skill below. On success, write
`/tmp/pm-complete-{ticket}.json` per the skill's final step — flow-dev
blocks on that file.

---

"""


def phase_pm_start(ticket, iteration="first-pass"):
    step(f"Phase 1/5 — pm-start {ticket} {iteration}")
    if not PM_START_SKILL.exists():
        fail(f"pm-start skill not found at {PM_START_SKILL}")
    prefix = _FLOW_DEV_PREFIX_TEMPLATE.format(ticket=ticket)
    body = prefix + PM_START_SKILL.read_text() + f"\n\n{ticket} {iteration}\n"
    rc = run_subagent(f"flow-{ticket}-work1", body)
    if rc != 0:
        fail(f"pm-start subagent exited {rc}")
    return read_pm_complete(ticket)


def phase_review(ticket, pr_num):
    step(f"Phase 2/5 — review PR #{pr_num}")
    rc = run_subagent(f"flow-{ticket}-review", review_prompt(ticket, pr_num))
    if rc != 0:
        fail(f"review subagent exited {rc}")
    rf = worktree_root() / ".pm" / "review-result.json"
    if not rf.exists():
        fail(f"review subagent did not write {rf} — check `tmux attach -t flow-{ticket}-review` for last state")
    return json.loads(rf.read_text())


def phase_work2(ticket, pr_num, comments_n):
    step(f"Phase 3/5 — address {comments_n} review comments")
    rc = run_subagent(f"flow-{ticket}-work2", work2_prompt(ticket, pr_num, comments_n))
    if rc != 0:
        fail(f"work-2 subagent exited {rc}")
    rf = worktree_root() / ".pm" / "work-2-result.json"
    if not rf.exists():
        fail(f"work-2 subagent did not write {rf}")
    return json.loads(rf.read_text())


def phase_work3(ticket, pr_num, reason):
    step(f"Phase 3/5 (re-QA) — address reviewer feedback")
    rc = run_subagent(f"flow-{ticket}-work3", work3_prompt(ticket, pr_num, reason))
    if rc != 0:
        fail(f"work-3 subagent exited {rc}")
    rf = worktree_root() / ".pm" / "work-3-result.json"
    if not rf.exists():
        fail(f"work-3 subagent did not write {rf}")
    return json.loads(rf.read_text())


def qa_card_actions(ticket, pr_url, preview_url):
    actions = []
    if preview_url:
        actions.append({"label": "Open preview", "kind": "openUrl"})
    if pr_url:
        actions.append({"label": "Open PR", "kind": "openUrl", "url": pr_url})
    actions += [
        {"label": "Approve & merge", "kind": "inject",
         "text": f"sos-flow-dev qa-approve {ticket}\n", "execute": True},
        {"label": "Request changes", "kind": "inject",
         "text": f"sos-flow-dev qa-reject {ticket} ", "execute": False},
    ]
    return actions


def gate(ticket, question):
    """Optional inter-phase pause via an action card. Abort → exit non-zero."""
    answer = prompt_user(
        question, ticket=ticket,
        actions=[
            {"label": "Continue", "kind": "reply", "text": "continue"},
            {"label": "Abort",    "kind": "reply", "text": "abort"},
        ],
        timeout=3600,
    )
    if answer.strip().lower() == "abort":
        fail("aborted by user via gate card")


# ─── Top-level subcommands ─────────────────────────────────────────────────

def _launch_runner(ticket, base=None, pause_after=None, iteration=None):
    """Spawn a detached tmux session running `sos-flow-dev start <ticket>`.

    Returns (session_name, log_path) on success, (None, error_message) on failure.
    """
    session = f"flow-runner-{ticket}"
    check_existing = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    if check_existing.returncode == 0:
        return None, f"tmux session '{session}' already exists — skipping"

    cmd_parts = ["sos-flow-dev", "start", ticket]
    if base:
        cmd_parts += ["--base", base]
    if iteration and iteration != "first-pass":
        cmd_parts += ["--iteration", iteration]
    if pause_after:
        cmd_parts += ["--pause-after", pause_after]
    log_path = f"/tmp/flow-runner-{ticket}.log"
    # `exec` replaces the shell with sos-flow-dev so the session dies cleanly
    # when the orchestrator exits. `tee` keeps a tail-able log.
    wrapped = (
        f"exec {' '.join(shlex.quote(c) for c in cmd_parts)} "
        f"2>&1 | tee {shlex.quote(log_path)}"
    )
    spawn = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session,
         "-x", "220", "-y", "50", "/bin/sh", "-c", wrapped],
        capture_output=True, text=True,
    )
    if spawn.returncode != 0:
        return None, f"tmux new-session failed: {spawn.stderr.strip()}"
    return session, log_path


def _fanout(tickets, base, pause_after, iteration=None):
    """Launch one detached runner per ticket. Print the resulting tmux IDs."""
    if shutil.which("tmux") is None:
        fail("tmux is required for multi-ticket or --detach runs (not on PATH)")
    results = []
    for t in tickets:
        session, info = _launch_runner(t, base=base, pause_after=pause_after,
                                       iteration=iteration)
        results.append((t, session, info))

    ok = [(t, s, log) for t, s, log in results if s is not None]
    err = [(t, _, msg) for t, _, msg in results if _ is None]

    if ok:
        print()
        print(f"Started {len(ok)} ticket(s). Each runs 5 phases in its own tmux tree.")
        print()
        for t, session, log in ok:
            print(f"  {t}")
            print(f"    runner log:   tail -f {log}")
            print(f"    runner tmux:  tmux attach -t {session}  (sparse — orchestrator only)")
            print(f"    ── leaf agents (the actually-live sessions) ──")
            print(f"    dev agent:    tmux attach -t pm-{t}                (during Work 1)")
            print(f"    reviewer:     tmux attach -t flow-{t}-review       (during Review)")
            print(f"    fix agent:    tmux attach -t flow-{t}-work2        (during Work 2)")
            print(f"    re-QA agent:  tmux attach -t flow-{t}-work3        (during Re-QA, if any)")
            print()
    if err:
        print("Skipped / failed:")
        for t, _, msg in err:
            print(f"  ✗ {t:<12}  {msg}", file=sys.stderr)
        print()

    print(
        f"{len(ok)} running. Monitor:\n"
        f"  sos-flow-dev watch             live dashboard (Ctrl+C to detach)\n"
        f"  sos-flow-dev status            one-shot snapshot\n"
        f"  tmux ls                        all live sessions\n"
        f"  sidebar at http://localhost:3030"
    )

    if err and not ok:
        sys.exit(1)


def cmd_start(args):
    # Multi-ticket OR explicit --detach → fan out to detached tmux runners
    # and return immediately. Single-ticket without --detach blocks (legacy).
    if len(args.tickets) > 1 or args.detach:
        _fanout(args.tickets, args.base, args.pause_after,
                iteration=getattr(args, "iteration", "first-pass"))
        if args.watch:
            # Drop into watch mode filtered to just the tickets we launched.
            print()
            print("Entering watch mode (Ctrl+C to detach — runs keep going):")
            print()
            watch_args = argparse.Namespace(tickets=args.tickets, interval=3)
            cmd_watch(watch_args)
        return
    _run_start_blocking(args.tickets[0], args)


def _run_start_blocking(ticket, args):
    session_set(ticket, phase="alloc", started_at=now_iso())

    # Phase 0 — AI-driven worktree allocation
    alloc = phase_worktree_alloc(ticket, hint_base=args.base)
    wt_path = alloc["worktree"]
    parent_branch = alloc.get("parent_branch") or ""
    action = alloc.get("action") or "unknown"
    os.chdir(wt_path)
    session_set(ticket, worktree=wt_path, parent_branch=parent_branch,
                worktree_action=action, phase="work-1")
    check(f"worktree {action}: {wt_path} (parent: {parent_branch})")

    # Phase 1 — pm-start
    pm = phase_pm_start(ticket, iteration=getattr(args, "iteration", "first-pass"))
    pr_url = pm.get("pr_url") or ""
    preview_url = pm.get("preview_url") or ""
    pr_num = extract_pr_num(pr_url)
    # Note: pm.worktree is just the basename pm-start observed; Phase 0's
    # full path is already on the session and authoritative. Don't overwrite.
    session_set(ticket, pr_url=pr_url, pr_num=pr_num,
                preview_url=preview_url, phase="review")
    post_card(
        "info", f"PR #{pr_num} opened" if pr_num else "Work 1 complete",
        ticket=ticket, url=pr_url or None,
        ctx=(f"Work 1 done · preview at {preview_url}" if preview_url
             else "Work 1 done · no preview configured"),
    )
    if args.pause_after == "work1":
        gate(ticket, "Work 1 done — continue to Review?")

    # Phase 2 — review
    r = phase_review(ticket, pr_num)
    comments_n = r.get("comments", 0)
    verdict = r.get("verdict")
    session_set(ticket, review_verdict=verdict, review_comments=comments_n,
                phase=("work-2" if verdict == "changes-requested" else "awaiting-qa"))
    post_card(
        "info", f"Review posted — {comments_n} comments",
        ticket=ticket, url=pr_url or None, ctx=f"Verdict: {verdict}",
    )
    if args.pause_after == "review":
        gate(ticket, f"Review verdict: {verdict} — continue?")

    # Phase 3 — work-2 (only if changes were requested)
    if verdict == "changes-requested":
        w = phase_work2(ticket, pr_num, comments_n)
        if "failed" in w:
            post_card("action", f"Work 2 failed — {ticket}",
                      ticket=ticket, ctx=w.get("failed", ""))
            fail(f"work-2 failed: {w.get('failed')}")
        preview_url = w.get("url") or preview_url
        session_set(ticket, preview_url=preview_url, phase="awaiting-qa")
        if args.pause_after == "work2":
            gate(ticket, "Work 2 done — continue to QA card?")

    # Phase 4 — QA card
    step("Phase 4/5 — post QA gate")
    card_id = post_card(
        "action", "Ready for QA",
        ticket=ticket, url=preview_url or pr_url or None,
        ctx=(f"PR #{pr_num} · preview at {preview_url} · QA per PR description"
             if preview_url else f"PR #{pr_num} · see PR description for QA steps"),
        actions=qa_card_actions(ticket, pr_url, preview_url),
    )
    check(f"flow-{ticket} → awaiting QA in sidebar (card {card_id})")


def cmd_review(args):
    ticket = args.ticket
    sess = session_get(ticket) or {}
    pr_num = sess.get("pr_num") or extract_pr_num(sess.get("pr_url", ""))
    if not pr_num:
        pm = read_pm_complete(ticket)
        pr_num = extract_pr_num(pm.get("pr_url", ""))
    if not pr_num:
        fail("no PR number on record; run `sos-flow-dev start` first")
    r = phase_review(ticket, pr_num)
    session_set(ticket,
                review_verdict=r.get("verdict"),
                review_comments=r.get("comments", 0))
    check(f"review done · verdict={r.get('verdict')} · comments={r.get('comments')}")


def cmd_work2(args):
    ticket = args.ticket
    sess = session_get(ticket) or {}
    pr_num = sess.get("pr_num") or fail("no PR number on record")
    comments_n = (args.comments if args.comments is not None
                  else sess.get("review_comments", 0))
    w = phase_work2(ticket, pr_num, comments_n)
    if "failed" in w:
        fail(f"work-2 failed: {w.get('failed')}")
    preview_url = w.get("url") or sess.get("preview_url", "")
    session_set(ticket, preview_url=preview_url, phase="awaiting-qa")
    check(f"work-2 done · ready={w.get('ready')} · preview={preview_url or 'none'}")


def _load_project_config(worktree):
    """Read the worktree's .pm/config.json, return {} on any failure."""
    if not worktree:
        return {}
    f = Path(worktree) / ".pm" / "config.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def cmd_qa_approve(args):
    """Merge the PR that pm-start opened for this ticket.

    Calls `gh pr merge` directly. The human has already explicitly approved
    via the inbox "Approve & merge" button, so there's no interactive
    confirmation step to add.

    Checks (lint, tests, typecheck) are NOT run locally before merge — that's
    CI's job. GitHub's branch protection rules already gate merges on required
    status checks. Duplicating in the CLI creates false-positive merge blocks
    when the local lint env drifts from CI (missing scripts, different node
    versions, uncommitted dep changes, etc). The CLI's job is just to merge;
    the repo's branch-protection config decides what "mergeable" means.

    Verifies `mergedAt` from GitHub after the merge call — refuses to post
    the "Merged" card if the PR isn't actually marked merged.
    """
    ticket = args.ticket
    sess = session_get(ticket) or {}
    pr_num = sess.get("pr_num")
    if not pr_num:
        fail(f"no PR number on record for {ticket}")
    worktree = sess.get("worktree")
    cfg = _load_project_config(worktree)

    git_cfg = cfg.get("git") or {}
    strategy = (git_cfg.get("merge_strategy") or "squash").lower()
    strategy_flag = {"squash": "--squash",
                     "merge": "--merge",
                     "rebase": "--rebase"}.get(strategy)
    if not strategy_flag:
        fail(f"unknown merge_strategy {strategy!r} — use squash|merge|rebase")
    delete_branch = git_cfg.get("delete_branch_on_merge", True)

    merge_cmd = ["gh", "pr", "merge", str(pr_num), strategy_flag]
    if delete_branch:
        merge_cmd.append("--delete-branch")
    step(f"merging PR #{pr_num} ({strategy})")
    r = subprocess.run(merge_cmd, cwd=worktree or None,
                       capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"gh pr merge failed: {(r.stderr or r.stdout).strip()}")

    # Verify GitHub actually marked it merged. Pre-refactor, a silent
    # subagent no-op would falsely post a "Merged" card.
    verify = subprocess.run(
        ["gh", "pr", "view", str(pr_num), "--json", "mergedAt"],
        cwd=worktree or None, capture_output=True, text=True,
    )
    merged_at = None
    if verify.returncode == 0:
        try:
            merged_at = json.loads(verify.stdout).get("mergedAt")
        except json.JSONDecodeError:
            pass
    if not merged_at:
        fail(f"gh pr merge returned 0 but PR #{pr_num} is not marked merged "
             f"(mergedAt is null) — refusing to post confirmation")

    # Update Jira if the project has auto_transition + a known done status.
    jira_cfg = cfg.get("jira") or {}
    if jira_cfg.get("auto_transition"):
        done_status = jira_cfg.get("done_status") or "Done"
        subprocess.run(["sos-jira", "move", ticket, done_status],
                       capture_output=True, text=True, check=False)
        subprocess.run(
            ["sos-jira", "comment", ticket,
             f"PR #{pr_num} merged via sos-flow-dev."],
            capture_output=True, text=True, check=False,
        )

    post_card(
        "info", f"Merged · {ticket}",
        ticket=ticket, url=sess.get("pr_url") or None,
        ctx=f"PR #{pr_num} merged · {strategy} → {sess.get('parent_branch', 'parent')}",
        actions=[
            {"label": "Open PR", "kind": "openUrl"},
            {"label": "Cleanup worktree", "kind": "inject",
             "text": f"sos-flow-dev cleanup {ticket}\n", "execute": True},
        ],
    )
    session_set(ticket, phase="merged", merged_at=now_iso(),
                merged_via=strategy)
    check(f"{ticket} merged (PR #{pr_num}, {strategy})")


def cmd_qa_reject(args):
    ticket = args.ticket
    reason = args.reason or ""
    sess = session_get(ticket) or {}
    pr_num = sess.get("pr_num") or fail("no PR number on record")
    session_set(ticket, reject_reason=reason, phase="work-3")
    w = phase_work3(ticket, pr_num, reason)
    if "failed" in w:
        fail(f"work-3 failed: {w.get('failed')}")
    preview_url = w.get("url") or sess.get("preview_url", "")
    session_set(ticket, preview_url=preview_url, phase="awaiting-qa-2")
    post_card(
        "action", f"Re-QA on {ticket}",
        ticket=ticket, url=preview_url or sess.get("pr_url") or None,
        ctx=f"PR #{pr_num} · re-review after feedback",
        actions=qa_card_actions(ticket, sess.get("pr_url", ""), preview_url),
    )
    check("re-QA card posted")


# ─── Preview server management ─────────────────────────────────────────────

PREVIEW_PORT_MIN = 6006
PREVIEW_PORT_MAX = 6099

# Ports already assigned within THIS process's current invocation. Without
# this set, when a single `sos-flow-dev preview` call starts multiple
# services back-to-back, each picks the same next-free port because the
# previous service hasn't had time to actually bind yet.
_RUNTIME_CLAIMED_PORTS = set()


def _next_free_port(start=PREVIEW_PORT_MIN, end=PREVIEW_PORT_MAX):
    """Return the first port in [start, end] that is (a) not bound on localhost
    AND (b) not already claimed within this invocation of sos-flow-dev.
    """
    import socket
    for p in range(start, end + 1):
        if p in _RUNTIME_CLAIMED_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}-{end}")


def _port_reachable(port, timeout=1.0):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _wait_for_port(port, timeout_s=30):
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _port_reachable(port):
            return True
        time.sleep(1)
    return False


def _load_global_config():
    """Return the parsed ~/.ghostty-mini/config.json, or {} if absent/broken.

    Recognized keys:
      source_repo         — path to the main git checkout where worktrees fork from
      default_base_branch — branch to use as --base when not explicitly given
    """
    if not _global_config_path().exists():
        return {}
    try:
        return json.loads(_global_config_path().read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_global_config(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _global_config_path().write_text(json.dumps(cfg, indent=2))


def _resolve_source_repo():
    """Find the source (main) git repo for flow-dev, regardless of CWD.

    Order:
      1. $SOS_FLOW_DEV_SOURCE env var, if set and exists.
      2. `source_repo` from ~/.ghostty-mini/config.json. The ghostty-mini UI's
         project picker writes this key when the operator switches projects,
         so it represents an *intentional* pin and must beat whatever repo
         the shell happens to be sitting in.
      3. CWD's git context — fallback for operators who haven't configured
         a source_repo yet; handles both "CWD is the main repo" AND "CWD is
         itself a worktree" (walks up to the shared main).

    Returns an absolute Path or None. None means the operator is outside
    any git context AND has no config — they need to `cd` into the source
    OR set ~/.ghostty-mini/config.json.
    """
    env = os.environ.get("SOS_FLOW_DEV_SOURCE")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p.resolve()

    cfg = _load_global_config()
    src = cfg.get("source_repo")
    if src:
        p = Path(src).expanduser()
        if p.is_dir():
            return p.resolve()

    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        toplevel = ""
    if toplevel:
        top = Path(toplevel).resolve()
        source = _source_repo_for_worktree(top)
        return source or top

    return None


def _resolve_base_branch(explicit=None):
    """Return the base branch to fork worktrees from.

    Order: explicit --base flag → config's default_base_branch → None.
    """
    if explicit:
        return explicit
    cfg = _load_global_config()
    return cfg.get("default_base_branch") or None


def _source_repo_for_worktree(worktree_path):
    """Given a git worktree path, return the source (main) repo root, or None.

    Uses `git rev-parse --git-common-dir` which points at the shared `.git`
    directory; the main repo lives in its parent.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    common_dir = Path(r.stdout.strip())
    # Make absolute if git returned a relative path
    if not common_dir.is_absolute():
        common_dir = (Path(worktree_path) / common_dir).resolve()
    if common_dir.name == ".git":
        return common_dir.parent
    return None


def _read_preview_block(config_path):
    """Read and sanity-check the preview block from a .pm/config.json.

    Returns the dict, or None if the file is missing / unreadable / has no
    useful preview config (all-null legacy template counts as "no config").
    """
    if not config_path.exists():
        return None
    try:
        cfg = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    preview = cfg.get("preview")
    if not isinstance(preview, dict):
        return None
    # Legacy template has command=null, services=null etc. Treat that as unset.
    if not preview.get("command") and not preview.get("services"):
        return None
    return preview


def _preview_config_for_ticket(ticket):
    """Load the preview block. Prefers the worktree's .pm/config.json, falls
    back to the source repo's when the worktree has no preview configured —
    so adding the block once at the source is enough for every worktree.
    """
    sess = session_get(ticket) or {}
    wt = sess.get("worktree")
    if not wt:
        return None
    worktree = Path(wt)
    # Try the worktree first.
    cfg = _read_preview_block(worktree / ".pm" / "config.json")
    if cfg:
        return cfg
    # Fall back to the source repo.
    source = _source_repo_for_worktree(worktree)
    if source:
        return _read_preview_block(source / ".pm" / "config.json")
    return None


def _normalize_preview_config(cfg):
    """Return a list of service dicts [{name, command, cwd, port}] from any
    supported preview config shape.

    Legacy:   {"command": "...", "cwd": "..."}         → one service named "default"
    Multi:    {"services": [{"name": ..., "command": ..., "cwd": ...}]}

    `port` is INTENTIONALLY ignored from config. Ports are a runtime concern —
    the preview runner assigns them from its free pool (6006-6099) each time
    a service starts, so config stays portable across worktrees and never
    collides with itself on parallel runs. Pass `--port N` on the CLI to force
    a specific port for a one-off invocation.

    Services without a `command` are dropped.
    """
    if not cfg or not isinstance(cfg, dict):
        return []
    svcs = cfg.get("services")
    if isinstance(svcs, list) and svcs:
        out = []
        for i, s in enumerate(svcs):
            if not isinstance(s, dict) or not s.get("command"):
                continue
            out.append({
                "name": (s.get("name") or f"svc{i}").lower(),
                "command": s["command"],
                "cwd": s.get("cwd") or "",
                "port": None,  # always auto-assign at runtime
            })
        return out
    if cfg.get("command"):
        return [{
            "name": "default",
            "command": cfg["command"],
            "cwd": cfg.get("cwd") or "",
            "port": None,
        }]
    return []


def _resolve_preview_tickets(args):
    """Figure out which tickets --all / positional / current-dir refer to."""
    if args.tickets:
        return list(args.tickets)
    if args.all:
        d = STATE_DIR / "sessions"
        if not d.exists():
            return []
        tickets = []
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("worktree"):
                tickets.append(f.stem)
        return tickets
    return []


def _preview_tmux_name(ticket, service):
    return f"preview-{ticket}-{service}"


def _assign_ports(services):
    """Ensure each service has a port. Ports without a value get next-free."""
    # Explicit ports in the input are already-claimed.
    for s in services:
        if s.get("port"):
            _RUNTIME_CLAIMED_PORTS.add(s["port"])
    for s in services:
        if s.get("port"):
            continue
        p = _next_free_port()
        s["port"] = p
        _RUNTIME_CLAIMED_PORTS.add(p)


def _start_preview_for(ticket, services, wait=True):
    """Start one or more preview services for a ticket.

    Returns a list of dicts: [{name, session, url, error}, ...].
    On success error is None; on failure session/url may be None.
    """
    sess = session_get(ticket) or {}
    wt = sess.get("worktree")
    if not wt or not Path(wt).is_dir():
        return [{"name": "*", "session": None, "url": None,
                 "error": f"no worktree on record for {ticket}"}]
    worktree = Path(wt)

    _assign_ports(services)

    results = []
    preview_urls = dict(sess.get("preview_urls") or {})
    preview_sessions = dict(sess.get("preview_sessions") or {})
    # Legacy single-service state migration
    if not preview_urls and sess.get("preview_url") and sess.get("preview_session"):
        preview_urls["default"] = sess["preview_url"]
        preview_sessions["default"] = sess["preview_session"]

    for svc in services:
        name = svc["name"]
        command = svc["command"]
        cwd = svc.get("cwd") or ""
        port = svc["port"]
        tmux_session = _preview_tmux_name(ticket, name)
        full_cwd = worktree / cwd if cwd else worktree

        if not full_cwd.is_dir():
            results.append({"name": name, "session": None, "url": None,
                            "error": f"cwd {full_cwd} not found"})
            continue
        if _tmux_session_exists(tmux_session):
            results.append({"name": name, "session": tmux_session,
                            "url": preview_urls.get(name),
                            "error": f"already running (tmux: {tmux_session})"})
            continue
        if _port_reachable(port):
            try:
                fallback = _next_free_port(start=port + 1)
            except RuntimeError as e:
                results.append({"name": name, "session": None, "url": None,
                                "error": f"port {port} in use and no free port: {e}"})
                continue
            _RUNTIME_CLAIMED_PORTS.add(fallback)
            results_warn = f"port {port} in use, switched to {fallback}"
            port = fallback
        else:
            results_warn = None

        log_path = worktree / ".pm" / f"preview-{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        wrapped = (
            f"cd {shlex.quote(str(full_cwd))} && "
            f"PORT={port} exec {command} 2>&1 | tee {shlex.quote(str(log_path))}"
        )
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_session,
             "-x", "220", "-y", "50", "/bin/sh", "-c", wrapped],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            results.append({"name": name, "session": None, "url": None,
                            "error": f"tmux spawn failed: {r.stderr.strip()}"})
            continue

        url = f"http://localhost:{port}"
        preview_urls[name] = url
        preview_sessions[name] = tmux_session
        results.append({"name": name, "session": tmux_session, "url": url,
                        "error": results_warn})

    # Persist consolidated state. `preview_url` (singular) stays populated with
    # the primary — first service that started cleanly — so existing
    # QA-card logic keeps working.
    primary_url = next(
        (r["url"] for r in results if r["session"] and r["url"]),
        None,
    )
    session_set(ticket,
                preview_urls=preview_urls,
                preview_sessions=preview_sessions,
                preview_url=primary_url or "")

    if wait:
        for r in results:
            if r["error"] is not None or r["url"] is None:
                continue
            port = int(r["url"].rsplit(":", 1)[-1])
            if not _wait_for_port(port, timeout_s=60):
                r["error"] = f"warning: port {port} didn't respond in 60s"

    return results


def _stop_preview_for(ticket, service_names=None):
    """Stop one, some, or all preview services for a ticket.

    service_names=None  → stop every recorded service
    service_names=["x","y"] → stop only those

    Returns (stopped_names, error_messages).
    """
    sess = session_get(ticket) or {}
    sessions = dict(sess.get("preview_sessions") or {})
    urls = dict(sess.get("preview_urls") or {})
    # Legacy single-service state
    if not sessions and sess.get("preview_session"):
        sessions["default"] = sess["preview_session"]
        if sess.get("preview_url"):
            urls["default"] = sess["preview_url"]

    if not sessions:
        return [], [f"{ticket}: no preview sessions recorded"]

    targets = service_names or list(sessions.keys())
    stopped, errors = [], []
    for name in targets:
        sname = sessions.get(name)
        if not sname:
            errors.append(f"{ticket}: no service named {name!r}")
            continue
        if _tmux_session_exists(sname):
            subprocess.run(["tmux", "kill-session", "-t", sname],
                           check=False, capture_output=True)
        stopped.append(name)
        sessions.pop(name, None)
        urls.pop(name, None)

    primary = next(iter(urls.values()), "")
    session_set(ticket,
                preview_urls=urls, preview_sessions=sessions,
                preview_url=primary,
                # Clear legacy fields
                preview_session=None, preview_port=None)
    return stopped, errors


def _post_preview_card(ticket, results):
    """Post one info card summarizing services that started CLEANLY.

    Deliberately skips services with a non-None `error` — that field holds
    either a hard failure (session=None) or a soft warning (e.g. "port
    didn't respond in 60s"). Posting an "Open preview" button pointing at a
    port that isn't actually answering is worse than no card at all. The
    warning has already been printed to stderr by the caller.

    If NO service started cleanly for this ticket, no card is posted.
    """
    running = [r for r in results if r["session"] and r["url"] and not r["error"]]
    if not running:
        return
    primary = running[0]["url"]
    ctx_parts = []
    for r in running:
        try:
            port = r["url"].rsplit(":", 1)[-1]
        except Exception:
            port = "?"
        ctx_parts.append(f"{r['name']}→{port}")
    sess = session_get(ticket) or {}
    pr_url = sess.get("pr_url") or "(none)"
    ctx = f"Services: {' · '.join(ctx_parts)} · PR {pr_url}"

    actions = []
    for r in running:
        label = "Open preview" if r["name"] == "default" else f"Open {r['name']}"
        actions.append({"label": label, "kind": "openUrl", "url": r["url"]})
    actions.append({
        "label": "Stop all" if len(running) > 1 else "Stop",
        "kind": "inject",
        "text": f"sos-flow-dev preview --stop {ticket}\n",
        "execute": True,
    })
    post_card("info", f"Preview ready · {ticket}", ticket=ticket,
              url=primary, ctx=ctx, actions=actions)


def cmd_preview(args):
    """Start/stop/list preview dev-servers. Supports multiple services per ticket."""
    tickets = _resolve_preview_tickets(args)
    selected_services = set(args.service) if args.service else None

    if args.list:
        r = subprocess.run(["tmux", "ls"], capture_output=True, text=True)
        names = [l.split(":")[0] for l in (r.stdout or "").splitlines()
                 if l.startswith("preview-")]
        if not names:
            print("(no preview sessions running)")
            return
        print("Running preview sessions:")
        for n in sorted(names):
            # preview-<TICKET>-<SERVICE>
            tail = n[len("preview-"):]
            parts = tail.rsplit("-", 1)
            if len(parts) == 2:
                t, svc = parts
            else:
                t, svc = tail, "default"
            sess = session_get(t) or {}
            url = (sess.get("preview_urls") or {}).get(svc) \
                  or sess.get("preview_url") or "?"
            print(f"  {n:<32}  {t:<12}  {svc:<12}  {url}")
        return

    if args.stop:
        if not tickets:
            fail("pass TICKET positional(s) or --all to select what to stop")
        for t in tickets:
            stopped, errors = _stop_preview_for(
                t, list(selected_services) if selected_services else None)
            for name in stopped:
                check(f"stopped {t}/{name}")
            for msg in errors:
                print(f"✗ {msg}", file=sys.stderr)
        return

    if not tickets:
        fail("pass TICKET positional(s) or --all to start previews")

    any_started = False
    for t in tickets:
        # Determine services to launch
        if args.command:
            # CLI override → single synthetic service
            svc_name = (next(iter(selected_services))
                        if selected_services else "default")
            services = [{
                "name": svc_name,
                "command": args.command,
                "cwd": args.cwd or "",
                "port": args.port,
            }]
        else:
            cfg = _preview_config_for_ticket(t)
            services = _normalize_preview_config(cfg)
            if selected_services:
                services = [s for s in services if s["name"] in selected_services]
            if not services:
                print(f"✗ {t}: no preview services configured.", file=sys.stderr)
                print(f"    Fix: add a `preview` block to the source repo's "
                      f".pm/config.json (applies to all worktrees), the "
                      f"worktree's own .pm/config.json, or pass --command/--cwd "
                      f"on this invocation.", file=sys.stderr)
                continue

        step(f"starting {len(services)} preview service(s) for {t}")
        results = _start_preview_for(t, services, wait=args.wait)
        for r in results:
            if r["session"] is None:
                print(f"  ✗ {r['name']}: {r['error']}", file=sys.stderr)
            elif r["error"]:
                print(f"  ⚠ {r['name']}: {r['error']}", file=sys.stderr)
            else:
                check(f"  {r['name']}: {r['url']}  (tmux: {r['session']})")
        _post_preview_card(t, results)
        if any(r["session"] and not r["error"] for r in results):
            any_started = True

    if any_started:
        print()
        print("Cards posted to each ticket's section in the sidebar.")


PHASE_TO_LIVE_SESSION = {
    "alloc":          lambda t: f"flow-{t}-alloc",
    "work-1":         lambda t: f"pm-{t}",
    "review":         lambda t: f"flow-{t}-review",
    "work-2":         lambda t: f"flow-{t}-work2",
    "work-3":         lambda t: f"flow-{t}-work3",
    "awaiting-qa":    lambda t: None,     # no leaf — waiting for human
    "awaiting-qa-2":  lambda t: None,
    "merged":         lambda t: None,
}


def _elapsed_since(iso_ts):
    if not iso_ts:
        return "—"
    try:
        started = datetime.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return "—"
    delta = datetime.datetime.now(datetime.timezone.utc) - started
    total_s = int(delta.total_seconds())
    if total_s < 60:
        return f"{total_s}s"
    m, s = divmod(total_s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _live_session_for(ticket, phase):
    """Return the leaf tmux session name for this phase, or None if not applicable."""
    builder = PHASE_TO_LIVE_SESSION.get(phase)
    if builder is None:
        return None
    return builder(ticket)


def _tmux_session_exists(name):
    if not name:
        return False
    r = subprocess.run(["tmux", "has-session", "-t", name],
                       capture_output=True)
    return r.returncode == 0


def _render_watch_table(tickets_filter=None):
    """Snapshot of session state → list of (ticket, phase, live_session, elapsed, pr, preview) tuples."""
    d = STATE_DIR / "sessions"
    if not d.exists():
        return []
    rows = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        t = f.stem
        if tickets_filter and t not in tickets_filter:
            continue
        phase = data.get("phase", "?")
        leaf = _live_session_for(t, phase)
        leaf_str = ""
        if leaf:
            leaf_str = leaf if _tmux_session_exists(leaf) else f"{leaf} (gone)"
        elapsed = _elapsed_since(data.get("started_at", ""))
        pr_num = data.get("pr_num") or ""
        pr_str = f"#{pr_num}" if pr_num else "—"
        preview = data.get("preview_url") or "—"
        rows.append((t, phase, leaf_str or "—", elapsed, pr_str, preview))
    return rows


def _print_watch(rows, first=False):
    """Print the dashboard, overwriting the previous render in place on repeats."""
    if not first and rows:
        # Move cursor up by (2 header lines + number of rows) and clear to end of screen
        sys.stdout.write(f"\x1b[{len(rows) + 3}A\x1b[J")
    # Columns sized for typical content; preview URL truncates but stays readable.
    hdr = f"{'TICKET':<12}  {'PHASE':<14}  {'LIVE TMUX':<30}  {'ELAPSED':<10}  {'PR':<6}  PREVIEW"
    print(hdr)
    print("─" * len(hdr))
    if not rows:
        print("(no active flow-dev sessions — run `sos-flow-dev start TICKET`)")
        return
    for t, phase, leaf, elapsed, pr, preview in rows:
        preview_short = preview[:40] + "…" if len(preview) > 41 else preview
        print(f"{t:<12}  {phase:<14}  {leaf:<30}  {elapsed:<10}  {pr:<6}  {preview_short}")


def cmd_watch(args):
    """Live dashboard of all active flow-dev sessions. Ctrl+C to exit."""
    interval = max(1, args.interval)
    tickets_filter = set(args.tickets) if args.tickets else None
    import time
    first = True
    try:
        while True:
            rows = _render_watch_table(tickets_filter)
            _print_watch(rows, first=first)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n(detached — runs continue. `sos-flow-dev watch` to resume.)")


def _existing_card_titles(ticket):
    """Return the set of card titles currently in the inbox for this ticket."""
    r = subprocess.run(
        ["sos-inbox", "list", "--ticket", ticket, "--json"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0 or not r.stdout:
        return set()
    try:
        cards = json.loads(r.stdout)
    except json.JSONDecodeError:
        return set()
    return {c.get("title", "") for c in cards if isinstance(c, dict)}


def _resync_cards_for(ticket):
    """Re-post the standard flow-dev cards for a ticket from session state.

    Idempotent — a card whose title already exists for the ticket is skipped.
    Returns the count of cards actually posted.
    """
    sess = session_get(ticket)
    if not sess:
        return 0
    pr_url = sess.get("pr_url") or ""
    pr_num = sess.get("pr_num") or ""
    phase = sess.get("phase", "")
    existing = _existing_card_titles(ticket)
    posted = 0

    def _maybe_post(kind, title, **kw):
        nonlocal posted
        if title in existing:
            return
        post_card(kind, title, ticket=ticket, **kw)
        posted += 1

    # 1. PR opened (info)
    if pr_num and phase not in ("alloc", "work-1", ""):
        preview_url = sess.get("preview_url") or ""
        ctx = (f"Work 1 done · preview at {preview_url}" if preview_url
               else "Work 1 done · no preview configured")
        _maybe_post("info", f"PR #{pr_num} opened", url=pr_url, ctx=ctx)

    # 2. Review posted (info)
    verdict = sess.get("review_verdict")
    comments = sess.get("review_comments")
    if verdict is not None and comments is not None:
        _maybe_post("info", f"Review posted — {comments} comments",
                    url=pr_url or None, ctx=f"Verdict: {verdict}")

    # 3. Phase-specific card
    if phase == "awaiting-qa":
        preview_url = sess.get("preview_url") or ""
        ctx = (f"PR #{pr_num} · preview at {preview_url} · QA per PR description"
               if preview_url
               else f"PR #{pr_num} · see PR description for QA steps")
        _maybe_post("action", "Ready for QA",
                    url=(preview_url or pr_url or None),
                    ctx=ctx,
                    actions=qa_card_actions(ticket, pr_url, preview_url))
    elif phase == "awaiting-qa-2":
        preview_url = sess.get("preview_url") or ""
        _maybe_post("action", f"Re-QA on {ticket}",
                    url=(preview_url or pr_url or None),
                    ctx=f"PR #{pr_num} · re-review after feedback",
                    actions=qa_card_actions(ticket, pr_url, preview_url))
    elif phase == "merged":
        _maybe_post("info", f"Merged · {ticket}",
                    url=pr_url or None, ctx="branch merged into parent")

    # 4. Preview ready (info) — only if services are actually running
    preview_urls = sess.get("preview_urls") or {}
    preview_sessions = sess.get("preview_sessions") or {}
    live_services = [
        (name, url) for name, url in preview_urls.items()
        if _tmux_session_exists(preview_sessions.get(name, ""))
    ]
    if live_services and f"Preview ready · {ticket}" not in existing:
        fake_results = [
            {"name": name, "session": preview_sessions.get(name, "?"),
             "url": url, "error": None}
            for name, url in live_services
        ]
        _post_preview_card(ticket, fake_results)
        posted += 1

    return posted


def cmd_resync(args):
    """Restore missing flow-dev cards for tickets by re-posting from session state."""
    if args.tickets:
        tickets = list(args.tickets)
    elif args.all:
        d = STATE_DIR / "sessions"
        tickets = sorted(f.stem for f in d.glob("*.json")) if d.exists() else []
    else:
        fail("pass TICKET(s) or --all")

    total = 0
    for t in tickets:
        n = _resync_cards_for(t)
        if n:
            check(f"{t}: posted {n} card(s)")
            total += n
        else:
            print(f"  {t}: nothing to resync (no state or already in sync)",
                  file=sys.stderr)
    print(f"\n{total} card(s) restored.")


def cmd_config(args):
    """Read or write ~/.ghostty-mini/config.json without hand-editing JSON."""
    cfg = _load_global_config()
    if args.action == "get":
        if args.key:
            val = cfg.get(args.key)
            print("" if val is None else json.dumps(val))
        else:
            print(json.dumps(cfg, indent=2))
        return
    if args.action == "set":
        if not args.key or args.value is None:
            fail("usage: sos-flow-dev config set KEY VALUE")
        # Reject bogus keys early to catch typos.
        allowed = {"source_repo", "default_base_branch"}
        if args.key not in allowed:
            fail(f"unknown config key {args.key!r}; allowed: {', '.join(sorted(allowed))}")
        val = args.value
        if args.key == "source_repo":
            p = Path(val).expanduser()
            if not p.is_dir():
                fail(f"source_repo path {val} is not a directory")
            val = str(p.resolve())
        cfg[args.key] = val
        _save_global_config(cfg)
        check(f"config {args.key} = {val}")
        return
    if args.action == "unset":
        if not args.key:
            fail("usage: sos-flow-dev config unset KEY")
        if args.key in cfg:
            del cfg[args.key]
            _save_global_config(cfg)
            check(f"config {args.key} removed")
        else:
            print(f"(no {args.key} in config)")
        return
    fail(f"unknown action {args.action}")


def cmd_status(args):
    if args.ticket:
        sess = session_get(args.ticket)
        if not sess:
            fail(f"no session for {args.ticket}")
        print(json.dumps(sess, indent=2))
        return
    d = STATE_DIR / "sessions"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        print("(no active flow-dev sessions)")
        return
    for f in files:
        data = json.loads(f.read_text())
        t = f.stem
        phase = data.get("phase", "?")
        pr = data.get("pr_url", "")
        preview = data.get("preview_url", "")
        print(f"{t}  phase={phase:<14}  pr={pr}  preview={preview}")


PM_STATE_FILES_TO_DROP = [
    "active-ticket.json", "work-summary.md", "dev-agent-instructions.md",
    "review-result.json", "work-2-result.json", "work-3-result.json",
    "failed.json", "preview.log", "summary.md",
]


def _reset_worktree_for_reuse(wt, parent):
    """Put a worktree back into 'available in the pool' state.

    Preserves: .pm/config.json (port assignment), node_modules, build caches.
    Drops: ticket-scoped .pm/ files, uncommitted changes.
    """
    wt_path = Path(wt)
    if not wt_path.is_dir():
        fail(f"worktree path {wt} not found — already cleaned up?")
    step(f"resetting {wt} to {parent} for reuse")
    try:
        subprocess.run(["git", "-C", wt, "fetch", "origin", parent],
                       check=False, capture_output=True)
        subprocess.run(["git", "-C", wt, "switch", parent], check=True)
        # Prefer origin/<parent>; fall back to local if origin doesn't have it.
        try:
            subprocess.run(["git", "-C", wt, "reset", "--hard", f"origin/{parent}"],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "-C", wt, "reset", "--hard", parent], check=True)
        subprocess.run(["git", "-C", wt, "clean", "-fd"], check=False)
    except subprocess.CalledProcessError as e:
        fail(f"git reset failed during cleanup: {e}")
    pm_dir = wt_path / ".pm"
    if pm_dir.is_dir():
        for name in PM_STATE_FILES_TO_DROP:
            p = pm_dir / name
            if p.exists():
                try: p.unlink()
                except OSError: pass
    check(f"worktree {wt_path.name} reset — available for reuse")


def _remove_worktree(wt):
    step(f"removing worktree {wt}")
    r = subprocess.run(["git", "worktree", "remove", "--force", wt], check=False,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  git worktree remove warning: {r.stderr.strip()}", file=sys.stderr)


def cmd_cleanup(args):
    ticket = args.ticket
    sess = session_get(ticket) or {}
    wt = sess.get("worktree")
    parent = sess.get("parent_branch") or "main"

    # Stop every preview service this ticket spawned so ports are freed.
    stopped, _ = _stop_preview_for(ticket)
    for name in stopped:
        check(f"stopped preview {ticket}/{name}")

    if wt:
        if args.remove:
            _remove_worktree(wt)
        else:
            _reset_worktree_for_reuse(wt, parent)

    session_rm(ticket)

    # Transient per-ticket files.
    pm_complete = Path(f"/tmp/pm-complete-{ticket}.json")
    if pm_complete.exists():
        pm_complete.unlink()
    alloc = Path(f"/tmp/flow-{ticket}-worktree.json")
    if alloc.exists():
        alloc.unlink()
    for p in _glob.glob(f"/tmp/sos-flow-dev-flow-{ticket}-*"):
        try: os.unlink(p)
        except OSError: pass
    check(f"cleaned up {ticket}")


# ─── argparse wiring ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="sos-flow-dev",
        description=(
            "Deterministic ticket workflow orchestrator. "
            "Runs Work 1 → Review → Work 2 → QA gate as sequential phases, "
            "each inside a detached tmux session."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser(
        "start",
        help="Full workflow per ticket: allocate worktree → Work 1 → Review → Work 2 → QA card",
        description=(
            "One ticket: blocks the current terminal and runs end-to-end. "
            "Multiple tickets (or --detach): fans each out to its own detached "
            "tmux session (flow-runner-<TICKET>), returns the tmux session names "
            "immediately. Phase 0 allocation still serializes across runners via "
            "an exclusive flock so pool worktrees don't collide."
        ),
    )
    p.add_argument("tickets", nargs="+", metavar="TICKET",
                   help="One or more ticket IDs to run in parallel")
    p.add_argument("--base", default=None,
                   help="Hint to the worktree-alloc subagent about the parent branch (e.g. 'sbook/epic'). "
                        "If omitted, the subagent infers from context and asks via sos-inbox prompt if ambiguous.")
    p.add_argument("--iteration", default="first-pass",
                   help="Iteration name passed to pm-start. Drives branch name "
                        "(feature/<TICKET>-<iteration>). Use a fresh name to restart a ticket "
                        "without colliding with an existing branch (e.g. 'iteration-2').")
    p.add_argument("--pause-after", choices=["work1", "review", "work2"], default=None,
                   help="Post a gate card after the named phase; requires a reply to continue")
    p.add_argument("--detach", action="store_true",
                   help="Force detached tmux runner even for a single ticket "
                        "(implicit when multiple tickets are passed)")
    p.add_argument("--watch", action="store_true",
                   help="After spawning runners, enter watch mode in this terminal. "
                        "Ctrl+C to detach (runs continue).")

    p = sub.add_parser("review", help="Re-run just the review phase against the current PR")
    p.add_argument("ticket")

    p = sub.add_parser("work2", help="Re-run just the work-2 phase")
    p.add_argument("ticket")
    p.add_argument("--comments", type=int, default=None,
                   help="Override review comment count (else read from session state)")

    p = sub.add_parser("qa-approve", help="Merge via /pm-finish")
    p.add_argument("ticket")

    p = sub.add_parser("qa-reject", help="Kick off work-3 with an optional reason")
    p.add_argument("ticket")
    p.add_argument("reason", nargs="?", default="")

    p = sub.add_parser(
        "config",
        help="Get/set global flow-dev settings at ~/.ghostty-mini/config.json",
        description=(
            "Allowed keys: source_repo (path to the main git checkout where "
            "worktrees fork from), default_base_branch (branch used when "
            "--base is omitted). These let you run sos-flow-dev from any CWD."
        ),
    )
    p.add_argument("action", choices=["get", "set", "unset"],
                   help="get/set/unset a config key")
    p.add_argument("key", nargs="?", default=None,
                   help="Key name (get with no key prints the full config)")
    p.add_argument("value", nargs="?", default=None,
                   help="Value (for set)")

    p = sub.add_parser("status", help="Show session state (all tickets or one)")
    p.add_argument("ticket", nargs="?", default=None)

    p = sub.add_parser(
        "resync",
        help="Re-post the standard flow-dev cards for a ticket from session state",
        description=(
            "When cards get wiped (accidental `sos-inbox clear`, server "
            "restart before persistence, etc), this reads session state and "
            "re-posts whichever standard cards the ticket's phase warrants: "
            "'PR #N opened', 'Review posted', the QA gate action card, "
            "and any running preview cards. Idempotent — cards whose titles "
            "already exist are skipped."
        ),
    )
    p.add_argument("tickets", nargs="*",
                   help="Specific tickets to restore (omit + use --all for every one)")
    p.add_argument("--all", action="store_true",
                   help="Restore cards for every ticket that has session state")

    p = sub.add_parser(
        "watch",
        help="Live dashboard of active flow-dev sessions (updates in place; Ctrl+C to exit)",
    )
    p.add_argument("tickets", nargs="*",
                   help="Optionally filter to these ticket IDs")
    p.add_argument("--interval", "-i", type=int, default=3,
                   help="Refresh interval in seconds (default: 3)")

    p = sub.add_parser(
        "preview",
        help="Start/stop preview dev servers for active tickets on demand",
        description=(
            "Spin up a detached tmux session running the project's preview "
            "command (from each worktree's .pm/config.json `preview.command`, "
            "or --command override), bind a unique port, and post an info "
            "card with the URL to the sidebar. Use this when the original "
            "flow ran without a preview configured, or to revive a preview "
            "after it died. `--stop` tears one down."
        ),
    )
    p.add_argument("tickets", nargs="*",
                   help="Ticket(s) to start/stop previews for")
    p.add_argument("--all", action="store_true",
                   help="Apply to all active tickets (those with a worktree on record)")
    p.add_argument("--stop", action="store_true",
                   help="Stop the preview instead of starting it")
    p.add_argument("--list", action="store_true",
                   help="Show currently-running preview sessions")
    p.add_argument("--service", action="append", default=[], metavar="NAME",
                   help="Target specific named service (repeat for multiple). "
                        "Default: start/stop ALL services defined in config.")
    p.add_argument("--command", default=None,
                   help="Preview command (overrides .pm/config.json preview.command). "
                        "When passed, only one service is launched.")
    p.add_argument("--cwd", default=None,
                   help="Subdirectory (relative to worktree) to run preview from")
    p.add_argument("--port", type=int, default=None,
                   help="Starting port (auto-bumps if more needed). "
                        "Default: next free port in 6006–6099.")
    p.add_argument("--no-wait", dest="wait", action="store_false", default=True,
                   help="Don't block waiting for the port to come up")

    p = sub.add_parser(
        "cleanup",
        help="Reset the ticket's worktree for reuse (default) or remove it outright",
    )
    p.add_argument("ticket")
    p.add_argument("--remove", action="store_true",
                   help="Destroy the worktree (git worktree remove --force) instead of "
                        "resetting it for reuse. Use when you want to shrink the pool.")

    args = parser.parse_args()

    {
        "start": cmd_start,
        "review": cmd_review,
        "work2": cmd_work2,
        "qa-approve": cmd_qa_approve,
        "qa-reject": cmd_qa_reject,
        "status": cmd_status,
        "watch": cmd_watch,
        "preview": cmd_preview,
        "resync": cmd_resync,
        "config": cmd_config,
        "cleanup": cmd_cleanup,
    }[args.subcommand](args)


if __name__ == "__main__":
    main()
