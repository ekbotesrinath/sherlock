"""
Datadog connection script for Sherlock.

Prompts for credentials interactively (falls back to .env or CLI args),
then connects and runs a quick smoke-test.

Usage:
    python scripts/connect_datadog.py                        # fully interactive
    python scripts/connect_datadog.py --tool monitors        # connect + list firing monitors
    python scripts/connect_datadog.py --tool logs            # connect + search recent error logs
    python scripts/connect_datadog.py --tool metrics         # connect + run a metrics query
    python scripts/connect_datadog.py --api-key X --app-key Y --tool monitors
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
        description="Connect Sherlock to Datadog and run a quick check.",
    )
    parser.add_argument("--api-key", help="Datadog API key")
    parser.add_argument("--app-key", help="Datadog Application key")
    parser.add_argument(
        "--site",
        choices=["US1", "US3", "US5", "EU1", "AP1"],
        help="Datadog site region (default: US1)",
    )
    parser.add_argument(
        "--tool",
        choices=["monitors", "logs", "metrics", "none"],
        default="monitors",
        help="What to run after connecting (default: monitors)",
    )
    parser.add_argument("--query",   default="avg:system.cpu.user{*}", help="Metrics query for --tool metrics")
    parser.add_argument("--log-query", default="status:error",         help="Log search query for --tool logs")
    parser.add_argument("--since",   type=int, default=None,           help="Look-back minutes (prompts if omitted)")
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
    from tools.datadog.datadog_tool import connect_datadog

    print()
    print("─" * 60)
    print("  Step 1 — Datadog credentials")
    print("─" * 60)
    api_key = _prompt("API Key", "DATADOG_API_KEY", args.api_key, secret=True)
    app_key = _prompt("App Key", "DATADOG_APP_KEY", args.app_key, secret=True)

    site = args.site or os.getenv("DATADOG_SITE", "")
    if not site:
        site_input = input("  Site region (US1/US3/US5/EU1/AP1) [US1]: ").strip().upper() or "US1"
        site = site_input if site_input in ("US1", "US3", "US5", "EU1", "AP1") else "US1"
    print(f"  Site: {site}")

    print()
    print("  Connecting…")
    result = json.loads(await connect_datadog(api_key=api_key, app_key=app_key, site=site))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print(f"✅  Connected to Datadog")
    print(f"    Site     : {site}")
    print(f"    Duration : {result.get('duration_ms', '?')}ms")
    return True


async def _run_monitors() -> None:
    from tools.datadog.datadog_tool import get_datadog_monitors

    print()
    print("─" * 60)
    print("  Step 2 — Monitors in Alert/Warn state")
    print("─" * 60)
    result = json.loads(await get_datadog_monitors(triggered_only=True))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    monitors = result.get("monitors", [])
    print(f"  Total triggered : {len(monitors)}")
    print()
    if not monitors:
        print("  ✅  No monitors in Alert or Warn state.")
        return
    for m in monitors[:20]:
        status = m.get("overall_state", "?")
        name   = m.get("name", "unknown")
        mtype  = m.get("type", "?")
        print(f"  🔴 [{status}] {name}  type={mtype}")


async def _run_logs(query: str, since: int) -> None:
    from tools.datadog.datadog_tool import search_datadog_logs

    print()
    print("─" * 60)
    print(f"  Step 2 — Logs: \"{query}\" (last {since}m)")
    print("─" * 60)
    result = json.loads(await search_datadog_logs(query=query, since_minutes=since, limit=20))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    logs = result.get("logs", [])
    print(f"  Events returned : {len(logs)}")
    print()
    for log in logs[:10]:
        attrs   = log.get("attributes", {})
        status  = attrs.get("status", "?")
        service = attrs.get("service", "?")
        message = str(attrs.get("message", ""))[:120]
        ts      = attrs.get("timestamp", "")
        print(f"  [{status}] {service}  {ts}  {message}")


async def _run_metrics(query: str, since: int) -> None:
    from tools.datadog.datadog_tool import query_datadog_metrics

    print()
    print("─" * 60)
    print(f"  Step 2 — Metrics: \"{query}\" (last {since}m)")
    print("─" * 60)
    result = json.loads(await query_datadog_metrics(query=query, since_minutes=since))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    series = result.get("series", [])
    print(f"  Series returned : {len(series)}")
    for s in series[:5]:
        scope     = s.get("scope", "?")
        pointlist = s.get("pointlist", [])
        if pointlist:
            last_val = pointlist[-1][1] if len(pointlist[-1]) > 1 else "?"
            print(f"  {scope}  latest={last_val}")


async def main() -> None:
    args = _parse_args()
    print("=" * 60)
    print("  Sherlock — Datadog Connection")
    print("=" * 60)

    connected = await _connect(args)
    if not connected:
        sys.exit(1)

    if args.tool == "monitors":
        await _run_monitors()
    elif args.tool in ("logs", "metrics"):
        since = args.since
        if since is None:
            raw = input("  Look-back window in minutes [60]: ").strip()
            since = int(raw) if raw.isdigit() else 60
        if args.tool == "logs":
            await _run_logs(args.log_query, since)
        else:
            await _run_metrics(args.query, since)

    print()


if __name__ == "__main__":
    asyncio.run(main())
