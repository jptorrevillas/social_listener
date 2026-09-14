"""Command-line entry points.

    python -m social_listener seed     # create schema, load terms + corpus
    python -m social_listener poll     # one capture cycle
    python -m social_listener sweep    # one compliance sweep
    python -m social_listener stats    # what is in the database
    python -m social_listener serve    # the web UI
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from .analytics import detect_spikes, totals
from .compliance import run_sweep
from .config import settings
from .db import init_db, reset_db, session_scope
from .demo.corpus import DEMO_SUBREDDITS, DEMO_TERMS
from .ingest import ingest_payloads, poll_subreddits
from .models import Subreddit, Term
from .reddit.source import DemoSource, get_source


def _banner() -> None:
    mode = settings.mode
    print(f"  mode: {mode.upper()}", file=sys.stderr)
    if mode == "demo":
        print(
            "  no Reddit credentials found -- running against the SYNTHETIC demo corpus.\n"
            "  set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for live capture.",
            file=sys.stderr,
        )


def cmd_seed(args: argparse.Namespace) -> int:
    if args.reset:
        reset_db()
    else:
        init_db()

    with session_scope() as session:
        for spec in DEMO_TERMS:
            if session.scalar(select(Term).where(Term.label == spec["label"])):
                continue
            session.add(Term(**spec))
        for name in DEMO_SUBREDDITS:
            if session.scalar(select(Subreddit).where(Subreddit.name == name)):
                continue
            session.add(Subreddit(name=name, poll_interval_secs=120))
        session.flush()

        source = DemoSource()
        stats = ingest_payloads(session, source.corpus)

    print("seeded.")
    for key, value in stats.as_dict().items():
        print(f"  {key:16} {value}")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    init_db()
    _banner()
    source = get_source()
    with session_scope() as session:
        stats = poll_subreddits(session, source, limit=args.limit)
    source.close()
    print("poll complete.")
    for key, value in stats.as_dict().items():
        print(f"  {key:16} {value}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    init_db()
    _banner()
    source = get_source()
    with session_scope() as session:
        stats = run_sweep(session, source)
    source.close()
    print("compliance sweep complete.")
    for key, value in stats.as_dict().items():
        print(f"  {key:16} {value}")
    if stats.tombstoned:
        print(
            f"  {stats.tombstoned} item(s) were deleted upstream and have been purged locally."
        )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        t = totals(session)
        spikes = detect_spikes(session)
    print(f"  items              {t.items}")
    print(f"  matched            {t.matched_items}")
    print(f"  negative/neutral/positive  {t.negative}/{t.neutral}/{t.positive}")
    print(f"  net sentiment      {t.net_sentiment:+.2%}")
    print(f"  spam filtered      {t.spam_filtered}")
    print(f"  escalated to LLM   {t.escalated}")
    print(f"  tombstoned         {t.tombstoned}")
    print(f"  open alerts        {t.alerts_open}")
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="social_listener")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create the schema and load the demo corpus")
    seed.add_argument("--reset", action="store_true", help="drop existing tables first")
    seed.set_defaults(func=cmd_seed)

    poll = sub.add_parser("poll", help="run one capture cycle")
    poll.add_argument("--limit", type=int, default=100)
    poll.set_defaults(func=cmd_poll)

    sweep = sub.add_parser("sweep", help="run one delete-compliance sweep")
    sweep.set_defaults(func=cmd_sweep)

    stats = sub.add_parser("stats", help="summarise the database")
    stats.set_defaults(func=cmd_stats)

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)
