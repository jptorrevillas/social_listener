"""Persisted quota accounting.

The ledger lives in the database specifically so a restart cannot lose the
day's spend and then overrun the budget, so these tests care about durability
and day boundaries.
"""

import datetime as dt

import pytest
from sqlalchemy import select

from social_listener import ledger
from social_listener.models import QuotaSpend
from social_listener.quota import DAILY_UNITS, QuotaExhausted

MORNING = dt.datetime(2026, 9, 17, 17, 0, tzinfo=dt.timezone.utc)  # 10:00 Pacific


def test_a_fresh_day_has_spent_nothing(session):
    assert ledger.status(session, MORNING).spent == 0


def test_charging_accumulates(session):
    ledger.charge(session, "commentThreads.list", now=MORNING)
    ledger.charge(session, "commentThreads.list", now=MORNING)
    assert ledger.status(session, MORNING).spent == 2


def test_a_search_costs_a_hundred(session):
    ledger.charge(session, "search.list", now=MORNING)
    assert ledger.status(session, MORNING).spent == 100


def test_spend_is_tracked_per_method(session):
    ledger.charge(session, "search.list", now=MORNING)
    ledger.charge(session, "commentThreads.list", calls=5, now=MORNING)
    by_method = ledger.status(session, MORNING).by_method
    assert by_method == {"search.list": 100, "commentThreads.list": 5}


def test_multiple_calls_charge_multiple_times(session):
    ledger.charge(session, "search.list", calls=3, now=MORNING)
    assert ledger.status(session, MORNING).spent == 300


def test_spend_is_scoped_to_the_quota_day(session):
    ledger.charge(session, "search.list", now=MORNING)
    tomorrow = MORNING + dt.timedelta(days=1)
    assert ledger.status(session, tomorrow).spent == 0


def test_the_ledger_survives_a_new_session_object(session):
    # Stand-in for a process restart: the rows, not memory, are the record.
    ledger.charge(session, "search.list", now=MORNING)
    session.expire_all()
    assert ledger.status(session, MORNING).spent == 100


def test_reserve_allows_an_affordable_call(session):
    ledger.reserve(session, "search.list", now=MORNING)


def test_reserve_refuses_when_the_budget_cannot_cover_it(session):
    ledger.charge(session, "search.list", calls=100, now=MORNING)
    with pytest.raises(QuotaExhausted) as exc:
        ledger.reserve(session, "search.list", now=MORNING)
    assert exc.value.method == "search.list"


def test_the_search_boundary_is_exact(session):
    # 99 searches leaves exactly 100 units, which still buys one more search.
    ledger.charge(session, "search.list", calls=99, now=MORNING)
    assert ledger.status(session, MORNING).remaining == 100
    ledger.reserve(session, "search.list", now=MORNING)


def test_a_spent_budget_still_allows_a_one_unit_call(session):
    # One unit past the search boundary: harvesting continues, searching stops.
    ledger.charge(session, "search.list", calls=99, now=MORNING)
    ledger.charge(session, "commentThreads.list", now=MORNING)
    assert ledger.status(session, MORNING).remaining == 99
    ledger.reserve(session, "commentThreads.list", now=MORNING)
    with pytest.raises(QuotaExhausted):
        ledger.reserve(session, "search.list", now=MORNING)


def test_one_row_per_method_per_day(session):
    for _ in range(4):
        ledger.charge(session, "videos.list", now=MORNING)
    rows = list(session.scalars(select(QuotaSpend)))
    assert len(rows) == 1
    assert rows[0].calls == 4
