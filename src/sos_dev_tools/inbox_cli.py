#!/usr/bin/env python3
"""sos-inbox — Post cards to the ghostty-mini browser sidebar.

The sidebar is a localhost-only attention surface hosted by ghostty-mini at
``http://localhost:3030`` (override via ``GHOSTTY_MINI_URL``). A card is a JSON
payload with at least a title; it groups into a section by ticket key and can
carry a URL, context line, and custom action buttons.

Two kinds:

- ``info``   — reference bookmark (PR link, deploy URL, review posted)
- ``action`` — needs human input (QA gate, blocker, approval)

Usage:
    sos-inbox info   "PR opened" --ticket FOO-123 --url https://github.com/...
    sos-inbox action "Ready for QA" --ticket FOO-123 --url http://localhost:3142 \\
        --ctx "dev server up" \\
        --actions '[{"label":"Open","kind":"openUrl"},
                    {"label":"Approve","kind":"inject","text":"/flow-dev qa-approve\\n","execute":true}]'
    sos-inbox list [--ticket FOO-123] [--json]
    sos-inbox remove CARD_ID
    sos-inbox clear  [--ticket FOO-123]
    sos-inbox status

All network errors except HTTP 4xx/5xx silently no-op and exit 0 — a missing
sidebar must never block the flow. HTTP errors (malformed payload, server bug)
print to stderr and exit 1.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_BASE = "http://localhost:3030"
TIMEOUT_SECONDS = 2.0


def inbox_base():
    """Return the ghostty-mini base URL, honoring the env override."""
    return os.environ.get("GHOSTTY_MINI_URL", DEFAULT_BASE).rstrip("/")


def _post(path, body):
    """POST JSON to the inbox. Returns parsed response, or None if unreachable."""
    req = urllib.request.Request(
        inbox_base() + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"inbox error: HTTP {e.code} — {err_body}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def _get(path):
    """GET JSON from the inbox. Returns parsed response, or None if unreachable."""
    try:
        with urllib.request.urlopen(inbox_base() + path, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"inbox error: HTTP {e.code} — {err_body}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def _delete(path):
    """DELETE against the inbox. Returns parsed response, or None if unreachable.

    A 404 (no card with that id) is surfaced as a SystemExit(1) so scripts can
    branch on "did I actually remove something" vs silent no-op for "server is
    down".
    """
    req = urllib.request.Request(inbox_base() + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"inbox error: HTTP {e.code} — {err_body}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def build_card(kind, args):
    """Construct the card payload from parsed argparse args."""
    card = {"kind": kind, "title": args.title}
    if getattr(args, "ticket", None):
        card["ticket"] = args.ticket
    if getattr(args, "url", None):
        card["url"] = args.url
    if getattr(args, "ctx", None):
        card["ctx"] = args.ctx

    actions_raw = getattr(args, "actions", None)
    if actions_raw:
        try:
            parsed = json.loads(actions_raw)
        except json.JSONDecodeError as e:
            print(f"--actions is not valid JSON: {e}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(parsed, list):
            print("--actions must be a JSON array", file=sys.stderr)
            sys.exit(2)
        card["actions"] = parsed

    return card


def cmd_post(kind, args):
    """Post a card of the given kind."""
    card = build_card(kind, args)
    result = _post("/inbox", card)
    if result is None:
        # Silent no-op — sidebar unreachable. The flow continues.
        return
    print(result.get("id", ""))


def cmd_status(args):
    """Print how many browser tabs are currently connected."""
    result = _get("/inbox/status")
    if result is None:
        print("disconnected (ghostty-mini unreachable)")
        sys.exit(1)
    connected = result.get("connected", 0)
    count = result.get("count", 0)
    print(f"connected={connected} cards={count}")


def _filter_by_ticket(cards, ticket):
    if not ticket:
        return cards
    return [c for c in cards if c.get("ticket") == ticket]


def cmd_list(args):
    """Show the current server-side inbox."""
    result = _get("/inbox")
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    cards = _filter_by_ticket(result.get("cards", []), args.ticket)

    if args.json:
        print(json.dumps(cards, indent=2))
        return

    if not cards:
        print("(inbox is empty)" if not args.ticket else f"(no cards for {args.ticket})")
        return

    for c in cards:
        ticket = c.get("ticket") or "-"
        kind = c.get("kind", "info")
        title = c.get("title", "")
        print(f"{c['id']}  {ticket:<10}  {kind:<6}  {title}")
        if c.get("url"):
            print(f"                                  {c['url']}")
        if c.get("ctx"):
            print(f"                                  ({c['ctx']})")


def cmd_remove(args):
    """Remove a single card by id."""
    result = _delete(f"/inbox/{args.card_id}")
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    print(f"removed {args.card_id}")


def cmd_clear(args):
    """Clear all cards — or all cards in a ticket group."""
    if args.ticket:
        # Targeted clear: fetch, filter, delete each.
        result = _get("/inbox")
        if result is None:
            print("inbox unreachable", file=sys.stderr)
            sys.exit(1)
        ids = [c["id"] for c in result.get("cards", []) if c.get("ticket") == args.ticket]
        if not ids:
            print(f"(no cards for {args.ticket})")
            return
        for cid in ids:
            _delete(f"/inbox/{cid}")
        print(f"removed {len(ids)} card(s) from {args.ticket}")
        return

    # Full clear.
    result = _delete("/inbox")
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    print(f"cleared {result.get('removed', 0)} card(s)")


def _add_card_args(p, with_actions=False):
    p.add_argument("title", help="Card title (required)")
    p.add_argument("--ticket", "-T", default=None,
                   help="Group card under this ticket key (e.g. FOO-123)")
    p.add_argument("--url", "-u", default=None, help="Primary URL for the card")
    p.add_argument("--ctx", "-c", default=None, help="Context / provenance line")
    if with_actions:
        p.add_argument(
            "--actions", default=None,
            help=(
                "JSON array of button objects. Each button: "
                "{label, kind, text?, url?, execute?}. "
                "kind is one of: openUrl, copy, inject, dismiss. "
                "For inject buttons, execute:true appends newline so the command "
                "runs on click; execute:false types without running."
            ),
        )


def main():
    parser = argparse.ArgumentParser(
        prog="sos-inbox",
        description="Post cards to the ghostty-mini browser sidebar",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="Post a reference card (no action required)")
    _add_card_args(p, with_actions=False)

    p = sub.add_parser("action", help="Post an action-required card")
    _add_card_args(p, with_actions=True)

    p = sub.add_parser("list", help="Show the current inbox contents")
    p.add_argument("--ticket", "-T", default=None,
                   help="Filter to cards in this ticket group")
    p.add_argument("--json", action="store_true",
                   help="Output as JSON (for scripts)")

    p = sub.add_parser("remove", help="Remove one card by id")
    p.add_argument("card_id", help="Card id (e.g. card_abc123)")

    p = sub.add_parser("clear", help="Clear all cards, or a single ticket group")
    p.add_argument("--ticket", "-T", default=None,
                   help="Clear only this ticket group (otherwise: clear everything)")

    sub.add_parser("status", help="Show whether any browser tab is connected")

    args = parser.parse_args()

    {
        "info": lambda a: cmd_post("info", a),
        "action": lambda a: cmd_post("action", a),
        "list": cmd_list,
        "remove": cmd_remove,
        "clear": cmd_clear,
        "status": cmd_status,
    }[args.command](args)


if __name__ == "__main__":
    main()
