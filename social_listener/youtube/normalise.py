"""Map YouTube API shapes onto our two internal shapes.

Kept apart from the client so it can be tested against recorded payloads with
no HTTP involved, and so the demo source can reuse it unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """RFC 3339 -> naive UTC datetime.

    YouTube returns 'Z'-suffixed timestamps; Python 3.9's fromisoformat cannot
    parse the Z, so it is normalised first.
    """
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_video_id(raw: dict, snippet: dict) -> Optional[str]:
    """The video id sits in a different place per endpoint, and the wrong one
    is often present and truthy -- so dispatch on the payload's own `kind`
    rather than probing fields in hope.

      videos.list        id is the video id (a string)
      search.list        id is {"kind": "...#video", "videoId": ...}
      playlistItems.list id is the PLAYLIST ITEM id; the video id lives in
                         contentDetails.videoId / snippet.resourceId.videoId
    """
    kind = raw.get("kind", "")
    raw_id = raw.get("id")

    if kind.endswith("#playlistItem"):
        return (raw.get("contentDetails", {}) or {}).get("videoId") or (
            snippet.get("resourceId", {}) or {}
        ).get("videoId")

    if isinstance(raw_id, dict):
        return raw_id.get("videoId")

    # No kind to go on: prefer an explicit videoId wherever it appears, since a
    # bare `id` may belong to a different resource.
    explicit = (raw.get("contentDetails", {}) or {}).get("videoId") or (
        snippet.get("resourceId", {}) or {}
    ).get("videoId")
    if explicit:
        return explicit
    return raw_id if isinstance(raw_id, str) else None


def normalise_video(raw: dict, provenance: str = "live") -> dict:
    """A videos.list / search.list / playlistItems.list item -> our video dict."""
    snippet = raw.get("snippet", {}) or {}
    stats = raw.get("statistics", {}) or {}

    video_id = _extract_video_id(raw, snippet)

    return {
        "youtube_id": video_id,
        "channel_youtube_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "published_at": parse_timestamp(snippet.get("publishedAt"))
        or datetime.now(timezone.utc).replace(tzinfo=None),
        "view_count": _int_or_none(stats.get("viewCount")),
        "like_count": _int_or_none(stats.get("likeCount")),
        "comment_count": _int_or_none(stats.get("commentCount")),
        "provenance": provenance,
    }


def _normalise_comment_snippet(
    comment_id: str, snippet: dict, kind: str, video_youtube_id: str, provenance: str
) -> dict:
    return {
        "youtube_id": comment_id,
        "kind": kind,
        "video_youtube_id": video_youtube_id or snippet.get("videoId"),
        "parent_youtube_id": snippet.get("parentId"),
        "author_name": snippet.get("authorDisplayName"),
        "author_channel_id": (snippet.get("authorChannelId", {}) or {}).get("value"),
        # textOriginal is the raw text; textDisplay carries HTML entities and
        # markup, which would pollute both matching and sentiment.
        "text": snippet.get("textOriginal") or snippet.get("textDisplay"),
        "like_count": _int_or_none(snippet.get("likeCount")),
        "published_at": parse_timestamp(snippet.get("publishedAt"))
        or datetime.now(timezone.utc).replace(tzinfo=None),
        "updated_at": parse_timestamp(snippet.get("updatedAt")),
        "provenance": provenance,
    }


def normalise_thread(raw: dict, provenance: str = "live") -> list[dict]:
    """One commentThreads.list item -> the top-level comment plus its replies.

    A thread carries up to five replies inline. Beyond that the API only gives
    a count, and fetching the rest costs another call per thread -- so the
    harvester takes the inline ones and records the count.
    """
    snippet = raw.get("snippet", {}) or {}
    video_youtube_id = snippet.get("videoId")
    top = snippet.get("topLevelComment", {}) or {}
    top_id = top.get("id") or raw.get("id")
    top_snippet = top.get("snippet", {}) or {}

    out = [
        _normalise_comment_snippet(
            top_id, top_snippet, "comment", video_youtube_id, provenance
        )
    ]
    out[0]["reply_count"] = _int_or_none(snippet.get("totalReplyCount")) or 0

    for reply in (raw.get("replies", {}) or {}).get("comments", []) or []:
        out.append(
            _normalise_comment_snippet(
                reply.get("id"),
                reply.get("snippet", {}) or {},
                "reply",
                video_youtube_id,
                provenance,
            )
        )
    return out
