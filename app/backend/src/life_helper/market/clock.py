"""The calendar date the market runs on.

The container has no timezone set, so ``date.today()`` would be the UTC date: between 00:00 and 09:00 JST it
is still yesterday in UTC, which would reject a NAV entered today and misjudge which prices are stale.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def market_today() -> date:
    """Today in Japan, the calendar every price and NAV in this app is dated by."""
    return datetime.now(JST).date()
