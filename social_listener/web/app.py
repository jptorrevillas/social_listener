"""The review UI.

§15 phase 2: the reviewable feed matters more than the dashboard. A listening
tool with a beautiful dashboard and an untrustworthy feed produces a page nobody
opens, so the feed shows the matched span and the classifier's confidence on
every row.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .. import analytics
from ..compliance import run_sweep
from ..config import settings
from ..db import init_db, session_scope
from ..ingest import poll_subreddits
from ..models import Alert, Item, Subreddit, Term
from ..reddit.source import get_source
from . import charts

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Social Listener")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _dedupe_spans(matches) -> list[dict]:
    """One span per term, and drop spans that are near-copies of one already shown.

    Several terms often hit the same sentence; three near-identical quotes make
    the row harder to scan, not easier.
    """
    out: list[dict] = []
    seen: list[str] = []
    for match in matches:
        if not match.matched_text:
            continue
        key = match.matched_text[:60]
        if any(key[:40] == existing[:40] for existing in seen):
            continue
        seen.append(key)
        out.append({"term": match.term.label, "text": match.matched_text})
    return out


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "mode": settings.mode,
        "is_demo": settings.mode == "demo",
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, flash: str | None = None):
    with session_scope() as session:
        totals = analytics.totals(session)
        series = analytics.volume_by_day(session)
        spikes = analytics.detect_spikes(session)
        topics = analytics.topic_breakdown(session)
        sov = analytics.share_of_voice(session)
        subs = analytics.top_subreddits(session)
        reach = analytics.estimated_reach(session)
        open_alerts = list(
            session.scalars(
                select(Alert)
                .where(Alert.acknowledged_at.is_(None))
                .order_by(Alert.severity.desc(), Alert.created_at.desc())
                .limit(6)
            )
        )
        alert_rows = [
            {
                "headline": a.headline,
                "detail": a.detail,
                "severity": a.severity,
                "permalink": a.item.permalink if a.item else None,
                # The headline already carries the title; repeating it here
                # just pushes the actual content off the card.
                "text": (
                    (a.item.body or a.item.title or "")[:170]
                    if a.item and not a.item.is_tombstoned
                    else (a.item.display_text if a.item else "")
                ),
            }
            for a in open_alerts
        ]

    context = _base_context(request)
    context.update(
        totals=totals,
        series=series,
        volume_chart=charts.stacked_volume(series),
        spikes=spikes,
        topics=topics,
        topic_bars=charts.hbars(topics, "topic", "count", negative_key="negative"),
        share_of_voice=sov,
        sov_bars=charts.hbars(sov, "label", "count"),
        subreddits=subs,
        sub_bars=charts.hbars(subs, "subreddit", "count"),
        reach=reach,
        alerts=alert_rows,
        flash=flash,
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/feed", response_class=HTMLResponse)
def feed(
    request: Request,
    sentiment: str | None = None,
    min_severity: int | None = None,
    subreddit: str | None = None,
):
    with session_scope() as session:
        rows = analytics.feed(
            session,
            sentiment=sentiment,
            min_severity=min_severity,
            subreddit=subreddit,
        )
        subs = [s["subreddit"] for s in analytics.top_subreddits(session, limit=12)]
        # Materialise before the session closes.
        entries = [
            {
                "fullname": r["item"].fullname,
                "subreddit": r["item"].subreddit,
                "author": r["item"].author,
                "permalink": r["item"].permalink,
                "created": r["item"].created_utc,
                "score": r["item"].score,
                "num_comments": r["item"].num_comments,
                "title": None if r["item"].is_tombstoned else r["item"].title,
                "body": r["item"].display_text if r["item"].is_tombstoned else r["item"].body,
                "is_tombstoned": r["item"].is_tombstoned,
                "kind": r["item"].kind,
                "sentiment": r["enrichment"].sentiment,
                "sentiment_score": float(r["enrichment"].sentiment_score or 0),
                "confidence": float(r["enrichment"].confidence or 0),
                "severity": r["enrichment"].severity or 1,
                "intent": r["enrichment"].intent,
                "rationale": r["enrichment"].rationale,
                "model": f"{r['enrichment'].model_name} {r['enrichment'].model_version}",
                "topics": r["topics"],
                "needs_review": r["needs_review"],
                "spans": _dedupe_spans(r["matches"]),
                "terms": [m.term.label for m in r["matches"]],
            }
            for r in rows
        ]

    context = _base_context(request)
    context.update(
        entries=entries,
        subreddits=subs,
        active_sentiment=sentiment,
        active_severity=min_severity,
        active_subreddit=subreddit,
    )
    return templates.TemplateResponse(request, "feed.html", context)


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    with session_scope() as session:
        rows = [
            {
                "label": t.label,
                "match_type": t.match_type,
                "pattern": t.pattern,
                "negative_pattern": t.negative_pattern,
                "is_active": t.is_active,
                "hits": len(t.matches),
            }
            for t in session.scalars(select(Term).order_by(Term.label))
        ]
        subs = [
            {
                "name": s.name,
                "interval": s.poll_interval_secs,
                "last_polled_at": s.last_polled_at,
                "source": s.source,
            }
            for s in session.scalars(select(Subreddit).order_by(Subreddit.name))
        ]
        tombstoned = session.scalar(
            select(Item).where(Item.purged_at.isnot(None)).limit(1)
        )
        tombstone_count = len(
            list(session.scalars(select(Item).where(Item.purged_at.isnot(None))))
        )

    context = _base_context(request)
    context.update(
        terms=rows,
        subreddits=subs,
        tombstone_count=tombstone_count,
        has_tombstones=tombstoned is not None,
    )
    return templates.TemplateResponse(request, "terms.html", context)


@app.post("/actions/poll")
def action_poll():
    source = get_source()
    with session_scope() as session:
        stats = poll_subreddits(session, source)
    source.close()
    message = (
        f"Poll complete: {stats.fetched} fetched, {stats.new_items} new, "
        f"{stats.duplicates} deduplicated, {stats.spam_filtered} bots filtered, "
        f"{stats.alerts} alert(s) raised."
    )
    return RedirectResponse(f"/?flash={message}", status_code=303)


@app.post("/actions/sweep")
def action_sweep():
    source = get_source()
    with session_scope() as session:
        stats = run_sweep(session, source)
    source.close()
    message = (
        f"Compliance sweep: {stats.checked} item(s) re-checked in {stats.batches} "
        f"batch(es) of 100, {stats.tombstoned} purged, "
        f"{stats.alerts_purged} derivative alert(s) scrubbed."
    )
    return RedirectResponse(f"/?flash={message}", status_code=303)


@app.post("/actions/acknowledge")
def action_acknowledge(alert_id: int = Form(...)):
    from datetime import datetime, timezone

    with session_scope() as session:
        alert = session.get(Alert, alert_id)
        if alert:
            alert.acknowledged_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return RedirectResponse("/", status_code=303)
