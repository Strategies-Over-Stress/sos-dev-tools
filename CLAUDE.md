# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Jira ticket management and Git feature branch lifecycle automation for the Strategies Over Stress team. Two CLI tools (`sos-jira`, `sos-feature`) with zero external dependencies — Python stdlib only.

## Development Commands

```bash
# Install in editable mode
pip install -e .

# Run tests
python -m unittest tests.test_jira_api -v

# Run a single test class
python -m unittest tests.test_jira_api.TestDiskCache -v

# Run a single test
python -m unittest tests.test_jira_api.TestDiskCache.test_save_then_load -v
```

No linter or formatter is configured.

## Architecture

Two CLI entry points defined in `pyproject.toml [project.scripts]`:

- **`sos-jira`** → `jira_cli.py:main` — CRUD operations on Jira tickets (create, edit, move, view, list, comment, delete, create-project)
- **`sos-feature`** → `feature_cli.py:main` — Feature branch lifecycle that bridges Git/GitHub and Jira (create ticket+branch, start, switch, pr, status)

Both CLIs call `env.load_env()` at startup, which walks up from CWD to find the nearest `.env` file and loads it via `os.environ.setdefault`. This means per-project Jira configs work by placing `.env` in each project root.

**`jira_api.py`** is the shared core:
- `api()` — generic Jira REST v3 client using `urllib` (no `requests`)
- Issue type and transition auto-discovery with a three-tier resolution: env var overrides → in-memory dict cache → disk cache (`.jira-cache.json`, 24h TTL) → Jira API
- `md_to_adf()` — Markdown to Atlassian Document Format converter (headings, bold, code, bullet/ordered lists)

**`feature_cli.py`** shells out to `git` and `gh` (GitHub CLI) via `subprocess.run`. The feature lifecycle is: `create` (ticket + branch) → `start` (checkout + IN PROGRESS) → `pr` (push + GH PR + IN REVIEW). Branch naming: `feature/{TICKET_KEY}-{slug}`.

## Key Patterns

- All Jira API errors exit the process via `sys.exit(1)` after printing to stderr — there is no exception-based error handling.
- The `-P`/`--project` flag on both CLIs overrides `JIRA_PROJECT_KEY` for that invocation via `set_project_key()` (sets `os.environ`).
- The `-t`/`--type` flag uses `type=str.lower` in argparse to normalize casing before lookup.
- Tests mock `jira_api.api` and disk cache functions — no live Jira instance needed. Cache tests use `tempfile.mkdtemp` with a patched `_cache_file`.
