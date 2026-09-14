"""A synthetic Reddit corpus, so the demo runs with no credentials.

EVERYTHING HERE IS FABRICATED. "Northbridge College" is a fictional institution
invented for this demo and the posts are generated from templates -- none of it
is real Reddit content and none of it describes a real organisation. The UI
labels this prominently, and it must stay labelled: a screenshot of this feed
should never be mistakable for real public sentiment about a real place.

The corpus is shaped to exercise the parts of the pipeline that matter:

  * a mention baseline with a deliberate spike on day 14 (a billing incident),
    so spike detection has something to fire on
  * sarcasm, which the lexicon lane gets wrong and flags low-confidence
  * bot boilerplate, which is filtered before enrichment
  * near-duplicate crossposts, which the fullname key dedupes
  * items marked deleted upstream, which the compliance sweep tombstones
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

BRAND = "Northbridge College"
SUBREDDITS = ("studentsph", "collegeadvice", "northbridge", "phEducation")

# (template, sentiment_hint, weight)
COMPLAINTS = (
    "Has anyone else had trouble with the {brand} enrolment portal? Third day in a row it times out at the payment step.",
    "Emailed the {brand} registrar twice about my transcript and got nothing back. Two weeks now. Absolutely ridiculous.",
    "The tuition invoice from {brand} has a fee on it nobody can explain to me. Rude response when I asked at the desk.",
    "{brand} wifi in the library has been broken all semester. Trying to submit a thesis draft on mobile data is awful.",
    "Why does {brand} still not have online payment? Queued two hours today. Worst enrolment experience I have had.",
    "Getting billed twice by {brand} and the helpdesk keeps closing my ticket. Considering talking to a lawyer at this point.",
)
PRAISE = (
    "Shout out to the {brand} faculty, genuinely the best decision I made. Would recommend to anyone considering it.",
    "My instructor at {brand} stayed back an hour to explain a topic. Grateful for teachers like that.",
    "{brand} campus renovation actually finished on time. The new library is great.",
    "Thank you to the {brand} registrar staff who sorted my records today, very patient with me.",
)
QUESTIONS = (
    "Does anyone know how long {brand} takes to release transcripts? Applying abroad and running out of time.",
    "Is {brand} worth it compared to the state university? Thinking of applying next semester.",
    "How do I pay {brand} tuition if the portal rejects my card? About to enrol and slightly panicking.",
    "Anyone know if {brand} accepts transferees mid-year?",
)
SARCASM = (
    "Oh great, the {brand} portal is down again during enrolment week. Thanks a lot /s",
    "Sure thing, another {brand} announcement about improving systems. Yeah right.",
)
# Real AutoModerator boilerplate routinely quotes the subject, so these mention
# the brand -- which means they pass the matching tier and must be caught by the
# spam lane instead. That is the tiering working as designed (§7.5).
BOT_POSTS = (
    "Your post about {brand} has been queued for moderator review. I am a bot, and this action was performed automatically. Please contact the moderators of this subreddit if you have any questions.",
    "Beep boop. This {brand} tuition thread was automatically flaired. I am a bot.",
)
# The day-14 billing incident.
SPIKE_POSTS = (
    "{brand} double-charged my tuition this morning. Anyone else? This is fraud territory if they do not fix it.",
    "Getting the same double charge from {brand}. Bank says it is on their end. Unacceptable.",
    "Three people in my class double-billed by {brand} today. Has anyone reached the ombudsman about this?",
    "{brand} billing incident thread -- post here if you were charged twice, a journalist is asking.",
    "Still no statement from {brand} about the double billing. Six hours now.",
    "{brand} finally acknowledged the double charge. Refunds in 5 days apparently.",
)

TITLES = {
    "complaint": (
        "{brand} enrolment portal down again",
        "Anyone else waiting on {brand} transcripts?",
        "Unexplained fee on my {brand} invoice",
        "{brand} library wifi still broken",
        "Two hours queueing at {brand} today",
        "Double-billed by {brand}, no resolution",
    ),
    "question": (
        "How long do {brand} transcripts take?",
        "Is {brand} worth it over the state uni?",
        "{brand} tuition payment keeps failing",
        "Does {brand} take mid-year transferees?",
    ),
    "praise": (
        "Credit where it is due: {brand} faculty",
        "Good experience with a {brand} instructor",
        "{brand} library renovation finished",
        "Thanks to the {brand} registrar staff",
    ),
    "sarcasm": (
        "{brand} systems working perfectly as always",
        "Another {brand} announcement, very reassuring",
    ),
    "spike": (
        "{brand} double-charged my tuition",
        "Same double charge from {brand}",
        "Several of us double-billed by {brand}",
        "{brand} billing incident megathread",
        "Still no statement from {brand}",
        "{brand} acknowledges the double charge",
    ),
}

AUTHORS = [
    "quietstudent", "manila_grad", "cs_transferee", "nightshift_ba", "thesis_panic",
    "campus_lurker", "batch2024", "parent_of_two", "dean_watcher", "commuter_life",
]


def _mk(
    idx: int,
    kind: str,
    subreddit: str,
    author: str | None,
    title: str | None,
    body: str,
    created: datetime,
    score: int,
    num_comments: int,
    deleted_upstream: bool = False,
) -> dict:
    prefix = "t3_" if kind == "post" else "t1_"
    slug = f"{prefix}demo{idx:04d}"
    return {
        "fullname": slug,
        "kind": kind,
        "subreddit": subreddit,
        "author": author,
        "title": title,
        "body": body,
        "permalink": f"https://reddit.com/r/{subreddit}/comments/demo{idx:04d}/",
        "parent_fullname": None,
        "link_fullname": None,
        "created_utc": created.replace(tzinfo=None),
        "score": score,
        "num_comments": num_comments,
        "edited_at": None,
        "provenance": "demo",
        "_deleted_upstream": deleted_upstream,
    }


def build_corpus(days: int = 21, seed: int = 20260914) -> list[dict]:
    """Generate the corpus deterministically, so the demo is reproducible."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    items: list[dict] = []
    idx = 0

    for day in range(days):
        day_start = start + timedelta(days=day)
        # Baseline volume, with a mid-week dip so the trend line is not flat.
        base = rng.randint(2, 4)
        if day_start.weekday() >= 5:
            base = max(base - 1, 1)

        for _ in range(base):
            idx += 1
            roll = rng.random()
            if roll < 0.42:
                template, kind_hint = rng.choice(COMPLAINTS), "complaint"
            elif roll < 0.68:
                template, kind_hint = rng.choice(QUESTIONS), "question"
            elif roll < 0.90:
                template, kind_hint = rng.choice(PRAISE), "praise"
            else:
                template, kind_hint = rng.choice(SARCASM), "sarcasm"

            body = template.format(brand=BRAND)
            created = day_start + timedelta(
                hours=rng.randint(7, 22), minutes=rng.randint(0, 59)
            )
            is_post = rng.random() < 0.55
            score = rng.randint(1, 60) if kind_hint != "complaint" else rng.randint(3, 180)
            title = rng.choice(TITLES[kind_hint]).format(brand=BRAND) if is_post else None
            items.append(
                _mk(
                    idx,
                    "post" if is_post else "comment",
                    rng.choice(SUBREDDITS),
                    rng.choice(AUTHORS),
                    title,
                    body,
                    created,
                    score,
                    rng.randint(0, 40),
                    # A small slice of history has since been deleted upstream.
                    deleted_upstream=(rng.random() < 0.04),
                )
            )

        # Two bot posts a week, to prove they are filtered.
        if day % 4 == 0:
            idx += 1
            items.append(
                _mk(
                    idx, "comment", rng.choice(SUBREDDITS), "AutoModerator", None,
                    rng.choice(BOT_POSTS).format(brand=BRAND),
                    day_start + timedelta(hours=9), 1, 0,
                )
            )

    # The day-14 spike: a billing incident, high volume and high severity.
    # Each template runs twice (the second as a different community reacting),
    # so the day clears mean + 3-sigma by a clear margin rather than a hair.
    spike_day = start + timedelta(days=14)
    for offset, template in enumerate(SPIKE_POSTS * 2):
        idx += 1
        items.append(
            _mk(
                idx,
                "post" if offset % 2 == 0 else "comment",
                "northbridge" if offset % 2 == 0 else rng.choice(SUBREDDITS),
                rng.choice(AUTHORS),
                TITLES["spike"][offset % len(TITLES["spike"])].format(brand=BRAND)
                if offset % 2 == 0
                else None,
                template.format(brand=BRAND),
                spike_day + timedelta(hours=8 + offset),
                rng.randint(80, 400),
                rng.randint(20, 160),
            )
        )

    # A near-duplicate crosspost of the loudest spike item. Same fullname on
    # purpose: this is what the dedupe key has to collapse.
    duplicate = dict(items[-4])
    items.append(duplicate)

    items.sort(key=lambda i: i["created_utc"])
    return items


DEMO_TERMS = [
    {
        "label": "Northbridge College",
        "match_type": "boolean",
        "pattern": '("northbridge college" OR "northbridge" OR nbc)',
        "negative_pattern": "northbridge road | northbridge mall",
    },
    {
        "label": "Enrolment issues",
        "match_type": "phrase",
        "pattern": "enrolment",
        "negative_pattern": None,
    },
    {
        "label": "Billing & fees",
        "match_type": "boolean",
        "pattern": '(tuition OR billing OR invoice OR "double charge" OR "double-charged")',
        "negative_pattern": None,
    },
]

DEMO_SUBREDDITS = list(SUBREDDITS)
