"""The operator's local calendar.

Containers run UTC (logs, DB, cron), which is right for machine time. But a
"day" an operator reads — the heatmap's daily bucket, a per-day rollup — is a
CALENDAR day in THEIR timezone, and computing it from UTC rolls the day over at
01:00/02:00 local and splits a night across two days. One source for the local
zone so every such boundary agrees.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Croatia. Overridable so a future non-CET deployment doesn't need a code change.
LOCAL_TZ = ZoneInfo(os.environ.get("BABA_TZ", "Europe/Zagreb"))


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    return local_now().date()
