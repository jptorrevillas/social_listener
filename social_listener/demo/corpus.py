"""A synthetic YouTube corpus, so the demo runs with no API key.

EVERYTHING HERE IS FABRICATED. "Northbridge College" is a fictional institution
invented for this demo; the videos, channels and comments are generated from
templates. None of it came from YouTube and none of it describes a real
organisation or real person. The UI labels this permanently, and it must stay
labelled: a screenshot of this feed should never be mistakable for real public
sentiment about a real place.

The corpus is shaped to exercise what actually goes wrong on YouTube:

  * comment spam at realistic volume -- crypto bait, "check out my channel",
    bot replies. This is the dominant noise source on YouTube and far heavier
    than Reddit's AutoModerator boilerplate.
  * a video with comments disabled, which is an ordinary state, not an error
  * sarcasm the lexicon scores backwards, flagged low-confidence
  * an edited comment (updatedAt later than publishedAt)
  * items deleted upstream, for the retention job to catch
  * comments older than the 30-day retention window, so the expiry engine has
    something real to purge
  * a review-bomb spike on one video
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

BRAND = "Northbridge College"

CHANNELS = [
    {"youtube_id": "UC_northbridge_official", "title": "Northbridge College Official",
     "uploads_playlist_id": "UU_northbridge_official", "is_owned": True},
    {"youtube_id": "UC_campus_review_ph", "title": "Campus Review PH",
     "uploads_playlist_id": "UU_campus_review_ph", "is_owned": False},
    {"youtube_id": "UC_student_vlogs", "title": "Student Life Vlogs",
     "uploads_playlist_id": "UU_student_vlogs", "is_owned": False},
]

VIDEO_TEMPLATES = [
    ("{brand} Enrolment Guide 2026", "Step-by-step walkthrough of the {brand} online enrolment portal.", "UC_northbridge_official"),
    ("{brand} Campus Tour", "A walk around the {brand} campus, library and new laboratories.", "UC_northbridge_official"),
    ("{brand} Commencement Highlights", "Highlights from this year's {brand} graduation ceremony.", "UC_northbridge_official"),
    ("Is {brand} Worth It? Honest Review", "We look at tuition, facilities and job placement at {brand}.", "UC_campus_review_ph"),
    ("Top 5 Colleges in Misamis Oriental", "Comparing {brand} against four other institutions on cost and outcomes.", "UC_campus_review_ph"),
    ("My First Week at {brand}", "Vlog: enrolment queues, finding classrooms, and canteen prices at {brand}.", "UC_student_vlogs"),
    ("{brand} Tuition Increase Explained", "Breaking down the announced {brand} fee adjustment for next semester.", "UC_campus_review_ph"),
]

COMPLAINTS = [
    "The {brand} enrolment portal timed out on me four times at the payment step. Absolutely ridiculous for 2026.",
    "Emailed the {brand} registrar three weeks ago about my transcript and heard nothing back. Unacceptable.",
    "Why is there a miscellaneous fee on my {brand} invoice that nobody at the window can explain? Felt fobbed off.",
    "Library wifi at {brand} has been broken all semester. Trying to finish a thesis on mobile data is awful.",
    "Queued two and a half hours at {brand} today just to get a signature. Worst enrolment experience of my life.",
    "{brand} double-charged my tuition and the helpdesk keeps closing my ticket without replying. Talking to a lawyer next.",
]
PRAISE = [
    "Credit to the {brand} faculty, genuinely the best decision I made. Would recommend to anyone considering it.",
    "My instructor at {brand} stayed back an hour to explain a topic I was stuck on. Grateful for teachers like that.",
    "The new {brand} library is excellent, and it actually opened on schedule.",
    "Shout out to the {brand} registrar staff who sorted my records today, very patient with me.",
    "Graduated from {brand} last year and walked straight into a job. The placement office earns its keep.",
]
QUESTIONS = [
    "Does anyone know how long {brand} takes to release transcripts? Applying abroad and running out of time.",
    "Is {brand} better than the state university for engineering? Deciding this week.",
    "How do I pay {brand} tuition if the portal rejects my card?",
    "Does {brand} accept transferees mid-year, and do units carry over?",
    "Anyone know if the {brand} tuition increase applies to continuing students too?",
]
SARCASM = [
    "Oh brilliant, the {brand} portal is down again during enrolment week. Thanks a lot /s",
    "Sure, another {brand} video about improving systems. Yeah right, I'll believe it when the wifi works.",
]
# YouTube's characteristic noise. Heavier and more varied than Reddit's.
SPAM = [
    "FIRST!!! also check out my channel for free tuition hacks, link in bio",
    "Make $5000 a week from home, DM me on WhatsApp +63xxxxxxxxx, no experience needed",
    "who else is watching this in 2026?? like if you agree",
    "I am a bot. This comment was generated automatically to boost engagement.",
    "SUBSCRIBE TO MY CHANNEL AND I SUB BACK, let's grow together fam",
    "Congratulations you have been selected! Claim your prize at the link in my profile",
]
REVIEW_BOMB = [
    "{brand} raising tuition again while the wifi is broken. Shameful.",
    "Same here, the {brand} fee increase was announced with no consultation at all.",
    "Three of us in the same class got the {brand} increase notice today. Nobody explained it.",
    "Has anyone taken the {brand} fee increase to CHED? This cannot be legal.",
    "Still no statement from {brand} about the increase. Six hours and counting.",
    "{brand} finally posted a statement about the fee adjustment. Too little, too late.",
    "Cancelling my {brand} enrolment over this. Fraud, plain and simple.",
    "A journalist is asking about the {brand} increase in the other thread.",
]

AUTHORS = [
    ("quietstudent", "UC_a1"), ("manila_grad", "UC_a2"), ("cs_transferee", "UC_a3"),
    ("nightshift_ba", "UC_a4"), ("thesis_panic", "UC_a5"), ("campus_lurker", "UC_a6"),
    ("batch2024", "UC_a7"), ("parent_of_two", "UC_a8"), ("commuter_life", "UC_a9"),
]
SPAM_AUTHORS = [
    ("CryptoWealthNow", "UC_s1"), ("FreeGiftCards2026", "UC_s2"),
    ("SubForSubOfficial", "UC_s3"), ("EngagementBot", "UC_s4"),
]


def _video(idx, title, description, channel_id, channel_title, published, stats,
           comments_disabled=False, deleted_upstream=False):
    return {
        "youtube_id": f"vid{idx:05d}",
        "channel_youtube_id": channel_id,
        "channel_title": channel_title,
        "title": title,
        "description": description,
        "published_at": published.replace(tzinfo=None),
        "view_count": stats[0],
        "like_count": stats[1],
        "comment_count": stats[2],
        "provenance": "demo",
        "_comments_disabled": comments_disabled,
        "_deleted_upstream": deleted_upstream,
    }


def _comment(idx, video_id, author, text, published, like_count, kind="comment",
             parent=None, updated=None, reply_count=0, deleted_upstream=False):
    name, channel = author
    return {
        "youtube_id": f"{'r' if kind == 'reply' else 'c'}mt{idx:06d}",
        "kind": kind,
        "video_youtube_id": video_id,
        "parent_youtube_id": parent,
        "author_name": name,
        "author_channel_id": channel,
        "text": text,
        "like_count": like_count,
        "reply_count": reply_count,
        "published_at": published.replace(tzinfo=None),
        "updated_at": updated.replace(tzinfo=None) if updated else None,
        "provenance": "demo",
        "_deleted_upstream": deleted_upstream,
    }


def build_corpus(days: int = 45, seed: int = 20260917) -> dict:
    """Deterministic, so the demo is reproducible.

    Spans 45 days on purpose: longer than the 30-day retention window, so part
    of the corpus is already expired when it lands and the retention engine has
    genuine work to do rather than a hypothetical.
    """
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    channel_titles = {c["youtube_id"]: c["title"] for c in CHANNELS}

    videos: list[dict] = []
    comments: list[dict] = []
    vid_idx = cmt_idx = 0

    for offset, (title_t, desc_t, channel_id) in enumerate(VIDEO_TEMPLATES):
        vid_idx += 1
        published = start + timedelta(days=offset * 5, hours=rng.randint(8, 18))
        videos.append(
            _video(
                vid_idx,
                title_t.format(brand=BRAND),
                desc_t.format(brand=BRAND),
                channel_id,
                channel_titles[channel_id],
                published,
                (rng.randint(800, 60000), rng.randint(20, 1800), 0),
                # The comparison video has comments switched off, which is
                # ordinary and must not read as a failure.
                comments_disabled=(offset == 4),
            )
        )

    # Organic comments spread across the videos and the whole window.
    for video in videos:
        if video["_comments_disabled"]:
            continue
        count = rng.randint(6, 14)
        for _ in range(count):
            cmt_idx += 1
            roll = rng.random()
            if roll < 0.34:
                text, loud = rng.choice(COMPLAINTS).format(brand=BRAND), True
            elif roll < 0.58:
                text, loud = rng.choice(QUESTIONS).format(brand=BRAND), False
            elif roll < 0.80:
                text, loud = rng.choice(PRAISE).format(brand=BRAND), False
            elif roll < 0.88:
                text, loud = rng.choice(SARCASM).format(brand=BRAND), False
            else:
                text, loud = rng.choice(SPAM), False

            is_spam = text in SPAM
            author = rng.choice(SPAM_AUTHORS if is_spam else AUTHORS)
            published = video["published_at"] + timedelta(
                hours=rng.randint(1, 24 * 20), minutes=rng.randint(0, 59)
            )
            if published > now.replace(tzinfo=None):
                published = now.replace(tzinfo=None) - timedelta(hours=rng.randint(1, 48))

            comments.append(
                _comment(
                    cmt_idx,
                    video["youtube_id"],
                    author,
                    text,
                    published.replace(tzinfo=timezone.utc),
                    rng.randint(0, 240) if loud else rng.randint(0, 40),
                    reply_count=rng.randint(0, 6),
                    # An edited comment: sentiment can invert after capture.
                    updated=(published + timedelta(hours=3)).replace(tzinfo=timezone.utc)
                    if rng.random() < 0.08
                    else None,
                    deleted_upstream=(rng.random() < 0.05),
                )
            )

    # The review bomb: a burst on the tuition video, high volume and severity.
    # Anchored to 08:00 on a single day and kept inside a 12-hour window, so it
    # lands as one day's spike rather than splitting across midnight -- split
    # in half it sits under mean + 3-sigma and the detector correctly ignores
    # it, which makes for a demo that teaches the wrong lesson.
    bomb_video = videos[-1]
    bomb_day = (now - timedelta(days=3)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    for offset, template in enumerate(REVIEW_BOMB * 2):
        cmt_idx += 1
        comments.append(
            _comment(
                cmt_idx,
                bomb_video["youtube_id"],
                rng.choice(AUTHORS),
                template.format(brand=BRAND),
                bomb_day + timedelta(minutes=offset * 40),
                rng.randint(40, 600),
                reply_count=rng.randint(2, 30),
            )
        )

    # A reply thread under the loudest complaint, to prove replies attach.
    parent = comments[-4]
    for text in (
        "Same thing happened to me, still waiting on a refund.",
        "SUBSCRIBE TO MY CHANNEL AND I SUB BACK",
    ):
        cmt_idx += 1
        comments.append(
            _comment(
                cmt_idx,
                parent["video_youtube_id"],
                rng.choice(SPAM_AUTHORS if "SUBSCRIBE" in text else AUTHORS),
                text,
                (parent["published_at"] + timedelta(hours=2)).replace(tzinfo=timezone.utc),
                rng.randint(0, 20),
                kind="reply",
                parent=parent["youtube_id"],
            )
        )

    # A duplicate delivery of one comment. Overlapping harvest windows and
    # retries guarantee these, so the dedupe key must collapse it.
    comments.append(dict(comments[5]))

    for video in videos:
        video["comment_count"] = sum(
            1 for c in comments if c["video_youtube_id"] == video["youtube_id"]
        )

    # Backdate the STORAGE timestamps, which is what the 30-day clock actually
    # runs on -- not publishedAt. A fresh install has nothing expired by
    # definition, so without this the retention engine has nothing to do and
    # the demo cannot show it working. These values represent an installation
    # that first saw each item shortly after it was posted.
    naive_now = now.replace(tzinfo=None)
    for record in (*videos, *comments):
        first_seen = record["published_at"] + timedelta(hours=rng.randint(1, 30))
        if first_seen > naive_now:
            first_seen = naive_now
        record["_first_seen_at"] = first_seen
        record["_content_expires_at"] = first_seen + timedelta(days=30)

    comments.sort(key=lambda c: c["published_at"])
    return {"videos": videos, "comments": comments}


DEMO_TERMS = [
    {"label": "Northbridge College", "match_type": "boolean",
     "pattern": '("northbridge college" OR northbridge OR nbc)',
     "negative_pattern": "northbridge road | northbridge mall", "use_in_discovery": True},
    {"label": "Enrolment issues", "match_type": "phrase", "pattern": "enrolment",
     "negative_pattern": None, "use_in_discovery": True},
    {"label": "Tuition & fees", "match_type": "boolean",
     "pattern": '(tuition OR "fee increase" OR invoice OR "double-charged" OR billing)',
     "negative_pattern": None, "use_in_discovery": False},
]
