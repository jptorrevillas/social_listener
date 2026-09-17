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


# -- timezone portability --------------------------------------------------
#
# The reported failure: zoneinfo reads the IANA database from the OS, Windows
# ships none, and the module raised ZoneInfoNotFoundError at import time. The
# fix is to declare tzdata as a dependency AND to degrade rather than die.


def test_tzdata_is_declared_as_a_dependency():
    """The real fix. The fallback below is only a safety net."""
    import pathlib

    requirements = (
        pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"
    ).read_text()
    assert "tzdata" in requirements, (
        "zoneinfo needs an IANA database, which Windows and slim container "
        "images do not provide"
    )


def test_the_quota_timezone_resolves_here():
    from social_listener.quota import QUOTA_TIMEZONE_KEY, timezone_label

    assert timezone_label() in (QUOTA_TIMEZONE_KEY, "PST-fallback")


class _BlockTzdata:
    """Meta-path hook that makes the tzdata package unimportable.

    Clearing TZPATH alone is not enough once tzdata is installed: zoneinfo
    falls back to the package, which is exactly the fix working. To reproduce a
    Windows machine without tzdata, both routes have to be closed.
    """

    PREFIX = "tzdata"

    def _blocked(self, fullname):
        return fullname == self.PREFIX or fullname.startswith(self.PREFIX + ".")

    def find_spec(self, fullname, path=None, target=None):
        if self._blocked(fullname):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

    # Python 3.9 still consults the legacy hook on some paths.
    def find_module(self, fullname, path=None):
        if self._blocked(fullname):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


def test_a_missing_tz_database_falls_back_instead_of_raising():
    """Reproduces the reported Windows failure, then proves we survive it."""
    import importlib
    import sys
    import warnings
    import zoneinfo

    original_path = zoneinfo.TZPATH
    blocker = _BlockTzdata()
    stashed = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "tzdata" or name.startswith("tzdata.")
    }

    try:
        for name in stashed:
            del sys.modules[name]
        sys.meta_path.insert(0, blocker)
        zoneinfo.reset_tzpath([])
        zoneinfo.ZoneInfo.clear_cache()  # instances are memoised

        try:
            zoneinfo.ZoneInfo("America/Los_Angeles")
            raise AssertionError(
                "could not simulate a missing tz database; this test would "
                "otherwise pass without exercising the fallback at all"
            )
        except zoneinfo.ZoneInfoNotFoundError:
            pass

        import social_listener.quota as quota_module

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            reloaded = importlib.reload(quota_module)

        assert reloaded.timezone_label() == "PST-fallback"
        assert any("tzdata" in str(w.message) for w in caught), (
            "the fallback must say how to fix it, not degrade silently"
        )
        assert 0 < reloaded.seconds_until_reset() <= 86_400
    finally:
        if blocker in sys.meta_path:
            sys.meta_path.remove(blocker)
        sys.modules.update(stashed)
        zoneinfo.reset_tzpath(list(original_path))
        zoneinfo.ZoneInfo.clear_cache()
        import social_listener.quota as quota_module

        importlib.reload(quota_module)
