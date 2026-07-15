"""
Webhook delivery tool for Sherlock.

Posts the most recent structured investigation report to the team's
Slack or Teams channel via an operator-configured incoming webhook.
The webhook URL comes ONLY from environment variables
(``SHERLOCK_SLACK_WEBHOOK_URL`` / ``SHERLOCK_TEAMS_WEBHOOK_URL``) — the
tool takes no URL argument, so report content cannot be redirected to
an arbitrary endpoint from the conversation.
"""

import json
import logging
from typing import Literal

from sherlock.core.webhook import WEBHOOK_ENV_VARS, send_report
from sherlock.tools.intelligence_tools import get_structured_report_tool

logger = logging.getLogger("sherlock.tools.webhook")


async def post_report_to_webhook(
    target: Literal["slack", "teams"],
    service_name: str = "",
) -> str:
    """Post the latest investigation report to the team's Slack/Teams channel.

    Formats the most recent structured report (optionally for a specific
    service) as a Slack Block Kit message or Teams MessageCard and POSTs
    it to the incoming webhook configured by the operator. Run an
    investigation first.

    Args:
        target: Which chat platform to deliver to ("slack" or "teams").
        service_name: Optional service whose report to send. Uses the
            most recent investigation when empty.

    Returns:
        JSON string with the delivery outcome.
    """
    try:
        raw = await get_structured_report_tool(service_name=service_name, format="full")
        parsed = json.loads(raw)
        if parsed.get("status") != "OK" or not parsed.get("report"):
            return json.dumps(
                {
                    "error": parsed.get("message", "no structured report available"),
                    "tool": "post_report_to_webhook",
                    "hint": "Run an investigation first, then post its report.",
                    "data_available": False,
                }
            )

        report = parsed["report"]
        outcome = await send_report(report, target)

        response = {
            "delivered": outcome["delivered"],
            "target": target,
            "service_name": report.get("service_name", ""),
            "severity": report.get("severity", "UNKNOWN"),
            "status_code": outcome["status_code"],
        }
        if not outcome["delivered"]:
            response["error"] = outcome["error"]
            response["hint"] = (
                f"Set {WEBHOOK_ENV_VARS[target]} to the team's incoming webhook "
                "URL (https) in the server environment, then retry."
            )
        return json.dumps(response)

    except Exception as exc:
        return json.dumps(
            {
                "error": str(exc),
                "tool": "post_report_to_webhook",
                "hint": "Run an investigation first; ensure the webhook env var is set.",
                "data_available": False,
            }
        )
