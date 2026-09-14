from datetime import datetime, timedelta

from sqlalchemy import func, select

from social_listener.compliance import run_sweep
from social_listener.ingest import ingest_payloads
from social_listener.models import Alert, Enrichment, Item, Match


def payload(fullname="t3_a1", body="Northbridge College tuition is a problem", **kw):
    base = {
        "fullname": fullname,
        "kind": "post",
        "subreddit": "testsub",
        "author": "someone",
        "title": None,
        "body": body,
        "permalink": "https://reddit.com/r/testsub/comments/a1/",
        "parent_fullname": None,
        "link_fullname": None,
        "created_utc": datetime(2026, 9, 1, 12, 0),
        "score": 10,
        "num_comments": 2,
        "edited_at": None,
        "provenance": "demo",
    }
    base.update(kw)
    return base


class FakeSource:
    """Returns exactly what it is told to, including nothing."""

    def __init__(self, items):
        self.items = items

    def fetch_subreddit(self, subreddit, limit=100):
        return self.items

    def search(self, query, limit=100):
        return self.items

    def hydrate(self, fullnames):
        return [i for i in self.items if i["fullname"] in set(fullnames)]

    def close(self):
        pass


def test_unmatched_items_are_not_stored(seeded):
    stats = ingest_payloads(seeded, [payload(body="completely unrelated chatter")])
    assert stats.unmatched == 1
    assert seeded.scalar(select(func.count(Item.id))) == 0


def test_matched_items_are_stored_once(seeded):
    ingest_payloads(seeded, [payload()])
    assert seeded.scalar(select(func.count(Item.id))) == 1


def test_the_same_fullname_never_creates_a_second_row(seeded):
    # The dedupe key is structural: retries and overlapping poll windows
    # guarantee duplicates, so this is the property that matters most.
    stats = ingest_payloads(seeded, [payload(), payload(), payload()])
    assert seeded.scalar(select(func.count(Item.id))) == 1
    assert stats.duplicates == 2


def test_a_duplicate_does_not_duplicate_enrichment(seeded):
    ingest_payloads(seeded, [payload(), payload()])
    assert seeded.scalar(select(func.count(Enrichment.id))) == 1


def test_a_duplicate_does_not_duplicate_matches(seeded):
    ingest_payloads(seeded, [payload(), payload()])
    counts = seeded.scalar(select(func.count(Match.id)))
    # Two terms match this text, and re-ingesting must not double them.
    assert counts == 2


def test_re_ingesting_updates_mutable_fields(seeded):
    ingest_payloads(seeded, [payload(score=10)])
    ingest_payloads(seeded, [payload(score=250, num_comments=99)])
    item = seeded.scalar(select(Item))
    assert item.score == 250
    assert item.num_comments == 99


def test_an_item_can_match_several_terms(seeded):
    ingest_payloads(seeded, [payload()])
    item = seeded.scalar(select(Item))
    assert {m.term.label for m in item.matches} == {"Brand", "Billing"}


def test_matched_span_is_recorded(seeded):
    ingest_payloads(seeded, [payload()])
    item = seeded.scalar(select(Item))
    assert all(m.matched_text for m in item.matches)


def test_high_severity_raises_one_alert(seeded):
    ingest_payloads(
        seeded,
        [payload(body="Northbridge College tuition fraud, absolutely unacceptable, calling a lawyer", score=900, num_comments=400)],
    )
    assert seeded.scalar(select(func.count(Alert.id))) == 1


def test_one_alert_per_thread_not_per_item(seeded):
    loud = "Northbridge College tuition fraud, unacceptable, contacting a lawyer"
    ingest_payloads(
        seeded,
        [
            payload(fullname="t1_c1", kind="comment", body=loud, link_fullname="t3_thread", score=900, num_comments=400),
            payload(fullname="t1_c2", kind="comment", body=loud, link_fullname="t3_thread", score=900, num_comments=400),
        ],
    )
    assert seeded.scalar(select(func.count(Alert.id))) == 1


def test_low_severity_raises_no_alert(seeded):
    ingest_payloads(seeded, [payload(body="Northbridge College tuition question, thanks!", score=1)])
    assert seeded.scalar(select(func.count(Alert.id))) == 0


def test_author_hash_is_recorded_for_analytics(seeded):
    ingest_payloads(seeded, [payload()])
    item = seeded.scalar(select(Item))
    assert item.author_hash and item.author_hash != item.author
