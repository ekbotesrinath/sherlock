"""
Proactive health digest for Sherlock.

Sweeps every known service of a profile (APM + OTel, discovered by
learn_account) through the golden-signals check and produces one
worst-first digest — the "morning coffee" view of the whole account
BEFORE anyone opens an incident. Optionally posts the digest to the
team's Slack/Teams webhook.

Usage:
    python -m sherlock.scripts.digest                    # all profiles
    python -m sherlock.scripts.digest --profile PROD     # one profile
    python -m sherlock.scripts.digest --window 60 --concurrency 5
    python -m sherlock.scripts.digest --post slack       # also post digest
    python -m sherlock.scripts.digest --json             # machine-readable
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sherlock.core.context import AccountContext
from sherlock.core.credentials import CredentialManager
from sherlock.core.webhook import (
    build_slack_payload,
    build_teams_payload,
    get_webhook_url,
    post_webhook,
)
from sherlock.tools.golden_signals import get_service_golden_signals
from sherlock.tools.intelligence_tools import connect_account

DEFAULT_WINDOW_MINUTES = 60
DEFAULT_CONCURRENCY = 5
DEFAULT_MAX_SERVICES = 50

# Worst first.
STATUS_RANK = {"CRITICAL": 0, "WARNING": 1, "NO_DATA": 2, "HEALTHY": 3}
STATUS_EMOJI = {"CRITICAL": "🔴", "WARNING": "🟡", "NO_DATA": "⚪", "HEALTHY": "🟢"}


def classify_result(service_name: str, raw_json: str) -> dict:
    """Reduce one golden-signals response to a digest row.

    Args:
        service_name: The service that was checked.
        raw_json: JSON string returned by get_service_golden_signals.

    Returns:
        {service, status, signals, baseline_verdicts, error_rate_pct,
        p95, rpm} — status is NO_DATA when the check errored or the
        service produced no traffic data.
    """
    row: dict = {
        "service": service_name,
        "status": "NO_DATA",
        "signals": [],
        "baseline_verdicts": [],
        "error_rate_pct": None,
        "p95": None,
        "rpm": None,
    }
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        row["signals"] = ["unparseable golden-signals response"]
        return row

    if parsed.get("error"):
        row["signals"] = [f"check failed: {parsed['error']}"]
        return row

    row["status"] = parsed.get("overall_status", "NO_DATA")
    row["signals"] = parsed.get("health_signals", [])
    row["error_rate_pct"] = (parsed.get("errors") or {}).get("error_rate_pct")
    row["p95"] = (parsed.get("latency") or {}).get("p95")
    row["rpm"] = (parsed.get("throughput") or {}).get("rpm")

    baseline = parsed.get("baseline") or {}
    if baseline.get("available"):
        row["baseline_verdicts"] = [v for v in baseline.get("verdicts", []) if v and "✅" not in v]
    return row


async def sweep_services(
    services: list[str],
    window_minutes: int,
    concurrency: int = DEFAULT_CONCURRENCY,
    checker: Callable[..., Awaitable[str]] = get_service_golden_signals,
) -> list[dict]:
    """Run the golden-signals check for every service, bounded-concurrent.

    Args:
        services: Service names to sweep.
        window_minutes: Golden-signals window per service.
        concurrency: Max simultaneous checks (keeps NerdGraph happy).
        checker: Injectable check coroutine (tests swap this out).

    Returns:
        One classify_result() row per service, in input order.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _check(name: str) -> dict:
        async with semaphore:
            try:
                raw = await checker(service_name=name, since_minutes=window_minutes)
            except Exception as exc:
                raw = json.dumps({"error": str(exc)})
            return classify_result(name, raw)

    return list(await asyncio.gather(*(_check(name) for name in services)))


def build_digest(
    profile: str,
    account_id: str,
    window_minutes: int,
    rows: list[dict],
) -> dict:
    """Assemble the digest bundle from per-service rows (worst first)."""
    rows = sorted(rows, key=lambda r: (STATUS_RANK.get(r["status"], 2), r["service"]))
    totals = {status: 0 for status in STATUS_RANK}
    for row in rows:
        totals[row.get("status", "NO_DATA")] = totals.get(row.get("status", "NO_DATA"), 0) + 1

    if totals["CRITICAL"]:
        overall = "CRITICAL"
    elif totals["WARNING"]:
        overall = "WARNING"
    elif totals["HEALTHY"]:
        overall = "HEALTHY"
    else:
        overall = "NO_DATA"

    return {
        "profile": profile,
        "account_id": account_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_minutes": window_minutes,
        "overall_status": overall,
        "totals": totals,
        "services": rows,
    }


def format_digest_text(digest: dict) -> str:
    """Human-readable digest for stdout."""
    totals = digest["totals"]
    lines = [
        f"Sherlock health digest — profile {digest['profile']} "
        f"(account {digest['account_id']}, last {digest['window_minutes']}m)",
        f"Overall: {STATUS_EMOJI.get(digest['overall_status'], '⚪')} "
        f"{digest['overall_status']}  |  "
        f"🔴 {totals['CRITICAL']}  🟡 {totals['WARNING']}  "
        f"🟢 {totals['HEALTHY']}  ⚪ {totals['NO_DATA']}",
        "",
    ]
    for row in digest["services"]:
        if row["status"] == "HEALTHY":
            continue
        emoji = STATUS_EMOJI.get(row["status"], "⚪")
        lines.append(f"{emoji} {row['service']} — {row['status']}")
        for signal in row["signals"]:
            lines.append(f"    {signal}")
        for verdict in row["baseline_verdicts"]:
            lines.append(f"    {verdict}")
    unhealthy = [r for r in digest["services"] if r["status"] != "HEALTHY"]
    if not unhealthy:
        lines.append("All services healthy. ☕")
    return "\n".join(lines)


def digest_to_webhook_payload(digest: dict, target: str) -> dict:
    """Format the digest as a Slack/Teams payload (unhealthy services only)."""
    totals = digest["totals"]
    title = (
        f"Sherlock digest: {digest['profile']} — "
        f"{totals['CRITICAL']} critical, {totals['WARNING']} warning"
    )
    fields: list[tuple[str, str]] = []
    for row in digest["services"]:
        if row["status"] == "HEALTHY":
            continue
        detail = "; ".join(row["signals"] + row["baseline_verdicts"]) or row["status"]
        fields.append((f"{STATUS_EMOJI.get(row['status'], '⚪')} {row['service']}", detail))
    body = (
        f"{totals['HEALTHY']} healthy, {totals['NO_DATA']} no-data of "
        f"{len(digest['services'])} services — window {digest['window_minutes']}m."
    )
    builder = build_slack_payload if target == "slack" else build_teams_payload
    return builder(title, digest["overall_status"], fields, body)


async def _digest_profile(
    name: str, manager: CredentialManager, args: argparse.Namespace
) -> dict | None:
    """Connect one profile and produce its digest (None on connect failure)."""
    try:
        creds = manager.load_profile(name)
    except Exception as exc:
        print(f"[SKIP] Cannot load profile '{name}': {exc}", file=sys.stderr)
        return None

    result = json.loads(
        await connect_account(
            account_id=creds.account_id,
            api_key=creds.api_key,
            region=creds.region,
        )
    )
    if result.get("error"):
        print(f"[FAIL] Connect '{name}' failed: {result['error']}", file=sys.stderr)
        return None

    ctx = AccountContext()
    _, intelligence = ctx.get_active()
    services = list(intelligence.apm.service_names)
    for otel_name in getattr(intelligence.otel, "service_names", []):
        if otel_name not in services:
            services.append(otel_name)
    if not services:
        print(f"[SKIP] Profile '{name}' has no known services.", file=sys.stderr)
        return None
    if len(services) > args.max_services:
        print(
            f"[NOTE] {len(services)} services — sweeping first {args.max_services} "
            "(raise --max-services to widen).",
            file=sys.stderr,
        )
        services = services[: args.max_services]

    rows = await sweep_services(services, args.window, args.concurrency)
    return build_digest(name, str(creds.account_id), args.window, rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sherlock — proactive health digest.")
    parser.add_argument("--profile", "-p", help="Profile to sweep (omit for all profiles)")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_MINUTES, help="Window in minutes (default 60)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Max simultaneous service checks (default 5)",
    )
    parser.add_argument(
        "--max-services",
        type=int,
        default=DEFAULT_MAX_SERVICES,
        help="Cap on services per profile (default 50)",
    )
    parser.add_argument(
        "--post",
        choices=["slack", "teams"],
        help="Also post the digest to the configured webhook",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    manager = CredentialManager()
    profiles = [p["name"] for p in manager.list_profiles()]
    if not profiles:
        print("No profiles found. Create one with sherlock-validate first.", file=sys.stderr)
        sys.exit(1)

    if args.profile:
        if args.profile not in profiles:
            print(f"Profile '{args.profile}' not found. Available: {', '.join(profiles)}")
            sys.exit(1)
        targets = [args.profile]
    else:
        targets = profiles

    digests: list[dict] = []
    for name in targets:
        digest = await _digest_profile(name, manager, args)
        if digest:
            digests.append(digest)

    if not digests:
        print("No digests produced.", file=sys.stderr)
        sys.exit(1)

    for digest in digests:
        if args.json:
            print(json.dumps(digest, indent=2))
        else:
            print(format_digest_text(digest))
            print()

    exit_code = 0
    if args.post:
        url = get_webhook_url(args.post)
        if not url:
            print(
                f"[FAIL] --post {args.post}: webhook env var not set (see core/webhook.py).",
                file=sys.stderr,
            )
            exit_code = 1
        else:
            for digest in digests:
                outcome = await post_webhook(url, digest_to_webhook_payload(digest, args.post))
                if outcome["delivered"]:
                    print(f"[OK] Posted digest '{digest['profile']}' to {args.post}.")
                else:
                    print(
                        f"[FAIL] Posting '{digest['profile']}': {outcome['error']}",
                        file=sys.stderr,
                    )
                    exit_code = 1

    # Non-zero when anything is on fire, so cron/CI can alert on it.
    if any(d["overall_status"] == "CRITICAL" for d in digests):
        exit_code = max(exit_code, 2)
    sys.exit(exit_code)


def main_sync() -> None:
    """Synchronous console-script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
