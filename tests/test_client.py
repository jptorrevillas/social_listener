"""Client behaviour, against a mock transport -- no network, no credentials."""

from types import SimpleNamespace

import httpx
import pytest

from social_listener.ratelimit import RateLimiter
from social_listener.reddit.client import RedditAuthError, RedditClient


def make_client(handler, **kw):
    limiter = RateLimiter(qpm=6000, headroom=0.0, clock=lambda: 0.0, sleeper=lambda s: None)
    return RedditClient(
        client_id="id",
        client_secret="secret",
        user_agent="server:test:v1 (by /u/test)",
        limiter=limiter,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def listing(children):
    return {"data": {"children": [{"data": c} for c in children]}}


def test_missing_credentials_raise_a_clear_error(monkeypatch):
    # Settings is a frozen dataclass, so swap the module's reference rather
    # than trying to mutate it.
    stub = SimpleNamespace(
        reddit_client_id=None, reddit_client_secret=None, reddit_user_agent="ua"
    )
    monkeypatch.setattr("social_listener.reddit.client.settings", stub)
    with pytest.raises(RedditAuthError):
        RedditClient(client_id=None, client_secret=None)


def test_token_is_fetched_then_reused():
    calls = {"token": 0, "api": 0}

    def handler(request):
        if "access_token" in str(request.url):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        calls["api"] += 1
        return httpx.Response(200, json=listing([]))

    client = make_client(handler)
    client.new_posts("testsub")
    client.new_posts("testsub")
    assert calls["token"] == 1, "the token must be cached, not refetched per call"
    assert calls["api"] == 2


def test_user_agent_is_sent_on_every_request():
    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent"))
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        return httpx.Response(200, json=listing([]))

    make_client(handler).new_posts("testsub")
    assert all(ua == "server:test:v1 (by /u/test)" for ua in seen)


def test_a_failed_token_request_raises():
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(RedditAuthError):
        make_client(handler).new_posts("testsub")


def test_a_401_triggers_one_refresh_then_succeeds():
    state = {"tokens": 0, "first_call_done": False}

    def handler(request):
        if "access_token" in str(request.url):
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"t{state['tokens']}", "expires_in": 3600})
        if not state["first_call_done"]:
            state["first_call_done"] = True
            return httpx.Response(401, json={})
        return httpx.Response(200, json=listing([{"name": "t3_a"}]))

    result = make_client(handler).new_posts("testsub")
    assert state["tokens"] == 2, "a 401 should force exactly one token refresh"
    assert len(result) == 1


def test_rate_limit_headers_are_observed():
    def handler(request):
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        return httpx.Response(
            200,
            json=listing([]),
            headers={"x-ratelimit-remaining": "42", "x-ratelimit-reset": "300", "x-ratelimit-used": "58"},
        )

    client = make_client(handler)
    client.new_posts("testsub")
    assert client.limiter.state.remaining == 42.0
    assert client.limiter.state.reset_seconds == 300.0


def test_listing_limit_is_capped_at_one_hundred():
    seen = {}

    def handler(request):
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=listing([]))

    make_client(handler).new_posts("testsub", limit=500)
    assert seen["limit"] == "100"


def test_info_chunks_fullnames_into_hundreds():
    batches = []

    def handler(request):
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        batches.append(request.url.params.get("id").split(","))
        return httpx.Response(200, json=listing([]))

    client = make_client(handler)
    client.info([f"t3_{i}" for i in range(250)])
    assert [len(b) for b in batches] == [100, 100, 50]


def test_search_restricts_to_a_subreddit_when_asked():
    seen = {}

    def handler(request):
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t0", "expires_in": 3600})
        seen["path"] = request.url.path
        seen["restrict"] = request.url.params.get("restrict_sr")
        return httpx.Response(200, json=listing([]))

    make_client(handler).search("northbridge", subreddit="testsub")
    assert seen["path"] == "/r/testsub/search"
    assert seen["restrict"] == "1"
