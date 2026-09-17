"""ORM models for YouTube listening.

Annotations here use Optional[X] rather than `X | None` on purpose: SQLAlchemy
evaluates every Mapped[...] annotation at runtime, and PEP 604 unions raise
TypeError when that happens on Python 3.9. tests/test_python_compat.py enforces
it.

The shape worth understanding before reading further:

    Channel   a channel we poll (ours, a peer's, a critic's)
      |
    Video     the container. Holds stats and whether comments are even open.
      |
    Mention   the unit of listening -- a comment, a reply, or a video whose own
              title/description mentions a watch term. Everything downstream
              (matching, enrichment, severity, alerts) operates on Mention, so
              the pipeline does not care which kind it got.

Retention is modelled, not documented. YouTube's Developer Policies require
stored API data to be deleted or refreshed within 30 calendar days, with a
carve-out letting derived metrics live up to 36 months. So:

  * text-bearing columns carry `content_expires_at` and are purged or refreshed
    before that date (see retention.py)
  * MetricSnapshot holds counts and no text at all, which is precisely what the
    36-month carve-out permits -- the separation is the mechanism, not a note
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
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

MATCH_TYPES = ("literal", "phrase", "regex", "boolean")
MENTION_KINDS = ("video", "comment", "reply")
PROVENANCE = ("live", "demo", "manual")
SENTIMENTS = ("positive", "neutral", "negative", "mixed")
INTENTS = (
    "complaint",
    "question",
    "praise",
    "recommendation",
    "comparison",
    "enquiry",
    "abuse",
    "other",
)

# YouTube API Services Developer Policies: stored API data must be deleted or
# refreshed within 30 calendar days.
CONTENT_RETENTION_DAYS = 30
# The derived-metrics carve-out, for counts only.
METRIC_RETENTION_DAYS = 36 * 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_content_expiry() -> datetime:
    return utcnow() + timedelta(days=CONTENT_RETENTION_DAYS)


def hash_author(channel_id: Optional[str]) -> Optional[str]:
    """Pseudonymous, stable author key that outlives the display name.

    Lets "is this the same commenter again?" keep working after the name and
    text have been purged under the 30-day rule.
    """
    if not channel_id:
        return None
    return hashlib.sha256(channel_id.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class RetainedContent:
    """Mixin for tables holding text that falls under the 30-day policy."""

    content_expires_at: Mapped[datetime] = mapped_column(
        DateTime, default=default_content_expiry, nullable=False
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def is_purged(self) -> bool:
        return self.purged_at is not None

    def days_until_expiry(self, now: Optional[datetime] = None) -> int:
        delta = self.content_expires_at - (now or utcnow())
        return max(delta.days, 0)


class WatchTerm(Base):
    """What we are listening for."""

    __tablename__ = "watch_term"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), default="phrase", nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    negative_pattern: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Whether this term is worth spending 100 units a search on.
    use_in_discovery: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    matches: Mapped[list["Match"]] = relationship(back_populates="term")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WatchTerm {self.label!r}>"


class Channel(Base):
    """A channel on the harvest loop.

    `is_owned` matters: on your own channel you may reasonably poll every video
    every cycle, whereas a peer's channel is monitored more cheaply.
    """

    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(primary_key=True)
    youtube_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    uploads_playlist_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_owned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel")


class Video(Base, RetainedContent):
    """The container we harvest comments from."""

    __tablename__ = "video"

    id: Mapped[int] = mapped_column(primary_key=True)
    youtube_id: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    channel_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("channel.id", ondelete="SET NULL"), nullable=True
    )
    channel_youtube_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    channel_title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    title: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    view_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    like_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # A very common real-world state, and not an error (§ commentsDisabled).
    comments_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Cursor for incremental comment harvesting.
    last_comment_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    newest_comment_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    provenance: Mapped[str] = mapped_column(String(16), default="live", nullable=False)

    channel: Mapped[Optional[Channel]] = relationship(back_populates="videos")
    mentions: Mapped[list["Mention"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_video_published", "published_at"),
        Index("ix_video_harvest", "comments_disabled", "last_comment_poll_at"),
        Index("ix_video_expiry", "content_expires_at", "purged_at"),
    )

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.youtube_id}"

    @property
    def display_title(self) -> str:
        if self.is_purged:
            return "[content purged under the 30-day retention policy]"
        return self.title or "(untitled)"


class Mention(Base, RetainedContent):
    """One comment, reply, or video whose own metadata mentions a watch term."""

    __tablename__ = "mention"

    id: Mapped[int] = mapped_column(primary_key=True)
    youtube_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    video_id: Mapped[int] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"), nullable=False
    )
    parent_youtube_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    author_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    author_channel_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    author_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    like_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # YouTube exposes updatedAt directly, so edits are cheap to detect -- unlike
    # Reddit, where you diff a fuzzy `edited` field.
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    is_deleted_upstream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provenance: Mapped[str] = mapped_column(String(16), default="live", nullable=False)

    video: Mapped[Video] = relationship(back_populates="mentions")
    matches: Mapped[list["Match"]] = relationship(
        back_populates="mention", cascade="all, delete-orphan"
    )
    enrichments: Mapped[list["Enrichment"]] = relationship(
        back_populates="mention", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="mention", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_mention_published", "published_at"),
        Index("ix_mention_video", "video_id", "published_at"),
        Index("ix_mention_expiry", "content_expires_at", "purged_at"),
    )

    @property
    def display_text(self) -> str:
        if self.is_purged:
            return "[content purged under the 30-day retention policy]"
        return self.text or ""

    @property
    def url(self) -> str:
        base = self.video.url if self.video else "https://www.youtube.com"
        if self.kind == "video":
            return base
        return f"{base}&lc={self.youtube_id}"

    @property
    def latest_enrichment(self) -> Optional["Enrichment"]:
        if not self.enrichments:
            return None
        return max(self.enrichments, key=lambda e: e.created_at)


class Match(Base):
    """Which terms a mention hit, and the span that proved it."""

    __tablename__ = "term_match"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(ForeignKey("mention.id", ondelete="CASCADE"))
    term_id: Mapped[int] = mapped_column(ForeignKey("watch_term.id", ondelete="CASCADE"))
    matched_text: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)

    mention: Mapped[Mention] = relationship(back_populates="matches")
    term: Mapped[WatchTerm] = relationship(back_populates="matches")

    __table_args__ = (UniqueConstraint("mention_id", "term_id", name="uq_term_match"),)


class Enrichment(Base):
    """Classifier output, versioned so a model change is re-runnable."""

    __tablename__ = "enrichment"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(ForeignKey("mention.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)

    sentiment: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    severity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    topics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_spam: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    mention: Mapped[Mention] = relationship(back_populates="enrichments")

    __table_args__ = (
        UniqueConstraint("mention_id", "model_name", "model_version", name="uq_enrichment"),
    )


class MetricSnapshot(Base):
    """Counts only -- deliberately no text.

    This table is what the derived-metrics carve-out permits keeping for up to
    36 months. Putting a single text column here would forfeit that, so don't.
    """

    __tablename__ = "metric_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(ForeignKey("mention.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    like_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    mention: Mapped[Mention] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_snapshot_mention_time", "mention_id", "observed_at"),)


class Alert(Base):
    """A raised notification. One per video thread, not per comment."""

    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mention.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    mention: Mapped[Optional[Mention]] = relationship()


class QuotaSpend(Base):
    """The persisted ledger, one row per method per quota day.

    In the database rather than in memory so a restart cannot lose the day's
    spend and then blow the budget.
    """

    __tablename__ = "quota_spend"

    id: Mapped[int] = mapped_column(primary_key=True)
    quota_date: Mapped[Date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("quota_date", "method", name="uq_quota_spend"),
        Index("ix_quota_spend_date", "quota_date"),
    )
