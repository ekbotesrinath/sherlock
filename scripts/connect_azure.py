"""
Azure Monitor connection script for Sherlock.

Prompts for Azure credentials interactively (falls back to .env or CLI args),
then connects and shows fired alerts as a quick smoke-test.

Usage:
    python scripts/connect_azure.py                         # fully interactive
    python scripts/connect_azure.py --tool alerts           # connect + show alerts
    python scripts/connect_azure.py --tool app-insights     # connect App Insights
    python scripts/connect_azure.py --tenant-id X --client-id Y ...  # non-interactive
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
        description="Connect Sherlock to Azure Monitor and run a quick check.",
    )
    parser.add_argument("--tenant-id",       help="Azure AD Tenant ID")
    parser.add_argument("--client-id",       help="Service Principal Client ID")
    parser.add_argument("--client-secret",   help="Service Principal Client Secret")
    parser.add_argument("--subscription-id", help="Azure Subscription ID")
    parser.add_argument(
        "--auth",
        choices=["sp", "cli", "interactive"],
        default="sp",
        help=(
            "Auth method: 'sp' = Service Principal (default), "
            "'cli' = Azure CLI (az login), "
            "'interactive' = browser device-code (no install needed)"
        ),
    )
    parser.add_argument(
        "--tool",
        choices=["alerts", "metrics", "app-insights", "none"],
        default="alerts",
        help="What to run after connecting (default: alerts)",
    )
    parser.add_argument("--time-range",  default="1h",  help="Alert look-back window (default: 1h)")
    parser.add_argument("--app-id",      help="App Insights Application ID (for app-insights tool)")
    parser.add_argument("--api-key",     help="App Insights API key (for app-insights tool)")
    parser.add_argument("--since",       type=int, default=60, help="Look-back minutes for App Insights (default: 60)")
    return parser.parse_args()


def _prompt(label: str, env_var: str, arg_val: str | None, secret: bool = False) -> str:
    """Return arg → env → interactive prompt, in that priority order."""
    value = arg_val or os.getenv(env_var, "")
    if value:
        masked = value[:4] + "***" + value[-4:] if secret and len(value) > 8 else value
        print(f"  {label}: {masked}  (from {'CLI arg' if arg_val else 'env'})")
        return value
    if secret:
        value = getpass.getpass(f"  Enter {label}: ").strip()
    else:
        value = input(f"  Enter {label}: ").strip()
    if not value:
        print(f"ERROR: {label} is required.")
        sys.exit(1)
    return value


async def _connect_azure_monitor_interactive(args: argparse.Namespace) -> bool:
    """Connect to Azure Monitor via device code (browser sign-in). Returns True on success."""
    from tools.azure_monitor_tool import connect_azure_monitor_interactive

    print()
    print("─" * 60)
    print("  Step 1 — Azure Monitor (interactive browser sign-in)")
    print("─" * 60)
    sub_id = args.subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "") or ""
    if sub_id:
        print(f"  Subscription ID : {sub_id}")
    else:
        print("  Subscription ID : (auto-detect after sign-in)")
    print()
    print("  A browser sign-in code will appear below.")
    print("  Open the URL shown and enter the code with your Azure credentials.")
    print()

    result = json.loads(await connect_azure_monitor_interactive(subscription_id=sub_id))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print()
    print("✅  Connected to Azure Monitor via browser sign-in")
    print(f"    Subscription : {result.get('subscription_name', '')} ({result.get('subscription_id', '')})")
    print(f"    Duration     : {result.get('duration_ms', '?')}ms")
    return True


async def _connect_azure_monitor_cli(args: argparse.Namespace) -> bool:
    """Connect to Azure Monitor via Azure CLI. Returns True on success."""
    from tools.azure_monitor_tool import connect_azure_monitor_cli

    print()
    print("─" * 60)
    print("  Step 1 — Azure Monitor (CLI auth)")
    print("─" * 60)
    sub_id = args.subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "") or ""
    if sub_id:
        print(f"  Subscription ID : {sub_id}  (from {'CLI arg' if args.subscription_id else 'env'})")
    else:
        print("  Subscription ID : (auto-detect from az account show)")
    print()
    print("  Connecting via az CLI…")
    result = json.loads(await connect_azure_monitor_cli(subscription_id=sub_id))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print("\u2705  Connected to Azure Monitor via Azure CLI")
    print(f"    Subscription : {result.get('subscription_name', '')} ({result.get('subscription_id', '')})")
    print(f"    Duration     : {result.get('duration_ms', '?')}ms")
    return True


async def _connect_azure_monitor(args: argparse.Namespace) -> bool:
    """Connect to Azure Monitor. Returns True on success."""
    from tools.azure_monitor_tool import connect_azure_monitor

    print()
    print("─" * 60)
    print("  Step 1 — Azure Monitor credentials")
    print("─" * 60)
    tenant_id       = _prompt("Tenant ID",       "AZURE_TENANT_ID",       args.tenant_id)
    client_id       = _prompt("Client ID",       "AZURE_CLIENT_ID",       args.client_id)
    client_secret   = _prompt("Client Secret",   "AZURE_CLIENT_SECRET",   args.client_secret, secret=True)
    subscription_id = _prompt("Subscription ID", "AZURE_SUBSCRIPTION_ID", args.subscription_id)

    print()
    print("  Connecting…")
    result = json.loads(await connect_azure_monitor(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        subscription_id=subscription_id,
    ))

    if "error" in result:
        print(f"\n❌  Connection failed: {result['error']}")
        if hint := result.get("hint"):
            print(f"    Hint: {hint}")
        return False

    print(f"✅  Connected to Azure Monitor")
    print(f"    Subscription : {result.get('subscription_id', subscription_id)}")
    print(f"    Duration     : {result.get('duration_ms', '?')}ms")
    return True


async def _run_alerts(time_range: str) -> None:
    """Fetch and print fired alerts."""
    from tools.azure_monitor_tool import get_azure_monitor_alerts

    print()
    print("─" * 60)
    print(f"  Step 2 — Fired alerts (last {time_range})")
    print("─" * 60)

    result = json.loads(await get_azure_monitor_alerts(time_range=time_range))

    if "error" in result:
        print(f"❌  {result['error']}")
        return

    alerts = result.get("alerts", [])
    print(f"  Total fired   : {result.get('total_fired', len(alerts))}")
    print(f"  Time range    : {time_range}")
    print()

    if not alerts:
        print("  ✅  No alerts firing in this window.")
        return

    for a in alerts:
        sev      = a.get("severity", "?")
        name     = a.get("alertRuleName") or a.get("name", "unknown")
        state    = a.get("alertState") or a.get("monitorCondition", "?")
        rg       = a.get("resourceGroupName", "")
        fired_at = a.get("startDateTime") or a.get("firedTime", "")
        rg_str   = f"  rg={rg}" if rg else ""
        print(f"  🔴 [{sev}] {name}  state={state}{rg_str}  fired={fired_at}")


async def _connect_app_insights_interactive(args: argparse.Namespace) -> None:
    """Connect to App Insights via device code — no API key needed."""
    from tools.app_insights_tool import connect_app_insights_interactive, get_app_insights_failures

    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    app_id = (args.app_id or "").strip()

    print()
    print("─" * 60)
    print("  App Insights — interactive auth (no API key needed)")
    if not app_id:
        print("  app_id not specified — will auto-discover from subscription")
    print("─" * 60)
    print()
    print("  Connecting…")
    connect_result = json.loads(
        await connect_app_insights_interactive(subscription_id=sub_id, app_id=app_id)
    )

    if "error" in connect_result:
        print(f"\n❌  Connection failed: {connect_result['error']}")
        if hint := connect_result.get("hint"):
            print(f"    Hint: {hint}")
        return

    resolved_app_id = connect_result.get("app_id", "?")
    comp_name       = connect_result.get("component_name", "")
    print(f"✅  Connected to App Insights  ({comp_name or resolved_app_id[:8] + '…'})")
    print()
    print(f"  Fetching failures (last {args.since}m)…")
    failures = json.loads(await get_app_insights_failures(since_minutes=args.since))
    if "error" in failures:
        print(f"❌  {failures['error']}")
        return
    req_count = failures.get("failed_request_count", 0)
    exc_count = failures.get("exception_count", 0)
    print(f"  Failed requests : {req_count}")
    print(f"  Exception types : {exc_count}")
    print()
    for r in failures.get("request_summary", [])[:5]:
        print(f"  🔴 {r.get('name','?')}  code={r.get('resultCode','?')}  count={r.get('count',0)}")
    for e in failures.get("exception_summary", [])[:5]:
        print(f"  💥 {e.get('type','?')}  count={e.get('count',0)}")


async def _connect_app_insights(args: argparse.Namespace) -> None:
    """Connect to App Insights and show failures."""
    from tools.app_insights_tool import connect_app_insights, get_app_insights_failures

    print()
    print("─" * 60)
    print("  App Insights credentials")
    print("  (Find app_id in: Portal > App Insights > Configure > API Access)")
    print("─" * 60)
    app_id  = _prompt("Application ID", "AZURE_APP_INSIGHTS_APP_ID", args.app_id)
    api_key = _prompt("API Key",        "AZURE_APP_INSIGHTS_API_KEY", args.api_key, secret=True)

    print()
    print("  Connecting…")
    connect_result = json.loads(await connect_app_insights(app_id=app_id, api_key=api_key))

    if "error" in connect_result:
        print(f"\n❌  Connection failed: {connect_result['error']}")
        if hint := connect_result.get("hint"):
            print(f"    Hint: {hint}")
        return

    print(f"✅  Connected to Application Insights  (app_id={app_id[:8]}…)")
    print()
    print(f"  Fetching failures (last {args.since}m)…")

    failures = json.loads(await get_app_insights_failures(since_minutes=args.since))

    if "error" in failures:
        print(f"❌  {failures['error']}")
        return

    req_count = failures.get("failed_request_count", 0)
    exc_count = failures.get("exception_count", 0)
    print(f"  Failed requests : {req_count}")
    print(f"  Exception types : {exc_count}")
    print()

    for r in failures.get("request_summary", [])[:5]:
        print(f"  🔴 {r.get('name','?')}  code={r.get('resultCode','?')}  count={r.get('count',0)}")
    for e in failures.get("exception_summary", [])[:5]:
        print(f"  💥 {e.get('type','?')}  count={e.get('count',0)}")


async def main() -> None:
    args = _parse_args()

    print("=" * 60)
    print("  Sherlock — Azure Connection")
    print("=" * 60)

    if args.tool == "app-insights":
        if args.auth == "interactive":
            await _connect_app_insights_interactive(args)
        else:
            await _connect_app_insights(args)
        print()
        return

    if args.auth == "cli":
        connected = await _connect_azure_monitor_cli(args)
    elif args.auth == "interactive":
        connected = await _connect_azure_monitor_interactive(args)
    else:
        connected = await _connect_azure_monitor(args)
    if not connected:
        sys.exit(1)

    if args.tool == "alerts":
        await _run_alerts(args.time_range)
    elif args.tool == "none":
        print("\n  Done. Use 'make azure-alerts' to check alerts.")

    print()


if __name__ == "__main__":
    asyncio.run(main())
