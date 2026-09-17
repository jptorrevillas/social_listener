"""Command-line entry points.

    python -m social_listener seed      # schema + watch terms + demo corpus
    python -m social_listener harvest   # channel uploads, then comments
    python -m social_listener discover  # keyword search (expensive: 100 units)
    python -m social_listener retention # refresh or purge, per the 30-day rule
    python -m social_listener quota     # where today's units went
    python -m social_listener stats     # what is in the database
    python -m social_listener serve     # the web UI
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from . import ledger
from .analytics import detect_spikes, totals
from .config import settings
from .db import init_db, reset_db, session_scope
from .demo.corpus import CHANNELS, DEMO_TERMS
from .harvest import harvest_channels, run_discovery
from .models import Channel, WatchTerm
from .quota import METHOD_COSTS, QuotaExhausted
from .retention import expiry_report, run_retention
from .youtube.source import DemoSource, get_source


def _banner() -> None:
    print(f"  mode: {settings.mode.upper()}", file=sys.stderr)
    if settings.mode == "demo":
        print(
            "  no YOUTUBE_API_KEY found -- running against the SYNTHETIC demo corpus.\n"
            "  set YOUTUBE_API_KEY for live capture.",
            file=sys.stderr,
        )


def _print_stats(stats) -> None:
    for key, value in stats.as_dict().items():
        print(f"  {key:26} {value}")


def cmd_seed(args: argparse.Namespace) -> int:
    reset_db() if args.reset else init_db()

    with session_scope() as session:
        for spec in DEMO_TERMS:
            if session.scalar(select(WatchTerm).where(WatchTerm.label == spec["label"])):
                continue
            session.add(WatchTerm(**spec))
        for spec in CHANNELS:
            if session.scalar(
                select(Channel).where(Channel.youtube_id == spec["youtube_id"])
            ):
                continue
            session.add(Channel(source="demo", **spec))
        session.flush()

        stats = harvest_channels(session, DemoSource())

    print("seeded.")
    _print_stats(stats)
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    init_db()
    _banner()
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = harvest_channels(session, source, comment_pages=args.pages)
        except QuotaExhausted as exc:
            print(f"  stopped: {exc}")
            return 1
        finally:
            source.close()
    print("harvest complete.")
    _print_stats(stats)
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    init_db()
    _banner()
    print(
        f"  each search costs {METHOD_COSTS['search.list']} units "
        f"({METHOD_COSTS['commentThreads.list']} unit per comment page)"
    )
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = run_discovery(session, source, max_searches=args.max_searches)
        except QuotaExhausted as exc:
            print(f"  stopped: {exc}")
            return 1
        finally:
            source.close()
    print("discovery complete.")
    _print_stats(stats)
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    init_db()
    _banner()
    with session_scope() as session:
        source = get_source(session)
        try:
            stats = run_retention(session, source)
        finally:
            source.close()
        report = expiry_report(session)

    print("retention pass complete.")
    _print_stats(stats)
    print("\n  position:")
    for key, value in report.items():
        print(f"    {key:22} {value}")
    if stats.purged:
        print(
            f"\n  {stats.purged} record(s) purged: "
            f"{stats.purged_deleted_upstream} gone from YouTube, "
            f"{stats.purged_expired} past the {report['retention_days']}-day window."
        )
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    init_db()
    from .quota import seconds_until_reset

    with session_scope() as session:
        current = ledger.status(session)
    print(f"  quota day        {current.day} (America/Los_Angeles)")
    print(f"  spent            {current.spent} / {current.limit} units "
          f"({current.fraction_used:.0%})")
    print(f"  remaining        {current.remaining}")
    print(f"  searches left    {current.searches_remaining()}")
    print(f"  resets in        {seconds_until_reset() // 3600}h "
          f"{(seconds_until_reset() % 3600) // 60}m")
    if current.by_method:
        print("\n  by method:")
        for method, units in sorted(
            current.by_method.items(), key=lambda kv: kv[1], reverse=True
        ):
            print(f"    {method:24} {units:>6} units")
    else:
        print("\n  nothing spent today (demo mode spends nothing)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        t = totals(session)
        spikes = detect_spikes(session)
        report = expiry_report(session)
    print(f"  mentions                 {t.mentions}")
    print(f"  videos                   {t.videos} ({t.comments_disabled_videos} with comments off)")
    print(f"  negative/neutral/positive {t.negative}/{t.neutral}/{t.positive}")
    print(f"  net sentiment            {t.net_sentiment:+.2%}")
    print(f"  spam filtered            {t.spam_filtered}")
    print(f"  escalated                {t.escalated}")
    print(f"  purged                   {t.purged}")
    print(f"  due for refresh          {report['due_for_refresh']}")
    print(f"  open alerts              {t.alerts_open}")
    for spike in spikes:
        print(
            f"  SPIKE {spike['date']}: {spike['total']} mentions "
            f"(baseline {spike['baseline_mean']}, threshold {spike['threshold']})"
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    init_db()
    _banner()
    uvicorn.run(
        "social_listener.web.app:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="social_listener")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create the schema and load the demo corpus")
    seed.add_argument("--reset", action="store_true", help="drop existing tables first")
    seed.set_defaults(func=cmd_seed)

    harvest = sub.add_parser("harvest", help="poll tracked channels and their comments")
    harvest.add_argument("--pages", type=int, default=None, help="comment pages per video")
    harvest.set_defaults(func=cmd_harvest)

    discover = sub.add_parser("discover", help="keyword search (100 units per search)")
    discover.add_argument("--max-searches", type=int, default=None, dest="max_searches")
    discover.set_defaults(func=cmd_discover)

    retention = sub.add_parser("retention", help="refresh or purge under the 30-day rule")
    retention.set_defaults(func=cmd_retention)

    quota = sub.add_parser("quota", help="today's unit spend")
    quota.set_defaults(func=cmd_quota)

    stats = sub.add_parser("stats", help="summarise the database")
    stats.set_defaults(func=cmd_stats)

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)
