"""The 30-day retention engine.

This suite is the most pointed in the project because the obligation is real:
stored YouTube data must be deleted or refreshed within 30 calendar days, and
kept consistent with what YouTube serves. The properties that matter are that
content genuinely disappears, that rows and aggregates survive, and that
derivative copies are scrubbed too.
"""

from datetime import timedelta

from sqlalchemy import func, select

from social_listener.harvest import ingest_mentions
from social_listener.models import (
    CONTENT_RETENTION_DAYS,
    Alert,
    Mention,
    MetricSnapshot,
    Video,
    utcnow,
)
from social_listener.retention import RECONCILE_AFTER_DAYS, expiry_report, run_retention

from .test_harvest import FakeSource, comment, video

LOUD = "Northbridge College tuition fraud, unacceptable, I am contacting a lawyer"


def _age(session, days, mention_only=False):
    """Backdate the stored-at clock, which is what the policy runs on."""
    now = utcnow()
    for mention in session.scalars(select(Mention)):
        mention.first_seen_at = now - timedelta(days=days)
        mention.last_checked_at = now - timedelta(days=days)
        mention.content_expires_at = now - timedelta(days=days - CONTENT_RETENTION_DAYS)
    if not mention_only:
        for row in session.scalars(select(Video)):
            row.first_seen_at = now - timedelta(days=days)
            row.last_checked_at = now - timedelta(days=days)
            row.content_expires_at = now - timedelta(days=days - CONTENT_RETENTION_DAYS)
    session.flush()


# -- refresh ---------------------------------------------------------------


def test_nothing_fresh_is_touched(seeded):
    ingest_mentions(seeded, video(), [comment()])
    stats = run_retention(seeded, FakeSource(videos=[video()], comments=[comment()]))
    assert stats.purged == 0
    assert seeded.scalar(select(Mention).where(Mention.youtube_id == "c1")).text


def test_a_refresh_restarts_the_clock(seeded):
    # The policy's own remedy: refreshing is what makes long-running
    # monitoring permissible at all.
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[comment()]))
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert mention.purged_at is None
    assert mention.days_until_expiry() >= CONTENT_RETENTION_DAYS - 1


def test_a_refresh_reconciles_changed_counts(seeded):
    ingest_mentions(seeded, video(), [comment(like_count=1)])
    _age(seeded, 28)
    run_retention(
        seeded, FakeSource(videos=[video()], comments=[comment(like_count=999)])
    )
    assert seeded.scalar(select(Mention).where(Mention.youtube_id == "c1")).like_count == 999


def test_stale_records_are_re_verified_even_when_not_near_expiry(seeded):
    # The consistency obligation, not the retention one: a deletion should be
    # noticed promptly rather than whenever the expiry clock comes round.
    ingest_mentions(seeded, video(), [comment()])
    now = utcnow()
    for mention in seeded.scalars(select(Mention)):
        mention.last_checked_at = now - timedelta(days=RECONCILE_AFTER_DAYS + 1)
    seeded.flush()
    stats = run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    assert stats.purged_deleted_upstream >= 1


# -- purging ---------------------------------------------------------------


def test_a_comment_gone_from_youtube_is_purged(seeded):
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    stats = run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    assert stats.purged_deleted_upstream >= 1


def test_purging_nulls_the_text_but_keeps_the_row(seeded):
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert mention is not None, "the row must survive so aggregates stay valid"
    assert mention.text is None
    assert mention.author_name is None
    assert mention.purged_at is not None


def test_the_author_hash_survives_a_purge(seeded):
    # Pseudonymous and text-free, so repeat-commenter analysis keeps working.
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    assert seeded.scalar(select(Mention).where(Mention.youtube_id == "c1")).author_hash


def test_metric_snapshots_survive_a_purge(seeded):
    # They hold counts and no text, which is what the derived-metrics
    # carve-out permits keeping for longer.
    ingest_mentions(seeded, video(), [comment()])
    before = seeded.scalar(select(func.count(MetricSnapshot.id)))
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    assert seeded.scalar(select(func.count(MetricSnapshot.id))) == before


def test_a_purged_mention_is_never_rendered(seeded):
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert "purged" in mention.display_text


def test_derivative_alert_copies_are_scrubbed(seeded):
    # A purged comment still quoted in an old alert is still a retained copy.
    ingest_mentions(seeded, video(), [comment(text=LOUD, like_count=400, reply_count=60)])
    assert seeded.scalar(select(func.count(Alert.id))) == 1
    _age(seeded, 28)
    stats = run_retention(seeded, FakeSource(videos=[video()], comments=[]))
    assert stats.alerts_scrubbed >= 1
    alert = seeded.scalar(select(Alert))
    assert "purged" in alert.detail
    assert "purged" in alert.headline
    assert LOUD not in (alert.headline or "")


def test_a_gone_video_is_purged(seeded):
    ingest_mentions(seeded, video(title="Northbridge College open day"), [])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    row = seeded.scalar(select(Video))
    assert row.title is None
    assert row.purged_at is not None


def test_a_gone_video_takes_its_video_mention_with_it(seeded):
    ingest_mentions(seeded, video(title="Northbridge College open day"), [])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    mention = seeded.scalar(select(Mention).where(Mention.kind == "video"))
    assert mention.purged_at is not None


def test_a_refreshed_video_does_not_purge_its_video_mention(seeded):
    # The subtle one: the video-kind mention is refreshed with the videos, not
    # the comments, so the overdue sweep must not claim it.
    payload = video(title="Northbridge College open day")
    ingest_mentions(seeded, payload, [])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[payload], comments=[]))
    mention = seeded.scalar(select(Mention).where(Mention.kind == "video"))
    assert mention.purged_at is None


# -- accounting ------------------------------------------------------------


def test_reported_purges_match_the_database(seeded):
    comments = [comment(youtube_id=f"c{i}") for i in range(5)]
    ingest_mentions(seeded, video(title="Northbridge College day"), comments)
    _age(seeded, 28)
    stats = run_retention(seeded, FakeSource(videos=[], comments=[]))
    in_db = (
        seeded.scalar(select(func.count(Mention.id)).where(Mention.purged_at.isnot(None)))
        + seeded.scalar(select(func.count(Video.id)).where(Video.purged_at.isnot(None)))
    )
    assert stats.purged == in_db


def test_no_purged_row_retains_text(seeded):
    ingest_mentions(seeded, video(title="Northbridge College day"), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    leaked = seeded.scalar(
        select(func.count(Mention.id)).where(
            Mention.purged_at.isnot(None), Mention.text.isnot(None)
        )
    )
    leaked_videos = seeded.scalar(
        select(func.count(Video.id)).where(
            Video.purged_at.isnot(None), Video.title.isnot(None)
        )
    )
    assert (leaked, leaked_videos) == (0, 0)


def test_a_second_pass_is_a_no_op(seeded):
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[video()], comments=[comment()]))
    second = run_retention(seeded, FakeSource(videos=[video()], comments=[comment()]))
    assert second.purged == 0
    assert second.refreshed == 0


def test_purged_rows_drop_out_of_later_passes(seeded):
    ingest_mentions(seeded, video(), [comment()])
    _age(seeded, 28)
    run_retention(seeded, FakeSource(videos=[], comments=[]))
    second = run_retention(seeded, FakeSource(videos=[], comments=[]))
    assert second.comments_checked == 0


def test_expiry_report_counts_are_consistent(seeded):
    ingest_mentions(seeded, video(), [comment()])
    report = expiry_report(seeded)
    assert report["retention_days"] == CONTENT_RETENTION_DAYS
    assert report["retained_comments"] >= 1
    assert report["purged_comments"] == 0
