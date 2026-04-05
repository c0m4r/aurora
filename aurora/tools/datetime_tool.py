"""DateTime tool — lets the model query the current date, time, and timezone."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from .base import BaseTool, ToolDefinition


class DateTimeTool(BaseTool):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_datetime",
            description=(
                "Get the current date, time, and timezone. "
                "Use this when you need to know the exact current time, "
                "build time-range queries (e.g. 'last hour', 'since midnight'), "
                "or convert between timezones."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone name to also return the time in, "
                            "e.g. 'Europe/Warsaw', 'US/Eastern'. "
                            "Optional — UTC and server local time are always returned."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, timezone: str | None = None, **_) -> str:  # noqa: A002
        now_utc = datetime.now(tz=__import__("datetime").timezone.utc)

        result: dict = {
            "utc": now_utc.isoformat(timespec="seconds"),
            "utc_human": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "unix_timestamp": int(now_utc.timestamp()),
        }

        # Server local time
        try:
            local_now = datetime.now().astimezone()
            tz_name = local_now.tzname() or "local"
            result["local"] = local_now.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
            result["local_timezone"] = tz_name
        except Exception:
            pass

        # Requested timezone
        if timezone:
            try:
                from zoneinfo import ZoneInfo
                tz_obj = ZoneInfo(timezone)
                tz_now = now_utc.astimezone(tz_obj)
                result["requested_timezone"] = timezone
                result["requested_time"] = tz_now.strftime("%Y-%m-%d %H:%M:%S %Z")
                result["utc_offset"] = tz_now.strftime("%z")
            except Exception as exc:
                result["timezone_error"] = str(exc)

        # Handy relative anchors for building queries
        result["anchors"] = {
            "1h_ago":         (now_utc - timedelta(hours=1)).isoformat(timespec="seconds"),
            "6h_ago":         (now_utc - timedelta(hours=6)).isoformat(timespec="seconds"),
            "24h_ago":        (now_utc - timedelta(hours=24)).isoformat(timespec="seconds"),
            "midnight_utc":   now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds"),
            "week_start_utc": (now_utc - timedelta(days=now_utc.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(timespec="seconds"),
        }

        return json.dumps(result, indent=2)
