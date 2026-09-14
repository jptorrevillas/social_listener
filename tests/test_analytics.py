from datetime import datetime, timedelta, timezone

from social_listener import analytics
from social_listener.ingest import ingest_payloads

from .test_ingest import payload


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_totals_counts_sentiment(seeded):
    ingest_payloads(
        seeded,
        [
            payload(fullname="t3_n", body="Northbridge College tuition is terrible and unacceptable", created_utc=_now()),
            payload(fullname="t3_p", body="Northbridge College tuition support was wonderful, highly recommend", created_utc=_now()),
        ],
    )
    totals = analytics.totals(seeded)
    assert totals.negative == 1
    assert totals.positive == 1


def test_net_sentiment_is_zero_when_balanced(seeded):
    ingest_payloads(
        seeded,
        [
            payload(fullname="t3_n", body="Northbridge College tuition is terrible and unacceptable", created_utc=_now()),
            payload(fullname="t3_p", body="Northbridge College tuition support was wonderful, highly recommend", created_utc=_now()),
        ],
    )
    assert analytics.totals(seeded).net_sentiment == 0.0


def test_net_sentiment_handles_an_empty_database(seeded):
    assert analytics.totals(seeded).net_sentiment == 0.0


def test_spam_is_excluded_from_sentiment_counts(seeded):
    ingest_payloads(
        seeded,
        [payload(body="Northbridge College tuition thread. I am a bot.", author="AutoModerator", created_utc=_now())],
    )
    totals = analytics.totals(seeded)
    assert totals.spam_filtered == 1
    assert totals.classified == 0


def test_share_of_voice_sums_to_one(seeded):
    ingest_payloads(seeded, [payload(created_utc=_now())])
    rows = analytics.share_of_voice(seeded)
    assert abs(sum(r["share"] for r in rows) - 1.0) < 1e-9


def test_volume_by_day_buckets_by_date(seeded):
    today = _now()
    ingest_payloads(
        seeded,
        [
            payload(fullname="t3_a", created_utc=today),
            payload(fullname="t3_b", created_utc=today),
            payload(fullname="t3_c", created_utc=today - timedelta(days=2)),
        ],
    )
    series = analytics.volume_by_day(seeded, days=7)
    by_date = {p["date"]: p["total"] for p in series}
    assert by_date[today.date().isoformat()] == 2


def test_purged_items_still_count_toward_volume(seeded):
    from social_listener.compliance import run_sweep

    from .test_ingest import FakeSource

    ingest_payloads(seeded, [payload(created_utc=_now())])
    run_sweep(seeded, FakeSource([]))
    series = analytics.volume_by_day(seeded, days=7)
    assert sum(p["total"] for p in series) == 1, "the content goes, the count stays"


def test_a_flat_series_produces_no_spikes(seeded):
    today = _now()
    items = []
    for day in range(20):
        for n in range(2):
            items.append(
                payload(fullname=f"t3_{day}_{n}", created_utc=today - timedelta(days=day))
            )
    ingest_payloads(seeded, items)
    assert analytics.detect_spikes(seeded) == []


def test_a_real_spike_is_detected(seeded):
    today = _now()
    items = []
    for day in range(3, 21):
        for n in range(2):
            items.append(
                payload(fullname=f"t3_{day}_{n}", created_utc=today - timedelta(days=day))
            )
    for n in range(25):
        items.append(payload(fullname=f"t3_spike_{n}", created_utc=today - timedelta(days=1)))
    ingest_payloads(seeded, items)
    spikes = analytics.detect_spikes(seeded)
    assert spikes, "a 25-item day against a baseline of 2 must register"


def test_feed_filters_by_sentiment(seeded):
    ingest_payloads(
        seeded,
        [
            payload(fullname="t3_n", body="Northbridge College tuition is terrible and unacceptable", created_utc=_now()),
            payload(fullname="t3_p", body="Northbridge College tuition support was wonderful, highly recommend", created_utc=_now()),
        ],
    )
    rows = analytics.feed(seeded, sentiment="negative")
    assert len(rows) == 1


def test_feed_excludes_spam(seeded):
    ingest_payloads(
        seeded,
        [payload(body="Northbridge College tuition thread. I am a bot.", author="AutoModerator", created_utc=_now())],
    )
    assert analytics.feed(seeded) == []
