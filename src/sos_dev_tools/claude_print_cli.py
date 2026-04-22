#!/usr/bin/env python3
"""sos-claude-print — Spawn `claude --print` with the mandatory env-strip wrapper.

When a parent Claude Code session launches another Claude subprocess (a dev
agent, reviewer, etc.), four environment variables MUST be removed before exec
or the subprocess breaks in subtle, confusing ways:

    CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_EXECPATH
        Set by the parent harness. If the child inherits them, it tries to
        reuse the parent's context and fails with:
            "Credit balance is too low"
        — even when the logged-in Max/Pro account is perfectly healthy.

    ANTHROPIC_API_KEY
        If present (often from a sourced .env), silently routes the child
        through API-key billing instead of the operator's Max/Pro plan. Same
        symptom, different root cause.

Every caller that spawns a sub-agent has to get all four right. `sos-claude-print`
encodes the wrapper once so downstream skills can't forget a var:

    # Instead of:
    env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH \\
        -u ANTHROPIC_API_KEY \\
      claude --dangerously-skip-permissions --print "$(cat instructions.md)"

    # Use:
    sos-claude-print --file instructions.md

    # Or pass the prompt directly:
    sos-claude-print "implement the ticket"

    # Extra flags after --:
    sos-claude-print --file instructions.md -- --model claude-opus-4-7
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

STRIP_ENV = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "ANTHROPIC_API_KEY",
]

DEFAULT_ARGS = ["--dangerously-skip-permissions", "--print"]

TMUX_POLL_INTERVAL_S = 1.0


def stripped_env(source=None):
    """Return a copy of the env with the parent-harness + API-key vars removed."""
    if source is None:
        source = os.environ
    return {k: v for k, v in source.items() if k not in STRIP_ENV}


def run_in_tmux(session, cmd_argv):
    """Run cmd_argv inside a detached tmux session, block until it exits,
    propagate the exit code.

    Why: `claude --print`'s stdout is block-buffered when not attached to a TTY
    (typical when a skill redirects to a log file). That leaves long agent runs
    invisible — 15+ minutes of silence followed by one flush at exit. Running
    inside tmux gives claude a real pty, so output line-buffers and the caller
    can `tmux attach -t SESSION` from anywhere to watch live.

    Exit code is captured by writing $? to a temp file inside the session shell,
    since tmux doesn't propagate exit codes natively.

    Env-var stripping is applied twice: once via the inherited env for the
    `tmux new-session` call, and again via `unset` inside the session shell,
    in case the tmux server's own environment is polluted.
    """
    if shutil.which("tmux") is None:
        print("sos-claude-print: --tmux requires tmux on PATH", file=sys.stderr)
        sys.exit(127)

    exit_file = tempfile.mktemp(suffix=".exit", prefix=f"sos-claude-{session}-")

    # The prompt is the last argv; for long prompts (pm-start + its flow-dev
    # prefix clocks at 20-30 KB), inlining the prompt into a `sh -c` wrapper
    # blows through macOS ARG_MAX (~256 KB after shlex.quote's backslash-
    # escaping overhead) and tmux new-session dies with "command too long".
    # Detach prompts above a safety threshold to a temp file and redirect via
    # stdin — wrapper stays short, claude reads the same content.
    STDIN_THRESHOLD = 4000
    prompt_file = None
    if cmd_argv and len(cmd_argv[-1]) > STDIN_THRESHOLD:
        prompt_file = tempfile.mktemp(suffix=".prompt",
                                      prefix=f"sos-claude-{session}-")
        with open(prompt_file, "w") as f:
            f.write(cmd_argv[-1])
        cmd_argv = cmd_argv[:-1]

    unset_cmd = "unset " + " ".join(STRIP_ENV)
    cmd_str = " ".join(shlex.quote(a) for a in cmd_argv)
    if prompt_file:
        stdin_redirect = f"< {shlex.quote(prompt_file)}"
    else:
        stdin_redirect = ""
    wrapper = (f"{unset_cmd}; {cmd_str} {stdin_redirect}; "
               f"rc=$?; echo $rc > {shlex.quote(exit_file)}")

    # new-session -d: detached. -A: attach to existing session with this name
    # instead of erroring, so re-runs are idempotent.
    try:
        subprocess.check_call([
            "tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
            "/bin/sh", "-c", wrapper,
        ], env=stripped_env())
    except subprocess.CalledProcessError as e:
        print(f"sos-claude-print: tmux new-session failed: {e}", file=sys.stderr)
        if prompt_file:
            try: os.unlink(prompt_file)
            except OSError: pass
        sys.exit(1)

    # Tee the tmux pane to a log file so post-mortem debugging is possible.
    # tmux scrollback dies with the session; when agents exit 0 silently the
    # only way to find out *why* is to have captured the live output somewhere
    # that survives. Failure here is non-fatal — logging is nice-to-have.
    log_path = f"/tmp/sos-claude-{session}.log"
    try:
        subprocess.run(
            ["tmux", "pipe-pane", "-t", session, f"cat >> {shlex.quote(log_path)}"],
            check=False, capture_output=True,
        )
    except Exception:
        pass

    print(f"[sos-claude-print] agent running in tmux session '{session}'", file=sys.stderr)
    print(f"[sos-claude-print] attach to watch live: tmux attach -t {session}", file=sys.stderr)
    print(f"[sos-claude-print] tail post-mortem log: tail -f {log_path}", file=sys.stderr)
    print(f"[sos-claude-print] detach without killing: Ctrl+B then D", file=sys.stderr)

    # Poll until the session dies.
    while True:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True)
        if r.returncode != 0:
            break
        time.sleep(TMUX_POLL_INTERVAL_S)

    # Retrieve the exit code.
    exit_code = 0
    try:
        with open(exit_file) as f:
            exit_code = int((f.read().strip() or "0"))
    except (FileNotFoundError, ValueError):
        # Session vanished without writing — treat as failure.
        exit_code = 1
    finally:
        try:
            os.unlink(exit_file)
        except OSError:
            pass
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass

    return exit_code


def build_cmd(prompt, extra_args, include_defaults=True):
    """Build the argv list for exec. Extra args go before the prompt.

    Any leading '--' in extra_args (argparse end-of-options marker) is dropped.
    """
    cmd = ["claude"]
    if include_defaults:
        cmd.extend(DEFAULT_ARGS)
    if extra_args:
        extras = list(extra_args)
        if extras and extras[0] == "--":
            extras = extras[1:]
        cmd.extend(extras)
    cmd.append(prompt)
    return cmd


def main():
    parser = argparse.ArgumentParser(
        prog="sos-claude-print",
        description=(
            "Spawn `claude --print` with the four-variable env strip required "
            "for sub-agent auth. See module docstring for rationale."
        ),
    )
    parser.add_argument("prompt", nargs="?", default=None,
                        help="Prompt text (or omit and pass --file)")
    parser.add_argument("--file", "-f", default=None,
                        help="Read prompt from this file instead of the positional arg")
    parser.add_argument("--tmux", default=None, metavar="SESSION",
                        help=(
                            "Run the agent inside a detached tmux session with the given name. "
                            "Block until it exits, then propagate its exit code. "
                            "Use `tmux attach -t SESSION` from anywhere to watch live output."
                        ))
    parser.add_argument("--no-default-args", action="store_true",
                        help="Omit --dangerously-skip-permissions --print (rare; for custom invocations)")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER,
                        help="Extra args passed directly to claude (place after --)")
    args = parser.parse_args()

    if args.file:
        try:
            with open(args.file, "r") as f:
                prompt = f.read()
        except FileNotFoundError:
            print(f"sos-claude-print: file not found: {args.file}", file=sys.stderr)
            sys.exit(2)
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        parser.error("must provide either a positional prompt argument or --file PATH")

    cmd = build_cmd(prompt, args.claude_args, include_defaults=not args.no_default_args)

    if args.tmux:
        sys.exit(run_in_tmux(args.tmux, cmd))

    env = stripped_env()
    try:
        os.execvpe("claude", cmd, env)
    except FileNotFoundError:
        print("sos-claude-print: `claude` not found on PATH", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
