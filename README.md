# social_listener

A Reddit social listening tool: captures mentions of a set of watch terms,
classifies them, stores them, and alerts a human.

**It runs with no Reddit credentials.** Obtaining them is a manual approval
process that takes days, so the app ships with a synthetic corpus and switches
to live capture the moment credentials appear — same code path, same pipeline.

📄 **[docs/SPECIFICATION.md](docs/SPECIFICATION.md)** — the research and design
rationale behind every decision here. Section references in the code (§3.3, §7.4)
point into it.

---

## Run the demo

**Requires Python 3.9 or newer.** Check with `python --version`. It is tested on
3.11; 3.9 is supported but reached end of life in October 2025, so 3.11+ is the
better choice if you get to pick.

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m social_listener seed --reset     # schema + watch terms + corpus
python -m social_listener serve            # http://127.0.0.1:8000
```

**Windows (PowerShell)**

```powershell
py -3.11 -m venv .venv     # or: py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m social_listener seed --reset
python -m social_listener serve
```

If PowerShell refuses to run the activate script, either use
`.venv\Scripts\activate.bat` or allow scripts for the current session:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

That's the whole setup. No database server, no API key, no build step.

```bash
python -m social_listener stats      # what is in the database
python -m social_listener poll       # one capture cycle
python -m social_listener sweep      # one delete-compliance sweep
pytest -q                            # 75 tests
```

Requirements are specified as **floors** (`>=`), not exact pins, so they resolve
against whatever your package index currently carries. Each floor sits at the
release that introduced an API this code actually uses, so a genuinely
too-old version fails loudly instead of subtly.

### ⚠️ The demo data is fabricated

"Northbridge College" is a **fictional institution** and every post is generated
from a template. None of it is real Reddit content and none of it describes a
real organisation. The UI carries a permanent banner saying so. **Keep that
banner** — a screenshot of this feed must never be mistakable for real public
sentiment about a real place.

## Going live

```bash
cp .env.example .env     # fill in REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET
```

The banner turns green, `get_source()` returns `LiveRedditSource` instead of
`DemoSource`, and nothing else changes. Getting those credentials is phase 0 of
the plan in §15 — it is the riskiest unknown in the project and the cheapest to
test, which is exactly why the rest of the system was built not to depend on it.

## What to show in a demo

Five things that are worth pausing on, because each is a decision the
specification argues for and the code actually implements:

1. **The bot filter runs before the classifier.** `Bots filtered: 6` on the
   dashboard. AutoModerator boilerplate mentions the brand, so it passes the
   matching tier — and gets caught by the spam lane before any model spends a
   cent on it.
2. **Sarcasm is caught by admitting ignorance, not by being clever.** Filter the
   feed and find a row tagged *low confidence — needs a human*. The lexicon
   scores "Oh great, the portal is down again. Thanks a lot /s" as **positive**.
   It is wrong. What matters is that it says so with 35% confidence and routes
   the item to a human instead of asserting it.
3. **Every row shows why it was flagged.** The matched span is quoted under each
   item, labelled with the term that hit. A reviewer who cannot see why an item
   was flagged stops trusting the feed, and a feed nobody trusts is a feed
   nobody reads.
4. **Spike detection is relative, not absolute.** The planted billing incident
   fires at 14 mentions against a rolling baseline of 3.0 (mean + 3σ). A fixed
   threshold would break the first time overall volume changed.
5. **Press "Run compliance sweep".** Items deleted upstream are purged locally —
   content, title and author nulled, the row kept so the counts stay valid, and
   the cached alert bodies scrubbed in the same pass. This is a contractual
   obligation under Reddit's terms, not a nicety, and it is the thing most
   likely to be breached by accident via exports and caches.

## How it fits together

```
DemoSource / LiveRedditSource     one interface, chosen by whether creds exist
        │
        ▼
   normalise          Reddit's shape -> one schema
        │
   dedupe             fullname is the key; the database enforces it
        │
   match              literal -> phrase -> boolean -> negative patterns
        │             (~99% of the stream is rejected here, before any model)
        ▼
   enrich             VADER + rules; escalation flagged, not yet routed
        │
   store + alert      severity 4-5 pages a human, one alert per thread
```

| Module | Does |
|---|---|
| `config.py` | Settings; decides demo vs live |
| `ratelimit.py` | Token bucket steered by Reddit's `X-Ratelimit-*` headers |
| `reddit/client.py` | OAuth `client_credentials`, batching, 401/429 retry |
| `reddit/source.py` | The demo/live seam |
| `matching.py` | Tiered matcher with a hand-written boolean parser |
| `enrichment.py` | Sentiment, intent, severity, topics, bot filter |
| `ingest.py` | The pipeline; upsert, match, enrich, alert |
| `compliance.py` | The delete-compliance sweep |
| `analytics.py` | Metric definitions — fixed early, on purpose |
| `web/` | Dashboard and review feed |

## What is deliberately not built yet

Being straight about this matters more than a longer feature list:

- **The transformer and LLM classification lanes.** `enrichment.py` defines the
  `Enricher` interface and already computes which items *would* escalate, so the
  cost projection is real. But only the lexicon lane runs. Adding the others is
  a registration, not a rewrite.
- **The discovery loop.** `Source.search()` exists and is tested; nothing calls
  it on a schedule yet. Tracked communities are configured by hand.
- **Archive backfill.** Live capture only. History needs Arctic Shift or
  PullPush — §2 and §5.5.
- **A Redis-backed limiter.** The current one is in-process and thread-safe,
  which is correct for one worker and wrong for several. The interface is the
  one a Redis version would expose, so the swap touches one file.
- **Alert delivery.** Alerts are raised and displayed, not emailed or texted.
- **Adaptive poll intervals.** The schema carries `observed_items_hour` and the
  arithmetic is in §5.2, but intervals are currently static.

### One number that will not match the specification

§12 budgets the expensive classification lane at 5–15% of matched volume. The
demo reports **~45%**, because the synthetic corpus is deliberately
complaint-heavy — it exists to exercise the severity and escalation paths, not
to model a realistic sentiment mix. On a real corpus, where most mentions are
neutral chatter, expect the figure to fall back toward the specification's
range. Worth saying out loud before someone in the room does the arithmetic on
the LLM bill.
