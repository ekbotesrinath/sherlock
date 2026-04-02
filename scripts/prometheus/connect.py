"""
Prometheus + Alertmanager connection script for Sherlock.

Prompts for credentials interactively (falls back to .env or CLI args),
then connects and runs a quick smoke-test.

Usage:
    python scripts/connect_prometheus.py                     # fully interactive
    python scripts/connect_prometheus.py --tool alerts       # connect + show firing alerts
    python scripts/connect_prometheus.py --tool query        # connect + run a PromQL query
    python scripts/connect_prometheus.py --url http://... --token xxx --tool alerts
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
        description="Connect Sherlock to Prometheus and run a quick check.",
    )
    parser.add_argument("--url",              help="Prometheus base URL (e.g. http://prometheus:9090)")
    parser.add_argument("--alertmanager-url", help="Alertmanager base URL (e.g. http://alertmanager:9093)")
    parser.add_argument("--token",            help="Bearer token (optional)")
    parser.add_argument("--username",         help="Basic auth username (optional)")
    parser.add_argument("--password",         help="Basic auth password (optional)")
    parser.add_argument(
        "--tool",
        choices=["alerts", "query", "none"],
        default="alerts",
        help="What to run after connecting (default: alerts)",
    )
    parser.add_argument("--promql", default="up", help="PromQL to run with --tool query (default: up)")
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
    from tools.prometheus.promql import connect_prometheus

    print()
    print("─" * 60)
    print("  Step 1 — Prometheus credentials")
    print("─" * 60)
    url = _prompt("Prometheus URL", "PROMETHEUS_URL", args.url)
    alertmanager_url = _prompt("Alertmanager URL", "ALERTMANAGER_URL", args.alertmanager_url, required=False) or None
    token    = _prompt("Bearer Token",    "PROMETHEUS_TOKEN",    args.token,    secret=True, required=False) or None
    username = _prompt("Basic Auth User", "PROMETHEUS_USERNAME", args.username, required=False) or None
    password = (
        _prompt("Basic Auth Password", "PROMETHEUS_PASSWORD", args.password, secret=True, required=False) or None
        if username else None
    )

    print()
    print("  Connecting…")
    result = json.loads(await connect_prometheus(
        url=url,
        alertmanager_url=alertmanager_url,
        token=token,
        username=username,
        password=password,
    ))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print(f"✅  Connected to Prometheus")
    print(f"    URL      : {url}")
    if alertmanager_url:
        print(f"    Alertmgr : {alertmanager_url}")
    print(f"    Duration : {result.get('duration_ms', '?')}ms")
    return True


async def _run_alerts() -> None:
    from tools.prometheus.promql import get_prometheus_alerts

    print()
    print("─" * 60)
    print("  Step 2 — Firing alerts (from Alertmanager)")
    print("─" * 60)
    result = json.loads(await get_prometheus_alerts())

    if "error" in result:
        print(f"❌  {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return

    alerts = result.get("alerts", [])
    print(f"  Total firing : {len(alerts)}")
    print()
    if not alerts:
        print("  ✅  No alerts firing.")
        return
    for a in alerts[:20]:
        name     = a.get("labels", {}).get("alertname", "unknown")
        severity = a.get("labels", {}).get("severity", "?")
        state    = a.get("status", {}).get("state", "?") if isinstance(a.get("status"), dict) else a.get("status", "?")
        summary  = a.get("annotations", {}).get("summary", "")
        print(f"  🔴 [{severity}] {name}  state={state}  {summary}")


async def _run_query(promql: str) -> None:
    from tools.prometheus.promql import run_promql_query

    print()
    print("─" * 60)
    print(f"  Step 2 — PromQL: {promql}")
    print("─" * 60)
    result = json.loads(await run_promql_query(promql=promql, query_type="instant"))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    rows = result.get("results", [])
    print(f"  Rows returned : {len(rows)}")
    for r in rows[:10]:
        print(f"  {r}")


async def main() -> None:
    args = _parse_args()
    print("=" * 60)
    print("  Sherlock — Prometheus Connection")
    print("=" * 60)

    connected = await _connect(args)
    if not connected:
        sys.exit(1)

    if args.tool == "alerts":
        await _run_alerts()
    elif args.tool == "query":
        await _run_query(args.promql)

    print()


if __name__ == "__main__":
    asyncio.run(main())
