"""Metric definitions."""

from datetime import timedelta

from social_listener import analytics
from social_listener.harvest import ingest_mentions
from social_listener.models import utcnow
from social_listener.retention import run_retention

from .test_harvest import FakeSource, comment, video


def _age_everything(session):
    """Backdate the stored-at clock so retention considers the records due."""
    from sqlalchemy import select

    from social_listener.models import Mention, Video as VideoModel

    now = utcnow()
    for table in (Mention, VideoModel):
        for row in session.scalars(select(table)):
            row.first_seen_at = now - timedelta(days=28)
            row.last_checked_at = now - timedelta(days=28)
            row.content_expires_at = now + timedelta(days=2)
    session.flush()


NEG = "Northbridge College tuition is terrible, unacceptable and ridiculous"
POS = "Northbridge College tuition support was wonderful, highly recommend"


def test_totals_count_sentiment(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(youtube_id="c1", text=NEG), comment(youtube_id="c2", text=POS)],
    )
    totals = analytics.totals(seeded)
    assert totals.negative == 1
    assert totals.positive == 1


def test_net_sentiment_is_zero_when_balanced(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(youtube_id="c1", text=NEG), comment(youtube_id="c2", text=POS)],
    )
    assert analytics.totals(seeded).net_sentiment == 0.0


def test_net_sentiment_on_an_empty_database_is_zero(seeded):
    assert analytics.totals(seeded).net_sentiment == 0.0


def test_spam_is_excluded_from_sentiment_counts(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(text="Northbridge College tuition hacks, check out my channel, link in bio")],
    )
    totals = analytics.totals(seeded)
    assert totals.spam_filtered == 1
    assert totals.classified == 0


def test_spam_is_excluded_from_the_feed_by_default(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(text="Northbridge College tuition hacks, check out my channel, link in bio")],
    )
    assert analytics.feed(seeded) == []


def test_spam_can_be_shown_on_request(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(text="Northbridge College tuition hacks, check out my channel, link in bio")],
    )
    assert len(analytics.feed(seeded, include_spam=True)) >= 1


def test_share_of_voice_sums_to_one(seeded):
    ingest_mentions(seeded, video(), [comment()])
    rows = analytics.share_of_voice(seeded)
    assert abs(sum(r["share"] for r in rows) - 1.0) < 1e-9


def test_volume_buckets_by_publication_date(seeded):
    today = utcnow()
    ingest_mentions(
        seeded,
        video(),
        [
            comment(youtube_id="c1", published_at=today),
            comment(youtube_id="c2", published_at=today),
            comment(youtube_id="c3", published_at=today - timedelta(days=3)),
        ],
    )
    series = analytics.volume_by_day(seeded, days=10)
    by_date = {p["date"]: p["total"] for p in series}
    assert by_date[today.date().isoformat()] >= 2


def test_purged_mentions_still_count_toward_volume(seeded):
    ingest_mentions(seeded, video(), [comment(published_at=utcnow())])
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    series = analytics.volume_by_day(seeded, days=10)
    assert sum(p["total"] for p in series) >= 1, "the content goes, the count stays"


def test_a_flat_series_produces_no_spikes(seeded):
    today = utcnow()
    comments = [
        comment(youtube_id=f"c{d}_{n}", published_at=today - timedelta(days=d))
        for d in range(20)
        for n in range(2)
    ]
    ingest_mentions(seeded, video(), comments)
    assert analytics.detect_spikes(seeded) == []


def test_a_real_spike_is_detected(seeded):
    today = utcnow()
    comments = [
        comment(youtube_id=f"c{d}_{n}", published_at=today - timedelta(days=d))
        for d in range(3, 21)
        for n in range(2)
    ]
    comments += [
        comment(youtube_id=f"spike{n}", published_at=today - timedelta(days=1))
        for n in range(30)
    ]
    ingest_mentions(seeded, video(), comments)
    assert analytics.detect_spikes(seeded), "30 against a baseline of 2 must register"


def test_feed_filters_by_sentiment(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(youtube_id="c1", text=NEG), comment(youtube_id="c2", text=POS)],
    )
    assert len(analytics.feed(seeded, sentiment="negative")) == 1


def test_top_videos_counts_negatives_per_video(seeded):
    ingest_mentions(
        seeded,
        video(),
        [comment(youtube_id="c1", text=NEG), comment(youtube_id="c2", text=POS)],
    )
    rows = analytics.top_videos(seeded)
    assert rows[0]["count"] >= 2
    assert rows[0]["negative"] == 1


def test_a_purged_video_title_is_not_leaked_by_analytics(seeded):
    ingest_mentions(seeded, video(title="Northbridge College open day"), [comment()])
    # Retention only looks at records due for a check, so age them first --
    # a freshly stored record is correctly left alone.
    _age_everything(seeded)
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    for row in analytics.top_videos(seeded):
        assert row["title"] == "[purged]"
