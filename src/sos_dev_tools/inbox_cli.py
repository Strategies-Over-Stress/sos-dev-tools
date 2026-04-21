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
    sos-inbox reply CARD_ID "text"       # append a reply to a card
    sos-inbox replies CARD_ID [--json]   # list replies on a card
    sos-inbox wait CARD_ID [--timeout SEC] [--since TS]  # block until a reply arrives
    sos-inbox prompt "Question" --ticket T \\
        --actions '[{"label":"A","kind":"reply","text":"use A"}, ...]' \\
        --timeout 1800
        # ↑ the blocker primitive: posts an action card, long-polls for a reply,
        #   auto-removes the card, prints the reply text. One line per blocker.
    sos-inbox status

All network errors except HTTP 4xx/5xx silently no-op and exit 0 — a missing
sidebar must never block the flow. HTTP errors (malformed payload, server bug)
print to stderr and exit 1.
"""

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request


DEFAULT_BASE = "http://localhost:3030"
TIMEOUT_SECONDS = 2.0
# Server caps a single long-poll at 5 minutes. `wait` loops under the hood
# so callers can ask for longer total waits without blowing the per-request cap.
SERVER_WAIT_CAP_MS = 290_000


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


def _get(path, timeout=TIMEOUT_SECONDS):
    """GET JSON from the inbox. Returns parsed response, or None if unreachable."""
    try:
        with urllib.request.urlopen(inbox_base() + path, timeout=timeout) as resp:
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


def cmd_prompt(args):
    """The blocker primitive: post an action card, wait for a reply, remove it.

    One line per blocker. Returns the reply text on stdout; non-zero exit if
    the wait times out or the sidebar is unreachable.
    """
    # 1. Post the card (kind is always "action" for a prompt).
    card = build_card("action", args)
    result = _post("/inbox", card)
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    card_id = result.get("id")
    if not card_id:
        print(f"unexpected response from POST /inbox: {result!r}", file=sys.stderr)
        sys.exit(1)

    # 2. Long-poll for the first reply.
    deadline = time.time() + args.timeout
    since = 0
    reply = None
    try:
        while reply is None:
            remaining_s = deadline - time.time()
            if remaining_s <= 0:
                print("prompt: timed out waiting for reply", file=sys.stderr)
                sys.exit(2)
            chunk_ms = min(int(remaining_s * 1000), SERVER_WAIT_CAP_MS)
            http_timeout = (chunk_ms / 1000) + 5
            got = _get(
                f"/inbox/{card_id}/wait?since={since}&timeout_ms={chunk_ms}",
                timeout=http_timeout,
            )
            if got is None:
                print("inbox unreachable", file=sys.stderr)
                sys.exit(1)
            if "reply" in got:
                reply = got["reply"]
            elif got.get("timeout"):
                since = int(time.time() * 1000)
                continue
            else:
                print(f"prompt: unexpected response {got!r}", file=sys.stderr)
                sys.exit(1)
    finally:
        # 3. Always clean the card — even if the user Ctrl-C's the prompt.
        _delete(f"/inbox/{card_id}")

    if args.json:
        print(json.dumps(reply))
    else:
        print(reply.get("text", ""))


def _encode_attachment(path):
    """Read a local image file, return the server's attachment payload shape."""
    with open(path, "rb") as f:
        data = f.read()
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        # Fallback — let the server reject unsupported mimes.
        mime = "application/octet-stream"
    return {
        "filename": os.path.basename(path),
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def cmd_reply(args):
    """Append a reply to a card, optionally with image attachments."""
    body = {"text": args.text or ""}
    if args.attach:
        try:
            body["attachments"] = [_encode_attachment(p) for p in args.attach]
        except FileNotFoundError as e:
            print(f"reply: file not found: {e.filename}", file=sys.stderr)
            sys.exit(2)
    if not body["text"].strip() and not body.get("attachments"):
        print("reply: at least text or one --attach is required", file=sys.stderr)
        sys.exit(2)
    result = _post(f"/inbox/{args.card_id}/reply", body)
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    n = len(body.get("attachments") or [])
    suffix = f" with {n} attachment{'s' if n != 1 else ''}" if n else ""
    print(f"replied to {args.card_id}{suffix}")


def cmd_replies(args):
    """List the reply thread on a card."""
    result = _get(f"/inbox/{args.card_id}/replies")
    if result is None:
        print("inbox unreachable", file=sys.stderr)
        sys.exit(1)
    replies = result.get("replies", [])
    if args.json:
        print(json.dumps(replies, indent=2))
        return
    if not replies:
        print("(no replies)")
        return
    for r in replies:
        ts = r.get("ts", 0)
        # ISO8601 UTC for readability — same format sos-pm uses.
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts / 1000)) if ts else "?"
        print(f"[{when}] {r.get('text', '')}")
        for a in r.get("attachments") or []:
            print(f"          📎 {a.get('filename', '?')} — {a.get('path', '?')}")


def cmd_wait(args):
    """Block until a reply arrives on a card, print it, exit.

    Total wait is the user-requested --timeout. Under the hood we issue
    repeated long-polls that each stay under the server's 5-minute cap.
    """
    deadline = time.time() + args.timeout
    since = args.since
    while True:
        remaining_s = deadline - time.time()
        if remaining_s <= 0:
            print("wait: timed out", file=sys.stderr)
            sys.exit(2)
        chunk_ms = min(int(remaining_s * 1000), SERVER_WAIT_CAP_MS)
        # HTTP timeout slightly longer than server's so its timeout response can arrive.
        http_timeout = (chunk_ms / 1000) + 5
        path = f"/inbox/{args.card_id}/wait?since={since}&timeout_ms={chunk_ms}"
        result = _get(path, timeout=http_timeout)
        if result is None:
            print("inbox unreachable", file=sys.stderr)
            sys.exit(1)
        if "reply" in result:
            reply = result["reply"]
            if args.json:
                print(json.dumps(reply))
            else:
                print(reply.get("text", ""))
            return
        if result.get("timeout"):
            # Bump `since` past now so we don't re-receive replies that arrived
            # during the gap between polls (shouldn't happen but belt-and-suspenders).
            since = int(time.time() * 1000)
            continue
        # Unknown shape — exit non-zero so the caller knows.
        print(f"wait: unexpected response {result!r}", file=sys.stderr)
        sys.exit(1)


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


def _add_card_args(p):
    """Common flags for card-posting subcommands (info, action, prompt).

    --actions is exposed on every card type. The server accepts actions on
    info cards too (reference cards with an 'Open X' button are legitimate);
    the CLI used to gate this flag behind the `action` subcommand only, which
    silently broke any caller that tried to post actionable info cards.
    """
    p.add_argument("title", help="Card title (required)")
    p.add_argument("--ticket", "-T", default=None,
                   help="Group card under this ticket key (e.g. FOO-123)")
    p.add_argument("--url", "-u", default=None, help="Primary URL for the card")
    p.add_argument("--ctx", "-c", default=None, help="Context / provenance line")
    p.add_argument(
        "--actions", default=None,
        help=(
            "JSON array of button objects. Each button: "
            "{label, kind, text?, url?, execute?}. "
            "kind is one of: openUrl, copy, inject, reply, dismiss. "
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
    _add_card_args(p)

    p = sub.add_parser("action", help="Post an action-required card")
    _add_card_args(p)

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

    p = sub.add_parser("reply", help="Append a reply to a card")
    p.add_argument("card_id", help="Card id (e.g. card_abc123)")
    p.add_argument("text", nargs="?", default="",
                   help="Reply text (optional if at least one --attach is given)")
    p.add_argument("--attach", "-a", action="append", default=[], metavar="PATH",
                   help="Attach an image file (repeat for multiple; up to 8)")

    p = sub.add_parser("replies", help="List the reply thread on a card")
    p.add_argument("card_id")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    p = sub.add_parser(
        "wait",
        help="Block until a reply lands on a card; print the reply text to stdout",
    )
    p.add_argument("card_id")
    p.add_argument("--timeout", type=int, default=3600,
                   help="Maximum total wait in seconds (default: 3600)")
    p.add_argument("--since", type=int, default=0,
                   help="Only return replies newer than this ms-epoch timestamp")
    p.add_argument("--json", action="store_true",
                   help="Output the full reply object as JSON instead of just text")

    p = sub.add_parser(
        "prompt",
        help="Blocker primitive: post action card, wait for reply, remove card",
        description=(
            "Single command for subagent blockers. Posts an action card, "
            "long-polls until a human replies (via the sidebar textarea or "
            "a reply-kind action button), removes the card, prints the "
            "reply text to stdout. Use this instead of manual "
            "action/wait/remove chains."
        ),
    )
    _add_card_args(p)
    p.add_argument("--timeout", type=int, default=3600,
                   help="Max total wait in seconds (default: 3600)")
    p.add_argument("--json", action="store_true",
                   help="Output the full reply object as JSON")

    sub.add_parser("status", help="Show whether any browser tab is connected")

    args = parser.parse_args()

    {
        "info": lambda a: cmd_post("info", a),
        "action": lambda a: cmd_post("action", a),
        "list": cmd_list,
        "remove": cmd_remove,
        "clear": cmd_clear,
        "reply": cmd_reply,
        "replies": cmd_replies,
        "wait": cmd_wait,
        "prompt": cmd_prompt,
        "status": cmd_status,
    }[args.command](args)


if __name__ == "__main__":
    main()
