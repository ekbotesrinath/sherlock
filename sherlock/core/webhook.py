"""
Slack / Teams webhook delivery for Sherlock reports.

Formats structured investigation reports (and health digests) as Slack
Block Kit or Teams MessageCard payloads and POSTs them to an
operator-configured incoming webhook.

Security posture: webhook URLs come ONLY from environment variables set
by the operator (``SHERLOCK_SLACK_WEBHOOK_URL`` /
``SHERLOCK_TEAMS_WEBHOOK_URL``). Tools never accept a URL argument, so a
prompt-injected model cannot redirect report content to an attacker
endpoint. Delivery is best-effort and never raises.
"""

import json
import logging
import os
from typing import Literal

import httpx

logger = logging.getLogger("sherlock.webhook")

WebhookTarget = Literal["slack", "teams"]

WEBHOOK_ENV_VARS: dict[str, str] = {
    "slack": "SHERLOCK_SLACK_WEBHOOK_URL",
    "teams": "SHERLOCK_TEAMS_WEBHOOK_URL",
}

SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "WARNING": "🟡",
    "HEALTHY": "🟢",
    "NO_DATA": "⚪",
    "UNKNOWN": "⚪",
}

# Teams MessageCard themeColor (hex, no #).
SEVERITY_COLOR = {
    "CRITICAL": "D93F3C",
    "WARNING": "E8A317",
    "HEALTHY": "2EB67D",
    "NO_DATA": "808080",
    "UNKNOWN": "808080",
}

POST_TIMEOUT_SECONDS = 10.0


def get_webhook_url(target: str) -> str | None:
    """The operator-configured webhook URL for *target*, or None.

    Only https URLs are accepted — anything else is treated as unset.
    """
    env_var = WEBHOOK_ENV_VARS.get(target)
    if not env_var:
        return None
    url = (os.environ.get(env_var) or "").strip()
    if not url.lower().startswith("https://"):
        if url:
            logger.warning("%s is set but not an https:// URL — ignoring.", env_var)
        return None
    return url


def _truncate(text: str, limit: int = 500) -> str:
    """Cap a field value so one giant root-cause string can't break a card."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_slack_payload(
    title: str,
    severity: str,
    fields: list[tuple[str, str]],
    body_text: str = "",
) -> dict:
    """Build a Slack incoming-webhook payload (Block Kit).

    Args:
        title: Headline without emoji (severity emoji is prepended).
        severity: CRITICAL | WARNING | HEALTHY | NO_DATA | UNKNOWN.
        fields: (label, value) pairs rendered as a two-column field grid.
        body_text: Optional mrkdwn paragraph under the fields.

    Returns:
        Payload dict ready to POST as JSON.
    """
    emoji = SEVERITY_EMOJI.get(severity, "⚪")
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _truncate(f"{emoji} {title}", 150)},
        }
    ]
    field_blocks = [
        {"type": "mrkdwn", "text": f"*{label}:*\n{_truncate(value)}"}
        for label, value in fields
        if value
    ]
    # Slack allows at most 10 fields per section block.
    for i in range(0, len(field_blocks), 10):
        blocks.append({"type": "section", "fields": field_blocks[i : i + 10]})
    if body_text:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(body_text, 3000)}}
        )
    return {"text": f"{emoji} {title}", "blocks": blocks}


def build_teams_payload(
    title: str,
    severity: str,
    fields: list[tuple[str, str]],
    body_text: str = "",
) -> dict:
    """Build a Teams incoming-webhook payload (MessageCard).

    Args:
        title: Headline without emoji (severity emoji is prepended).
        severity: CRITICAL | WARNING | HEALTHY | NO_DATA | UNKNOWN.
        fields: (label, value) pairs rendered as card facts.
        body_text: Optional markdown paragraph under the facts.

    Returns:
        Payload dict ready to POST as JSON.
    """
    emoji = SEVERITY_EMOJI.get(severity, "⚪")
    section: dict = {
        "facts": [{"name": label, "value": _truncate(value)} for label, value in fields if value],
        "markdown": True,
    }
    if body_text:
        section["text"] = _truncate(body_text, 3000)
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": SEVERITY_COLOR.get(severity, "808080"),
        "summary": f"{emoji} {title}",
        "title": f"{emoji} {title}",
        "sections": [section],
    }


def report_to_fields(report: dict) -> list[tuple[str, str]]:
    """Flatten an InvestigationReport dict into webhook card fields."""
    fields: list[tuple[str, str]] = [
        ("Severity", str(report.get("severity", "UNKNOWN"))),
        ("Confidence", str(report.get("confidence", ""))),
        ("Account", str(report.get("account_name") or report.get("account_id") or "")),
        ("Window", f"{report.get('window_minutes', '?')} min"),
        ("Root cause", str(report.get("root_cause") or "not identified")),
        ("Causal chain", str(report.get("causal_chain") or "")),
    ]
    error_rate = report.get("error_rate")
    if error_rate is not None:
        fields.append(("Error rate", f"{error_rate}%"))
    incidents = report.get("open_incident_ids") or []
    if incidents:
        fields.append(("Open incidents", str(len(incidents))))
    return fields


def build_report_payload(report: dict, target: WebhookTarget) -> dict:
    """Format an InvestigationReport dict for *target*.

    Args:
        report: dataclasses.asdict() of an InvestigationReport.
        target: "slack" or "teams".

    Returns:
        The webhook payload dict.
    """
    service = report.get("service_name", "unknown service")
    severity = str(report.get("severity", "UNKNOWN"))
    title = f"Sherlock: {service} — {severity}"
    fields = report_to_fields(report)

    recommendations = report.get("recommendations") or []
    body_lines = [
        f"• *{rec.get('priority', '?')}*: {rec.get('action', '')}"
        for rec in recommendations[:5]
        if isinstance(rec, dict)
    ]
    body_text = "\n".join(body_lines)

    builder = build_slack_payload if target == "slack" else build_teams_payload
    return builder(title, severity, fields, body_text)


async def post_webhook(url: str, payload: dict) -> dict:
    """POST a payload to a webhook URL. Best-effort — never raises.

    Args:
        url: The https webhook URL.
        payload: JSON-serializable payload dict.

    Returns:
        {"delivered": bool, "status_code": int | None, "error": str | None}
    """
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
        delivered = 200 <= response.status_code < 300
        if not delivered:
            logger.warning(
                "Webhook POST failed: HTTP %s %s",
                response.status_code,
                response.text[:200],
            )
        return {
            "delivered": delivered,
            "status_code": response.status_code,
            "error": None if delivered else f"HTTP {response.status_code}",
        }
    except Exception as exc:
        logger.warning("Webhook POST raised: %s", exc)
        return {"delivered": False, "status_code": None, "error": str(exc)}


async def send_report(report: dict, target: WebhookTarget) -> dict:
    """Format and deliver an investigation report to the configured webhook.

    Args:
        report: dataclasses.asdict() of an InvestigationReport.
        target: "slack" or "teams".

    Returns:
        post_webhook()'s outcome dict, or a not-configured error dict.
    """
    url = get_webhook_url(target)
    if not url:
        return {
            "delivered": False,
            "status_code": None,
            "error": (
                f"{WEBHOOK_ENV_VARS[target]} is not set (or not https). "
                "Ask the operator to configure the incoming webhook URL."
            ),
        }
    payload = build_report_payload(report, target)
    logger.info("Posting report to %s webhook (%d bytes)", target, len(json.dumps(payload)))
    return await post_webhook(url, payload)
