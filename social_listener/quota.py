"""The daily quota ledger.

YouTube does not rate-limit requests the way Reddit does; it charges them. Every
project gets 10,000 units per day and each method deducts a fixed cost, so the
scarce resource is a budget rather than a request rate. That changes the shape
of the limiter completely:

  * Reddit  -> a token bucket. "How fast may I call?"
  * YouTube -> a ledger.       "How much have I already spent today?"

Two consequences the design takes seriously:

1. **The ledger is persisted.** An in-process counter forgets everything on
   restart and then cheerfully overspends, which on YouTube means every call
   fails with quotaExceeded until midnight Pacific. The spend lives in the
   database.

2. **Cost asymmetry drives the whole capture strategy.** A search costs 100
   units and a page of comments costs 1. Searching is therefore ~100x more
   expensive than harvesting, so the pipeline searches rarely to discover
   videos and then reads their comments generously. Getting this backwards
   burns the day's budget in 100 calls.

Quota resets at midnight America/Los_Angeles, not UTC and not local time.

That timezone is the one portability trap in this module. `zoneinfo` reads the
IANA database from the operating system, and Windows does not ship one -- nor do
slim Linux container images. On those the lookup raises
ZoneInfoNotFoundError, which is why `tzdata` is a hard requirement rather than
an optional extra. If it is somehow missing anyway, this module degrades to a
fixed -08:00 offset with a loud warning instead of refusing to import: a quota
boundary an hour out during daylight saving is a much smaller problem than an
application that will not start.
"""

from __future__ import annotations

import datetime as dt
import warnings
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Documented costs for the read methods this project uses. Anything absent is
# rejected rather than guessed at -- an unpriced call is how a budget silently
# disappears.
METHOD_COSTS: dict[str, int] = {
    "search.list": 100,
    "videos.list": 1,
    "channels.list": 1,
    "commentThreads.list": 1,
    "comments.list": 1,
    "playlistItems.list": 1,
}

DAILY_UNITS = 10_000
QUOTA_TIMEZONE_KEY = "America/Los_Angeles"


def _resolve_quota_timezone():
    """The reset timezone, or a fixed fallback if no tz database is available.

    Pacific time is UTC-8 in winter and UTC-7 under daylight saving, so the
    fallback puts the reset boundary up to an hour out for part of the year.
    That is a real inaccuracy and the warning says so -- but it is strictly
    better than an ImportError, and installing tzdata removes it entirely.
    """
    try:
        return ZoneInfo(QUOTA_TIMEZONE_KEY)
    except (ZoneInfoNotFoundError, KeyError):
        warnings.warn(
            f"No IANA time zone database found, so {QUOTA_TIMEZONE_KEY} could "
            "not be loaded. Falling back to a fixed UTC-08:00 offset, which "
            "puts the quota reset boundary up to an hour out during daylight "
            "saving. Install the 'tzdata' package to fix this: it ships in "
            "requirements.txt and is required on Windows and on slim container "
            "images, neither of which carries a system tz database.",
            RuntimeWarning,
            stacklevel=2,
        )
        return dt.timezone(dt.timedelta(hours=-8), "PST-fallback")


QUOTA_TIMEZONE = _resolve_quota_timezone()


def timezone_label() -> str:
    """How to describe the reset timezone in output, fallback included."""
    return getattr(QUOTA_TIMEZONE, "key", None) or str(QUOTA_TIMEZONE)


class QuotaExhausted(RuntimeError):
    """Raised instead of making a call that would exceed the day's budget."""

    def __init__(self, method: str, cost: int, remaining: int) -> None:
        super().__init__(
            f"{method} costs {cost} units but only {remaining} remain today; "
            f"quota resets at midnight {timezone_label()}"
        )
        self.method = method
        self.cost = cost
        self.remaining = remaining


class UnpricedMethod(KeyError):
    """Raised for a method with no entry in METHOD_COSTS."""


def quota_day(now: Optional[dt.datetime] = None) -> dt.date:
    """The quota day a moment falls in, in YouTube's reset timezone."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(QUOTA_TIMEZONE).date()


def seconds_until_reset(now: Optional[dt.datetime] = None) -> int:
    """Seconds until the budget refills."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    local = moment.astimezone(QUOTA_TIMEZONE)
    tomorrow = (local + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((tomorrow - local).total_seconds())


def cost_of(method: str) -> int:
    try:
        return METHOD_COSTS[method]
    except KeyError as exc:
        raise UnpricedMethod(
            f"{method} has no documented cost; add it to METHOD_COSTS rather "
            f"than letting it spend the budget unmeasured"
        ) from exc


@dataclass
class QuotaStatus:
    day: dt.date
    limit: int
    spent: int
    by_method: dict[str, int]

    @property
    def remaining(self) -> int:
        return max(self.limit - self.spent, 0)

    @property
    def fraction_used(self) -> float:
        return min(self.spent / self.limit, 1.0) if self.limit else 1.0

    def can_afford(self, method: str) -> bool:
        return cost_of(method) <= self.remaining

    def searches_remaining(self) -> int:
        """The number people actually care about."""
        return self.remaining // METHOD_COSTS["search.list"]
