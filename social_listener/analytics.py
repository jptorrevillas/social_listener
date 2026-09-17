"""Metric definitions, fixed early because changing one invalidates history.

Two honesty rules carried forward:

  * Reach is a model, not a measurement. YouTube does give real view counts,
    which is better than Reddit's fuzzed scores -- but a view is not a read of
    any particular comment, so comment-level reach stays an estimate.
  * Purged content still counts. The text goes; the row and its aggregates
    stay. That is exactly what the schema split is for.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import Alert, Enrichment, Match, Mention, Video, WatchTerm, utcnow

SPIKE_SIGMA = 3.0
BASELINE_DAYS = 14


@dataclass
class Totals:
    mentions: int
    videos: int
    purged: int
    spam_filtered: int
    alerts_open: int
    escalated: int
    negative: int
    neutral: int
    positive: int
    comments_disabled_videos: int

    @property
    def classified(self) -> int:
        return self.negative + self.neutral + self.positive

    @property
    def net_sentiment(self) -> float:
        if not self.classified:
            return 0.0
        return (self.positive - self.negative) / self.classified


def latest_enrichment_subquery():
    return (
        select(
            Enrichment.mention_id.label("mention_id"),
            func.max(Enrichment.id).label("enrichment_id"),
        )
        .group_by(Enrichment.mention_id)
        .subquery()
    )


def totals(session: Session) -> Totals:
    latest = latest_enrichment_subquery()
    rows = list(
        session.execute(
            select(
                Enrichment.sentiment,
                Enrichment.is_spam,
                Enrichment.severity,
                Enrichment.confidence,
            ).join(latest, Enrichment.id == latest.c.enrichment_id)
        )
    )
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    spam = escalated = 0
    for sentiment, is_spam, severity, confidence in rows:
        if is_spam:
            spam += 1
            continue
        if sentiment in counts:
            counts[sentiment] += 1
        if (severity or 0) >= 4 or (confidence or 1) < 0.5:
            escalated += 1

    return Totals(
        mentions=session.scalar(select(func.count(Mention.id))) or 0,
        videos=session.scalar(select(func.count(Video.id))) or 0,
        purged=session.scalar(
            select(func.count(Mention.id)).where(Mention.purged_at.isnot(None))
        ) or 0,
        spam_filtered=spam,
        alerts_open=session.scalar(
            select(func.count(Alert.id)).where(Alert.acknowledged_at.is_(None))
        ) or 0,
        escalated=escalated,
        negative=counts["negative"],
        neutral=counts["neutral"],
        positive=counts["positive"],
        comments_disabled_videos=session.scalar(
            select(func.count(Video.id)).where(Video.comments_disabled.is_(True))
        ) or 0,
    )


def volume_by_day(session: Session, days: int = 30) -> list:
    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(Mention.published_at, Mention.id).where(Mention.published_at >= since)
    ).all()

    latest = latest_enrichment_subquery()
    sentiment_by_mention = dict(
        session.execute(
            select(Enrichment.mention_id, Enrichment.sentiment)
            .join(latest, Enrichment.id == latest.c.enrichment_id)
            .where(Enrichment.is_spam.is_(False))
        ).all()
    )

    buckets = {}
    for offset in range(days + 1):
        key = (since + timedelta(days=offset)).date().isoformat()
        buckets[key] = {"date": key, "total": 0, "negative": 0, "neutral": 0, "positive": 0}

    for published, mention_id in rows:
        bucket = buckets.get(published.date().isoformat())
        if bucket is None:
            continue
        bucket["total"] += 1
        sentiment = sentiment_by_mention.get(mention_id)
        if sentiment in ("negative", "neutral", "positive"):
            bucket[sentiment] += 1

    return list(buckets.values())


def share_of_voice(session: Session) -> list:
    rows = session.execute(
        select(WatchTerm.label, func.count(Match.id))
        .join(Match, Match.term_id == WatchTerm.id)
        .group_by(WatchTerm.label)
        .order_by(func.count(Match.id).desc())
    ).all()
    total = sum(count for _, count in rows) or 1
    return [{"label": label, "count": count, "share": count / total} for label, count in rows]


def top_videos(session: Session, limit: int = 6) -> list:
    """Which videos the conversation is actually happening under.

    Counted in one grouped pass: total mentions, and negatives via a
    conditional sum, rather than a follow-up query per video.
    """
    latest = latest_enrichment_subquery()
    negative_flag = case((Enrichment.sentiment == "negative", 1), else_=0)

    rows = session.execute(
        select(
            Video.youtube_id,
            Video.title,
            Video.channel_title,
            Video.purged_at,
            func.count(Mention.id).label("total"),
            func.sum(negative_flag).label("negative"),
        )
        .join(Mention, Mention.video_id == Video.id)
        .join(latest, latest.c.mention_id == Mention.id)
        .join(Enrichment, Enrichment.id == latest.c.enrichment_id)
        .where(Enrichment.is_spam.is_(False))
        .group_by(Video.id)
        .order_by(func.count(Mention.id).desc())
        .limit(limit)
    ).all()

    return [
        {
            "youtube_id": youtube_id,
            "title": "[purged]" if purged_at else (title or "(untitled)"),
            "channel": channel or "",
            "count": total,
            "negative": int(negative or 0),
            "url": f"https://www.youtube.com/watch?v={youtube_id}",
        }
        for youtube_id, title, channel, purged_at, total, negative in rows
    ]


def topic_breakdown(session: Session, limit: int = 7) -> list:
    latest = latest_enrichment_subquery()
    rows = session.execute(
        select(Enrichment.topics, Enrichment.sentiment)
        .join(latest, Enrichment.id == latest.c.enrichment_id)
        .where(Enrichment.is_spam.is_(False))
    ).all()
    tally = {}
    for topics_json, sentiment in rows:
        try:
            topics = json.loads(topics_json or "[]")
        except json.JSONDecodeError:
            continue
        for topic in topics:
            entry = tally.setdefault(topic, {"topic": topic, "count": 0, "negative": 0})
            entry["count"] += 1
            if sentiment == "negative":
                entry["negative"] += 1
    return sorted(tally.values(), key=lambda e: e["count"], reverse=True)[:limit]


def detect_spikes(session: Session, days: int = 30) -> list:
    """mean + 3 sigma against a rolling baseline. Absolute thresholds break.

    Leading empty days are dropped before any baseline is computed. They mean
    "capture had not started yet", not "volume was zero", and averaging them in
    gives a near-zero baseline against which the first real day of traffic
    looks like a spike -- which would make a freshly deployed system alarm
    constantly on its own arrival.
    """
    series = volume_by_day(session, days=days)

    first_active = next(
        (index for index, point in enumerate(series) if point["total"]), None
    )
    if first_active is None:
        return []
    series = series[first_active:]

    spikes = []
    for index, point in enumerate(series):
        window = [p["total"] for p in series[max(0, index - BASELINE_DAYS) : index]]
        if len(window) < 5:
            continue
        mean = statistics.fmean(window)
        sigma = statistics.pstdev(window) or 0.0
        threshold = mean + SPIKE_SIGMA * sigma
        if sigma > 0 and point["total"] > threshold:
            spikes.append(
                {
                    "date": point["date"],
                    "total": point["total"],
                    "baseline_mean": round(mean, 2),
                    "threshold": round(threshold, 2),
                }
            )
    return spikes


def feed(
    session: Session,
    sentiment: Optional[str] = None,
    min_severity: Optional[int] = None,
    video: Optional[str] = None,
    include_spam: bool = False,
    limit: int = 60,
) -> list:
    latest = latest_enrichment_subquery()
    query = (
        select(Mention, Enrichment)
        .join(latest, latest.c.mention_id == Mention.id)
        .join(Enrichment, Enrichment.id == latest.c.enrichment_id)
        .order_by(Enrichment.severity.desc(), Mention.published_at.desc())
    )
    if not include_spam:
        query = query.where(Enrichment.is_spam.is_(False))
    if sentiment:
        query = query.where(Enrichment.sentiment == sentiment)
    if min_severity:
        query = query.where(Enrichment.severity >= min_severity)
    if video:
        query = query.join(Video, Video.id == Mention.video_id).where(
            Video.youtube_id == video
        )

    results = []
    for mention, enrichment in session.execute(query.limit(limit)).all():
        try:
            topics = json.loads(enrichment.topics or "[]")
        except json.JSONDecodeError:
            topics = []
        results.append(
            {
                "mention": mention,
                "enrichment": enrichment,
                "topics": topics,
                "needs_review": (enrichment.confidence or 1) < 0.5,
            }
        )
    return results


def estimated_reach(session: Session) -> int:
    """A model. YouTube views are real, but a view is not a read of a comment."""
    rows = session.execute(
        select(Video.view_count, Video.comment_count).where(Video.purged_at.is_(None))
    ).all()
    return int(sum((views or 0) for views, _ in rows))
