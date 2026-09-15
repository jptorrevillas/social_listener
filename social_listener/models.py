"""ORM models, following the schema in docs/SPECIFICATION.md §6.

The specification's DDL targets MySQL 8. These models are written to the
portable subset so the demo runs on SQLite unchanged; the only deliberate
divergence is that ENUM columns are plain strings with a CHECK-free Python-side
vocabulary, which SQLite handles and MySQL will accept.

Annotations here use Optional[X] rather than `X | None` on purpose. SQLAlchemy
evaluates every Mapped[...] annotation at runtime to build the column, and
`from __future__ import annotations` does not save you from that -- it only
defers the evaluation. PEP 604 unions raise TypeError when that evaluation
happens on Python 3.9, so this file stays on the older spelling and the project
runs on 3.9 as well as 3.13. tests/test_python_compat.py enforces it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Vocabularies. Kept in Python rather than as database ENUMs so that adding a
# value is a code change, not a migration.
MATCH_TYPES = ("literal", "phrase", "regex", "boolean")
ITEM_KINDS = ("post", "comment")
PROVENANCE = ("live", "demo", "archive", "manual")
SENTIMENTS = ("positive", "neutral", "negative", "mixed")
INTENTS = (
    "complaint",
    "question",
    "recommendation",
    "comparison",
    "news",
    "purchase_intent",
    "other",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_author(author: Optional[str]) -> Optional[str]:
    """Stable pseudonymous handle for analytics that outlive the raw username.

    §6 keeps this alongside `author` so the handle can be dropped on a deletion
    or privacy request without destroying the aggregates built on it.
    """
    if not author:
        return None
    return hashlib.sha256(author.lower().encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class Term(Base):
    """What we are listening for."""

    __tablename__ = "listening_term"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), default="phrase", nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    # Exclusions. §8 -- users need NOT more than they expect.
    negative_pattern: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    matches: Mapped[list["Match"]] = relationship(back_populates="term")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Term {self.label!r} ({self.match_type})>"


class Subreddit(Base):
    """A community on the fast poll loop."""

    __tablename__ = "listening_subreddit"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    poll_interval_secs: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    # Measured from live data; drives the interval (§5.2).
    observed_items_hour: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_fullname_seen: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Item(Base):
    """One captured post or comment.

    `fullname` is the natural key and the structural dedupe mechanism -- every
    endpoint returns it, it is globally unique, and it is stable. §6.
    """

    __tablename__ = "listening_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    fullname: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    subreddit: Mapped[str] = mapped_column(String(80), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    author_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    permalink: Mapped[str] = mapped_column(String(400), nullable=False)
    parent_fullname: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    link_fullname: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    is_removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    provenance: Mapped[str] = mapped_column(String(16), default="live", nullable=False)

    matches: Mapped[list["Match"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    enrichments: Mapped[list["Enrichment"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_listening_item_created", "created_utc"),
        Index("ix_listening_item_sub_created", "subreddit", "created_utc"),
        Index("ix_listening_item_revisit", "last_checked_at", "is_deleted"),
    )

    @property
    def is_tombstoned(self) -> bool:
        return self.purged_at is not None

    @property
    def display_text(self) -> str:
        """What a reviewer sees. Tombstoned content is never rendered."""
        if self.is_tombstoned:
            return "[removed from Reddit -- content purged]"
        return " ".join(filter(None, (self.title, self.body)))

    @property
    def latest_enrichment(self) -> Optional["Enrichment"]:
        if not self.enrichments:
            return None
        return max(self.enrichments, key=lambda e: e.created_at)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Item {self.fullname} r/{self.subreddit}>"


class Match(Base):
    """Which terms an item hit. An item can match several."""

    __tablename__ = "listening_match"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("listening_item.id", ondelete="CASCADE"))
    term_id: Mapped[int] = mapped_column(ForeignKey("listening_term.id", ondelete="CASCADE"))
    # The span that hit. §8 -- without it reviewers cannot see why an item was
    # flagged, and stop trusting the feed.
    matched_text: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)

    item: Mapped[Item] = relationship(back_populates="matches")
    term: Mapped[Term] = relationship(back_populates="matches")

    __table_args__ = (UniqueConstraint("item_id", "term_id", name="uq_listening_match"),)


class Enrichment(Base):
    """Model output, versioned so a model change is re-runnable and auditable."""

    __tablename__ = "listening_enrichment"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("listening_item.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)

    sentiment: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    severity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    topics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-encoded
    is_spam: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    item: Mapped[Item] = relationship(back_populates="enrichments")

    __table_args__ = (
        UniqueConstraint("item_id", "model_name", "model_version", name="uq_enrichment"),
    )


class MetricSnapshot(Base):
    """Score/comment history, for velocity and spike detection (§11)."""

    __tablename__ = "listening_metric_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("listening_item.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    item: Mapped[Item] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_snapshot_item_time", "item_id", "observed_at"),)


class Alert(Base):
    """A delivered (or pending) notification. §10."""

    __tablename__ = "listening_alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("listening_item.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # severity | spike
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    item: Mapped[Optional[Item]] = relationship()
