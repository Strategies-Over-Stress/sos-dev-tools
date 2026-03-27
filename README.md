# sos-dev-tools

Jira ticket management and Git feature branch lifecycle automation. Zero external dependencies.

## Install

```bash
pip install git+https://github.com/Strategies-Over-Stress/sos-dev-tools.git
```

## Commands

### sos-jira

```bash
sos-jira create -s "Add checkout optimization"
sos-jira create -s "Title" -d "## Description with **markdown**"
sos-jira edit RICH-1 -s "New title"
sos-jira move RICH-1 "IN PROGRESS"
sos-jira view RICH-1
sos-jira list --status "To Do"
sos-jira comment RICH-1 "## Update"
sos-jira delete RICH-3
```

### sos-feature

```bash
sos-feature create "Add risk reversal to contact section"
sos-feature start 5          # shorthand for RICH-5
sos-feature switch 3
sos-feature pr
sos-feature status
```

## Lifecycle

```
sos-feature create "desc"  → ticket (TO DO) + branch
sos-feature start 5        → checkout branch, ticket → IN PROGRESS
sos-feature pr             → push + GitHub PR, ticket → IN REVIEW
sos-jira move RICH-5 DONE  → manual after merge
```

## Configuration

Place a `.env` file in your project root (or any parent directory):

```env
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-token
JIRA_PROJECT_KEY=PROJ
```

The CLI walks up from your current directory to find the nearest `.env`.
