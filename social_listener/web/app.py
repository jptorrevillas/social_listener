"""The review UI.

The reviewable feed matters more than the dashboard: a listening tool with
beautiful charts and an untrustworthy feed produces a page nobody opens. So
every row carries the matched span, the classifier's confidence, and a visible
flag when the model is unsure.

Two panels exist here that the Reddit build had no need for, and they are the
two things an operator actually has to watch on YouTube:

  QUOTA      10,000 units a day, and a single careless search loop eats 20% of
             it. The breakdown shows where the units went.
  RETENTION  the 30-day clock, how many records are due for refresh, and how
             many have been purged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from contextlib import asynccontextmanager

from .. import analytics, ledger
from ..config import settings
from ..db import init_db, session_scope
from ..harvest import harvest_channels, run_discovery
from ..models import Alert, Channel, Video, WatchTerm, utcnow
from ..quota import METHOD_COSTS, QuotaExhausted, seconds_until_reset
from ..retention import RECONCILE_AFTER_DAYS, expiry_report, run_retention
from ..youtube.source import get_source
from . import charts

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Social Listener", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _base_context(request: Request, session) -> dict:
    quota = ledger.status(session)
    return {
        "request": request,
        "mode": settings.mode,
        "is_demo": settings.mode == "demo",
        "quota": quota,
        "quota_resets_in": f"{seconds_until_reset() // 3600}h "
        f"{(seconds_until_reset() % 3600) // 60}m",
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, flash: Optional[str] = None):
    with session_scope() as session:
        context = _base_context(request, session)
        totals = analytics.totals(session)
        series = analytics.volume_by_day(session)
        spikes = analytics.detect_spikes(session)
        topics = analytics.topic_breakdown(session)
        sov = analytics.share_of_voice(session)
        videos = analytics.top_videos(session)
        retention = expiry_report(session)
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
                "id": alert.id,
                "headline": alert.headline,
                "detail": alert.detail,
                "severity": alert.severity,
                "text": (alert.mention.display_text[:180] if alert.mention else ""),
                "url": alert.mention.url if alert.mention else None,
                "purged": bool(alert.mention and alert.mention.is_purged),
            }
            for alert in open_alerts
        ]

        context.update(
            totals=totals,
            series=series,
            volume_chart=charts.stacked_volume(series),
            spikes=spikes,
            topics=topics,
            topic_bars=charts.hbars(topics, "topic", "count", negative_key="negative"),
            share_of_voice=sov,
            sov_bars=charts.hbars(sov, "label", "count"),
            videos=videos,
            retention=retention,
            reconcile_days=RECONCILE_AFTER_DAYS,
            reach=reach,
            alerts=alert_rows,
            quota_meter=charts.quota_meter(context["quota"]),
            quota_breakdown=charts.quota_breakdown(context["quota"]),
            search_cost=METHOD_COSTS["search.list"],
            comment_cost=METHOD_COSTS["commentThreads.list"],
            flash=flash,
        )
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/feed", response_class=HTMLResponse)
def feed(
    request: Request,
    sentiment: Optional[str] = None,
    min_severity: Optional[int] = None,
    video: Optional[str] = None,
    spam: Optional[int] = None,
):
    with session_scope() as session:
        context = _base_context(request, session)
        rows = analytics.feed(
            session,
            sentiment=sentiment,
            min_severity=min_severity,
            video=video,
            include_spam=bool(spam),
        )
        video_options = [
            {"youtube_id": v["youtube_id"], "title": v["title"][:44]}
            for v in analytics.top_videos(session, limit=8)
        ]

        entries = []
        for row in rows:
            mention, enrichment = row["mention"], row["enrichment"]
            entries.append(
                {
                    "kind": mention.kind,
                    "author": mention.author_name,
                    "text": mention.display_text,
                    "purged": mention.is_purged,
                    "days_left": mention.days_until_expiry(),
                    "url": mention.url,
                    "video_title": mention.video.display_title if mention.video else "",
                    "channel": mention.video.channel_title if mention.video else "",
                    "published": mention.published_at,
                    "edited": mention.updated_at is not None,
                    "likes": mention.like_count,
                    "replies": mention.reply_count,
                    "sentiment": enrichment.sentiment,
                    "sentiment_score": float(enrichment.sentiment_score or 0),
                    "confidence": float(enrichment.confidence or 0),
                    "severity": enrichment.severity or 1,
                    "intent": enrichment.intent,
                    "is_spam": enrichment.is_spam,
                    "rationale": enrichment.rationale,
                    "model": f"{enrichment.model_name} {enrichment.model_version}",
                    "topics": row["topics"],
                    "needs_review": row["needs_review"],
                    "spans": _dedupe_spans(mention.matches),
                    "terms": [m.term.label for m in mention.matches],
                }
            )

        context.update(
            entries=entries,
            video_options=video_options,
            active_sentiment=sentiment,
            active_severity=min_severity,
            active_video=video,
            show_spam=bool(spam),
        )
    return templates.TemplateResponse(request, "feed.html", context)


def _dedupe_spans(matches) -> list:
    """One span per term, dropping near-copies of one already shown."""
    out, seen = [], []
    for match in matches:
        if not match.matched_text:
            continue
        key = match.matched_text[:60]
        if any(key[:40] == existing[:40] for existing in seen):
            continue
        seen.append(key)
        out.append({"term": match.term.label, "text": match.matched_text})
    return out


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    with session_scope() as session:
        context = _base_context(request, session)
        terms = [
            {
                "label": term.label,
                "match_type": term.match_type,
                "pattern": term.pattern,
                "negative_pattern": term.negative_pattern,
                "use_in_discovery": term.use_in_discovery,
                "hits": len(term.matches),
            }
            for term in session.scalars(select(WatchTerm).order_by(WatchTerm.label))
        ]
        channels = [
            {
                "youtube_id": channel.youtube_id,
                "title": channel.title,
                "is_owned": channel.is_owned,
                "last_polled_at": channel.last_polled_at,
                "videos": len(channel.videos),
            }
            for channel in session.scalars(select(Channel).order_by(Channel.title))
        ]
        videos = [
            {
                "youtube_id": video.youtube_id,
                "title": video.display_title,
                "comments_disabled": video.comments_disabled,
                "days_left": video.days_until_expiry(),
                "purged": video.is_purged,
                "mentions": len(video.mentions),
            }
            for video in session.scalars(select(Video).order_by(Video.published_at.desc()))
        ]
        context.update(
            terms=terms,
            channels=channels,
            videos=videos,
            retention=expiry_report(session),
            reconcile_days=RECONCILE_AFTER_DAYS,
            method_costs=sorted(METHOD_COSTS.items(), key=lambda kv: -kv[1]),
        )
    return templates.TemplateResponse(request, "config.html", context)


# -- actions ---------------------------------------------------------------


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse(f"/?flash={message}", status_code=303)


@app.post("/actions/harvest")
def action_harvest():
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = harvest_channels(session, source)
        except QuotaExhausted as exc:
            return _redirect(f"Harvest stopped: {exc}")
        finally:
            source.close()
        message = (
            f"Harvest: {stats.channels_polled} channel(s), {stats.comments_fetched} "
            f"comment(s), {stats.duplicates} deduplicated, {stats.spam_filtered} spam "
            f"filtered, {stats.alerts} alert(s). Quota spent today: {stats.quota_spent}."
        )
    return _redirect(message)


@app.post("/actions/discover")
def action_discover():
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = run_discovery(session, source)
        except QuotaExhausted as exc:
            return _redirect(f"Discovery stopped: {exc}")
        finally:
            source.close()
        message = (
            f"Discovery: {stats.searches_run} search(es) at "
            f"{METHOD_COSTS['search.list']} units each, {stats.comments_fetched} "
            f"comment(s) read. Quota spent today: {stats.quota_spent}."
        )
    return _redirect(message)


@app.post("/actions/retention")
def action_retention():
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = run_retention(session, source)
        finally:
            source.close()
        message = (
            f"Retention: {stats.refreshed} refreshed (30-day clock restarted), "
            f"{stats.purged_deleted_upstream} purged as gone from YouTube, "
            f"{stats.purged_expired} purged past the window, "
            f"{stats.alerts_scrubbed} alert body/bodies scrubbed."
        )
    return _redirect(message)


@app.post("/actions/acknowledge")
def action_acknowledge(alert_id: int = Form(...)):
    with session_scope() as session:
        alert = session.get(Alert, alert_id)
        if alert:
            alert.acknowledged_at = utcnow()
    return RedirectResponse("/", status_code=303)
