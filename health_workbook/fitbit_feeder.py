"""Read the loopback Fitbit MCP and write today and yesterday through the workbook service."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from health_workbook.feeders import FITBIT_TOOLS, WorkbookClient, sync_dates, sync_fitbit


def exit_code(result: dict) -> int:
    """Fail when a workbook write failed or every Fitbit tool failed.

    One tool can fail while the other tools still update the workbook. That run
    stays in the JSON log and does not fail the oneshot.
    """
    failed = result.get("failed_tools") or []
    write_errors = int(result.get("errors") or 0) - len(failed)
    if write_errors > 0:
        return 1
    if failed and len(failed) >= len(FITBIT_TOOLS):
        return 1
    return 0
from workouts.fitbit import (
    DEFAULT_URL,
    FitbitUnavailable,
    _decode,
    _loopback_mcp_url,
    unwrap_tool_result,
    urllib_transport,
)


class FitbitClient:
    def __init__(self, url: str = DEFAULT_URL, timeout_s: float = 60.0):
        self.url = _loopback_mcp_url(url)
        self.timeout_s = timeout_s

    def call_tool(self, name: str, arguments: dict):
        if name not in FITBIT_TOOLS or not isinstance(arguments, dict):
            raise FitbitUnavailable()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        status, response_headers, body = urllib_transport(self.url, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "syzygy-fitbit-sync", "version": "1"},
            },
        }, headers, self.timeout_s)
        message = _decode(body)
        if status >= 300 or not isinstance(message, dict):
            raise FitbitUnavailable()
        result = message.get("result")
        if not isinstance(result, dict) or result.get("protocolVersion") != "2024-11-05":
            raise FitbitUnavailable()
        for key, value in response_headers.items():
            if key.lower() == "mcp-session-id" and value:
                headers["Mcp-Session-Id"] = value
        headers["MCP-Protocol-Version"] = "2024-11-05"
        urllib_transport(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers, self.timeout_s)
        status, _, body = urllib_transport(self.url, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, headers, self.timeout_s)
        if status >= 300:
            raise FitbitUnavailable()
        return unwrap_tool_result(_decode(body))


def main() -> int:
    token = os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", "")
    if not token:
        print(json.dumps({"feeder": "fitbit", "errors": 1, "failed": "missing_token"}))
        return 1
    dates = sync_dates()
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = FitbitClient(os.environ.get("FITBIT_MCP_URL", DEFAULT_URL))
    workbook = WorkbookClient(os.environ.get("HEALTH_WORKBOOK_URL", "http://127.0.0.1:5052"), token)

    def fetch(name, _dates):
        return client.call_tool(name, {"start_date": dates[0], "end_date": dates[-1]})

    try:
        result = sync_fitbit(fetch, workbook, dates, recorded_at)
    except Exception:
        print(json.dumps({"feeder": "fitbit", "errors": 1, "failed": "sync_failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
