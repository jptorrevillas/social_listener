"""A quota-aware YouTube Data API v3 client.

Public read access needs only an API key, so there is no OAuth dance and no
approval gate -- a marked contrast with Reddit, and the main reason this is the
easier platform to build against.

Every call is priced before it is made and charged after. `commentsDisabled` is
treated as an ordinary outcome rather than an error, because a large share of
real videos have comments turned off and a listener that raises on that will
fall over constantly.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

import httpx

from ..config import settings
from ..quota import QuotaExhausted

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"

# Page sizes the API permits. search caps lower than the comment endpoints.
MAX_RESULTS = {"search.list": 50, "commentThreads.list": 100, "comments.list": 100}


class YouTubeError(RuntimeError):
    pass


class CommentsDisabled(YouTubeError):
    """The video owner turned comments off. Expected, not exceptional."""


class VideoNotFound(YouTubeError):
    """Deleted, private, or region-blocked."""


class ApiKeyMissing(YouTubeError):
    pass


def _reason(payload: dict) -> str:
    try:
        return payload["error"]["errors"][0].get("reason", "")
    except (KeyError, IndexError, TypeError):
        return ""


class YouTubeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        charge=None,
        reserve=None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key or settings.youtube_api_key
        if not self.api_key:
            raise ApiKeyMissing(
                "YOUTUBE_API_KEY is required for live mode. Without it the app "
                "runs against the demo corpus instead."
            )
        # Injected so the client does not need to know about the database.
        self._charge = charge or (lambda method, calls=1: None)
        self._reserve = reserve or (lambda method: None)
        self._http = httpx.Client(timeout=30.0, transport=transport)

    # -- plumbing -----------------------------------------------------------

    def _get(self, method: str, path: str, params: dict[str, Any]) -> dict:
        self._reserve(method)
        query = dict(params)
        query["key"] = self.api_key
        response = self._http.get(f"{API_BASE}{path}", params=query)

        # Charge first: YouTube bills the request even when it fails.
        self._charge(method)

        if response.status_code == 200:
            return response.json()

        payload = {}
        try:
            payload = response.json()
        except ValueError:
            pass
        reason = _reason(payload)

        if reason == "commentsDisabled":
            raise CommentsDisabled(path)
        if reason in ("videoNotFound", "notFound"):
            raise VideoNotFound(path)
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise QuotaExhausted(method, 0, 0)
        raise YouTubeError(
            f"{method} failed: {response.status_code} {reason or response.text[:200]}"
        )

    def _paginate(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        max_pages: int = 10,
    ) -> Iterator[dict]:
        """Walk pageToken pages. Each page costs the method's full price."""
        page_token = None
        for _ in range(max_pages):
            call_params = dict(params)
            if page_token:
                call_params["pageToken"] = page_token
            payload = self._get(method, path, call_params)
            for item in payload.get("items", []):
                yield item
            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    # -- endpoints ----------------------------------------------------------

    def search_videos(
        self, query: str, published_after: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """search.list -- 100 units. The expensive one. Use it sparingly."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "date",
            "maxResults": min(limit, MAX_RESULTS["search.list"]),
        }
        if published_after:
            params["publishedAfter"] = published_after
        payload = self._get("search.list", "/search", params)
        return payload.get("items", [])

    def videos(self, video_ids: list[str]) -> list[dict]:
        """videos.list -- 1 unit for up to 50 ids. The cheap bulk lever."""
        out: list[dict] = []
        for start in range(0, len(video_ids), 50):
            chunk = video_ids[start : start + 50]
            payload = self._get(
                "videos.list",
                "/videos",
                {"part": "snippet,statistics", "id": ",".join(chunk)},
            )
            out.extend(payload.get("items", []))
        return out

    def channels(self, channel_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(channel_ids), 50):
            chunk = channel_ids[start : start + 50]
            payload = self._get(
                "channels.list",
                "/channels",
                {"part": "snippet,contentDetails", "id": ",".join(chunk)},
            )
            out.extend(payload.get("items", []))
        return out

    def channel_uploads(self, uploads_playlist_id: str, limit: int = 50) -> list[dict]:
        """playlistItems.list -- 1 unit. Cheaper than search for known channels.

        This is the key cost trick: enumerating a channel's own uploads costs 1
        unit, where finding the same videos via search.list would cost 100.
        """
        payload = self._get(
            "playlistItems.list",
            "/playlistItems",
            {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": min(limit, 50),
            },
        )
        return payload.get("items", [])

    def comment_threads(
        self, video_id: str, max_pages: int = 5, order: str = "time"
    ) -> list[dict]:
        """commentThreads.list -- 1 unit per page. Harvest freely."""
        params = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": MAX_RESULTS["commentThreads.list"],
            "order": order,
            "textFormat": "plainText",
        }
        try:
            return list(
                self._paginate(
                    "commentThreads.list", "/commentThreads", params, max_pages
                )
            )
        except CommentsDisabled:
            log.info("comments disabled on %s", video_id)
            return []

    def comments(self, comment_ids: list[str]) -> list[dict]:
        """comments.list by id -- used by the retention refresh to check liveness."""
        out: list[dict] = []
        for start in range(0, len(comment_ids), 50):
            chunk = comment_ids[start : start + 50]
            payload = self._get(
                "comments.list",
                "/comments",
                {"part": "snippet", "id": ",".join(chunk), "textFormat": "plainText"},
            )
            out.extend(payload.get("items", []))
        return out

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "YouTubeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
