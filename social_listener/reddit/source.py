"""The seam between live Reddit and the demo corpus.

Both sources yield the same normalised dicts, so ingest.py, matching, enrichment
and the compliance sweep are identical in either mode. Switching to live data is
a credentials change, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..config import settings


def _to_datetime(epoch: float | None) -> datetime:
    if not epoch:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(tzinfo=None)


def normalise(raw: dict[str, Any], kind: str, provenance: str = "live") -> dict[str, Any]:
    """Map a Reddit API object onto our one schema (§5).

    Comments carry `body`; submissions carry `title` + `selftext`. Everything
    else is shared.
    """
    fullname = raw.get("name")
    if not fullname:
        prefix = "t1_" if kind == "comment" else "t3_"
        fullname = f"{prefix}{raw.get('id', '')}"

    edited = raw.get("edited")
    edited_at = _to_datetime(edited) if isinstance(edited, (int, float)) and edited else None

    author = raw.get("author")
    if author in ("[deleted]", "[removed]"):
        author = None

    return {
        "fullname": fullname,
        "kind": kind,
        "subreddit": raw.get("subreddit", ""),
        "author": author,
        "title": raw.get("title") if kind == "post" else None,
        "body": raw.get("selftext") if kind == "post" else raw.get("body"),
        "permalink": f"https://reddit.com{raw.get('permalink', '')}"
        if raw.get("permalink", "").startswith("/")
        else raw.get("permalink", ""),
        "parent_fullname": raw.get("parent_id"),
        "link_fullname": raw.get("link_id"),
        "created_utc": _to_datetime(raw.get("created_utc")),
        "score": raw.get("score"),
        "num_comments": raw.get("num_comments"),
        "edited_at": edited_at,
        "provenance": provenance,
    }


class Source(ABC):
    """What ingest.py needs from a data source, and nothing more."""

    mode: str = "abstract"

    @abstractmethod
    def fetch_subreddit(self, subreddit: str, limit: int = 100) -> list[dict[str, Any]]:
        """Recent posts and comments from one community."""

    @abstractmethod
    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """Site-wide keyword search, for the discovery loop (§5.1)."""

    @abstractmethod
    def hydrate(self, fullnames: list[str]) -> list[dict[str, Any]]:
        """Re-fetch by fullname. Items absent from the result were deleted."""

    def close(self) -> None:  # pragma: no cover - default no-op
        pass


class LiveRedditSource(Source):
    mode = "live"

    def __init__(self, client=None) -> None:
        from .client import RedditClient

        self.client = client or RedditClient()

    def fetch_subreddit(self, subreddit: str, limit: int = 100) -> list[dict[str, Any]]:
        posts = [
            normalise(raw, "post") for raw in self.client.new_posts(subreddit, limit)
        ]
        comments = [
            normalise(raw, "comment") for raw in self.client.new_comments(subreddit, limit)
        ]
        return posts + comments

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        return [normalise(raw, "post") for raw in self.client.search(query, limit=limit)]

    def hydrate(self, fullnames: list[str]) -> list[dict[str, Any]]:
        out = []
        for raw in self.client.info(fullnames):
            kind = "comment" if (raw.get("name") or "").startswith("t1_") else "post"
            out.append(normalise(raw, kind))
        return out

    def close(self) -> None:
        self.client.close()


class DemoSource(Source):
    """Replays the synthetic corpus. No network, no credentials, no API budget."""

    mode = "demo"

    def __init__(self, corpus: list[dict[str, Any]] | None = None) -> None:
        from ..demo.corpus import build_corpus

        self.corpus = corpus if corpus is not None else build_corpus()

    def fetch_subreddit(self, subreddit: str, limit: int = 100) -> list[dict[str, Any]]:
        matching = [item for item in self.corpus if item["subreddit"] == subreddit]
        return matching[:limit]

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        needle = query.lower()
        hits = [
            item
            for item in self.corpus
            if needle in f"{item.get('title') or ''} {item.get('body') or ''}".lower()
        ]
        return hits[:limit]

    def hydrate(self, fullnames: list[str]) -> list[dict[str, Any]]:
        wanted = set(fullnames)
        # Items the corpus marks as deleted are simply absent, exactly as the
        # real /api/info behaves -- which is what the compliance sweep detects.
        return [
            item
            for item in self.corpus
            if item["fullname"] in wanted and not item.get("_deleted_upstream")
        ]


def get_source() -> Source:
    """Live when credentials exist, demo until then."""
    if settings.has_reddit_credentials:
        return LiveRedditSource()
    return DemoSource()
