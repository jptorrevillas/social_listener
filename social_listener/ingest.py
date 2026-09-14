"""Normalise -> dedupe -> match -> enrich -> store, plus alert generation (§5).

The one structural rule: `fullname` is the dedupe key and the database enforces
it. Every ingestion design generates duplicates by construction -- retries, and
the deliberately overlapping poll windows that guarantee no gaps -- so dedupe
has to be structural, not best-effort.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enrichment import DEFAULT_ENRICHER, Enricher
from .matching import Matcher
from .models import Alert, Enrichment, Item, Match, MetricSnapshot, Subreddit, Term, hash_author
from .reddit.source import Source

log = logging.getLogger(__name__)

# §10: severity 4-5 pages a human.
ALERT_SEVERITY_THRESHOLD = 4


@dataclass
class IngestStats:
    fetched: int = 0
    new_items: int = 0
    duplicates: int = 0
    updated: int = 0
    matched: int = 0
    unmatched: int = 0
    spam_filtered: int = 0
    escalated: int = 0
    alerts: int = 0
    threads_alerted: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        data["threads_alerted"] = len(self.threads_alerted)
        return data


def upsert_item(session: Session, payload: dict, stats: IngestStats) -> tuple[Item, bool]:
    """Insert, or update the mutable fields of an item we already hold.

    Returns (item, is_new). Score, comment count and body are all mutable on
    Reddit (§7), so a re-fetch is an update rather than a no-op.
    """
    existing = session.scalar(select(Item).where(Item.fullname == payload["fullname"]))
    if existing is not None:
        stats.duplicates += 1
        changed = False
        for column in ("score", "num_comments", "body", "title", "edited_at"):
            new_value = payload.get(column)
            if new_value is not None and getattr(existing, column) != new_value:
                setattr(existing, column, new_value)
                changed = True
        existing.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if changed:
            stats.updated += 1
        session.add(
            MetricSnapshot(
                item=existing,
                observed_at=existing.last_checked_at,
                score=payload.get("score"),
                num_comments=payload.get("num_comments"),
            )
        )
        return existing, False

    item = Item(
        fullname=payload["fullname"],
        kind=payload["kind"],
        subreddit=payload["subreddit"],
        author=payload.get("author"),
        author_hash=hash_author(payload.get("author")),
        title=payload.get("title"),
        body=payload.get("body"),
        permalink=payload.get("permalink", ""),
        parent_fullname=payload.get("parent_fullname"),
        link_fullname=payload.get("link_fullname"),
        created_utc=payload["created_utc"],
        score=payload.get("score"),
        num_comments=payload.get("num_comments"),
        edited_at=payload.get("edited_at"),
        provenance=payload.get("provenance", "live"),
        last_checked_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(item)
    session.flush()
    session.add(
        MetricSnapshot(
            item=item,
            observed_at=item.last_checked_at,
            score=item.score,
            num_comments=item.num_comments,
        )
    )
    stats.new_items += 1
    return item, True


def enrich_item(session: Session, item: Item, enricher: Enricher, stats: IngestStats) -> None:
    """Classify, unless this exact model version already ran on this item."""
    # Check the relationship collection, not a SELECT: with autoflush off, a
    # pending enrichment added earlier in this same batch is not yet visible to
    # a query, and the duplicate would hit the unique constraint on commit.
    for existing in item.enrichments:
        if (
            existing.model_name == enricher.model_name
            and existing.model_version == enricher.model_version
        ):
            return

    result = enricher.enrich(
        {
            "title": item.title,
            "body": item.body,
            "author": item.author,
            "score": item.score,
            "num_comments": item.num_comments,
        }
    )
    if result.is_spam:
        stats.spam_filtered += 1
    if result.needs_expensive_lane:
        stats.escalated += 1

    session.add(
        Enrichment(
            item=item,
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
        _raise_alert(session, item, result, stats)


def _raise_alert(session: Session, item: Item, result, stats: IngestStats) -> None:
    """One alert per thread, not per item (§10)."""
    thread_key = item.link_fullname or item.fullname
    if thread_key in stats.threads_alerted:
        return
    if session.scalar(
        select(Alert).where(Alert.item_id == item.id, Alert.kind == "severity")
    ) is not None:
        return
    stats.threads_alerted.add(thread_key)
    stats.alerts += 1
    # A distinguishing snippet, so six alerts do not all read identically.
    snippet = (item.title or item.body or "").strip().replace("\n", " ")
    if len(snippet) > 70:
        snippet = snippet[:70].rsplit(" ", 1)[0] + "..."
    headline = f"r/{item.subreddit}: {snippet}" if snippet else f"r/{item.subreddit} mention"
    session.add(
        Alert(
            item=item,
            kind="severity",
            severity=result.severity,
            headline=headline[:400],
            detail=result.rationale,
        )
    )


def ingest_payloads(
    session: Session,
    payloads: list[dict],
    enricher: Enricher | None = None,
) -> IngestStats:
    """Run a batch through the full pipeline."""
    enricher = enricher or DEFAULT_ENRICHER
    stats = IngestStats()
    terms = list(session.scalars(select(Term).where(Term.is_active.is_(True))))
    matcher = Matcher(terms)
    terms_by_label = {t.label: t for t in terms}

    for payload in payloads:
        stats.fetched += 1
        text = " ".join(filter(None, (payload.get("title"), payload.get("body"))))
        hits = matcher.match(text)
        if not hits:
            stats.unmatched += 1
            continue

        item, _is_new = upsert_item(session, payload, stats)
        stats.matched += 1

        # Same reasoning as above: read the collection so pending matches from
        # earlier in this batch count.
        seen_term_ids = {m.term_id for m in item.matches if m.term_id is not None}
        seen_term_ids.update(
            m.term.id for m in item.matches if m.term_id is None and m.term is not None
        )
        for hit in hits:
            term = terms_by_label.get(hit.term_label)
            if term is None or term.id in seen_term_ids:
                continue
            seen_term_ids.add(term.id)
            session.add(
                Match(
                    item=item,
                    term=term,
                    matched_text=hit.matched_text[:400],
                    confidence=hit.confidence,
                )
            )

        enrich_item(session, item, enricher, stats)

    session.flush()
    return stats


def poll_subreddits(session: Session, source: Source, limit: int = 100) -> IngestStats:
    """The primary capture path (§5.2)."""
    subreddits = list(session.scalars(select(Subreddit).where(Subreddit.is_active.is_(True))))
    payloads: list[dict] = []
    for sub in subreddits:
        payloads.extend(source.fetch_subreddit(sub.name, limit=limit))
        sub.last_polled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return ingest_payloads(session, payloads)
