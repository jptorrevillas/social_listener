"""The delete-compliance sweep is a contractual obligation, so it gets the
most pointed tests in the suite: content must actually disappear, the row must
survive, and derivative copies must be scrubbed too.
"""

from datetime import datetime

from sqlalchemy import func, select

from social_listener.compliance import run_sweep
from social_listener.ingest import ingest_payloads
from social_listener.models import Alert, Item

from .test_ingest import FakeSource, payload

LOUD = "Northbridge College tuition fraud, unacceptable, contacting a lawyer"


def test_items_still_present_upstream_are_untouched(seeded):
    items = [payload()]
    ingest_payloads(seeded, items)
    stats = run_sweep(seeded, FakeSource(items))
    assert stats.tombstoned == 0
    assert seeded.scalar(select(Item)).body is not None


def test_items_absent_from_the_response_are_tombstoned(seeded):
    ingest_payloads(seeded, [payload()])
    # Source returns nothing: the item is gone from Reddit.
    stats = run_sweep(seeded, FakeSource([]))
    assert stats.tombstoned == 1


def test_tombstoning_nulls_the_content_but_keeps_the_row(seeded):
    ingest_payloads(seeded, [payload(title="A title")])
    run_sweep(seeded, FakeSource([]))
    item = seeded.scalar(select(Item))
    assert item is not None, "the row must survive so aggregates stay valid"
    assert item.body is None
    assert item.title is None
    assert item.author is None
    assert item.is_deleted is True
    assert item.purged_at is not None


def test_the_author_hash_survives_for_analytics_continuity(seeded):
    ingest_payloads(seeded, [payload()])
    run_sweep(seeded, FakeSource([]))
    assert seeded.scalar(select(Item)).author_hash is not None


def test_aggregate_counts_survive_a_purge(seeded):
    ingest_payloads(seeded, [payload()])
    run_sweep(seeded, FakeSource([]))
    assert seeded.scalar(select(func.count(Item.id))) == 1


def test_deletion_markers_are_treated_as_deletion(seeded):
    ingest_payloads(seeded, [payload()])
    stats = run_sweep(seeded, FakeSource([payload(body="[deleted]")]))
    assert stats.tombstoned == 1
    assert seeded.scalar(select(Item)).body is None


def test_removed_marker_is_treated_as_deletion(seeded):
    ingest_payloads(seeded, [payload()])
    stats = run_sweep(seeded, FakeSource([payload(body="[removed]")]))
    assert stats.tombstoned == 1


def test_derivative_alert_copies_are_scrubbed(seeded):
    # A purged item still quoted in an old alert is still a copy.
    ingest_payloads(seeded, [payload(body=LOUD, score=900, num_comments=400)])
    assert seeded.scalar(select(func.count(Alert.id))) == 1
    stats = run_sweep(seeded, FakeSource([]))
    assert stats.alerts_purged == 1
    assert "purged" in seeded.scalar(select(Alert)).detail


def test_tombstoned_items_are_never_rendered(seeded):
    ingest_payloads(seeded, [payload()])
    run_sweep(seeded, FakeSource([]))
    item = seeded.scalar(select(Item))
    assert item.is_tombstoned
    assert "purged" in item.display_text


def test_a_second_sweep_does_not_re_purge(seeded):
    ingest_payloads(seeded, [payload()])
    run_sweep(seeded, FakeSource([]))
    second = run_sweep(seeded, FakeSource([]))
    assert second.checked == 0, "already-purged items drop out of the sweep"


def test_the_sweep_batches_by_hundred(seeded):
    many = [payload(fullname=f"t3_x{i}") for i in range(250)]
    ingest_payloads(seeded, many)
    stats = run_sweep(seeded, FakeSource(many))
    assert stats.batches == 3
    assert stats.checked == 250
