"""Payload mapping. Recorded shapes, no HTTP.

The video id is the fiddly part: it lives in a different place per endpoint,
and the wrong one is often present and truthy -- which is exactly the bug this
file exists to prevent recurring.
"""

from datetime import datetime

from social_listener.youtube.normalise import (
    normalise_thread,
    normalise_video,
    parse_timestamp,
)


def test_z_suffixed_timestamps_parse():
    # Python 3.9's fromisoformat cannot read the Z, so it must be normalised.
    assert parse_timestamp("2026-09-01T08:30:00Z") == datetime(2026, 9, 1, 8, 30)


def test_offset_timestamps_are_converted_to_utc():
    assert parse_timestamp("2026-09-01T10:30:00+02:00") == datetime(2026, 9, 1, 8, 30)


def test_a_malformed_timestamp_returns_none_rather_than_raising():
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_videos_list_id_is_a_plain_string():
    raw = {"kind": "youtube#video", "id": "vid_real", "snippet": {}}
    assert normalise_video(raw)["youtube_id"] == "vid_real"


def test_search_list_id_is_nested():
    raw = {
        "kind": "youtube#searchResult",
        "id": {"kind": "youtube#video", "videoId": "vid_search"},
        "snippet": {},
    }
    assert normalise_video(raw)["youtube_id"] == "vid_search"


def test_playlist_item_id_is_not_the_video_id():
    # The regression: `id` here is the playlist item, and taking it silently
    # attributes every comment to the wrong video.
    raw = {
        "kind": "youtube#playlistItem",
        "id": "PLITEM_xyz",
        "contentDetails": {"videoId": "vid_playlist"},
        "snippet": {},
    }
    assert normalise_video(raw)["youtube_id"] == "vid_playlist"


def test_playlist_item_falls_back_to_resource_id():
    raw = {
        "kind": "youtube#playlistItem",
        "id": "PLITEM_xyz",
        "snippet": {"resourceId": {"videoId": "vid_res"}},
    }
    assert normalise_video(raw)["youtube_id"] == "vid_res"


def test_statistics_are_coerced_to_ints():
    raw = {
        "kind": "youtube#video",
        "id": "v",
        "snippet": {},
        "statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "7"},
    }
    video = normalise_video(raw)
    assert (video["view_count"], video["like_count"], video["comment_count"]) == (1234, 56, 7)


def test_missing_statistics_are_none_not_zero():
    # Zero would be a lie: it means "no data", not "nobody watched".
    video = normalise_video({"kind": "youtube#video", "id": "v", "snippet": {}})
    assert video["view_count"] is None


def _thread(replies=None, total=0):
    return {
        "id": "th1",
        "snippet": {
            "videoId": "v1",
            "totalReplyCount": total,
            "topLevelComment": {
                "id": "c1",
                "snippet": {
                    "authorDisplayName": "A",
                    "authorChannelId": {"value": "UC_a"},
                    "textOriginal": "plain text",
                    "textDisplay": "<b>markup</b>",
                    "likeCount": "4",
                    "publishedAt": "2026-09-01T08:30:00Z",
                },
            },
        },
        "replies": {"comments": replies or []},
    }


def test_a_thread_yields_its_top_level_comment():
    rows = normalise_thread(_thread())
    assert len(rows) == 1
    assert rows[0]["kind"] == "comment"
    assert rows[0]["youtube_id"] == "c1"


def test_text_original_is_preferred_over_text_display():
    # textDisplay carries HTML, which would pollute matching and sentiment.
    assert normalise_thread(_thread())[0]["text"] == "plain text"


def test_inline_replies_become_reply_rows():
    reply = {
        "id": "r1",
        "snippet": {
            "parentId": "c1",
            "authorDisplayName": "B",
            "textOriginal": "a reply",
            "publishedAt": "2026-09-01T09:00:00Z",
        },
    }
    rows = normalise_thread(_thread(replies=[reply]))
    assert [r["kind"] for r in rows] == ["comment", "reply"]
    assert rows[1]["parent_youtube_id"] == "c1"


def test_total_reply_count_is_kept_even_when_replies_are_not_inlined():
    # Only five replies come inline; the count is how we know more exist.
    rows = normalise_thread(_thread(total=47))
    assert rows[0]["reply_count"] == 47


def test_author_channel_id_is_unwrapped():
    assert normalise_thread(_thread())[0]["author_channel_id"] == "UC_a"
