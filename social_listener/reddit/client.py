"""A thin Reddit Data API client.

Deliberately not PRAW. §14: PRAW hides the X-Ratelimit-* headers behind its own
limiter, and those headers are the one thing the ingestion design most needs to
steer by. This client is small enough to read in one sitting and exposes the
budget directly.

Auth is the client_credentials flow (§3.1) -- application-only access, which is
all a listener needs: it reads public content and never posts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import settings
from ..ratelimit import RateLimiter

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

# Bearer tokens last an hour (§3.1). Refresh early so a long call cannot
# straddle the expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 600


class RedditAuthError(RuntimeError):
    pass


class RedditClient:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
        limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client_id = client_id or settings.reddit_client_id
        self.client_secret = client_secret or settings.reddit_client_secret
        self.user_agent = user_agent or settings.reddit_user_agent
        if not self.client_id or not self.client_secret:
            raise RedditAuthError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are required for live mode. "
                "Without them the app runs against the demo corpus instead."
            )
        self.limiter = limiter or RateLimiter()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._http = httpx.Client(
            timeout=30.0,
            headers={"User-Agent": self.user_agent},
            transport=transport,
        )

    # -- auth ---------------------------------------------------------------

    def _token_is_fresh(self) -> bool:
        return bool(self._token) and time.time() < self._token_expires_at

    def ensure_token(self) -> str:
        if self._token_is_fresh():
            return self._token  # type: ignore[return-value]
        response = self._http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if response.status_code != 200:
            raise RedditAuthError(
                f"token request failed: {response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._token_expires_at = time.time() + max(
            expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 60
        )
        return self._token  # type: ignore[return-value]

    # -- requests -----------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None, _retries: int = 2) -> dict:
        """One rate-limited GET against oauth.reddit.com."""
        token = self.ensure_token()
        self.limiter.acquire()
        response = self._http.get(
            f"{API_BASE}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.limiter.observe(response.headers)

        if response.status_code == 401 and _retries > 0:
            # "refresh once, then retry once" (§3.1).
            self._token = None
            return self.get(path, params, _retries=_retries - 1)

        if response.status_code == 429 and _retries > 0:
            delay = self.limiter.backoff_seconds(3 - _retries)
            log.warning("rate limited; backing off %.1fs", delay)
            time.sleep(delay)
            return self.get(path, params, _retries=_retries - 1)

        response.raise_for_status()
        return response.json()

    # -- endpoints (§4) -----------------------------------------------------

    def listing(self, path: str, limit: int = 100, after: str | None = None) -> list[dict]:
        """Fetch one page of a listing. `limit` maxes at 100 -- always ask for it."""
        params: dict[str, Any] = {"limit": min(limit, 100), "raw_json": 1}
        if after:
            params["after"] = after
        payload = self.get(path, params)
        children = payload.get("data", {}).get("children", [])
        return [child.get("data", {}) for child in children]

    def new_posts(self, subreddit: str, limit: int = 100) -> list[dict]:
        return self.listing(f"/r/{subreddit}/new", limit=limit)

    def new_comments(self, subreddit: str, limit: int = 100) -> list[dict]:
        return self.listing(f"/r/{subreddit}/comments", limit=limit)

    def search(
        self, query: str, subreddit: str | None = None, limit: int = 100
    ) -> list[dict]:
        params: dict[str, Any] = {
            "q": query,
            "sort": "new",
            "limit": min(limit, 100),
            "raw_json": 1,
        }
        if subreddit:
            params["restrict_sr"] = 1
            path = f"/r/{subreddit}/search"
        else:
            path = "/search"
        payload = self.get(path, params)
        children = payload.get("data", {}).get("children", [])
        return [child.get("data", {}) for child in children]

    def info(self, fullnames: list[str]) -> list[dict]:
        """Hydrate up to 100 items in one call -- the cheapest call in the API.

        §4: any design that re-fetches items one at a time spends 100x more
        budget than necessary.
        """
        results: list[dict] = []
        for chunk_start in range(0, len(fullnames), 100):
            chunk = fullnames[chunk_start : chunk_start + 100]
            payload = self.get("/api/info", {"id": ",".join(chunk), "raw_json": 1})
            children = payload.get("data", {}).get("children", [])
            results.extend(child.get("data", {}) for child in children)
        return results

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RedditClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
