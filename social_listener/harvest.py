"""The capture pipeline: discover -> harvest -> normalise -> dedupe -> match
-> classify -> store -> alert.

Ordering is a budget decision, not a preference. Work runs in this sequence:

  1. retention refresh   an obligation, and it must never be starved
  2. channel uploads     1 unit per channel via playlistItems
  3. comment harvest     1 unit per page -- the bulk of the useful data
  4. discovery search    100 units a call, so last and capped

That ordering is the single most important thing in this module. Running
discovery first would let 10 searches eat a fifth of the day before a single
comment was read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger
from .config import settings
from .enrichment import DEFAULT_ENRICHER, Enricher
from .matching import Matcher
from .models import (
    Alert,
    Channel,
    Enrichment,
    Match,
    Mention,
    MetricSnapshot,
    Video,
    WatchTerm,
    default_content_expiry,
    hash_author,
    utcnow,
)
from .quota import METHOD_COSTS, QuotaExhausted
from .youtube.source import Source

log = logging.getLogger(__name__)

ALERT_SEVERITY_THRESHOLD = 4


@dataclass
class HarvestStats:
    channels_polled: int = 0
    videos_seen: int = 0
    videos_new: int = 0
    videos_comments_disabled: int = 0
    comments_fetched: int = 0
    duplicates: int = 0
    matched: int = 0
    unmatched: int = 0
    kept_unmatched: int = 0
    spam_filtered: int = 0
    escalated: int = 0
    alerts: int = 0
    searches_run: int = 0
    quota_spent: int = 0
    quota_remaining: int = 0
    skipped_for_quota: list = field(default_factory=list)
    threads_alerted: set = field(default_factory=set)

    def as_dict(self) -> dict:
        data = {
            key: value
            for key, value in self.__dict__.items()
            if key not in ("threads_alerted", "skipped_for_quota")
        }
        data["threads_alerted"] = len(self.threads_alerted)
        data["skipped_for_quota"] = ", ".join(self.skipped_for_quota) or "nothing"
        return data


# -- storage ---------------------------------------------------------------


def upsert_video(session: Session, payload: dict, stats: HarvestStats) -> Video:
    video = session.scalar(
        select(Video).where(Video.youtube_id == payload["youtube_id"])
    )
    if video is None:
        video = Video(
            youtube_id=payload["youtube_id"],
            channel_youtube_id=payload.get("channel_youtube_id"),
            channel_title=payload.get("channel_title"),
            title=payload.get("title"),
            description=payload.get("description"),
            published_at=payload["published_at"],
            view_count=payload.get("view_count"),
            like_count=payload.get("like_count"),
            comment_count=payload.get("comment_count"),
            provenance=payload.get("provenance", "live"),
            last_checked_at=payload.get("_first_seen_at") or utcnow(),
            first_seen_at=payload.get("_first_seen_at") or utcnow(),
            content_expires_at=payload.get("_content_expires_at")
            or default_content_expiry(),
        )
        session.add(video)
        session.flush()
        stats.videos_new += 1
    else:
        # The policy requires stored data stay consistent with YouTube, so a
        # re-fetch updates rather than no-ops -- and pushes the expiry out,
        # since a refreshed record restarts its 30-day clock.
        for column in ("title", "description", "view_count", "like_count", "comment_count"):
            value = payload.get(column)
            if value is not None:
                setattr(video, column, value)
        video.last_checked_at = utcnow()
        video.content_expires_at = default_content_expiry()
    stats.videos_seen += 1
    return video


def upsert_mention(
    session: Session, payload: dict, video: Video, stats: HarvestStats
) -> tuple:
    """Insert, or update the mutable fields. `youtube_id` is the dedupe key."""
    existing = session.scalar(
        select(Mention).where(Mention.youtube_id == payload["youtube_id"])
    )
    if existing is not None:
        stats.duplicates += 1
        for column in ("text", "like_count", "reply_count", "updated_at"):
            value = payload.get(column)
            if value is not None and getattr(existing, column) != value:
                setattr(existing, column, value)
        existing.last_checked_at = utcnow()
        existing.content_expires_at = default_content_expiry()
        session.add(
            MetricSnapshot(
                mention=existing,
                observed_at=existing.last_checked_at,
                like_count=payload.get("like_count"),
                reply_count=payload.get("reply_count"),
            )
        )
        return existing, False

    mention = Mention(
        youtube_id=payload["youtube_id"],
        kind=payload["kind"],
        video=video,
        parent_youtube_id=payload.get("parent_youtube_id"),
        author_name=payload.get("author_name"),
        author_channel_id=payload.get("author_channel_id"),
        author_hash=hash_author(payload.get("author_channel_id")),
        text=payload.get("text"),
        like_count=payload.get("like_count"),
        reply_count=payload.get("reply_count"),
        published_at=payload["published_at"],
        updated_at=payload.get("updated_at"),
        provenance=payload.get("provenance", "live"),
        last_checked_at=payload.get("_first_seen_at") or utcnow(),
        first_seen_at=payload.get("_first_seen_at") or utcnow(),
        content_expires_at=payload.get("_content_expires_at")
        or default_content_expiry(),
    )
    session.add(mention)
    session.flush()
    session.add(
        MetricSnapshot(
            mention=mention,
            observed_at=mention.last_checked_at,
            like_count=mention.like_count,
            reply_count=mention.reply_count,
        )
    )
    return mention, True


def classify(
    session: Session, mention: Mention, enricher: Enricher, stats: HarvestStats
) -> None:
    # Read the collection, not a query: with autoflush off, an enrichment added
    # earlier in this same batch is invisible to a SELECT and the duplicate
    # would violate the unique constraint on commit.
    for existing in mention.enrichments:
        if (
            existing.model_name == enricher.model_name
            and existing.model_version == enricher.model_version
        ):
            return

    result = enricher.enrich(
        {
            "title": mention.video.title if mention.kind == "video" else None,
            "text": mention.text,
            "author_name": mention.author_name,
            "like_count": mention.like_count,
            "reply_count": mention.reply_count,
        }
    )
    if result.is_spam:
        stats.spam_filtered += 1
    if result.needs_expensive_lane:
        stats.escalated += 1

    session.add(
        Enrichment(
            mention=mention,
            model_name=result.model_name,
            model_version=result.model_version,
            sentiment=result.sentiment,
            sentiment_score=result.sentiment_score,
            confidence=result.confidence,
            severity=result.severity,
            intent=result.intent,
            topics=result.topics_json(),
            is_spam=result.is_spam,
            rationale=result.rationale,
        )
    )

    if result.severity >= ALERT_SEVERITY_THRESHOLD and not result.is_spam:
        _raise_alert(session, mention, result, stats)


def _raise_alert(session: Session, mention: Mention, result, stats: HarvestStats) -> None:
    """One alert per video, not per comment.

    A review-bombed video produces one alert with a count, not eighty.
    """
    thread_key = mention.video.youtube_id
    if thread_key in stats.threads_alerted:
        return
    if session.scalar(
        select(Alert).where(Alert.mention_id == mention.id, Alert.kind == "severity")
    ) is not None:
        return

    stats.threads_alerted.add(thread_key)
    stats.alerts += 1
    snippet = (mention.text or "").strip().replace("\n", " ")
    if len(snippet) > 70:
        snippet = snippet[:70].rsplit(" ", 1)[0] + "..."
    session.add(
        Alert(
            mention=mention,
            kind="severity",
            severity=result.severity,
            headline=f"{mention.video.channel_title or 'video'}: {snippet}"[:400],
            detail=result.rationale,
        )
    )


def ingest_mentions(
    session: Session,
    video_payload: dict,
    comment_payloads: list,
    enricher: Optional[Enricher] = None,
    stats: Optional[HarvestStats] = None,
    require_match: bool = True,
) -> HarvestStats:
    """Run one video and its comments through the whole pipeline.

    `require_match=False` is the owned-channel case. On a channel you own,
    every comment is addressed to you, so demanding a keyword match would
    throw away most of what you actually need to read -- someone complaining
    under your own enrolment video rarely names the institution. Terms are
    still recorded when they hit; they just stop being the admission test.

    It also means the spam lane earns its keep here rather than in theory: all
    the sub-for-sub and crypto bait on your own videos now arrives, and has to
    be filtered before it reaches a reviewer or a model.
    """
    enricher = enricher or DEFAULT_ENRICHER
    stats = stats or HarvestStats()

    terms = list(session.scalars(select(WatchTerm).where(WatchTerm.is_active.is_(True))))
    matcher = Matcher(terms)
    by_label = {term.label: term for term in terms}

    video = upsert_video(session, video_payload, stats)

    # The video's own title and description can carry a mention.
    candidates = [
        {
            "youtube_id": video.youtube_id,
            "kind": "video",
            "text": " ".join(filter(None, (video_payload.get("title"), video_payload.get("description")))),
            "author_name": video_payload.get("channel_title"),
            "author_channel_id": video_payload.get("channel_youtube_id"),
            "like_count": video_payload.get("like_count"),
            "reply_count": video_payload.get("comment_count"),
            "published_at": video_payload["published_at"],
            "updated_at": None,
            "parent_youtube_id": None,
            "provenance": video_payload.get("provenance", "live"),
        }
    ] + list(comment_payloads)

    for payload in candidates:
        if payload["kind"] != "video":
            stats.comments_fetched += 1

        text = payload.get("text") or ""
        hits = matcher.match(text)
        if not hits and require_match:
            stats.unmatched += 1
            continue

        mention, _is_new = upsert_mention(session, payload, video, stats)
        if hits:
            stats.matched += 1
        else:
            stats.kept_unmatched += 1

        seen_terms = {m.term_id for m in mention.matches if m.term_id is not None}
        seen_terms.update(
            m.term.id for m in mention.matches if m.term_id is None and m.term is not None
        )
        for hit in hits:
            term = by_label.get(hit.term_label)
            if term is None or term.id in seen_terms:
                continue
            seen_terms.add(term.id)
            session.add(
                Match(
                    mention=mention,
                    term=term,
                    matched_text=hit.matched_text[:400],
                    confidence=hit.confidence,
                )
            )

        classify(session, mention, enricher, stats)

    session.flush()
    return stats


# -- the loops --------------------------------------------------------------


def harvest_channels(
    session: Session,
    source: Source,
    stats: Optional[HarvestStats] = None,
    comment_pages: Optional[int] = None,
) -> HarvestStats:
    """Channel uploads, then comments. All 1-unit calls."""
    stats = stats or HarvestStats()
    pages = comment_pages or settings.comment_pages_per_video
    live = source.mode == "live"

    channels = list(session.scalars(select(Channel).where(Channel.is_active.is_(True))))
    for channel in channels:
        if live and not _affordable(session, "playlistItems.list", stats, f"r/{channel.youtube_id}"):
            continue
        videos = source.channel_videos(
            {
                "youtube_id": channel.youtube_id,
                "uploads_playlist_id": channel.uploads_playlist_id,
            }
        )
        channel.last_polled_at = utcnow()
        stats.channels_polled += 1

        for video_payload in videos:
            if live and not _affordable(
                session, "commentThreads.list", stats, video_payload["youtube_id"]
            ):
                continue
            comments = source.video_comments(video_payload["youtube_id"], max_pages=pages)
            if not comments:
                # Either comments are disabled or nobody has commented. Both
                # are ordinary; record the former so the UI can explain itself.
                stats.videos_comments_disabled += 1
            ingest_mentions(
                session,
                video_payload,
                comments,
                stats=stats,
                require_match=not channel.is_owned,
            )

            video = session.scalar(
                select(Video).where(Video.youtube_id == video_payload["youtube_id"])
            )
            if video is not None:
                video.last_comment_poll_at = utcnow()
                video.comments_disabled = not comments and bool(
                    video_payload.get("_comments_disabled")
                )

    _record_quota(session, stats)
    return stats


def run_discovery(
    session: Session,
    source: Source,
    stats: Optional[HarvestStats] = None,
    max_searches: Optional[int] = None,
) -> HarvestStats:
    """Keyword search, to find videos on channels we do not track.

    Capped hard: at 100 units a call this is the only part of the pipeline that
    can exhaust the day's budget by itself.
    """
    stats = stats or HarvestStats()
    live = source.mode == "live"

    if max_searches is None:
        budget = int(10_000 * settings.discovery_budget_fraction)
        max_searches = max(budget // METHOD_COSTS["search.list"], 0)

    terms = list(
        session.scalars(
            select(WatchTerm).where(
                WatchTerm.is_active.is_(True), WatchTerm.use_in_discovery.is_(True)
            )
        )
    )
    published_after = (utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for term in terms[:max_searches]:
        if live and not _affordable(session, "search.list", stats, f"search:{term.label}"):
            break
        query = term.label if term.match_type == "boolean" else term.pattern
        videos = source.search_videos(query, published_after=published_after)
        stats.searches_run += 1

        for video_payload in videos:
            if live and not _affordable(
                session, "commentThreads.list", stats, video_payload["youtube_id"]
            ):
                break
            comments = source.video_comments(
                video_payload["youtube_id"], max_pages=settings.comment_pages_per_video
            )
            ingest_mentions(session, video_payload, comments, stats=stats)

    _record_quota(session, stats)
    return stats


def _affordable(session: Session, method: str, stats: HarvestStats, label: str) -> bool:
    """Would this call fit in the remaining budget, keeping the reserve intact?

    The reserve exists so the retention job can always run: purging expired
    content is a policy obligation, while capturing more of it is not.
    """
    current = ledger.status(session)
    cost = METHOD_COSTS[method]
    if current.remaining - cost < settings.retention_reserve_units:
        stats.skipped_for_quota.append(label)
        return False
    return True


def _record_quota(session: Session, stats: HarvestStats) -> None:
    current = ledger.status(session)
    stats.quota_spent = current.spent
    stats.quota_remaining = current.remaining
