"""
Grafana connection script for Sherlock.

Prompts for credentials interactively (falls back to .env or CLI args),
then connects and runs a quick smoke-test.

Usage:
    python scripts/connect_grafana.py                           # fully interactive (shows datasources)
    python scripts/connect_grafana.py --tool alerts             # connect + show firing alerts
    python scripts/connect_grafana.py --url http://... --token xxx --tool alerts

For Grafana Cloud:
    url      = https://your-org.grafana.net
    token    = service account token with Viewer role

For local / self-hosted Grafana:
    url      = http://grafana:3000
    username = admin
    password = admin  (or whatever was set during install)
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect Sherlock to Grafana and run a quick check.",
    )
    parser.add_argument("--url",      help="Grafana base URL (e.g. http://grafana:3000)")
    parser.add_argument("--token",    help="Service account token or personal API token (recommended)")
    parser.add_argument("--api-key",  help="Legacy Grafana API key")
    parser.add_argument("--username", help="Basic auth username (e.g. admin)")
    parser.add_argument("--password", help="Basic auth password")
    parser.add_argument("--org-id",   type=int, help="Grafana organization ID (multi-org only, optional)")
    parser.add_argument(
        "--tool",
        choices=["alerts", "datasources", "none"],
        default="datasources",
        help="What to run after connecting (default: datasources — list configured data sources)",
    )
    parser.add_argument("--severity",   default=None, help="Filter alerts by severity label (e.g. critical)")
    parser.add_argument("--service",    default=None, help="Filter alerts by service label")
    parser.add_argument("--namespace",  default=None, help="Filter alerts by namespace label")
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
        print(f"  ERROR: {label} is required.", file=sys.stderr)
        sys.exit(1)
    return value


async def main() -> None:
    args = _parse_args()

    print("\n  Grafana — connect + check")
    print("  " + "-" * 58)

    url = _prompt("Grafana URL", "GRAFANA_URL", args.url, required=True)

    # Attempt auto-detect of auth method
    token = os.getenv("GRAFANA_TOKEN") or args.token
    api_key = os.getenv("GRAFANA_API_KEY") or args.api_key
    username = os.getenv("GRAFANA_USERNAME") or args.username
    password = os.getenv("GRAFANA_PASSWORD") or args.password

    if not any([token, api_key, username]):
        print("\n  No credentials found in env. Choose auth method:")
        print("    1. Service account token  (recommended)")
        print("    2. Username + password     (Basic auth)")
        print("    3. No auth                 (unsecured local Grafana)")
        choice = input("  Choice [1/2/3] (default 1): ").strip() or "1"

        if choice == "1":
            token = _prompt("Service account token", "GRAFANA_TOKEN", None, secret=True, required=True)
        elif choice == "2":
            username = _prompt("Username", "GRAFANA_USERNAME", None, required=True)
            password = _prompt("Password", "GRAFANA_PASSWORD", None, secret=True, required=True)
        # choice 3 = no auth, leave all None

    org_id = args.org_id or (int(os.getenv("GRAFANA_ORG_ID", "0")) or None)

    from tools.grafana_tool import connect_grafana, get_grafana_alerts

    print("\n  Connecting to Grafana...")
    result_raw = await connect_grafana(
        url=url,
        token=token or None,
        api_key=api_key or None,
        username=username or None,
        password=password or None,
        org_id=org_id,
    )
    result = json.loads(result_raw)

    if "error" in result:
        print(f"\n  ERROR: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(f"  Hint: {result['hint']}", file=sys.stderr)
        sys.exit(1)

    print(f"  Connected: Grafana {result.get('grafana_version', '?')} at {result.get('url')}")
    print(f"  Database:  {result.get('database')}")

    datasources = result.get("datasources", [])
    if datasources:
        print(f"\n  Datasources ({len(datasources)}):")
        for ds in datasources:
            print(f"    - {ds.get('name')} ({ds.get('type')})")

    if args.tool == "alerts":
        print("\n  " + "-" * 58)
        print("  Firing Alerts")
        print("  " + "-" * 58)
        alerts_raw = await get_grafana_alerts(
            severity=args.severity,
            service=args.service,
            namespace=args.namespace,
        )
        alerts = json.loads(alerts_raw)

        if "error" in alerts:
            print(f"\n  ERROR: {alerts['error']}", file=sys.stderr)
            sys.exit(1)

        total = alerts.get("total_firing", 0)
        by_sev = alerts.get("by_severity", {})
        api_ver = alerts.get("api_version_used", "v2")
        print(f"\n  Total firing: {total}  (API: {api_ver})")
        for sev, count in sorted(by_sev.items()):
            print(f"    {sev:12s}: {count}")

        if total > 0:
            print("\n  Alerts:")
            for a in alerts.get("alerts", []):
                sev = a.get("severity", "?")
                name = a.get("name", "?")
                svc = a.get("service") or a.get("namespace") or ""
                summary = a.get("summary", "")
                started = (a.get("starts_at") or "")[:19]
                svc_tag = f"  [{svc}]" if svc else ""
                print(f"    [{sev.upper():8s}] {name}{svc_tag}")
                if summary:
                    print(f"             {summary[:90]}")
                if started:
                    print(f"             Started: {started}")
        else:
            print("\n  No firing alerts.")

    print()


if __name__ == "__main__":
    asyncio.run(main())
