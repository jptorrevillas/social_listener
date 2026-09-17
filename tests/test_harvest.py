"""The capture pipeline."""

from datetime import datetime, timedelta

from sqlalchemy import func, select

from social_listener.harvest import harvest_channels, ingest_mentions
from social_listener.models import Alert, Enrichment, Match, Mention, Video

NOW = datetime(2026, 9, 17, 12, 0)
LOUD = "Northbridge College tuition fraud, unacceptable, I am contacting a lawyer"


def video(youtube_id="vid1", channel="UC_peer", **kw):
    base = {
        "youtube_id": youtube_id,
        "channel_youtube_id": channel,
        "channel_title": "Peer Channel",
        "title": "A video about something",
        "description": "",
        "published_at": NOW - timedelta(days=2),
        "view_count": 100,
        "like_count": 5,
        "comment_count": 2,
        "provenance": "demo",
    }
    base.update(kw)
    return base


def comment(youtube_id="c1", text="Northbridge College tuition is a problem", **kw):
    base = {
        "youtube_id": youtube_id,
        "kind": "comment",
        "video_youtube_id": "vid1",
        "parent_youtube_id": None,
        "author_name": "someone",
        "author_channel_id": "UC_x",
        "text": text,
        "like_count": 3,
        "reply_count": 0,
        "published_at": NOW - timedelta(days=1),
        "updated_at": None,
        "provenance": "demo",
    }
    base.update(kw)
    return base


class FakeSource:
    """Returns exactly what it is told to, including nothing."""

    mode = "demo"

    def __init__(self, videos=None, comments=None, disabled=()):
        self._videos = videos or []
        self._comments = comments or []
        self._disabled = set(disabled)

    def search_videos(self, query, published_after=None):
        return self._videos

    def channel_videos(self, channel):
        return [v for v in self._videos if v["channel_youtube_id"] == channel["youtube_id"]]

    def video_comments(self, video_youtube_id, max_pages=3):
        if video_youtube_id in self._disabled:
            return []
        return [c for c in self._comments if c["video_youtube_id"] == video_youtube_id]

    def refresh_videos(self, ids):
        wanted = set(ids)
        return [v for v in self._videos if v["youtube_id"] in wanted]

    def refresh_comments(self, ids):
        wanted = set(ids)
        return [c for c in self._comments if c["youtube_id"] in wanted]

    def close(self):
        pass


# -- dedupe ----------------------------------------------------------------


def test_a_matching_comment_is_stored(seeded):
    ingest_mentions(seeded, video(), [comment()])
    assert seeded.scalar(select(func.count(Mention.id))) >= 1


def test_the_same_comment_id_never_creates_a_second_row(seeded):
    # Overlapping harvest windows and retries guarantee duplicates, so the
    # dedupe key has to be structural rather than best-effort.
    stats = ingest_mentions(seeded, video(), [comment(), comment(), comment()])
    rows = seeded.scalar(
        select(func.count(Mention.id)).where(Mention.youtube_id == "c1")
    )
    assert rows == 1
    assert stats.duplicates == 2


def test_a_duplicate_does_not_duplicate_enrichment(seeded):
    ingest_mentions(seeded, video(), [comment(), comment()])
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert len(mention.enrichments) == 1


def test_a_duplicate_does_not_duplicate_matches(seeded):
    ingest_mentions(seeded, video(), [comment(), comment()])
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    # Two terms match this text; re-ingesting must not double them.
    assert len(mention.matches) == 2


def test_re_ingesting_updates_mutable_counts(seeded):
    ingest_mentions(seeded, video(), [comment(like_count=3)])
    ingest_mentions(seeded, video(), [comment(like_count=250, reply_count=9)])
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert (mention.like_count, mention.reply_count) == (250, 9)


# -- matching gate ---------------------------------------------------------


def test_an_unmatched_comment_is_dropped_on_a_peer_channel(seeded):
    # Two candidates are evaluated: the video's own title and the comment.
    # Neither matches here, so neither is stored.
    stats = ingest_mentions(
        seeded, video(), [comment(text="unrelated chatter about the weather")]
    )
    assert stats.unmatched == 2
    assert stats.kept_unmatched == 0
    assert seeded.scalar(
        select(func.count(Mention.id)).where(Mention.youtube_id == "c1")
    ) == 0


def test_an_unmatched_comment_is_kept_on_an_owned_channel(seeded):
    # On your own channel every comment is addressed to you, and complaints
    # under your own video rarely name the institution.
    stats = ingest_mentions(
        seeded,
        video(),
        [comment(text="unrelated chatter about the weather")],
        require_match=False,
    )
    assert stats.unmatched == 0
    assert seeded.scalar(
        select(func.count(Mention.id)).where(Mention.youtube_id == "c1")
    ) == 1


def test_a_kept_unmatched_comment_has_no_match_rows(seeded):
    ingest_mentions(seeded, video(), [comment(text="weather")], require_match=False)
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert mention.matches == []


def test_a_video_whose_title_mentions_a_term_becomes_a_mention(seeded):
    ingest_mentions(seeded, video(title="Northbridge College open day"), [])
    mention = seeded.scalar(select(Mention).where(Mention.kind == "video"))
    assert mention is not None


def test_the_matched_span_is_recorded(seeded):
    ingest_mentions(seeded, video(), [comment()])
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert all(m.matched_text for m in mention.matches)


def test_a_comment_can_match_several_terms(seeded):
    ingest_mentions(seeded, video(), [comment()])
    mention = seeded.scalar(select(Mention).where(Mention.youtube_id == "c1"))
    assert {m.term.label for m in mention.matches} == {"Brand", "Billing"}


# -- alerts ----------------------------------------------------------------


def test_a_severe_comment_raises_an_alert(seeded):
    ingest_mentions(
        seeded, video(), [comment(text=LOUD, like_count=400, reply_count=60)]
    )
    assert seeded.scalar(select(func.count(Alert.id))) == 1


def test_one_alert_per_video_not_per_comment(seeded):
    # A review-bombed video produces one alert with a count, not eighty.
    comments = [
        comment(youtube_id=f"c{i}", text=LOUD, like_count=400, reply_count=60)
        for i in range(5)
    ]
    ingest_mentions(seeded, video(), comments)
    assert seeded.scalar(select(func.count(Alert.id))) == 1


def test_a_mild_comment_raises_no_alert(seeded):
    ingest_mentions(
        seeded, video(), [comment(text="Northbridge College tuition question, thanks!")]
    )
    assert seeded.scalar(select(func.count(Alert.id))) == 0


def test_spam_never_alerts_however_loud(seeded):
    spam = comment(
        text="Northbridge College tuition hacks! SUBSCRIBE TO MY CHANNEL AND I SUB BACK, link in bio",
        like_count=9000,
        reply_count=9000,
    )
    ingest_mentions(seeded, video(), [spam])
    assert seeded.scalar(select(func.count(Alert.id))) == 0


def test_spam_is_flagged_in_the_enrichment(seeded):
    spam = comment(
        text="Northbridge College tuition hacks, check out my channel, link in bio"
    )
    stats = ingest_mentions(seeded, video(), [spam])
    assert stats.spam_filtered == 1
    enrichment = seeded.scalar(select(Enrichment))
    assert enrichment.is_spam is True


# -- channel loop ----------------------------------------------------------


def test_harvest_polls_every_active_channel(seeded):
    source = FakeSource(
        videos=[video(channel="UC_owned"), video(youtube_id="vid2", channel="UC_peer")],
        comments=[comment()],
    )
    stats = harvest_channels(seeded, source)
    assert stats.channels_polled == 2


def test_a_video_with_comments_disabled_is_recorded_not_raised(seeded):
    source = FakeSource(videos=[video(channel="UC_peer")], comments=[], disabled=["vid1"])
    stats = harvest_channels(seeded, source)
    assert stats.videos_comments_disabled == 1


def test_harvest_stores_the_video_even_with_no_comments(seeded):
    source = FakeSource(videos=[video(channel="UC_peer")], disabled=["vid1"])
    harvest_channels(seeded, source)
    assert seeded.scalar(select(func.count(Video.id))) == 1


def test_owned_channel_comments_bypass_the_term_gate_in_the_loop(seeded):
    source = FakeSource(
        videos=[video(youtube_id="vidO", channel="UC_owned")],
        comments=[
            comment(youtube_id="cO", video_youtube_id="vidO", text="just saying hi")
        ],
    )
    harvest_channels(seeded, source)
    # The comment names nothing, but it is under our own video, so it is kept.
    assert seeded.scalar(
        select(func.count(Mention.id)).where(Mention.youtube_id == "cO")
    ) == 1


def test_demo_mode_spends_no_quota(seeded):
    source = FakeSource(videos=[video(channel="UC_peer")], comments=[comment()])
    stats = harvest_channels(seeded, source)
    assert stats.quota_spent == 0
