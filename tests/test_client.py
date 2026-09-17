"""Client behaviour against a mock transport. No network, no API key."""

import httpx
import pytest

from social_listener.quota import QuotaExhausted
from social_listener.youtube.client import (
    ApiKeyMissing,
    CommentsDisabled,
    VideoNotFound,
    YouTubeClient,
    YouTubeError,
)


def make_client(handler, charged=None):
    charged = charged if charged is not None else []
    return (
        YouTubeClient(
            api_key="test-key",
            charge=lambda method, calls=1: charged.append((method, calls)),
            transport=httpx.MockTransport(handler),
        ),
        charged,
    )


def ok(items, next_token=None):
    payload = {"items": items}
    if next_token:
        payload["nextPageToken"] = next_token
    return httpx.Response(200, json=payload)


def error(status, reason):
    return httpx.Response(
        status, json={"error": {"errors": [{"reason": reason}], "message": reason}}
    )


def test_a_missing_key_fails_loudly(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "social_listener.youtube.client.settings",
        SimpleNamespace(youtube_api_key=None),
    )
    with pytest.raises(ApiKeyMissing):
        YouTubeClient(api_key=None)


def test_the_api_key_is_sent_on_every_request():
    seen = []

    def handler(request):
        seen.append(request.url.params.get("key"))
        return ok([])

    client, _ = make_client(handler)
    client.videos(["v1"])
    assert seen == ["test-key"]


def test_comments_disabled_returns_empty_not_an_exception():
    # A large share of real videos have comments off. A listener that raises
    # here falls over constantly.
    client, _ = make_client(lambda request: error(403, "commentsDisabled"))
    assert client.comment_threads("v1") == []


def test_comments_disabled_still_raises_from_the_low_level_call():
    client, _ = make_client(lambda request: error(403, "commentsDisabled"))
    with pytest.raises(CommentsDisabled):
        client._get("commentThreads.list", "/commentThreads", {})


def test_a_missing_video_is_distinguishable():
    client, _ = make_client(lambda request: error(404, "videoNotFound"))
    with pytest.raises(VideoNotFound):
        client.videos(["gone"])


def test_quota_exceeded_surfaces_as_quota_exhausted():
    client, _ = make_client(lambda request: error(403, "quotaExceeded"))
    with pytest.raises(QuotaExhausted):
        client.videos(["v1"])


def test_an_unknown_error_is_not_swallowed():
    client, _ = make_client(lambda request: error(500, "backendError"))
    with pytest.raises(YouTubeError):
        client.videos(["v1"])


def test_a_failed_call_is_still_charged():
    # YouTube bills the request whether or not the response was useful, so the
    # ledger has to reflect that or the budget drifts.
    client, charged = make_client(lambda request: error(403, "commentsDisabled"))
    client.comment_threads("v1")
    assert charged == [("commentThreads.list", 1)]


def test_reserve_runs_before_the_request_is_made():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return ok([])

    client = YouTubeClient(
        api_key="k",
        charge=lambda method, calls=1: None,
        reserve=lambda method: (_ for _ in ()).throw(QuotaExhausted(method, 100, 0)),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(QuotaExhausted):
        client.search_videos("anything")
    assert calls == [], "no HTTP request should be made when the budget refuses it"


def test_each_page_is_charged_separately():
    pages = {"n": 0}

    def handler(request):
        pages["n"] += 1
        if pages["n"] < 3:
            return ok([{"id": pages["n"]}], next_token=f"tok{pages['n']}")
        return ok([{"id": 3}])

    client, charged = make_client(handler)
    results = client.comment_threads("v1", max_pages=5)
    assert len(results) == 3
    assert charged == [("commentThreads.list", 1)] * 3


def test_pagination_respects_max_pages():
    def handler(request):
        return ok([{"id": "x"}], next_token="always-more")

    client, charged = make_client(handler)
    client.comment_threads("v1", max_pages=2)
    assert len(charged) == 2


def test_videos_are_batched_fifty_at_a_time():
    batches = []

    def handler(request):
        batches.append(request.url.params.get("id").split(","))
        return ok([])

    client, _ = make_client(handler)
    client.videos([f"v{i}" for i in range(120)])
    assert [len(b) for b in batches] == [50, 50, 20]


def test_search_caps_max_results_at_fifty():
    seen = {}

    def handler(request):
        seen["maxResults"] = request.url.params.get("maxResults")
        return ok([])

    client, _ = make_client(handler)
    client.search_videos("query", limit=500)
    assert seen["maxResults"] == "50"


def test_comment_threads_ask_for_plain_text():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return ok([])

    client, _ = make_client(handler)
    client.comment_threads("v1", max_pages=1)
    assert seen["textFormat"] == "plainText"
    assert seen["maxResults"] == "100"
