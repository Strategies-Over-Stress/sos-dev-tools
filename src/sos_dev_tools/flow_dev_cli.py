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
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("GHOSTTY_MINI_STATE", str(Path.home() / ".ghostty-mini")))
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
    """Post an info/action card via sos-inbox; return card id (or '' on error)."""
    cmd = ["sos-inbox", kind, title]
    if ticket: cmd += ["--ticket", ticket]
    if url:    cmd += ["--url", url]
    if ctx:    cmd += ["--ctx", ctx]
    if actions: cmd += ["--actions", json.dumps(actions)]
    try:
        return run_capture(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # A missing sidebar never blocks the flow — sos-inbox already
        # silent-no-ops on unreachable; this catches the case where
        # sos-inbox itself isn't installed.
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


def worktree_alloc_prompt(ticket, cwd, hint_base=None):
    """Phase-0 subagent: decide where to do the work, write the answer to a file."""
    base_hint = (
        f"The user passed `--base {hint_base}` — use that as the parent branch "
        f"unless you find a strong reason not to.\n"
        if hint_base else
        "No `--base` was supplied. Infer the parent from context: the branch "
        "currently checked out in CWD, hints in `.pm/config.json` "
        "(`git.default_base` or `git.branch_prefix`), or recent flow-dev "
        "sessions at `$HOME/.ghostty-mini/sessions/*.json`. If you can't "
        "resolve it confidently, ASK via `sos-inbox prompt`.\n"
    )
    return f"""You are allocating a git worktree for ticket {ticket}. The work
happens in that worktree; the downstream phases (pm-start, Review, Work 2, QA)
expect to run inside whatever directory you pick.

CWD: {cwd}
POOL DIR: {cwd}/claude/worktrees/  (may not exist yet)
RESULT FILE: /tmp/flow-{ticket}-worktree.json  ← write your decision here

{base_hint}
## Steps

1. **Inspect CWD.**
   - Is it a git repo?  `git rev-parse --show-toplevel`  — if not, write
     `{{"failed": "CWD is not a git repo"}}` to the result file and exit 0.
   - Is CWD itself already a worktree (i.e., `git rev-parse --git-dir` returns
     a path under `.git/worktrees/`)? If so, you can probably use CWD directly
     unless it already has an active non-merged ticket.

2. **Inspect the pool** at `{cwd}/claude/worktrees/`.
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
   - Ensure `{cwd}/claude/worktrees/` exists (`mkdir -p`).
   - Pick the lowest unused integer N for `wt-N` (scan existing pool).
   - Create:  `git -C {cwd} worktree add claude/worktrees/wt-N <parent_branch>`
   - If CWD has a `.pm/config.json`, copy it into the new worktree's `.pm/` dir.
     Then bump `preview.port` by (N — the new worktree's index) so each pool
     entry has a unique port. If no config.json exists in CWD, skip this step
     (flow will fall through to "no preview" per pm-start's handling).
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

    Serializes concurrent callers via flock on $GHOSTTY_MINI_STATE/alloc.lock,
    so two simultaneous `sos-flow-dev start` invocations can't both pick the
    same pool worktree. Before releasing the lock, writes a stub
    .pm/active-ticket.json into the chosen worktree so the next allocator sees
    it as busy — pm-start overwrites this stub in its step 3 with the full
    version.
    """
    step(f"Phase 0/5 — allocating worktree for {ticket}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "alloc.lock"
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        cwd = os.getcwd()
        prompt = worktree_alloc_prompt(ticket, cwd, hint_base)
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


def phase_pm_start(ticket):
    step(f"Phase 1/5 — pm-start {ticket}")
    if not PM_START_SKILL.exists():
        fail(f"pm-start skill not found at {PM_START_SKILL}")
    body = PM_START_SKILL.read_text() + f"\n\n{ticket} first-pass\n"
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

def cmd_start(args):
    ticket = args.ticket
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
    pm = phase_pm_start(ticket)
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


def cmd_qa_approve(args):
    ticket = args.ticket
    if not PM_FINISH_SKILL.exists():
        fail(f"pm-finish skill not found at {PM_FINISH_SKILL}")
    step(f"delegating merge to pm-finish {ticket}")
    body = PM_FINISH_SKILL.read_text() + f"\n\n{ticket}\n"
    rc = run_subagent(f"flow-{ticket}-merge", body)
    if rc != 0:
        fail(f"pm-finish subagent exited {rc}")
    sess = session_get(ticket) or {}
    post_card(
        "info", f"Merged · {ticket}",
        ticket=ticket, url=sess.get("pr_url") or None,
        ctx="branch merged into parent",
        actions=[
            {"label": "Open PR", "kind": "openUrl"},
            {"label": "Cleanup worktree", "kind": "inject",
             "text": f"sos-flow-dev cleanup {ticket}\n", "execute": True},
        ],
    )
    session_set(ticket, phase="merged", merged_at=now_iso())
    check(f"{ticket} merged")


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
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="Full workflow: allocate worktree → Work 1 → Review → Work 2 → QA card")
    p.add_argument("ticket")
    p.add_argument("--base", default=None,
                   help="Hint to the worktree-alloc subagent about the parent branch (e.g. 'sbook/epic'). "
                        "If omitted, the subagent infers from context and asks via sos-inbox prompt if ambiguous.")
    p.add_argument("--pause-after", choices=["work1", "review", "work2"], default=None,
                   help="Post a gate card after the named phase; requires a reply to continue")

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

    p = sub.add_parser("status", help="Show session state (all tickets or one)")
    p.add_argument("ticket", nargs="?", default=None)

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
        "cleanup": cmd_cleanup,
    }[args.command](args)


if __name__ == "__main__":
    main()
