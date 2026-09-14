"""Metric definitions (§11).

These are fixed deliberately and early, because changing a definition later
invalidates history. Two honesty rules carried over from the specification:

  * Reach is a MODEL, not a measurement. Reddit gives no impression data, so
    every reach figure here is labelled as an estimate in the UI.
  * Scores are fuzzed by Reddit's anti-spam (§7.3), so they inform ranking and
    thresholds but are never presented as precise.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Alert, Enrichment, Item, Match, Term

SPIKE_SIGMA = 3.0
BASELINE_DAYS = 14


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Totals:
    items: int
    matched_items: int
    tombstoned: int
    spam_filtered: int
    alerts_open: int
    escalated: int
    negative: int
    neutral: int
    positive: int

    @property
    def classified(self) -> int:
        return self.negative + self.neutral + self.positive

    @property
    def net_sentiment(self) -> float:
        """(positive - negative) / total. Reported with the classifier version."""
        if not self.classified:
            return 0.0
        return (self.positive - self.negative) / self.classified


def latest_enrichment_subquery():
    """Most recent enrichment per item -- enrichment is versioned (§6)."""
    return (
        select(
            Enrichment.item_id.label("item_id"),
            func.max(Enrichment.id).label("enrichment_id"),
        )
        .group_by(Enrichment.item_id)
        .subquery()
    )


def totals(session: Session) -> Totals:
    latest = latest_enrichment_subquery()
    rows = list(
        session.execute(
            select(Enrichment.sentiment, Enrichment.is_spam, Enrichment.severity, Enrichment.confidence)
            .join(latest, Enrichment.id == latest.c.enrichment_id)
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
        items=session.scalar(select(func.count(Item.id))) or 0,
        matched_items=session.scalar(select(func.count(func.distinct(Match.item_id)))) or 0,
        tombstoned=session.scalar(
            select(func.count(Item.id)).where(Item.purged_at.isnot(None))
        ) or 0,
        spam_filtered=spam,
        alerts_open=session.scalar(
            select(func.count(Alert.id)).where(Alert.acknowledged_at.is_(None))
        ) or 0,
        escalated=escalated,
        negative=counts["negative"],
        neutral=counts["neutral"],
        positive=counts["positive"],
    )


def volume_by_day(session: Session, days: int = 21) -> list[dict]:
    """Mention volume per day. Tombstoned items keep their count (§11)."""
    since = _now() - timedelta(days=days)
    rows = session.execute(
        select(Item.created_utc, Item.id).where(Item.created_utc >= since)
    ).all()

    latest = latest_enrichment_subquery()
    sentiment_by_item = dict(
        session.execute(
            select(Enrichment.item_id, Enrichment.sentiment)
            .join(latest, Enrichment.id == latest.c.enrichment_id)
            .where(Enrichment.is_spam.is_(False))
        ).all()
    )

    buckets: dict[str, dict] = {}
    for day_offset in range(days + 1):
        key = (since + timedelta(days=day_offset)).date().isoformat()
        buckets[key] = {"date": key, "total": 0, "negative": 0, "neutral": 0, "positive": 0}

    for created, item_id in rows:
        key = created.date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        sentiment = sentiment_by_item.get(item_id)
        if sentiment in ("negative", "neutral", "positive"):
            bucket[sentiment] += 1

    return list(buckets.values())


def share_of_voice(session: Session) -> list[dict]:
    """Each term's mentions as a share of all matched mentions."""
    rows = session.execute(
        select(Term.label, func.count(Match.id))
        .join(Match, Match.term_id == Term.id)
        .group_by(Term.label)
        .order_by(func.count(Match.id).desc())
    ).all()
    total = sum(count for _, count in rows) or 1
    return [
        {"label": label, "count": count, "share": count / total} for label, count in rows
    ]


def top_subreddits(session: Session, limit: int = 6) -> list[dict]:
    rows = session.execute(
        select(Item.subreddit, func.count(Item.id))
        .group_by(Item.subreddit)
        .order_by(func.count(Item.id).desc())
        .limit(limit)
    ).all()
    return [{"subreddit": name, "count": count} for name, count in rows]


def topic_breakdown(session: Session, limit: int = 6) -> list[dict]:
    """Aspect counts -- what the complaints are actually about (§9.2)."""
    latest = latest_enrichment_subquery()
    rows = session.execute(
        select(Enrichment.topics, Enrichment.sentiment)
        .join(latest, Enrichment.id == latest.c.enrichment_id)
        .where(Enrichment.is_spam.is_(False))
    ).all()
    tally: dict[str, dict] = {}
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
    ordered = sorted(tally.values(), key=lambda e: e["count"], reverse=True)
    return ordered[:limit]


def detect_spikes(session: Session, days: int = 21) -> list[dict]:
    """Flag days exceeding mean + 3σ of the trailing baseline (§10).

    Absolute thresholds break the moment volume changes, so the baseline is
    rolling.
    """
    series = volume_by_day(session, days=days)
    spikes: list[dict] = []
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
    sentiment: str | None = None,
    min_severity: int | None = None,
    subreddit: str | None = None,
    limit: int = 60,
) -> list[dict]:
    """The reviewable feed. §15 phase 2 -- this matters more than a dashboard."""
    latest = latest_enrichment_subquery()
    query = (
        select(Item, Enrichment)
        .join(latest, latest.c.item_id == Item.id)
        .join(Enrichment, Enrichment.id == latest.c.enrichment_id)
        .where(Enrichment.is_spam.is_(False))
        .order_by(Enrichment.severity.desc(), Item.created_utc.desc())
    )
    if sentiment:
        query = query.where(Enrichment.sentiment == sentiment)
    if min_severity:
        query = query.where(Enrichment.severity >= min_severity)
    if subreddit:
        query = query.where(Item.subreddit == subreddit)

    rows = session.execute(query.limit(limit)).all()
    results = []
    for item, enrichment in rows:
        try:
            topics = json.loads(enrichment.topics or "[]")
        except json.JSONDecodeError:
            topics = []
        results.append(
            {
                "item": item,
                "enrichment": enrichment,
                "topics": topics,
                "matches": item.matches,
                "needs_review": (enrichment.confidence or 1) < 0.5,
            }
        )
    return results


def estimated_reach(session: Session) -> int:
    """A MODEL, not a measurement -- Reddit publishes no impression data.

    Deliberately crude and labelled as an estimate wherever it is shown.
    """
    rows = session.execute(select(Item.score, Item.num_comments)).all()
    return int(sum((score or 0) * 3 + (comments or 0) * 12 for score, comments in rows))
