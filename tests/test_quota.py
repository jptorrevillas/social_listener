"""The quota arithmetic. Pure functions, no database."""

import datetime as dt

import pytest

from social_listener.quota import (
    DAILY_UNITS,
    METHOD_COSTS,
    QuotaStatus,
    UnpricedMethod,
    cost_of,
    quota_day,
    seconds_until_reset,
)


def status(spent, by_method=None):
    return QuotaStatus(
        day=dt.date(2026, 9, 17),
        limit=DAILY_UNITS,
        spent=spent,
        by_method=by_method or {},
    )


def test_search_is_a_hundred_times_a_comment_page():
    # The whole capture strategy rests on this ratio.
    assert METHOD_COSTS["search.list"] == 100 * METHOD_COSTS["commentThreads.list"]


def test_cheap_read_methods_cost_one():
    for method in ("videos.list", "channels.list", "commentThreads.list", "playlistItems.list"):
        assert cost_of(method) == 1


def test_an_unpriced_method_is_refused_not_guessed():
    with pytest.raises(UnpricedMethod):
        cost_of("videos.insert")


def test_remaining_never_goes_negative():
    assert status(99_999).remaining == 0


def test_a_hundred_searches_exhaust_the_day():
    assert status(0).searches_remaining() == 100
    assert status(DAILY_UNITS).searches_remaining() == 0


def test_a_nearly_spent_budget_still_affords_comments_but_not_search():
    # The graceful-degradation property: harvesting survives a spent budget.
    nearly = status(DAILY_UNITS - 50)
    assert nearly.can_afford("commentThreads.list") is True
    assert nearly.can_afford("search.list") is False


def test_quota_day_uses_pacific_not_utc():
    # 06:00 UTC is still the previous day in Los Angeles.
    moment = dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.timezone.utc)
    assert quota_day(moment) == dt.date(2026, 9, 16)


def test_quota_day_rolls_after_pacific_midnight():
    moment = dt.datetime(2026, 9, 17, 8, 0, tzinfo=dt.timezone.utc)
    assert quota_day(moment) == dt.date(2026, 9, 17)


def test_naive_datetimes_are_treated_as_utc():
    assert quota_day(dt.datetime(2026, 9, 17, 6, 0)) == dt.date(2026, 9, 16)


def test_seconds_until_reset_is_within_a_day():
    assert 0 < seconds_until_reset() <= 86_400


def test_fraction_used_is_capped():
    assert status(DAILY_UNITS * 3).fraction_used == 1.0
