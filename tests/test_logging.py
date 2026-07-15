"""
Tests for the JSON log formatter (B5).

The old hand-rolled format string produced invalid JSON whenever a log
message contained quotes or newlines. python-json-logger escapes both.
"""

import json
import logging

from sherlock.main import _json_formatter


def _format(message: str, **extra) -> str:
    record = logging.LogRecord(
        name="sherlock.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return _json_formatter().format(record)


class TestJsonLogging:
    def test_plain_message_is_valid_json(self):
        parsed = json.loads(_format("server started"))
        assert parsed["message"] == "server started"
        assert parsed["level"] == "WARNING"
        assert parsed["logger"] == "sherlock.test"
        assert "time" in parsed

    def test_quotes_in_message_stay_valid_json(self):
        """B5 regression: quotes used to break the hand-rolled format."""
        msg = 'user said "ignore this" and query was SELECT "x"'
        parsed = json.loads(_format(msg))
        assert parsed["message"] == msg

    def test_newlines_in_message_stay_valid_json(self):
        """B5 regression: newlines used to break the hand-rolled format."""
        msg = "line one\nline two\r\nline three"
        parsed = json.loads(_format(msg))
        assert parsed["message"] == msg

    def test_backslashes_and_unicode(self):
        msg = "path C:\\Users\\x — ünïcode 💥"
        parsed = json.loads(_format(msg))
        assert parsed["message"] == msg

    def test_extra_fields_are_serialized(self):
        """extra= dict fields (e.g. injection warnings) land in the JSON."""
        parsed = json.loads(_format("Prompt injection attempt detected", tool="search_logs"))
        assert parsed["tool"] == "search_logs"

    def test_audit_payload_roundtrips(self):
        """Audit events (json.dumps'd payloads) stay parseable end to end."""
        payload = json.dumps({"event": "tool_call", "tool": "get_alerts", "success": True})
        parsed = json.loads(_format(payload))
        assert json.loads(parsed["message"])["event"] == "tool_call"
