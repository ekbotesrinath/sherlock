"""
Grafana Loki connection script for Sherlock.

Prompts for credentials interactively (falls back to .env or CLI args),
then connects and runs a quick smoke-test.

Usage:
    python scripts/connect_loki.py                           # fully interactive (lists labels)
    python scripts/connect_loki.py --tool logs               # connect + search recent error logs
    python scripts/connect_loki.py --url https://... --tool logs
    python scripts/connect_loki.py --url https://... --username <tenant-id> --password <api-key>

For Grafana Cloud:
    username = numeric tenant ID
    password = Grafana Cloud API key
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect Sherlock to Loki and run a quick check.",
    )
    parser.add_argument("--url",      help="Loki base URL (e.g. http://loki:3100 or Grafana Cloud URL)")
    parser.add_argument("--token",    help="Bearer token (optional)")
    parser.add_argument("--username", help="Basic auth username — Grafana Cloud: numeric tenant ID (optional)")
    parser.add_argument("--password", help="Basic auth password — Grafana Cloud: API key (optional)")
    parser.add_argument("--org-id",   help="X-Scope-OrgID header (multi-tenant Loki, optional)")
    parser.add_argument(
        "--tool",
        choices=["logs", "labels", "none"],
        default="labels",
        help="What to run after connecting (default: labels — list available label names)",
    )
    parser.add_argument("--logql",  default='{job=~".+"}',  help="LogQL for --tool logs")
    parser.add_argument("--since",  type=int, default=None, help="Look-back minutes for --tool logs (prompts if omitted)")
    parser.add_argument("--limit",  type=int, default=20,   help="Max log lines to return (default: 20)")
    return parser.parse_args()


def _prompt(label: str, env_var: str, arg_val: str | None, secret: bool = False, required: bool = True) -> str:
    value = arg_val or os.getenv(env_var, "")
    if value:
        masked = value[:4] + "***" + value[-4:] if secret and len(value) > 8 else value
        print(f"  {label}: {masked}  (from {'CLI arg' if arg_val else 'env'})")
        return value
    if not required:
        raw = input(f"  {label} (optional, press Enter to skip): ").strip()
        return raw
    if secret:
        value = getpass.getpass(f"  Enter {label}: ").strip()
    else:
        value = input(f"  Enter {label}: ").strip()
    if not value:
        print(f"ERROR: {label} is required.")
        sys.exit(1)
    return value


async def _connect(args: argparse.Namespace) -> bool:
    from tools.loki.loki import connect_loki

    print()
    print("─" * 60)
    print("  Step 1 — Loki credentials")
    print("  (Grafana Cloud: username=<tenant-id>  password=<api-key>)")
    print("─" * 60)
    url      = _prompt("Loki URL",            "LOKI_URL",      args.url)
    token    = _prompt("Bearer Token",        "LOKI_TOKEN",    args.token,    secret=True, required=False) or None
    username = _prompt("Basic Auth Username", "LOKI_USERNAME", args.username, required=False) or None
    password = (
        _prompt("Basic Auth Password", "LOKI_PASSWORD", args.password, secret=True, required=False) or None
        if username else None
    )
    org_id   = _prompt("Org ID (X-Scope-OrgID)", "LOKI_ORG_ID", args.org_id, required=False) or None

    print()
    print("  Connecting…")
    result = json.loads(await connect_loki(
        url=url,
        token=token,
        username=username,
        password=password,
        org_id=org_id,
    ))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print(f"✅  Connected to Loki")
    print(f"    URL      : {url}")
    print(f"    Duration : {result.get('duration_ms', '?')}ms")
    return True


async def _run_labels() -> None:
    from tools.loki.loki import search_loki_logs

    print()
    print("─" * 60)
    print("  Step 2 — Available label names")
    print("─" * 60)
    # Passing empty logql triggers label discovery in the tool.
    result = json.loads(await search_loki_logs(logql=""))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    labels = result.get("labels", [])
    if labels:
        print(f"  Labels found ({len(labels)}): {', '.join(labels[:30])}")
        if len(labels) > 30:
            print(f"  … and {len(labels) - 30} more")
    else:
        print("  No labels returned.")
        if result.get("hint"):
            print(f"  Hint: {result['hint']}")


async def _run_logs(logql: str, since: int, limit: int) -> None:
    from tools.loki.loki import search_loki_logs

    print()
    print("─" * 60)
    print(f"  Step 2 — LogQL: {logql}  (last {since}m, limit {limit})")
    print("─" * 60)
    result = json.loads(await search_loki_logs(logql=logql, since_minutes=since, limit=limit))

    if "error" in result:
        print(f"❌  {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return

    lines = result.get("lines", [])
    total = result.get("total_lines", len(lines))
    print(f"  Lines returned : {total}")
    print()
    for entry in lines[:20]:
        ts      = entry.get("timestamp", "")
        message = str(entry.get("line", ""))[:140]
        labels  = entry.get("stream", {})
        label_str = "  ".join(f"{k}={v}" for k, v in list(labels.items())[:3])
        print(f"  {ts}  {label_str}  {message}")


async def main() -> None:
    args = _parse_args()
    print("=" * 60)
    print("  Sherlock — Loki Connection")
    print("=" * 60)

    connected = await _connect(args)
    if not connected:
        sys.exit(1)

    if args.tool == "labels":
        await _run_labels()
    elif args.tool == "logs":
        since = args.since
        if since is None:
            raw = input("  Look-back window in minutes [60]: ").strip()
            since = int(raw) if raw.isdigit() else 60
        await _run_logs(args.logql, since, args.limit)

    print()


if __name__ == "__main__":
    asyncio.run(main())
