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
import sys

STRIP_ENV = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "ANTHROPIC_API_KEY",
]

DEFAULT_ARGS = ["--dangerously-skip-permissions", "--print"]


def stripped_env(source=None):
    """Return a copy of the env with the parent-harness + API-key vars removed."""
    if source is None:
        source = os.environ
    return {k: v for k, v in source.items() if k not in STRIP_ENV}


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

    env = stripped_env()
    cmd = build_cmd(prompt, args.claude_args, include_defaults=not args.no_default_args)

    try:
        os.execvpe("claude", cmd, env)
    except FileNotFoundError:
        print("sos-claude-print: `claude` not found on PATH", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
