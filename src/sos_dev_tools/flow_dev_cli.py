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
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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


def review_prompt(ticket, pr_num, is_rereview=False):
    if is_rereview:
        return f"""You are a senior code reviewer on a RE-REVIEW of PR #{pr_num}.

## Scope

A prior review cycle posted comments; a work-N agent has since pushed
commits to address them. Your job has three parts:

1. **Verify prior comments are resolved.** For each comment from the
   prior round, read the file in its current state and the commit(s)
   that were supposed to address it. If any prior issue is still open
   (unfixed, partially fixed, or wrongly fixed), post a new comment
   quoting the prior comment id and pointing at the remaining gap.

2. **Catch regressions.** If work-N's fixes broke anything that was
   working before, post those too.

3. **Catch BLOCKERS the first review missed.** Real bugs the initial
   reviewer overlooked are legitimate findings — do not suppress them
   just because they weren't raised last round. Apply the SAME
   blocker-vs-preference standard as a first-pass review (below).

## What counts as a BLOCKER (post it)

- Logic bugs and edge-case breakage (null/empty/huge inputs,
  off-by-one, race conditions, data loss, state desync)
- Security holes (unvalidated input, XSS, secret leakage)
- Missed acceptance criteria
- Regressions (functionality worse than before work-N)
- Accessibility blockers (prefers-reduced-motion ignored,
  missing aria, broken keyboard nav)
- Self-contradicting specs or docs that would mislead a downstream
  consumer (type vs example mismatch, undefined reference, math error)

## What does NOT count (do NOT post)

- "This could be extracted into a helper"
- "Consider using a reducer / hook / pattern X"
- Style or naming preferences not codified in the repo's tooling
- Pattern divergence from other files that doesn't actively break
- "Add a comment explaining Y"
- Speculative improvements for hypothetical future needs
- Things the first review mentioned that work-N addressed well
  enough — don't re-litigate

If you see a non-blocker finding worth preserving, write it to
`.pm/followups.md` (one bullet per item) instead of the PR. The
backlog is for refactors; the PR is for blockers.

## Steps

1. Read prior review comments:
       gh api repos/:owner/:repo/pulls/{pr_num}/comments
2. For each prior comment, inspect current state. Post a new comment
   ONLY if unresolved.
3. Read work-N's commits for regressions.
4. Scan the full diff for blocker-level issues the first reviewer
   may have missed. Ignore nits.
5. Submit the review:
       gh pr review {pr_num} --approve -b "..."          # all clear
       gh pr review {pr_num} --request-changes -b "..."  # blockers found
6. Write `.pm/review-result.json`:
       {{"comments": N, "verdict": "approve" | "changes-requested"}}
   Where N = count of comments YOU posted in THIS review.

Verdict rules:
- All prior resolved AND no regressions AND no missed blockers → `approve`
- Any unresolved prior, regression, or missed blocker → `changes-requested`

The anti-churn lever is the blocker-vs-preference rule, not a
"prior-comments-only" rule. Real bugs the first review missed are
real bugs; catching them is your job. Nits are nits; leave them.

{_blocker_for(ticket)}
"""

    return f"""You are a senior code reviewer. Review PR #{pr_num} in this repo.

Ticket context: read `.pm/active-ticket.json` for summary + acceptance criteria.

## This is your ONLY thorough pass

The re-review after work-N is DELIBERATELY scope-locked: it verifies
your specific comments are resolved and catches regressions, nothing
else. It cannot post new findings to discover issues you missed here.
That means every real concern in this PR has to be caught NOW. Treat
this as the only review that will happen.

## Steps

1. Read the full PR diff:          `gh pr diff {pr_num}`
2. Read the PR description:         `gh pr view {pr_num}`
3. Read the ticket and ACs:         `.pm/active-ticket.json`
4. For EVERY file in the diff, read the COMPLETE updated file (not just
   the hunk). Context outside the diff often reveals the bug.

## Check all of these categories

### Correctness (highest priority — these block merge)
- Logic bugs, incorrect conditionals, off-by-one, wrong operator
- Edge cases: empty arrays, null/undefined, zero, negative, huge N
- Race conditions between async operations, unordered promise resolves
- State management bugs: stale closures, unmounted-component updates,
  effect dependency omissions
- Error handling: missing try/catch, unhandled promise rejections,
  thrown errors that surface to the user as "undefined"
- Data loss: async-overwritten user input, lost-update patterns on
  shared state

### Acceptance criteria (mechanically verify each one)
For each AC in `.pm/active-ticket.json`:
- Identify the code that implements it (file + line range)
- Confirm it actually satisfies the criterion as written (not "looks
  like it would")
- If the ticket specifies a numeric target (N variants, K fps, etc.),
  count/measure mechanically in the diff — don't trust agent claims

### Security
- Unvalidated input on routes / API handlers (path injection, huge
  bodies, missing auth)
- Secrets in code or env-leakage into logs
- XSS (innerHTML, dangerouslySetInnerHTML with user input)
- CSRF on state-changing endpoints

### Accessibility (when UI code)
- Missing alt text, aria-labels, keyboard handlers
- `prefers-reduced-motion` ignored on new animations
- Contrast, focus rings, tab order

### Performance (when the ticket is UI/perf-sensitive)
- Unnecessary re-renders (unstable keys, inline object/function props
  passed to memoized children)
- N+1 queries, sync XHR, blocking main thread
- Memory leaks: uncleaned intervals/listeners, growing caches
- Large bundle imports where a smaller one would do

### Hygiene
- Dead code, commented-out blocks, leftover debug prints
- Stray TODO/FIXME comments without a ticket reference
- New dev dependencies not noted in work-summary
- Style inconsistencies with the repo's existing conventions

## Anti-over-engineering — important

This pass is exhaustive about CORRECTNESS, not about "could be better."
For every comment you're about to post, ask: does this BLOCK merge, or
is it a preference? If it's a preference — a cleaner abstraction, a
different pattern, "consider using X" — DO NOT post it as a review
comment. Add it to `.pm/followups.md` as a one-line note instead.

Post review comments for:
- ✅ Bugs that would surface in production
- ✅ Security / privacy holes
- ✅ Missed acceptance criteria
- ✅ Regressions (functionality worse than before)
- ✅ Accessibility blockers
- ✅ Memory/perf issues on hot paths

Do NOT post review comments for:
- ❌ "This could be extracted into a helper"
- ❌ "Consider using a reducer instead of useState"
- ❌ "This pattern differs from other files" (unless it actively breaks)
- ❌ "Add a comment explaining X"
- ❌ Style preferences not codified in the repo's linter/formatter
- ❌ Speculative improvements ("if we wanted to add Y later, this
  structure would make it easier")

The cost of a false-positive review comment is high: work-N spends
effort addressing something that didn't need addressing, and the
re-review may catch regressions introduced by the over-fix. Be
specific, be concrete, and tie every comment to an ACTUAL problem,
not a hypothetical one.

## Posting comments + submitting the review

5. For each concrete issue, post an inline review comment on the
   relevant line:
       gh pr review {pr_num} --comment -F <feedback-file>
6. When done, write `.pm/review-result.json` in the worktree root:
       {{"comments": N, "verdict": "changes-requested" | "approve"}}
   Do NOT print the JSON to stdout — the orchestrator reads the file.

Verdict rules:
- Zero review comments posted → `approve`
- Any review comment posted → `changes-requested`

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
5. If a preview dev server is configured (see `.pm/config.json` preview), use
   it to verify in-flight.

**Do NOT write `.pm/work-2-result.json`.** A separate verifier agent reads
git state + PR state + your tmux log and writes the canonical deliverable.
Your job is to do the work; the verifier's job is to judge completion.

After you finish, the orchestrator will run the reviewer AGAIN against
your changes. Any comments the reviewer finds become the next iteration's
work. Make sure your commits directly address each review comment so
the re-review has nothing to flag.

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

**Do NOT write `.pm/work-3-result.json`.** A separate verifier agent reads
git state + PR state + your tmux log and writes the canonical deliverable.
Your job is to do the work; the verifier's job is to judge completion.

After you finish, the orchestrator will run the reviewer against your
changes. If the reviewer finds new issues, flow halts and surfaces them
to the operator.

{_blocker_for(ticket)}
"""


# ─── Verifier (phase-completion judge) ─────────────────────────────────────
#
# Workers are unreliable at self-reporting completion — they skip writing
# deliverable files, claim partial work is complete, or just exit early.
# That's not a bug in any specific worker; it's a structural problem with
# letting the same agent that did the work judge whether the work is done.
#
# Every phase (pm-start, work-2, work-3) runs the worker, then a separate
# verifier subagent. The verifier's single job: read the phase spec, read
# observable state (git, PR, tmux log), and write a verdict JSON. If the
# verifier says "incomplete", flow-dev re-invokes the worker with the
# verifier's specific feedback, up to max_retries times.
#
# Design invariants:
#   - Verifier writes NO code, NO commits, NO PR comments. It only reads + judges.
#   - Deliverable files are authored by the verifier, never by the worker.
#   - Verifier verdict JSON has a strict schema — parsing failures = treat as
#     "failed" phase so the operator sees it.
#   - Max 2 retries (3 total worker attempts). After exhaustion, fail the phase
#     and surface the accumulated feedback to the operator.

PHASE_SPECS = {
    "pm-start": {
        "description": (
            "pm-start initializes a Jira ticket's implementation: fetches "
            "the ticket, creates a feature branch, launches a dev agent "
            "that implements the work and commits, opens a PR, optionally "
            "starts a preview server."
        ),
        "success_criteria": [
            "A feature branch exists matching `feature/<TICKET>-<iteration>` "
            "(or the repo's configured branch_prefix)",
            "The feature branch has at least one commit authored by the "
            "dev agent beyond the parent branch",
            "An open PR exists targeting the parent branch (verify via "
            "`gh pr list --head <branch>`)",
            "The PR body includes the ticket ID and a reference to the "
            "acceptance criteria",
        ],
        "deliverable_path": "/tmp/pm-complete-{ticket}.json",
        "deliverable_schema": {
            "ticket": "string — the ticket ID",
            "worktree": "string — basename of the worktree dir",
            "branch": "string — the feature branch name",
            "pr_url": "string or null — full GitHub PR URL",
            "preview_url": "string or null — from session state if preview ran",
            "smoke_test": "string — one-line smoke-test instruction from PR or .pm/summary.md",
            "open_questions": "list — from .pm/work-summary.md if present",
            "completed_at": "ISO 8601 timestamp (now)",
        },
    },
    "work-2": {
        "description": (
            "work-2 addresses review comments on the open PR. The dev "
            "agent reads every comment, fixes the underlying issues, "
            "commits each fix, pushes."
        ),
        "success_criteria": [
            "Every review comment on the PR has been addressed by code "
            "change OR explicitly replied to in a thread with rationale",
            "At least one new commit exists on the branch beyond the "
            "pre-work-2 HEAD",
            "The branch has been pushed (remote HEAD matches local HEAD "
            "for the feature branch)",
        ],
        "deliverable_path": "{worktree}/.pm/work-2-result.json",
        "deliverable_schema": {
            "ready": "bool — true if all comments addressed",
            "url": "string — preview URL or empty",
        },
    },
    "work-3": {
        "description": (
            "work-3 addresses operator rejection feedback (from a "
            "qa-reject action on an already-reviewed PR)."
        ),
        "success_criteria": [
            "The operator's rejection feedback (free-form text or inbox "
            "reply) has been addressed in the code",
            "At least one new commit exists on the branch beyond the "
            "pre-work-3 HEAD",
            "The branch has been pushed",
        ],
        "deliverable_path": "{worktree}/.pm/work-3-result.json",
        "deliverable_schema": {
            "ready": "bool",
            "url": "string — preview URL or empty",
        },
    },
    # Note: review phase does NOT have a verifier entry. Review's output
    # IS the next phase's input — if no comments are posted, work-N has
    # nothing to address and flow naturally routes to QA. A dedicated
    # review verifier adds complexity without catching a meaningful
    # failure mode; dropping it keeps the architecture simpler.
}


def verifier_prompt(ticket, phase, worktree):
    """Build the verifier prompt. Verifier is a read-only agent whose
    output is a single JSON verdict file."""
    spec = PHASE_SPECS[phase]
    deliverable_path = spec["deliverable_path"].format(
        ticket=ticket, worktree=str(worktree))
    verdict_path = f"/tmp/verify-{ticket}-{phase}.json"
    worker_log = f"/tmp/sos-claude-flow-{ticket}-{phase}.log"
    sess_path = str(STATE_DIR / "sessions" / f"{ticket}.json")

    schema_lines = "\n".join(
        f"  \"{k}\": {v!r}" for k, v in spec["deliverable_schema"].items()
    )
    criteria = "\n".join(f"{i+1}. {c}" for i, c in enumerate(spec["success_criteria"]))

    return f"""You are the phase verifier for {phase} on ticket {ticket}.

Your ONLY job: read observable state, decide if the phase is complete,
write a verdict file. You do NOT produce code, commits, PR comments,
or any other production work.

## What the worker was supposed to do

{spec["description"]}

## Success criteria (ALL must be met for `complete`)

{criteria}

## Evidence sources

- Git state:
    git -C {worktree} log --oneline -10
    git -C {worktree} diff --stat @~5..HEAD
    git -C {worktree} branch --show-current
    git -C {worktree} status --porcelain
- Remote state:
    gh pr list --repo <owner>/<repo> --head <branch> --json number,url,state
    gh pr view <N> --json reviews,comments
    gh api repos/:owner/:repo/pulls/<N>/comments
- Session state: {sess_path}
- Worker's final tmux output: {worker_log}
  (claude --print buffers stdout until exit; the last chunk typically
  contains the worker's self-summary — read it but do not trust it
  without cross-referencing git/PR state)
- Existing deliverable file (if worker did write one): {deliverable_path}

## Your verdict

Write JSON to `{verdict_path}` with this exact schema:

```json
{{
  "state": "complete" | "incomplete" | "failed",
  "summary": "1-2 sentences: what the worker ACTUALLY did (not claimed)",
  "deliverable": {{
{schema_lines}
  }},
  "feedback": ["specific missing item", "another"] // required if state=incomplete
}}
```

## Verdict rules

- **complete** — every success criterion above is met. Write a full
  deliverable whose values come from actual state (git branch --show-current,
  gh pr list, etc.), not from the worker's self-report.
- **incomplete** — worker did some work but specific criteria are unmet.
  List the gaps concretely in `feedback` so the operator can fix and
  re-run manually. Include the criterion number or the specific missing
  artifact. There is no automatic retry — be specific enough that the
  operator knows exactly what to tell the worker.
- **failed** — worker produced work that breaks acceptance (committed
  to wrong branch, broke tests, regressed a previously-passing spec).
  Surface to operator via `deliverable.error` field.

Do NOT be lenient. Self-reports are unreliable — that is WHY you exist.
If 4 of 6 review comments are addressed, verdict is incomplete with
the 2 unaddressed comment IDs in feedback. If nothing is committed,
verdict is failed.

After writing the verdict file, print one line to stdout:

    verify {phase} {ticket} → <state>

Then exit 0.
"""


_VERDICT_REQUIRED = ("state", "summary", "deliverable")


def _validate_verdict_schema(data):
    """Validate the verifier's output has the fields flow-dev expects.
    Returns (ok, error_msg). Schema: top-level required keys + state enum
    + `deliverable` is an object + `feedback` is a list when present."""
    if not isinstance(data, dict):
        return False, "verdict is not a JSON object"
    for k in _VERDICT_REQUIRED:
        if k not in data:
            return False, f"missing required field: {k!r}"
    if data["state"] not in ("complete", "incomplete", "failed"):
        return False, f"invalid state: {data['state']!r}"
    if not isinstance(data["deliverable"], dict):
        return False, "deliverable must be an object"
    if "feedback" in data and not isinstance(data["feedback"], list):
        return False, "feedback must be a list"
    return True, None


def _run_verifier(ticket, phase, worktree):
    """Spawn the verifier subagent, wait for it, parse + schema-validate
    the verdict JSON.

    Returns a dict {state, summary, deliverable, feedback?} or None if
    the verifier itself crashed or produced malformed output.
    """
    prompt = verifier_prompt(ticket, phase, worktree)
    session = f"verify-{ticket}-{phase}"
    verdict_path = Path(f"/tmp/verify-{ticket}-{phase}.json")
    if verdict_path.exists():
        try:
            verdict_path.unlink()
        except OSError:
            pass
    step(f"Phase {phase} — verify")

    # No sidebar watcher for the verifier: it's short-lived (30-60s), does
    # not touch the worktree, and its card would show metrics identical to
    # the worker's final state (same worktree, no new commits). Just log
    # start/end to the console; operator sees it in their terminal.
    verifier_rc = run_subagent(session, prompt)

    if verifier_rc != 0:
        print(f"  ✗ verifier subagent exited {verifier_rc}", file=sys.stderr)
        return None
    if not verdict_path.exists():
        print(f"  ✗ verifier did not write {verdict_path}", file=sys.stderr)
        return None
    try:
        data = json.loads(verdict_path.read_text())
    except json.JSONDecodeError as e:
        print(f"  ✗ verifier verdict invalid JSON: {e}", file=sys.stderr)
        return None
    ok, err = _validate_verdict_schema(data)
    if not ok:
        print(f"  ✗ verifier verdict schema invalid: {err}", file=sys.stderr)
        return None
    return data


def _run_phase_with_verifier(ticket, phase, worker_fn, worktree):
    """Run worker, then verifier. Single-shot — no retries. Operator
    re-invokes manually if they want a retry.

    Philosophy: a phase either produces complete work or it doesn't.
    Auto-retry with feedback tends to: duplicate work when the worker
    misreads its own prior attempt, burn token budget in ambiguous
    loops, and mask real problems behind "one more try." Halting on
    the first incomplete/failed surfaces the issue immediately and
    lets the operator decide whether to fix, re-run, or escalate.

    Returns the verifier's `deliverable` dict on success; raises via
    fail() on any non-complete verdict. Worker rc!=0 does NOT abort
    before the verifier — the verifier judges from state, and a
    crashed worker that still committed real work can still pass.
    """
    spec = PHASE_SPECS[phase]

    # Run the worker. rc is captured but not gating — verifier decides.
    worker_rc = worker_fn()

    verdict = _run_verifier(ticket, phase, worktree)
    if verdict is None:
        fail(f"{phase} verifier could not produce a verdict "
             f"(worker rc={worker_rc})")
    state = verdict["state"]
    summary = verdict.get("summary", "")
    feedback = verdict.get("feedback") or []

    # Write canonical deliverable file (verifier-authored, not worker).
    deliverable = verdict.get("deliverable") or {}
    deliverable_path = Path(spec["deliverable_path"].format(
        ticket=ticket, worktree=str(worktree)))
    try:
        deliverable_path.parent.mkdir(parents=True, exist_ok=True)
        deliverable_path.write_text(json.dumps(deliverable, indent=2))
    except OSError as e:
        print(f"  ⚠ could not write {deliverable_path}: {e}",
              file=sys.stderr)

    check(f"verify {phase} {ticket} → {state}"
          + (f" — {summary}" if summary else ""))

    if state == "complete":
        return deliverable

    # Log verifier feedback to stderr AND to the inbox so the operator
    # sees WHY without grepping. The activity-watcher card already flipped
    # to red via stop_watcher(error=True) — append a concrete error card
    # with the feedback items for at-a-glance visibility.
    if feedback:
        print(f"  ↳ verifier feedback:", file=sys.stderr)
        for item in feedback:
            print(f"    - {item}", file=sys.stderr)
    _post_phase_failure_card(ticket, phase, state, summary, feedback,
                             deliverable)
    if state == "failed":
        err = deliverable.get("error") or summary or "verifier marked phase as failed"
        fail(f"{phase} failed (verifier): {err}")
    # state == "incomplete"
    fb_list = " | ".join(feedback) if feedback else "(no specific feedback)"
    fail(f"{phase} incomplete — {summary}. Feedback: {fb_list}. "
         f"Fix the gaps and re-run the phase manually if desired.")


def _post_phase_failure_card(ticket, phase, state, summary, feedback,
                             deliverable):
    """Surface verifier verdict to the inbox so operator doesn't have to
    read stderr. Graceful if inbox is unreachable."""
    icon = "✗" if state == "failed" else "⊘"
    title = f"{icon} {phase} {state}"
    ctx_parts = []
    if summary:
        ctx_parts.append(summary)
    if state == "failed" and deliverable.get("error"):
        ctx_parts.append(deliverable["error"])
    if feedback:
        ctx_parts.append(" · Gaps: " + " / ".join(feedback[:5]))
    post_card("info", title, ticket=ticket,
              ctx=" ".join(ctx_parts)[:480] if ctx_parts else None)


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

**You do NOT need to write `/tmp/pm-complete-{ticket}.json`.** A separate
verifier agent reads observable state (git branch, PR via `gh pr list`,
commits, your tmux log) and writes the canonical completion marker. Your
job is to do the real work — create the branch, launch the dev agent,
open the PR, optionally start a preview — and exit. The verifier handles
the paperwork.

The skill has 10 steps. Do steps 1-9 to the best of your ability. Step
10 (writing the completion marker) is now the verifier's responsibility,
not yours; you can ignore or skip it.

If step 5's Bash tool times out (sos-claude-print --tmux can run longer
than Bash's default 2-min timeout), re-invoke with `timeout=600000` (10
min) and a polling loop:
    `while tmux has-session -t pm-{ticket} 2>/dev/null; do sleep 30; done`
Repeat the poll call until the session is gone, THEN proceed to step 6.

---

"""


def phase_pm_start(ticket, iteration="first-pass"):
    step(f"Phase 1/5 — pm-start {ticket} {iteration}")
    if not PM_START_SKILL.exists():
        fail(f"pm-start skill not found at {PM_START_SKILL}")
    wt = Path(session_get(ticket, "worktree") or "")

    prefix = _FLOW_DEV_PREFIX_TEMPLATE.format(ticket=ticket)
    body = prefix + PM_START_SKILL.read_text() + f"\n\n{ticket} {iteration}\n"

    def worker():
        watcher = spawn_watcher(ticket, "work-1")
        rc = 1
        try:
            rc = run_subagent(f"flow-{ticket}-work1", body)
        finally:
            stop_watcher(watcher, error=(rc != 0))
        return rc

    return _run_phase_with_verifier(ticket, "pm-start", worker, wt)


def _capture_head(worktree):
    """Return `git rev-parse HEAD` or None if not resolvable."""
    try:
        return run_capture(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        ).strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _run_subagent_with_watcher(ticket, phase_label, session, prompt,
                               deliverable_path, synthesize_fn=None):
    """Run a subagent with an activity watcher, flipping the card to 'error'
    only when there's no evidence the work happened.

    Evidence of success, in priority order:
      1. deliverable file exists → trust it
      2. synthesize_fn is provided AND HEAD moved on the branch during the
         phase → call synthesize_fn(), write its result to deliverable_path,
         treat as success. Agents frequently commit real work and forget
         the deliverable-file ritual; git state is the authoritative signal.
      3. otherwise → error (no deliverable, no commits, nothing happened)

    Synthesized results carry `_synthesized: true` and `_commits_since:
    <sha>` so downstream and post-mortem can tell real deliverables from
    inferred ones.
    """
    wt_root = Path(deliverable_path).parent.parent if deliverable_path else None
    head_before = _capture_head(wt_root) if wt_root else None

    watcher = spawn_watcher(ticket, phase_label)
    error = False
    rc = None
    missing = False
    try:
        rc = run_subagent(session, prompt)
        if rc != 0:
            error = True
        elif deliverable_path is None or Path(deliverable_path).exists():
            pass  # explicit deliverable — trust it
        elif synthesize_fn is not None and head_before and wt_root:
            head_after = _capture_head(wt_root)
            if head_after and head_after != head_before:
                synth = synthesize_fn() or {}
                synth["_synthesized"] = True
                synth["_commits_since"] = head_before[:7]
                try:
                    Path(deliverable_path).write_text(
                        json.dumps(synth, indent=2))
                    print(f"  ↳ synthesized {Path(deliverable_path).name} "
                          f"from git state ({head_after[:7]} vs "
                          f"{head_before[:7]}) — agent committed but "
                          f"skipped the deliverable file",
                          flush=True)
                except OSError as e:
                    print(f"  ↳ synthesis write failed: {e}",
                          file=sys.stderr, flush=True)
                    error = True
                    missing = True
            else:
                error = True
                missing = True
        else:
            error = True
            missing = True
    finally:
        stop_watcher(watcher, error=error)
    return rc, missing


def phase_review(ticket, pr_num, is_rereview=False):
    """Review phase — no verifier. Review's output (comment count +
    verdict written to .pm/review-result.json) is the next phase's input.

    First-pass review: full quality sweep against the whole diff.
    Re-review (is_rereview=True): narrow-scope, verifies the prior
    round's comments are addressed + catches regressions, does NOT
    expand scope to new findings. Without this narrowing, every
    re-review tends to invent new nits and flow never converges.
    """
    label = "re-review" if is_rereview else "review"
    step(f"Phase 2/5 — {label} PR #{pr_num}")
    rf = worktree_root() / ".pm" / "review-result.json"
    # Clear any prior review-result.json so a stale file can't accidentally
    # satisfy the next read (especially important on re-review).
    if rf.exists():
        try:
            rf.unlink()
        except OSError:
            pass
    session = f"flow-{ticket}-review-2" if is_rereview else f"flow-{ticket}-review"
    rc, missing = _run_subagent_with_watcher(
        ticket, label.replace("-", "_"), session,
        review_prompt(ticket, pr_num, is_rereview=is_rereview), rf,
    )
    if rc != 0:
        fail(f"{label} subagent exited {rc}")
    if missing:
        fail(f"{label} subagent did not write {rf} — check `tmux attach -t {session}` for last state")
    return json.loads(rf.read_text())


def phase_work2(ticket, pr_num, comments_n):
    step(f"Phase 3/5 — address {comments_n} review comments")
    wt = worktree_root()

    def worker():
        watcher = spawn_watcher(ticket, "work-2")
        rc = 1
        try:
            rc = run_subagent(
                f"flow-{ticket}-work2",
                work2_prompt(ticket, pr_num, comments_n),
            )
        finally:
            stop_watcher(watcher, error=(rc != 0))
        return rc

    return _run_phase_with_verifier(ticket, "work-2", worker, wt)


def phase_work3(ticket, pr_num, reason):
    step(f"Phase 3/5 (re-QA) — address reviewer feedback")
    wt = worktree_root()

    def worker():
        watcher = spawn_watcher(ticket, "work-3")
        rc = 1
        try:
            rc = run_subagent(
                f"flow-{ticket}-work3",
                work3_prompt(ticket, pr_num, reason),
            )
        finally:
            stop_watcher(watcher, error=(rc != 0))
        return rc

    return _run_phase_with_verifier(ticket, "work-3", worker, wt)


def _count_pr_comments(pr_num):
    """Count review comments currently on the PR. Used on retry loops to
    refresh the worker's comment-count argument with the latest state
    (e.g., re-review added new comments the retry must also address)."""
    try:
        out = run_capture([
            "gh", "api",
            f"repos/:owner/:repo/pulls/{pr_num}/comments",
            "--jq", ". | length",
        ])
        return int(out) if out.isdigit() else 0
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return 0


def _work_rereview_loop(ticket, pr_num, worker_fn, max_retries=3,
                        on_cap_halt=None):
    """Run worker → re-review until re-review approves or cap is hit.

    Worker is any callable that does a full work-N cycle (worker subagent
    + verifier, writing commits to the branch). After each worker call
    we run phase_review(is_rereview=True). If re-review approves, return
    the final review result. If it requests changes, re-invoke the worker
    (attempt counter increments). On cap-hit, call on_cap_halt with the
    last review result + attempt count, then fail().

    Rationale: re-review deciding "changes-requested" should NOT require
    operator intervention — the whole point of the agent chain is to
    drive convergence without a human bottleneck. The cap exists to
    bound runaway token budgets, not to force human review of every
    round. Default 3 retries = up to 4 work cycles per invocation.
    """
    last_review = None
    for attempt in range(max_retries + 1):
        worker_fn(attempt)
        last_review = phase_review(ticket, pr_num, is_rereview=True)
        verdict = last_review.get("verdict")
        comments = last_review.get("comments", 0)
        if verdict != "changes-requested":
            if attempt > 0:
                check(f"re-review approved after {attempt + 1} work cycle(s)")
            return last_review
        if attempt < max_retries:
            step(f"↻ re-review found {comments} issues — auto-retrying "
                 f"work (attempt {attempt + 2} of {max_retries + 1})")
            continue
    # Cap exhausted
    if on_cap_halt:
        on_cap_halt(last_review, max_retries + 1)
    fail(f"auto-retry cap hit ({max_retries + 1} work cycles); re-review "
         f"still found {last_review.get('comments', 0)} issues after the "
         f"final pass. Operator action required — see halt card.")


def qa_card_actions(ticket, pr_url, preview_url):
    actions = []
    if preview_url:
        actions.append({"label": "Open preview", "kind": "openUrl"})
    if pr_url:
        actions.append({"label": "Open PR", "kind": "openUrl", "url": pr_url})
    actions += [
        # `exec` runs on the server via /exec endpoint, isolated from
        # the main PTY so it doesn't collide with operator typing.
        {"label": "Approve & merge", "kind": "exec",
         "cmd": f"sos-flow-dev qa-approve {ticket}"},
        # "Request changes" uses `inject` deliberately — operator needs
        # to type the rejection reason before pressing Enter. The inject
        # text is pre-filled but not executed (execute=False).
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


_TICKET_RANGE_RE = re.compile(r"^([A-Z][A-Z0-9]+)-(\d+)-(\d+)$")


def _ticket_exists(ticket):
    """Tri-state existence check: True | False | None (unknown).

    Runs `sos-jira view TICKET`:
      - rc=0                     → True   (confirmed exists)
      - rc!=0 + "not found" msg  → False  (confirmed 404 / deleted)
      - rc!=0 any other reason   → None   (infra error: JIRA_BASE_URL
                                            missing, network down,
                                            sos-jira not installed, etc.)
      - subprocess itself failed → None

    Caller can choose to pass through tickets whose existence is
    UNKNOWN rather than silently dropping them — better to let
    pm-start surface a specific error than for the operator's input
    to disappear into the void.
    """
    try:
        r = subprocess.run(
            ["sos-jira", "view", ticket],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if r.returncode == 0:
        return True
    combined = ((r.stderr or "") + (r.stdout or "")).lower()
    not_found_markers = ("not found", "does not exist", "no issue",
                         "404", "issue does not exist")
    if any(m in combined for m in not_found_markers):
        return False
    return None  # ambiguous — infra error, don't confidently drop the ticket


def _expand_ticket_specs(specs):
    """Expand ticket specs with optional range syntax PROJ-N-Y.

    Each spec is either:
      - "PROJ-N"   (single ticket — trusted verbatim, not validated)
      - "PROJ-N-Y" (range from N to Y inclusive — validated via sos-jira,
                   non-existent tickets are silently skipped with a log line)

    The validation-only-on-range design keeps single-ticket invocations
    fast (no Jira round-trip) while still giving ranges the "skip missing"
    semantics operators expect when typing a span.
    """
    out = []
    for spec in specs:
        m = _TICKET_RANGE_RE.match(spec)
        if not m:
            out.append(spec)
            continue
        prefix, start_s, end_s = m.group(1), m.group(2), m.group(3)
        start, end = int(start_s), int(end_s)
        if start > end:
            print(f"skipping malformed range {spec!r}: start > end",
                  file=sys.stderr)
            continue
        print(f"expanding range {spec} → {prefix}-{start}..{prefix}-{end} "
              f"(validating each via sos-jira)", flush=True)
        unknown_run = False  # stop retrying sos-jira after the first
                             # infra failure — it'll almost certainly
                             # fail for every remaining candidate too.
        for n in range(start, end + 1):
            candidate = f"{prefix}-{n}"
            if unknown_run:
                exists = None  # already decided to pass through
            else:
                exists = _ticket_exists(candidate)
            if exists is True:
                out.append(candidate)
                print(f"  ✓ {candidate}", flush=True)
            elif exists is False:
                print(f"  ⊘ {candidate} — not found in Jira, skipping",
                      flush=True)
            else:  # None — infra error
                out.append(candidate)
                print(f"  ? {candidate} — could not verify (sos-jira infra "
                      f"error); including anyway, downstream will fail "
                      f"with specifics if it's a real miss",
                      flush=True)
                unknown_run = True
    return out


def cmd_start(args):
    # Expand range syntax (FX-3-7 → FX-3, FX-4, ...) and validate existence.
    # Singles stay untouched; ranges get sos-jira view per candidate.
    tickets = _expand_ticket_specs(args.tickets)
    if not tickets:
        fail("no tickets to start (after range expansion + existence check)")
    # Multi-ticket OR explicit --detach → fan out to detached tmux runners
    # and return immediately. Single-ticket without --detach blocks (legacy).
    if len(tickets) > 1 or args.detach:
        _fanout(tickets, args.base, args.pause_after,
                iteration=getattr(args, "iteration", "first-pass"))
        if args.watch:
            # Drop into watch mode filtered to just the tickets we launched.
            print()
            print("Entering watch mode (Ctrl+C to detach — runs keep going):")
            print()
            watch_args = argparse.Namespace(tickets=tickets, interval=3)
            cmd_watch(watch_args)
        return
    _run_start_blocking(tickets[0], args)


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

    # Phase 3 — work-2 loop (only if changes were requested).
    # Auto-retries work-2 → re-review on each changes-requested verdict,
    # up to --max-retries. Halt card fires only when the cap is hit.
    if verdict == "changes-requested":
        def work2_worker(attempt):
            # On attempt 0, comments_n comes from the initial review.
            # On subsequent attempts, comments_n is updated per PR state
            # (re-review posted new comments that the retry must address).
            cur_n = comments_n if attempt == 0 else _count_pr_comments(pr_num)
            w = phase_work2(ticket, pr_num, cur_n)
            if "failed" in w:
                post_card("action", f"Work 2 failed — {ticket}",
                          ticket=ticket, ctx=w.get("failed", ""))
                fail(f"work-2 failed: {w.get('failed')}")
            nonlocal preview_url
            preview_url = w.get("url") or preview_url
            session_set(ticket, preview_url=preview_url,
                        phase=f"review-2-attempt-{attempt + 1}")

        def on_work2_cap_halt(last_review, attempts):
            session_set(ticket,
                        review_verdict=last_review.get("verdict"),
                        review_comments=last_review.get("comments", 0),
                        phase="review-2-failed")
            post_card(
                "action", f"⊘ {ticket} halted — {attempts} work cycles exhausted",
                ticket=ticket, url=pr_url or None,
                ctx=(f"Ran {attempts} work-2/re-review cycles; re-review still "
                     f"flags {last_review.get('comments', 0)} issues. Inspect "
                     f"PR #{pr_num} — force-approve, retry, or fix manually."),
                actions=[
                    {"label": "Open PR", "kind": "openUrl"},
                    {"label": "Retry anyway", "kind": "exec",
                     "cmd": (f"sos-flow-dev qa-reject {ticket} "
                             f"\"address re-review feedback on PR #{pr_num}\"")},
                    {"label": "Approve anyway", "kind": "exec",
                     "cmd": f"sos-flow-dev qa-approve {ticket}"},
                ],
            )

        r2 = _work_rereview_loop(
            ticket, pr_num, work2_worker,
            max_retries=getattr(args, "max_retries", 3),
            on_cap_halt=on_work2_cap_halt,
        )
        verdict2 = r2.get("verdict")
        comments2 = r2.get("comments", 0)
        post_card(
            "info", f"Re-review posted — {comments2} comments",
            ticket=ticket, url=pr_url or None,
            ctx=f"Verdict: {verdict2} (after work-2 loop)",
        )
        session_set(ticket, review_verdict=verdict2,
                    review_comments=comments2,
                    phase="awaiting-qa")
        if args.pause_after == "work2":
            gate(ticket, "Work 2 loop + re-review done — continue to QA card?")

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
            {"label": "Cleanup worktree", "kind": "exec",
             "cmd": f"sos-flow-dev cleanup {ticket}"},
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
    prior_count = sess.get("qa_reject_count") or 0
    iteration = prior_count + 1
    session_set(ticket, reject_reason=reason, phase="work-3",
                qa_reject_count=iteration)

    preview_url = sess.get("preview_url", "")

    def work3_worker(attempt):
        # On retry, the operator's original reason is stale — re-review
        # has posted its own comments that work-3 must now address.
        # Use a synthesized reason so work-3 knows to read the latest
        # PR feedback rather than stick to the original reject text.
        effective_reason = (
            reason if attempt == 0
            else f"address the latest re-review feedback on PR #{pr_num} "
                 f"(attempt {attempt + 1} of this qa-reject cycle)"
        )
        w = phase_work3(ticket, pr_num, effective_reason)
        if "failed" in w:
            fail(f"work-3 failed: {w.get('failed')}")
        nonlocal preview_url
        preview_url = w.get("url") or preview_url
        session_set(ticket, preview_url=preview_url,
                    phase=f"review-3-attempt-{attempt + 1}")

    def on_work3_cap_halt(last_review, attempts):
        next_work_n = iteration + attempts + 2  # human-friendly label
        session_set(ticket,
                    review_verdict=last_review.get("verdict"),
                    review_comments=last_review.get("comments", 0),
                    phase="review-3-failed")
        post_card(
            "action", f"⊘ {ticket} halted — {attempts} work-3 cycles exhausted",
            ticket=ticket, url=sess.get("pr_url") or None,
            ctx=(f"Ran {attempts} work-3/re-review cycles; re-review still "
                 f"flags {last_review.get('comments', 0)} issues. Inspect "
                 f"PR #{pr_num}: retry another cycle, force-approve, or "
                 f"fix manually."),
            actions=[
                {"label": "Open PR", "kind": "openUrl"},
                {"label": f"Retry (work-{next_work_n})", "kind": "exec",
                 "cmd": (f"sos-flow-dev qa-reject {ticket} "
                         f"\"address re-review feedback on PR #{pr_num}\"")},
                {"label": "Approve anyway", "kind": "exec",
                 "cmd": f"sos-flow-dev qa-approve {ticket}"},
            ],
        )

    r3 = _work_rereview_loop(
        ticket, pr_num, work3_worker,
        max_retries=getattr(args, "max_retries", 3),
        on_cap_halt=on_work3_cap_halt,
    )
    verdict3 = r3.get("verdict")
    comments3 = r3.get("comments", 0)
    post_card(
        "info", f"Re-review posted — {comments3} comments",
        ticket=ticket, url=sess.get("pr_url") or None,
        ctx=f"Verdict: {verdict3} (after work-3 loop)",
    )
    session_set(ticket, review_verdict=verdict3,
                review_comments=comments3,
                phase="awaiting-qa-2")

    post_card(
        "action", f"Re-QA on {ticket}",
        ticket=ticket, url=preview_url or sess.get("pr_url") or None,
        ctx=f"PR #{pr_num} · re-review after feedback (verdict: {verdict3})",
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


def _read_preview_suggested(path):
    """Read a `.pm/preview-suggested.json` written by the dev agent.

    Shape is the same as the `preview` block inside `.pm/config.json` —
    either {command, cwd} or {services: [...]}. Returns the parsed dict
    or None if missing/unreadable/empty.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("command") and not data.get("services"):
        return None
    return data


def _preview_config_for_ticket(ticket):
    """Load the preview block. Lookup order:

      1. Worktree `.pm/config.json` preview block (operator-defined,
         ticket-local override).
      2. Source-repo `.pm/config.json` preview block (project default).
      3. Worktree `.pm/preview-suggested.json` — written by the dev agent
         when it knows what to serve but neither config has a preview.
         Agent-suggested is a fallback, not an override — so an operator
         who deliberately disables preview at the source level stays in
         control.

    Step 3 is deliberately last: config-defined beats agent-suggested so
    operators' explicit decisions always win.
    """
    sess = session_get(ticket) or {}
    wt = sess.get("worktree")
    if not wt:
        return None
    worktree = Path(wt)
    # Try the worktree config first.
    cfg = _read_preview_block(worktree / ".pm" / "config.json")
    if cfg:
        return cfg
    # Fall back to the source repo.
    source = _source_repo_for_worktree(worktree)
    if source:
        cfg = _read_preview_block(source / ".pm" / "config.json")
        if cfg:
            return cfg
    # Last resort: dev-agent suggestion.
    return _read_preview_suggested(worktree / ".pm" / "preview-suggested.json")


def _sanitize_routes(raw):
    """Normalize a service's routes list. Each route is {label, path}."""
    if not isinstance(raw, list) or not raw:
        return [{"label": "Open preview", "path": "/"}]
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "").strip()
        path = str(r.get("path") or "/").strip()
        if not label:
            continue
        if not path.startswith("/"):
            path = "/" + path
        out.append({"label": label[:40], "path": path[:200]})
    return out or [{"label": "Open preview", "path": "/"}]


def _normalize_preview_config(cfg):
    """Return a list of service dicts [{name, command, cwd, port, routes}]
    from any supported preview config shape.

    Legacy:   {"command": "...", "cwd": "...", "routes": [...]}  → one "default"
    Multi:    {"services": [{"name": ..., "command": ..., "cwd": ..., "routes": [...]}]}

    `port` is INTENTIONALLY ignored from config. Ports are a runtime concern —
    the preview runner assigns them from its free pool (6006-6099) each time
    a service starts, so config stays portable across worktrees and never
    collides with itself on parallel runs. Pass `--port N` on the CLI to force
    a specific port for a one-off invocation.

    `routes` drives the sidebar's per-ticket preview buttons. Each entry is
    {label, path}. Omitted or empty → defaults to [{"label": "Open preview",
    "path": "/"}] so there's always at least one button per service.

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
                "routes": _sanitize_routes(s.get("routes")),
            })
        return out
    if cfg.get("command"):
        return [{
            "name": "default",
            "command": cfg["command"],
            "cwd": cfg.get("cwd") or "",
            "port": None,
            "routes": _sanitize_routes(cfg.get("routes")),
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
    """Deprecated — was a one-shot info card posted after preview start.
    Replaced by persistent per-route buttons in the ghostty-mini sidebar
    group header (driven by `sos-flow-dev previews` + server WS broadcast).
    Kept as a no-op so existing call sites keep working; scheduled for
    deletion once resync + any external callers are cleaned up.
    """
    return


def _previews_state():
    """Walk all session files, resolve each ticket's preview services
    (via the same config chain cmd_preview uses), and report current
    running status + route list.

    Returns a list of {ticket, phase, services: [{name, routes, status,
    url}]} — one entry per active ticket that has any preview config.
    Tickets without any preview config (no worktree config, no source
    config, no agent suggestion) are omitted so the UI can render
    "nothing to show" cleanly.
    """
    sessions_dir = STATE_DIR / "sessions"
    if not sessions_dir.is_dir():
        return []
    out = []
    for p in sorted(sessions_dir.glob("*.json")):
        try:
            sess = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ticket = sess.get("ticket") or p.stem
        cfg = _preview_config_for_ticket(ticket)
        services = _normalize_preview_config(cfg)
        if not services:
            continue
        urls = sess.get("preview_urls") or {}
        # Legacy: preview_url (single) + preview_session (single) maps to default.
        if not urls and sess.get("preview_url") and sess.get("preview_session"):
            urls = {"default": sess["preview_url"]}
        svc_out = []
        for svc in services:
            name = svc["name"]
            tmux = _preview_tmux_name(ticket, name)
            running = _tmux_session_exists(tmux) if shutil.which("tmux") else False
            svc_out.append({
                "name": name,
                "routes": svc["routes"],
                "status": "running" if running else "stopped",
                "url": urls.get(name) if running else None,
                "tmux": tmux,
            })
        out.append({
            "ticket": ticket,
            "phase": sess.get("phase") or "",
            "services": svc_out,
        })
    return out


def cmd_previews(args):
    """Dump preview state as JSON for the ghostty-mini UI to consume."""
    print(json.dumps(_previews_state(), indent=2 if args.pretty else None))


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
    first = True
    try:
        while True:
            rows = _render_watch_table(tickets_filter)
            _print_watch(rows, first=first)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n(detached — runs continue. `sos-flow-dev watch` to resume.)")


# ─── Activity watcher ──────────────────────────────────────────────────────
#
# Claude --print buffers stdout until exit, so tmux scrollback + pipe-pane
# give us nothing while a 10-minute dev-agent run is in flight. File + git
# activity in the worktree is the real ground-truth signal: if new files are
# appearing, the agent is alive; if nothing has changed for N minutes, it's
# stuck.
#
# cmd_activity polls the worktree every INTERVAL seconds, computes a diff
# against the previous snapshot, and POSTs progress-card updates to the
# ghostty-mini inbox. Also tees each event to /tmp/flow-<TICKET>-<PHASE>-
# activity.log so post-mortem inspection survives a dead ghostty-mini server.

INBOX_BASE_URL = os.environ.get("GHOSTTY_MINI_URL", "http://localhost:3030")


def _inbox_post(path, body):
    """POST JSON to the ghostty-mini inbox. Returns parsed response or None.

    Inbox-unreachable (server down) is deliberately non-fatal — watcher keeps
    polling so the file log survives even when the UI is gone.
    """
    try:
        req = urllib.request.Request(
            INBOX_BASE_URL + path,
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # Print once then stop spamming — the watcher stays alive either way.
        if not getattr(_inbox_post, "_warned", False):
            print(f"[watcher] inbox unreachable: {e}", file=sys.stderr, flush=True)
            _inbox_post._warned = True
        return None


def _pm_dir_has_recent_activity(worktree, since_ts):
    """True if any file under the worktree's `.pm/` dir has been modified
    since `since_ts`. Used as a silence-detection side-channel: pm-start's
    steps 1-5 write gitignored files (active-ticket.json,
    dev-agent-instructions.md, failed.json) that are invisible to
    `git status --porcelain`. Without this check, the watcher reports
    false-silence during pm-start's legitimate prep phase.

    Only scans .pm/ itself (not subdirs), checks file mtimes.
    Failures (dir missing, perm error) are silently False.
    """
    pm_dir = Path(worktree) / ".pm"
    if not pm_dir.is_dir():
        return False
    try:
        for entry in pm_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime > since_ts:
                    return True
            except OSError:
                continue
        return False
    except OSError:
        return False


def _git_snapshot(worktree, base_ref, phase_start_head=None):
    """Cumulative snapshot: (files-touched-this-phase, commits-this-phase).

    "Files touched this phase" = union of
      - currently-uncommitted files in the working tree (porcelain)
      - files in the diff from phase_start_head..HEAD (i.e., files
        changed by any commits the agent has landed since the phase began)

    Without the second set, the file count would non-monotonically
    decrease as the agent commits work — porcelain stops reporting a
    file once it's committed, so the watcher would see "removed". That's
    technically correct for "uncommitted changes" but misleading as a
    progress signal: the agent did MORE work and the count WENT DOWN.

    Cumulative union gives a monotonically non-decreasing file count that
    matches operator intuition ("how many files has this phase touched?").

    Commits count comes from phase_start_head..HEAD when provided, else
    base_ref..HEAD as a fallback. phase_start_head is preferred because
    it filters out commits that existed at phase entry (e.g., base branch
    already ahead of the remote).
    """
    try:
        # --untracked-files=all: without it, an untracked directory is
        # collapsed to a single "?? dir/" entry and the watcher never sees
        # files added inside — false silence warnings during scaffolding.
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except subprocess.SubprocessError:
        return set(), 0

    files = set()
    for line in status.splitlines():
        if len(line) < 3:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[-1]
        files.add(path.strip('"'))

    # Add files changed via commits since phase started. This keeps the
    # set from shrinking when the agent commits — the file moves out of
    # porcelain but is still "touched this phase."
    commits_ref = phase_start_head or base_ref
    if phase_start_head:
        try:
            diff_out = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only",
                 f"{phase_start_head}..HEAD"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
            for p in diff_out.splitlines():
                p = p.strip()
                if p:
                    files.add(p)
        except subprocess.SubprocessError:
            pass

    n_commits = 0
    if commits_ref:
        try:
            out = subprocess.run(
                ["git", "-C", str(worktree), "rev-list", "--count",
                 f"{commits_ref}..HEAD"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
            if out.isdigit():
                n_commits = int(out)
        except subprocess.SubprocessError:
            pass

    return files, n_commits


def _diff_to_log_lines(prev_files, cur_files, prev_commits, cur_commits,
                      worktree):
    """Turn two snapshots into human-readable log lines. Groups bulk changes."""
    added = cur_files - prev_files
    removed = prev_files - cur_files
    lines = []

    if added:
        # Group by top-level path segment so "+40 files in apps/" beats 40
        # individual lines during scaffolding bursts.
        by_top = {}
        for f in added:
            top = f.split("/", 1)[0] if "/" in f else f
            by_top.setdefault(top, []).append(f)
        for top, fs in sorted(by_top.items()):
            if len(fs) <= 3:
                for f in sorted(fs):
                    lines.append(f"+ {f}")
            else:
                sample = sorted(fs)[0]
                lines.append(f"+{len(fs)} in {top}/ (e.g. {sample})")
    if removed:
        for f in sorted(removed)[:3]:
            lines.append(f"- {f}")
        if len(removed) > 3:
            lines.append(f"-{len(removed) - 3} more removed")

    new_commits = cur_commits - prev_commits
    if new_commits > 0:
        try:
            log = subprocess.run(
                ["git", "-C", str(worktree), "log", "--oneline",
                 f"-{new_commits}"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip().splitlines()
            for l in log[:5]:
                lines.append(f"● commit {l}")
            if new_commits > 5:
                lines.append(f"● ...and {new_commits - 5} more commits")
        except subprocess.SubprocessError:
            lines.append(f"● {new_commits} new commit(s)")

    return lines


def _review_snapshot(pr_num):
    """Review-phase signal: PR review comment set + latest review state.

    Returns (comments_dict, review_state_str). comments_dict maps a stable
    comment id to {path, line, body_preview, author}. review_state is the
    latest submitted review state ('APPROVED', 'CHANGES_REQUESTED',
    'COMMENTED', or None if no review submitted yet).

    File/commit tracking is useless during review — the reviewer doesn't
    modify the working tree, only posts GitHub comments. Activity = new
    comments on the PR.
    """
    if not pr_num:
        return {}, None
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr_num), "--json", "reviews,comments"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
        data = json.loads(out) if out.strip() else {}
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}, None

    comments = {}
    for c in data.get("comments") or []:
        # Prefer GitHub's node_id for stability; fall back to a path/line/time
        # composite so new comments still register even if the JSON schema shifts.
        cid = (c.get("id") or c.get("node_id")
               or f"{c.get('path','')}:{c.get('line','')}:{c.get('createdAt','')}")
        comments[str(cid)] = {
            "path": c.get("path") or "",
            "line": c.get("line") or c.get("position") or "",
            "body": (c.get("body") or "").strip(),
            "author": (c.get("author") or {}).get("login", ""),
        }

    review_state = None
    for r in data.get("reviews") or []:
        s = r.get("state")
        if s in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            review_state = s  # list is chronological; last one wins

    return comments, review_state


def _diff_review_snapshot(prev, cur):
    """Turn two review snapshots into log lines. prev/cur are the tuples
    returned by _review_snapshot: (comments_dict, review_state)."""
    prev_comments, prev_state = prev
    cur_comments, cur_state = cur
    lines = []
    new_ids = set(cur_comments) - set(prev_comments)
    for cid in sorted(new_ids, key=lambda x: (cur_comments[x].get("path", ""),
                                              cur_comments[x].get("line", 0))):
        c = cur_comments[cid]
        loc = f"{c['path']}:{c['line']}" if (c['path'] and c['line']) else (c['path'] or "PR body")
        snip = " ".join(c['body'].split())[:80]
        lines.append(f"● comment on {loc} — {snip}")
    if cur_state and cur_state != prev_state:
        lines.append(f"● review submitted · {cur_state} · {len(cur_comments)} comments")
    return lines


def _make_phase_tracker(phase, worktree, base_ref, pr_num,
                        phase_start_head=None):
    """Pick the right snapshot/diff pair for the phase.

    Review phase watches GitHub PR comment state (file/commit tracking is
    zero-signal during review). Other phases default to git-based tracking
    with cumulative file + commit counting scoped to phase_start_head.
    """
    if phase == "review":
        return {
            "snapshot": lambda: _review_snapshot(pr_num),
            "diff": _diff_review_snapshot,
            "metrics": lambda sig: {
                "comments": len(sig[0]),
                "review_state": sig[1] or "pending",
            },
            "initial_summary": lambda sig: (
                f"{len(sig[0])} comments" + (f" · {sig[1]}" if sig[1] else "")
            ),
        }
    return {
        "snapshot": lambda: _git_snapshot(worktree, base_ref, phase_start_head),
        "diff": lambda p, c: _diff_to_log_lines(p[0], c[0], p[1], c[1], worktree),
        "metrics": lambda sig: {
            "files_changed": len(sig[0]),
            "commits": sig[1],
        },
        "initial_summary": lambda sig: f"{len(sig[0])} files · {sig[1]} commits",
    }


def cmd_activity(args):
    """Watch a ticket's worktree, post progress-card updates to the inbox.

    Run in a foreground process or as subprocess.Popen; SIGTERM/SIGINT cause
    a graceful exit that flips the card status to 'done'.
    """
    ticket = args.ticket
    sess = session_get(ticket)
    if not sess:
        fail(f"no session for {ticket} — run `sos-flow-dev start` first")
    worktree = Path(sess.get("worktree", ""))
    if not worktree.is_dir():
        fail(f"worktree missing: {worktree}")

    phase = args.phase or sess.get("phase", "unknown")
    base_ref = sess.get("parent_branch") or "HEAD"
    pr_num = sess.get("pr_num") or extract_pr_num(sess.get("pr_url", "") or "")
    interval = max(2, args.interval)
    silence_s = max(30, args.silence_threshold)

    started = time.time()
    # Pin HEAD at watcher-start so file + commit counts are scoped to THIS
    # phase, not "whatever the branch has been doing since it was created."
    phase_start_head = _capture_head(worktree)
    tracker = _make_phase_tracker(phase, worktree, base_ref, pr_num,
                                  phase_start_head=phase_start_head)
    prev_signal = tracker["snapshot"]()

    title = f"{ticket} · {phase}"
    links = [
        {"label": f"tmux flow-{ticket}-{_phase_to_session_suffix(phase)}",
         "command": f"tmux attach -t flow-{ticket}-{_phase_to_session_suffix(phase)}"},
        {"label": "worktree", "path": str(worktree)},
        {"label": "activity log",
         "command": f"tail -f /tmp/flow-{ticket}-{phase}-activity.log"},
    ]
    pr_url = sess.get("pr_url")
    preview_url = sess.get("preview_url")
    if pr_url:
        links.append({"label": "PR", "url": pr_url})
    if preview_url:
        links.append({"label": "Preview", "url": preview_url})

    initial_metrics = {
        **tracker["metrics"](prev_signal),
        "started_at": int(started * 1000),
        "last_activity_at": int(started * 1000),
    }
    resp = _inbox_post("/inbox", {
        "kind": "progress",
        "ticket": ticket,
        "phase": phase,
        "title": title,
        "status": "working",
        "metrics": initial_metrics,
        "links": links,
    })
    card_id = (resp or {}).get("id")
    if not card_id:
        # Inbox unreachable — keep going with file log only.
        print(f"[watcher] no card (inbox down?); file log still active",
              file=sys.stderr, flush=True)

    log_path = Path(f"/tmp/flow-{ticket}-{phase}-activity.log")
    def _write_file_log(lines):
        try:
            with log_path.open("a") as f:
                ts = time.strftime("%H:%M:%S")
                for l in lines:
                    f.write(f"[{ts}] {l}\n")
        except OSError:
            pass
    _write_file_log([f"=== watcher start · {worktree} · base={base_ref} ==="])

    # Two stop paths:
    #   SIGTERM / SIGINT → graceful stop, card status=done
    #   SIGUSR1          → phase failed, card status=error (flow-dev uses
    #                      this when run_subagent returns non-zero OR the
    #                      expected deliverable file is missing)
    stopping = {"flag": False, "status": "done"}
    def _stop_ok(signum, _frame):
        stopping["flag"] = True
    def _stop_err(signum, _frame):
        stopping["flag"] = True
        stopping["status"] = "error"
    signal.signal(signal.SIGTERM, _stop_ok)
    signal.signal(signal.SIGINT, _stop_ok)
    signal.signal(signal.SIGUSR1, _stop_err)

    last_activity = started
    silence_warned = False

    try:
        while not stopping["flag"]:
            # Sleep in small chunks so SIGTERM takes effect within ~1s.
            slept = 0
            while slept < interval and not stopping["flag"]:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
            if stopping["flag"]:
                break

            cur_signal = tracker["snapshot"]()
            delta = tracker["diff"](prev_signal, cur_signal)
            now = time.time()

            if delta:
                last_activity = now
                silence_warned = False
                _write_file_log(delta)
                if card_id:
                    _inbox_post(f"/inbox/{card_id}/progress", {
                        "log_append": [{"ts": int(now * 1000), "text": l}
                                       for l in delta],
                        "metrics_patch": {
                            **tracker["metrics"](cur_signal),
                            "last_activity_at": int(now * 1000),
                        },
                    })
                prev_signal = cur_signal
            elif not silence_warned and (now - last_activity) > silence_s:
                # Side-channel: pm-start writes gitignored .pm/* files
                # (active-ticket.json, dev-agent-instructions.md,
                # failed.json, etc.) during steps 1-5 that never show in
                # `git status --porcelain`. Without this check, pm-start's
                # 3-5 minutes of prep look like silence even though the
                # skill is actively running. Reset last_activity if any
                # .pm/ file has been touched since the last activity point.
                if _pm_dir_has_recent_activity(worktree, last_activity):
                    last_activity = now
                else:
                    silence_warned = True
                    text = f"⚠ {int((now - last_activity) / 60)}m of silence"
                    _write_file_log([text])
                    if card_id:
                        _inbox_post(f"/inbox/{card_id}/progress", {
                            "log_append": [{"ts": int(now * 1000),
                                            "text": text}],
                        })
    finally:
        end = int(time.time() * 1000)
        status = stopping.get("status", "done")
        _write_file_log([f"=== watcher stopped ({status}) ==="])
        if card_id:
            _inbox_post(f"/inbox/{card_id}/progress", {
                "status": status,
                "metrics_patch": {
                    **tracker["metrics"](prev_signal),
                    "last_activity_at": end,
                },
                "log_append": [{"ts": end, "text": f"watcher stopped · {status}"}],
            })


def _phase_to_session_suffix(phase):
    """Map session-state phase names to the tmux suffix the subagent uses."""
    return {
        "alloc": "alloc",
        "work-1": "work1",
        "review": "review",
        "work-2": "work2",
        "work-3": "work3",
        "awaiting-qa": "qa",
    }.get(phase, phase)


def spawn_watcher(ticket, phase):
    """Launch `sos-flow-dev activity TICKET --phase PHASE` as a background
    subprocess. Returns a Popen — caller calls .terminate() when the phase
    ends. Logs go to /tmp/watcher-<TICKET>-<PHASE>.stderr so they don't pollute
    the main orchestrator's output.
    """
    stderr_path = f"/tmp/watcher-{ticket}-{phase}.stderr"
    try:
        stderr = open(stderr_path, "ab")
    except OSError:
        stderr = subprocess.DEVNULL
    try:
        return subprocess.Popen(
            ["sos-flow-dev", "activity", ticket, "--phase", phase],
            stdout=subprocess.DEVNULL, stderr=stderr,
            start_new_session=True,  # so our SIGINT doesn't cascade
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[watcher] could not spawn: {e}", file=sys.stderr, flush=True)
        return None


def stop_watcher(proc, timeout=10, error=False):
    """Signal the watcher to exit and wait for it.

    error=False → SIGTERM → card status flips to "done"
    error=True  → SIGUSR1 → card status flips to "error" so the UI surfaces
                   the failure instead of showing a green checkmark on a
                   phase that actually wedged.

    Forces kill on timeout so a wedged watcher doesn't block the phase exit.
    """
    if proc is None:
        return
    try:
        if error:
            proc.send_signal(signal.SIGUSR1)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
    except (OSError, subprocess.SubprocessError):
        pass


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


def _list_ticket_tmux_sessions(ticket, include_preview=False):
    """Enumerate every tmux session name that belongs to this ticket.

    Matches the naming conventions established across flow-dev:
      flow-runner-<T>                   orchestrator (detached runner)
      flow-<T>-{alloc,work1,work2,work3,review,review-2}
                                        phase subagents
      pm-<T>                            dev-agent spawned by pm-start
      verify-<T>-<phase>                verifier subagents
      preview-<T>-<service>             preview dev-servers (opt-in)

    Returns a list of session names currently live for this ticket.
    """
    if shutil.which("tmux") is None:
        return []
    try:
        out = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, check=False,
        ).stdout
    except subprocess.SubprocessError:
        return []
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    match = []
    suffix = ticket
    for n in names:
        if n == f"flow-runner-{suffix}":
            match.append(n)
        elif n == f"pm-{suffix}":
            match.append(n)
        elif n.startswith(f"flow-{suffix}-"):
            match.append(n)
        elif n.startswith(f"verify-{suffix}-"):
            match.append(n)
        elif include_preview and n.startswith(f"preview-{suffix}-"):
            match.append(n)
    return match


def _stop_ticket(ticket, include_preview=False, include_worktree=False):
    """Stop every running process belonging to this ticket.

    Order matters:
      1. Kill the orchestrator + phase tmux sessions first (they spawn
         the others, so killing them prevents new children).
      2. pkill the activity watcher subprocess (lives OUTSIDE tmux, spawned
         by spawn_watcher as a detached Popen).
      3. Optionally kill preview tmux sessions.
      4. Optionally remove the worktree via cmd_cleanup.

    Returns a dict {sessions_killed, watcher_killed, preview_killed,
    worktree_removed, errors} for operator visibility.
    """
    result = {"sessions_killed": [], "watcher_killed": False,
              "preview_killed": [], "worktree_removed": False, "errors": []}
    # Phase tmux + orchestrator
    sessions = _list_ticket_tmux_sessions(ticket, include_preview=False)
    for name in sessions:
        r = subprocess.run(["tmux", "kill-session", "-t", name],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            result["sessions_killed"].append(name)
        else:
            result["errors"].append(f"tmux kill {name}: {r.stderr.strip()}")

    # Activity watcher (Popen subprocess, not in tmux)
    r = subprocess.run(["pkill", "-f", f"sos-flow-dev activity {ticket}"],
                       capture_output=True, text=True, check=False)
    # pkill rc 0 = one or more processes matched and killed; rc 1 = no match
    if r.returncode == 0:
        result["watcher_killed"] = True
    elif r.returncode not in (0, 1):
        result["errors"].append(f"pkill watcher: rc={r.returncode}")

    if include_preview:
        preview_sessions = [n for n in _list_ticket_tmux_sessions(
            ticket, include_preview=True) if n.startswith(f"preview-{ticket}-")]
        for name in preview_sessions:
            r = subprocess.run(["tmux", "kill-session", "-t", name],
                               capture_output=True, text=True, check=False)
            if r.returncode == 0:
                result["preview_killed"].append(name)

    if include_worktree:
        try:
            cmd_cleanup(argparse.Namespace(ticket=ticket, remove=True))
            result["worktree_removed"] = True
        except SystemExit:
            result["errors"].append("worktree removal failed")
        except Exception as e:
            result["errors"].append(f"worktree removal: {e}")

    # Record the stop in session state so the UI can see it
    sess = session_get(ticket)
    if sess:
        session_set(ticket, phase="stopped", stopped_at=now_iso())

    return result


def cmd_stop(args):
    if args.all:
        d = STATE_DIR / "sessions"
        tickets = []
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                try:
                    data = json.loads(p.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                phase = data.get("phase") or ""
                if phase not in ("merged", "stopped", ""):
                    tickets.append(data.get("ticket") or p.stem)
        if not tickets:
            print("(no running tickets to stop)")
            return
        print(f"stopping {len(tickets)} ticket(s): {', '.join(tickets)}")
    else:
        if not args.tickets:
            fail("pass one or more TICKET ids, or --all to stop every active ticket")
        tickets = list(args.tickets)

    any_errors = False
    for t in tickets:
        step(f"stopping {t}")
        r = _stop_ticket(t, include_preview=args.include_preview,
                         include_worktree=args.include_worktree)
        if r["sessions_killed"]:
            check(f"{t}: killed {len(r['sessions_killed'])} tmux session(s) "
                  f"({', '.join(r['sessions_killed'])})")
        else:
            print(f"  {t}: no tmux sessions matched", flush=True)
        if r["watcher_killed"]:
            check(f"{t}: killed activity watcher")
        if r["preview_killed"]:
            check(f"{t}: killed {len(r['preview_killed'])} preview session(s)")
        if r["worktree_removed"]:
            check(f"{t}: worktree removed")
        for e in r["errors"]:
            print(f"  ✗ {t}: {e}", file=sys.stderr, flush=True)
            any_errors = True
        # Inbox card so the operator sees it in the sidebar
        post_card("info", f"⏹ {t} stopped", ticket=t,
                  ctx=(f"Killed {len(r['sessions_killed'])} tmux session(s)"
                       + (", watcher" if r["watcher_killed"] else "")
                       + (f", {len(r['preview_killed'])} preview(s)"
                          if r["preview_killed"] else "")
                       + (", worktree removed"
                          if r["worktree_removed"] else "")))
    if any_errors:
        sys.exit(1)


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
    p.add_argument("--max-retries", type=int, default=3,
                   help="Max auto-retries of the work-2 → re-review loop. "
                        "0 disables auto-retry (halts on first re-review changes-"
                        "requested). Default 3 = up to 4 work cycles per run "
                        "before the halt card forces operator attention.")
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
    p.add_argument("--max-retries", type=int, default=3,
                   help="Max auto-retries of the work-3 → re-review loop "
                        "within this qa-reject invocation. Default 3.")

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
        "previews",
        help="Dump preview state for all active tickets as JSON (consumed "
             "by ghostty-mini's sidebar preview buttons).",
        description=(
            "One-shot JSON of every active ticket's preview services: "
            "name, routes (label + path), running status, URL if up. "
            "ghostty-mini's server polls this and renders per-route "
            "buttons in the ticket's sidebar group header."
        ),
    )
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print JSON output (default: compact)")

    p = sub.add_parser(
        "stop",
        help="Kill every running process for a ticket (flow-runner + phase "
             "subagents + activity watcher). Optionally kill previews + "
             "remove the worktree.",
    )
    p.add_argument("tickets", nargs="*",
                   help="Ticket(s) to stop. If empty, requires --all.")
    p.add_argument("--all", action="store_true",
                   help="Stop every ticket whose session state shows a "
                        "non-terminal phase. Equivalent to passing every "
                        "active ticket individually.")
    p.add_argument("--include-preview", action="store_true",
                   help="Also kill this ticket's preview-<T>-<svc> tmux "
                        "sessions. Off by default — preview is often "
                        "still useful after stopping a flow.")
    p.add_argument("--include-worktree", action="store_true",
                   help="Also remove the ticket's worktree (git worktree "
                        "remove --force). Off by default — uncommitted "
                        "work in the tree would be lost.")

    p = sub.add_parser(
        "cleanup",
        help="Reset the ticket's worktree for reuse (default) or remove it outright",
    )
    p.add_argument("ticket")
    p.add_argument("--remove", action="store_true",
                   help="Destroy the worktree (git worktree remove --force) instead of "
                        "resetting it for reuse. Use when you want to shrink the pool.")

    p = sub.add_parser(
        "activity",
        help="Watch a ticket's worktree for file/commit activity; post a "
             "progress card to the inbox. Auto-spawned by flow-dev during "
             "work phases; invoke manually for ad-hoc visibility.",
    )
    p.add_argument("ticket")
    p.add_argument("--phase", default=None,
                   help="Phase label for the progress card (default: reads "
                        "session state). Use work-1, review, work-2, work-3.")
    p.add_argument("--interval", type=int, default=10,
                   help="Poll interval in seconds (default 10, min 2)")
    p.add_argument("--silence-threshold", type=int, default=120,
                   help="Emit a silence warning after this many idle seconds "
                        "(default 120)")

    args = parser.parse_args()

    {
        "start": cmd_start,
        "review": cmd_review,
        "work2": cmd_work2,
        "qa-approve": cmd_qa_approve,
        "qa-reject": cmd_qa_reject,
        "status": cmd_status,
        "watch": cmd_watch,
        "activity": cmd_activity,
        "preview": cmd_preview,
        "previews": cmd_previews,
        "resync": cmd_resync,
        "config": cmd_config,
        "cleanup": cmd_cleanup,
        "stop": cmd_stop,
    }[args.subcommand](args)


if __name__ == "__main__":
    main()
