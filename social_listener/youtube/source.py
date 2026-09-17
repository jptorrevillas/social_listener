"""The seam between the live API and the demo corpus.

Both sides return the same normalised dicts, so the harvester, matcher,
classifier and retention job are identical in either mode. Going live is an
API-key change, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..config import settings


class Source(ABC):
    mode = "abstract"

    @abstractmethod
    def search_videos(self, query: str, published_after: Optional[str] = None) -> list[dict]:
        """Discovery. On the live source this is the 100-unit call."""

    @abstractmethod
    def channel_videos(self, channel: dict) -> list[dict]:
        """A tracked channel's recent uploads. 1 unit on the live source."""

    @abstractmethod
    def video_comments(self, video_youtube_id: str, max_pages: int = 3) -> list[dict]:
        """Comments and inline replies for one video. 1 unit per page."""

    @abstractmethod
    def refresh_videos(self, video_ids: list[str]) -> list[dict]:
        """Re-fetch video metadata. Absentees are gone from YouTube."""

    @abstractmethod
    def refresh_comments(self, comment_ids: list[str]) -> list[dict]:
        """Re-fetch comments. Absentees are gone from YouTube."""

    def close(self) -> None:  # pragma: no cover
        pass


class LiveYouTubeSource(Source):
    mode = "live"

    def __init__(self, client) -> None:
        self.client = client

    def search_videos(self, query: str, published_after: Optional[str] = None) -> list[dict]:
        from .normalise import normalise_video

        raw = self.client.search_videos(query, published_after=published_after)
        videos = [normalise_video(item) for item in raw]
        return [v for v in videos if v["youtube_id"]]

    def channel_videos(self, channel: dict) -> list[dict]:
        from .normalise import normalise_video

        playlist = channel.get("uploads_playlist_id")
        if not playlist:
            return []
        raw = self.client.channel_uploads(playlist)
        videos = [normalise_video(item) for item in raw]
        return [v for v in videos if v["youtube_id"]]

    def video_comments(self, video_youtube_id: str, max_pages: int = 3) -> list[dict]:
        from .normalise import normalise_thread

        out: list[dict] = []
        for thread in self.client.comment_threads(video_youtube_id, max_pages=max_pages):
            out.extend(normalise_thread(thread))
        # A thread's videoId is absent on some payloads; fill it from the request.
        for row in out:
            row["video_youtube_id"] = row.get("video_youtube_id") or video_youtube_id
        return out

    def refresh_videos(self, video_ids: list[str]) -> list[dict]:
        from .normalise import normalise_video

        return [normalise_video(item) for item in self.client.videos(video_ids)]

    def refresh_comments(self, comment_ids: list[str]) -> list[dict]:
        from .normalise import _normalise_comment_snippet

        out = []
        for item in self.client.comments(comment_ids):
            snippet = item.get("snippet", {}) or {}
            kind = "reply" if snippet.get("parentId") else "comment"
            out.append(
                _normalise_comment_snippet(
                    item.get("id"), snippet, kind, snippet.get("videoId", ""), "live"
                )
            )
        return out

    def close(self) -> None:
        self.client.close()


class DemoSource(Source):
    """Replays the synthetic corpus. No key, no network, no quota spent."""

    mode = "demo"

    def __init__(self, corpus=None) -> None:
        from ..demo.corpus import build_corpus

        self.corpus = corpus if corpus is not None else build_corpus()
        self.videos = self.corpus["videos"]
        self.comments = self.corpus["comments"]

    def search_videos(self, query: str, published_after: Optional[str] = None) -> list[dict]:
        needle = query.lower()
        return [
            video
            for video in self.videos
            if needle in f"{video.get('title') or ''} {video.get('description') or ''}".lower()
        ]

    def channel_videos(self, channel: dict) -> list[dict]:
        return [
            video
            for video in self.videos
            if video["channel_youtube_id"] == channel.get("youtube_id")
        ]

    def video_comments(self, video_youtube_id: str, max_pages: int = 3) -> list[dict]:
        video = next(
            (v for v in self.videos if v["youtube_id"] == video_youtube_id), None
        )
        # Mirrors the live client swallowing CommentsDisabled and returning [].
        if video is not None and video.get("_comments_disabled"):
            return []
        return [
            comment
            for comment in self.comments
            if comment["video_youtube_id"] == video_youtube_id
        ]

    def refresh_videos(self, video_ids: list[str]) -> list[dict]:
        wanted = set(video_ids)
        return [
            v
            for v in self.videos
            if v["youtube_id"] in wanted and not v.get("_deleted_upstream")
        ]

    def refresh_comments(self, comment_ids: list[str]) -> list[dict]:
        wanted = set(comment_ids)
        return [
            c
            for c in self.comments
            if c["youtube_id"] in wanted and not c.get("_deleted_upstream")
        ]


def get_source(session=None) -> Source:
    """Live when an API key exists, demo until then.

    The live client needs the session to charge the quota ledger, which is why
    this takes one.
    """
    if not settings.has_api_key:
        return DemoSource()

    from .. import ledger
    from .client import YouTubeClient

    if session is None:
        raise ValueError("live mode needs a database session for quota accounting")

    return LiveYouTubeSource(
        YouTubeClient(
            charge=lambda method, calls=1: ledger.charge(session, method, calls),
            reserve=lambda method: ledger.reserve(session, method),
        )
    )
