"""The 30-day retention engine.

This is the module that differs most from the Reddit build, and the one with
actual policy exposure. YouTube's API Services Developer Policies require that
stored API data be **deleted or refreshed** within 30 calendar days, and that
stored data be kept consistent with what YouTube currently serves. A separate
carve-out lets *derived metrics* -- view, like, reply and comment counts -- live
for up to 36 months, but titles, descriptions, author names and comment text
get no such extension.

So retention here is an engine with three jobs, not a checkbox:

  REFRESH  re-fetch records approaching expiry. A successful refresh restarts
           the 30-day clock, which is the policy's own remedy and the reason
           long-running monitoring is permitted at all.
  PURGE    anything that could not be refreshed -- expired, or gone from
           YouTube -- has its text nulled. The row survives so aggregates stay
           valid; the content does not.
  RECONCILE  records still present upstream get their metadata updated, since
           consistency is a requirement and not a nicety.

The separation in the schema is what makes this tractable: MetricSnapshot holds
counts and no text, so purging content never destroys the trend history. That is
the whole reason the two live in different tables.

Derivatives are purged too. A comment scrubbed from `mention.text` but still
quoted in an old alert body is still a retained copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import (
    CONTENT_RETENTION_DAYS,
    Alert,
    Mention,
    Video,
    default_content_expiry,
    utcnow,
)
from .youtube.source import Source

log = logging.getLogger(__name__)

# Refresh this many days before expiry, so a failed run has room to retry
# before anything actually breaches.
REFRESH_LEAD_DAYS = 5
# Re-verify anything not checked in this long, regardless of expiry. Without
# this, a comment deleted on YouTube could sit in our store for ~25 days before
# the expiry refresh noticed -- and the policy asks for stored data to track
# YouTube "as quickly as possible", not eventually. Each check costs 1 unit per
# 50 records, so this is cheap.
RECONCILE_AFTER_DAYS = 7
BATCH_SIZE = 50


@dataclass
class RetentionStats:
    videos_checked: int = 0
    comments_checked: int = 0
    refreshed: int = 0
    purged_expired: int = 0
    purged_deleted_upstream: int = 0
    alerts_scrubbed: int = 0
    batches: int = 0

    @property
    def purged(self) -> int:
        return self.purged_expired + self.purged_deleted_upstream

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        data["purged_total"] = self.purged
        return data


def purge_mention(session: Session, mention: Mention, reason: str) -> int:
    """Null the text, keep the row. Returns alerts scrubbed."""
    mention.text = None
    mention.author_name = None  # author_hash survives: pseudonymous, no text
    mention.purged_at = utcnow()

    scrubbed = 0
    for alert in session.scalars(select(Alert).where(Alert.mention_id == mention.id)):
        alert.detail = f"[purged: {reason}]"
        alert.headline = f"[purged: {reason}]"
        scrubbed += 1

    log.info("purged mention %s (%s)", mention.youtube_id, reason)
    return scrubbed


def purge_video(session: Session, video: Video, reason: str) -> None:
    video.title = None
    video.description = None
    video.channel_title = None
    video.purged_at = utcnow()
    log.info("purged video %s (%s)", video.youtube_id, reason)


def _due(model, now):
    """Records needing a look: near expiry, never checked, or stale.

    The third case is the consistency obligation rather than the retention
    one -- it is how a deletion gets noticed promptly instead of whenever the
    expiry clock happens to come round.
    """
    horizon = now + timedelta(days=REFRESH_LEAD_DAYS)
    stale_before = now - timedelta(days=RECONCILE_AFTER_DAYS)
    return or_(
        model.content_expires_at <= horizon,
        model.last_checked_at.is_(None),
        model.last_checked_at <= stale_before,
    )


def run_retention(
    session: Session, source: Source, batch_size: int = BATCH_SIZE
) -> RetentionStats:
    """One full pass. Safe to run repeatedly; purged rows drop out."""
    stats = RetentionStats()
    now = utcnow()
    hard_deadline = now - timedelta(days=CONTENT_RETENTION_DAYS)

    # -- comments ----------------------------------------------------------
    mentions = list(
        session.scalars(
            select(Mention)
            .where(Mention.purged_at.is_(None))
            .where(_due(Mention, now))
            .order_by(Mention.content_expires_at)
        )
    )
    by_id = {mention.youtube_id: mention for mention in mentions}

    for start in range(0, len(mentions), batch_size):
        batch = mentions[start : start + batch_size]
        stats.batches += 1
        stats.comments_checked += len(batch)

        # A video-kind mention is refreshed with the videos, not the comments.
        comment_ids = [m.youtube_id for m in batch if m.kind != "video"]
        returned = source.refresh_comments(comment_ids) if comment_ids else []
        seen = set()

        for payload in returned:
            mention = by_id.get(payload.get("youtube_id"))
            if mention is None:
                continue
            seen.add(mention.youtube_id)
            # Reconcile, then restart the clock: a refreshed record is
            # compliant again.
            for column in ("text", "like_count", "updated_at"):
                value = payload.get(column)
                if value is not None:
                    setattr(mention, column, value)
            mention.last_checked_at = now
            mention.content_expires_at = default_content_expiry()
            stats.refreshed += 1

        for mention in batch:
            if mention.kind == "video" or mention.youtube_id in seen:
                continue
            # Absent from the response means gone from YouTube. Consistency
            # requires we stop serving it.
            mention.is_deleted_upstream = True
            stats.alerts_scrubbed += purge_mention(
                session, mention, "no longer available on YouTube"
            )
            stats.purged_deleted_upstream += 1

    # Flush first: with autoflush off, this SELECT would not see the purges
    # pending from the loop above and would purge (and count) them again.
    session.flush()

    # Anything still past the hard deadline could not be refreshed, whatever
    # the reason. The policy has no grace period, so it goes.
    overdue = session.scalars(
        select(Mention)
        .where(Mention.purged_at.is_(None))
        .where(Mention.content_expires_at <= now)
        .where(Mention.first_seen_at <= hard_deadline)
    )
    for mention in overdue:
        stats.alerts_scrubbed += purge_mention(
            session, mention, f"exceeded the {CONTENT_RETENTION_DAYS}-day retention window"
        )
        stats.purged_expired += 1

    # -- videos ------------------------------------------------------------
    videos = list(
        session.scalars(
            select(Video)
            .where(Video.purged_at.is_(None))
            .where(_due(Video, now))
            .order_by(Video.content_expires_at)
        )
    )
    videos_by_id = {video.youtube_id: video for video in videos}

    for start in range(0, len(videos), batch_size):
        batch = videos[start : start + batch_size]
        stats.batches += 1
        stats.videos_checked += len(batch)
        returned = source.refresh_videos([v.youtube_id for v in batch])
        seen = set()

        for payload in returned:
            video = videos_by_id.get(payload.get("youtube_id"))
            if video is None:
                continue
            seen.add(video.youtube_id)
            for column in ("title", "description", "view_count", "like_count", "comment_count"):
                value = payload.get(column)
                if value is not None:
                    setattr(video, column, value)
            video.last_checked_at = now
            video.content_expires_at = default_content_expiry()
            stats.refreshed += 1

            # The video-kind mention carries the same title and description, so
            # refreshing the Video renews it too. Without this the mention
            # would fall to the overdue sweep and be purged while its own
            # source record had just been confirmed live.
            for mention in video.mentions:
                if mention.kind == "video" and mention.purged_at is None:
                    mention.text = " ".join(
                        filter(None, (payload.get("title"), payload.get("description")))
                    ) or mention.text
                    mention.last_checked_at = now
                    mention.content_expires_at = default_content_expiry()

        for video in batch:
            if video.youtube_id in seen:
                continue
            purge_video(session, video, "no longer available on YouTube")
            stats.purged_deleted_upstream += 1
            # A gone video takes its own video-kind mention with it. That
            # mention is a purged row in its own right, so it must be counted
            # as one -- otherwise the reported total under-reports what the
            # database actually did.
            for mention in video.mentions:
                if mention.kind == "video" and mention.purged_at is None:
                    stats.alerts_scrubbed += purge_mention(
                        session, mention, "video no longer available on YouTube"
                    )
                    stats.purged_deleted_upstream += 1

    session.flush()

    overdue_videos = session.scalars(
        select(Video)
        .where(Video.purged_at.is_(None))
        .where(Video.content_expires_at <= now)
        .where(Video.first_seen_at <= hard_deadline)
    )
    for video in overdue_videos:
        purge_video(
            session, video, f"exceeded the {CONTENT_RETENTION_DAYS}-day retention window"
        )
        stats.purged_expired += 1

    session.flush()
    return stats


def expiry_report(session: Session) -> dict:
    """What the dashboard needs to show the retention position honestly."""
    now = utcnow()
    soon = now + timedelta(days=REFRESH_LEAD_DAYS)

    def count(stmt) -> int:
        return len(list(session.scalars(stmt)))

    return {
        "retained_comments": count(
            select(Mention).where(Mention.purged_at.is_(None))
        ),
        "retained_videos": count(select(Video).where(Video.purged_at.is_(None))),
        "purged_comments": count(
            select(Mention).where(Mention.purged_at.isnot(None))
        ),
        "purged_videos": count(select(Video).where(Video.purged_at.isnot(None))),
        "due_for_refresh": count(
            select(Mention)
            .where(Mention.purged_at.is_(None))
            .where(Mention.content_expires_at <= soon)
        ),
        "overdue": count(
            select(Mention)
            .where(Mention.purged_at.is_(None))
            .where(Mention.content_expires_at <= now)
        ),
        "retention_days": CONTENT_RETENTION_DAYS,
    }
