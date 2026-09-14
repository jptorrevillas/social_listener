"""The delete-compliance sweep (§7.4).

This is a contractual obligation, not a nicety: anyone accessing Reddit public
content must stop displaying or using it once the user or Reddit deletes it,
and Reddit can revoke access for non-compliance.

The mechanism is a rolling re-check of every retained item via /api/info in
batches of 100. Items absent from the response, or returned with
[deleted]/[removed] bodies, are TOMBSTONED rather than row-deleted: content is
nulled out, flags and purged_at are set, aggregate counts survive.

Derivatives matter as much as the primary store -- a tombstoned item still
quoted in an old alert is still a copy -- so alert bodies are purged too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Alert, Item
from .reddit.source import Source

log = logging.getLogger(__name__)

BATCH_SIZE = 100
DELETED_MARKERS = ("[deleted]", "[removed]")


@dataclass
class SweepStats:
    checked: int = 0
    tombstoned: int = 0
    alerts_purged: int = 0
    batches: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def tombstone(session: Session, item: Item, reason: str) -> int:
    """Null the content, keep the row. Returns the number of alerts purged.

    Aggregates built on this item stay valid; the content does not survive.
    """
    item.body = None
    item.title = None
    item.author = None  # author_hash survives for analytics continuity
    item.is_deleted = True
    item.purged_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Purge the derivative copies too.
    purged = 0
    for alert in session.scalars(select(Alert).where(Alert.item_id == item.id)):
        alert.detail = f"[purged: {reason}]"
        purged += 1

    log.info("tombstoned %s (%s)", item.fullname, reason)
    return purged


def run_sweep(session: Session, source: Source, batch_size: int = BATCH_SIZE) -> SweepStats:
    """Re-check every retained, not-yet-purged item."""
    stats = SweepStats()
    items = list(
        session.scalars(select(Item).where(Item.purged_at.is_(None)).order_by(Item.last_checked_at))
    )
    by_fullname = {item.fullname: item for item in items}

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        stats.batches += 1
        fullnames = [item.fullname for item in batch]
        returned = source.hydrate(fullnames)
        stats.checked += len(batch)

        seen: set[str] = set()
        for payload in returned:
            fullname = payload.get("fullname")
            if not fullname:
                continue
            seen.add(fullname)
            item = by_fullname.get(fullname)
            if item is None:
                continue
            body = (payload.get("body") or "").strip().lower()
            title = (payload.get("title") or "").strip().lower()
            if body in DELETED_MARKERS or title in DELETED_MARKERS:
                stats.alerts_purged += tombstone(
                    session, item, "content replaced with a deletion marker"
                )
                stats.tombstoned += 1
            else:
                item.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # Absent from the response == gone from Reddit. §4 calls this out: diff
        # the returned names against what you asked for.
        for fullname in fullnames:
            if fullname in seen:
                continue
            item = by_fullname[fullname]
            stats.alerts_purged += tombstone(
                session, item, "absent from /api/info response"
            )
            stats.tombstoned += 1

    session.flush()
    return stats
