"""Database-backed quota accounting.

Separated from quota.py so the cost table and reset arithmetic stay pure and
testable, while this module owns the persistence.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import QuotaSpend
from .quota import DAILY_UNITS, QuotaExhausted, QuotaStatus, cost_of, quota_day


def status(session: Session, now: Optional[dt.datetime] = None) -> QuotaStatus:
    day = quota_day(now)
    rows = list(session.scalars(select(QuotaSpend).where(QuotaSpend.quota_date == day)))
    return QuotaStatus(
        day=day,
        limit=DAILY_UNITS,
        spent=sum(row.units for row in rows),
        by_method={row.method: row.units for row in rows},
    )


def charge(
    session: Session,
    method: str,
    calls: int = 1,
    now: Optional[dt.datetime] = None,
) -> QuotaStatus:
    """Record spend for a call already made.

    Charging after the fact is deliberate: YouTube bills the request whether or
    not the response was useful, so a call that returned an error still cost
    units and must still be recorded.
    """
    unit_cost = cost_of(method) * calls
    day = quota_day(now)
    row = session.scalar(
        select(QuotaSpend).where(
            QuotaSpend.quota_date == day, QuotaSpend.method == method
        )
    )
    if row is None:
        row = QuotaSpend(quota_date=day, method=method, calls=0, units=0)
        session.add(row)
        session.flush()
    row.calls += calls
    row.units += unit_cost
    session.flush()
    return status(session, now)


def reserve(session: Session, method: str, now: Optional[dt.datetime] = None) -> None:
    """Refuse a call the budget cannot cover, before it is made."""
    current = status(session, now)
    if not current.can_afford(method):
        raise QuotaExhausted(method, cost_of(method), current.remaining)
