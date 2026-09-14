# Technical Specification Research: A Reddit Social Listening Tool

Research note. Last revised 10 September 2026.

**Scope.** This document specifies what it takes to build a system that watches
Reddit for mentions of a set of terms, classifies them, stores them, and alerts
a human. It covers data acquisition, the API's hard limits, ingestion
architecture, the data model, enrichment, alerting, cost, and compliance.

**Status.** Research and specification only. No implementation exists in this
repository yet; every section below is a design position to be argued with, not
a description of shipped code. Figures sourced from secondary reporting are
marked as such.

---

## 1. Executive summary

Five findings drive every design decision that follows.

1. **There is no firehose.** Reddit exposes no streaming or webhook API for
   public content. Every "real-time" Reddit tool in existence is polling
   listing endpoints on a timer. Latency is a budget decision, not a platform
   feature.
2. **The rate limit is the architecture.** 100 queries per minute per OAuth
   client — about 144,000 calls/day — is the single scarce resource. The whole
   ingestion design is an exercise in spending those calls well.
3. **The free tier is non-commercial, and access is now gated.** Since the
   Responsible Builder Policy update (5 June 2026), new OAuth clients go
   through manual approval rather than self-service registration, and
   commercial use requires written approval and a negotiated licence.
4. **The commercial cliff is the reason this product category thinned out.**
   Reported commercial terms are ~$0.24 per 1,000 calls with a bundled tier
   around $12,000/month for 50M calls. There is nothing between free and that.
   GummySearch — the best-known Reddit listening tool — stopped new signups on
   30 November 2025 citing exactly this.
5. **Historical coverage is a separate problem from live coverage.** Listing
   endpoints cap at ~1,000 items per query, so the Data API cannot answer
   "everything ever said about X". Backfill needs an external archive
   (Arctic Shift, PullPush) with its own, weaker guarantees.

**Recommended shape:** a scoped, keyword-and-subreddit-driven poller on the free
tier, with an archive-backed one-time backfill, LLM-assisted classification, and
a strict delete-compliance job. Budget the API calls explicitly; treat the
100 QPM ceiling as a design constraint from day one, not a scaling concern.

---

## 2. Data acquisition options

| Route | Coverage | Latency | Cost | Legal standing | Verdict |
|---|---|---|---|---|---|
| **Reddit Data API (OAuth)** | Live + ~1,000 items back per query | Poll-bound; 30s–5min achievable | Free (non-commercial) / negotiated | Sanctioned, with approval | **Primary** |
| **Arctic Shift** | 2005→present, ~2.5B items, monthly dumps | 4–6 week lag | Free | Third-party archive | **Backfill** |
| **PullPush** | Cross-subreddit historical search | Hours–days | Free | Third-party archive, no SLA | Fallback only |
| **Academic Torrents dumps** | ~4 TB, 40k subreddits, 2005–2025 | Monthly | Free | Research use | Bulk corpus / model training |
| **Commercial resellers** (Apify, SocialCrawl, redditapis, et al.) | Varies | Varies | ~$0.002–0.30 / call | Reseller's risk, not yours | Escape hatch if approval is denied |
| **HTML scraping / old.reddit** | Anything visible | Any | Infra only | **Violates the Developer Terms** | Do not |

### Why not scraping

Beyond the terms issue, scraping Reddit is operationally worse than it looks:
Cloudflare challenges, `.json` suffix endpoints that are rate-limited more
aggressively than OAuth, no `X-Ratelimit-*` headers to steer by, and no legal
basis for retaining what you collect. The 100 QPM free tier is more generous
than a residential-proxy budget of comparable throughput. Use the API.

### On the archives

Arctic Shift is the practical Pushshift successor: free, unauthenticated,
roughly 120k requests/hour, published as Parquet on Hugging Face and updated
monthly. Its important limitation for a listening tool is that **full-text
search is scoped to a single subreddit** — there is no global keyword search.
PullPush retains cross-subreddit search (the only free source that does) but
runs near 1,000 req/hour with a documented history of outages.

Design consequence: **the archive gives you depth in known communities, not
discovery across unknown ones.** Discovery of new communities must come from
the live API's `/search` endpoints.

---

## 3. Authentication and rate limits

### 3.1 OAuth

| Item | Value |
|---|---|
| Token endpoint | `POST https://www.reddit.com/api/v1/access_token` |
| API host | `https://oauth.reddit.com` |
| Grant for a server-side listener | `client_credentials` (confidential client) |
| Token lifetime | **1 hour**; no refresh token issued under `client_credentials` |
| Scope needed for read-only listening | `read` |
| App type to register | *script* (own hardware, holds a secret) or *web* |

Register as a **script** app if the listener runs on infrastructure you control
and never acts on behalf of an end user. `client_credentials` gives
application-only access, which is all a listener needs — it reads public
content and never posts.

Token handling: refresh at ~50 minutes, cache the token in Redis (not
per-process) so a fleet of workers shares one token, and treat a `401` as
"refresh once, then retry once" rather than a fatal error.

### 3.2 User-Agent

Mandatory format, enforced:

```
<platform>:<app ID>:<version> (by /u/<reddit username>)
```

e.g. `server:com.example.listener:v0.3.1 (by /u/example_ops)`

Never spoof a browser. Include a real version number — Reddit uses it to block
specific buggy client versions rather than the whole app.

### 3.3 Rate limits

| Client | Limit |
|---|---|
| OAuth-authenticated | **100 QPM per client ID**, averaged over a 10-minute window |
| Unauthenticated | ~10 QPM |

The limit is **per API key, not per end user** — a multi-tenant product does
not get more headroom by adding customers, which is precisely the economic
trap that killed the mid-market tools.

Three headers come back on every call and are the only reliable source of
truth:

```
X-Ratelimit-Used        requests consumed this period
X-Ratelimit-Remaining   requests left this period
X-Ratelimit-Reset       seconds until the window resets
```

**Note on conflicting figures.** Reddit's archived developer wiki still states
60 requests/minute. Current policy documentation and practice is 100 QPM
averaged over 10 minutes. Build the limiter to *read the headers* and treat any
hard-coded number as a fallback — that way a policy change degrades throughput
instead of causing a ban.

### 3.4 Limiter design

- A single **Redis token bucket** shared by all workers, refilled from
  `X-Ratelimit-Remaining` after each call rather than from a static assumption.
- Reserve ~15% of the budget as headroom for retries, hydration, and
  delete-compliance sweeps.
- On `429`: exponential backoff from 2s, honour `X-Ratelimit-Reset`, and
  **shed low-priority work first** (revisits before discovery).
- Per-worker concurrency cap so a burst of workers cannot collectively blow the
  10-minute average.

---

## 4. Endpoint inventory

The endpoints that matter for listening, and what each one costs.

| Purpose | Endpoint | Yield | Notes |
|---|---|---|---|
| New posts in a subreddit | `GET /r/{sub}/new?limit=100` | ≤100 posts | The workhorse for tracked communities |
| New comments in a subreddit | `GET /r/{sub}/comments?limit=100` | ≤100 comments | Flat, chronological — no tree walk needed |
| Keyword search, one subreddit | `GET /r/{sub}/search?q=…&restrict_sr=1&sort=new` | ≤1,000 total | Reddit's index, not exhaustive |
| Keyword search, site-wide | `GET /search?q=…&sort=new` | ≤1,000 total | Discovery of unknown communities |
| Global comment stream | `GET /r/all/comments?limit=100` | ≤100 | Only viable for very high poll rates |
| Hydrate / refresh by ID | `GET /api/info?id=t3_x,t1_y,…` | **100 IDs per call** | The cheapest call in the API |
| Expand collapsed comment trees | `POST /api/morechildren` | Batch | Needs `link_id` + comma-separated ID36s |
| Post + full comment tree | `GET /r/{sub}/comments/{id}` | One thread | Expensive; use only for threads that matter |

Three properties to design around:

- **`limit` maxes at 100** (default 25). Always ask for 100.
- **`before`/`after` are listing cursors, not timestamps.** They mean "before/
  after in this listing", which is not the same as chronological order once
  sorting or moderation intervenes.
- **Listings terminate at ~1,000 items.** Ten pages of 100. This is the hard
  ceiling on any single query and the reason backfill needs an archive.

**`/api/info` is the efficiency lever.** One call refreshes 100 items. Any
design that re-fetches items one at a time is spending 100× more budget than
necessary. Always chunk into 100s, and diff the returned `name` values against
what you requested — items missing from the response were deleted or removed,
which is a signal in its own right (see [§7.4](#74-delete-compliance)).

---

## 5. Ingestion architecture

Four independent loops, each with its own budget allocation and its own
priority under rate-limit pressure.

```
                    ┌──────────────────────────────────────────┐
                    │  Redis: token bucket + scheduler queues   │
                    └──────────────────────────────────────────┘
                        ▲          ▲          ▲          ▲
        ┌───────────────┘   ┌──────┘    ┌─────┘     ┌────┘
        │                   │           │           │
   ┌────┴─────┐      ┌──────┴─────┐ ┌───┴─────┐ ┌───┴────────┐
   │ DISCOVERY│      │ SUBREDDIT  │ │ REVISIT │ │ COMPLIANCE │
   │  search  │      │   poll     │ │ hydrate │ │   sweep    │
   │ site-wide│      │ new+comments│ │ /api/info│ │  /api/info │
   └────┬─────┘      └──────┬─────┘ └───┬─────┘ └───┬────────┘
        └────────────┬──────┴───────────┴───────────┘
                     ▼
              ┌──────────────┐
              │  NORMALISE   │  one schema, stable IDs
              │  + DEDUPE    │  fullname is the primary key
              └──────┬───────┘
                     ▼
              ┌──────────────┐
              │    MATCH     │  which watch terms did this hit?
              └──────┬───────┘
                     ▼
              ┌──────────────┐
              │   ENRICH     │  sentiment · intent · spam · topic
              └──────┬───────┘
                     ▼
        ┌────────────┴────────────┐
        ▼                         ▼
   ┌─────────┐              ┌──────────┐
   │  STORE  │              │  ALERT   │
   │  MySQL  │              │ email/SMS│
   └─────────┘              └──────────┘
```

### 5.1 Discovery loop

Site-wide `/search` per watch term, sorted by `new`, on a slow cadence
(every 15–30 min). Purpose is **finding communities you are not yet tracking**,
not primary capture — Reddit's search index is neither exhaustive nor
immediate. When a term hits in an untracked subreddit more than *n* times in a
window, promote that subreddit into the tracked set and let the fast loop own
it.

Budget: `terms × 1 call` per cycle. 30 terms every 20 minutes = 90 calls/hour.

### 5.2 Subreddit poll loop

The primary capture path. For each tracked subreddit, poll `/new` and
`/comments` at `limit=100`.

**Choosing the interval.** The constraint is that fewer than 100 new items must
appear between two consecutive polls, or you silently lose the overflow. So:

```
interval_seconds  ≤  (100 × safety_factor) / items_per_second
```

with `safety_factor ≈ 0.5` to absorb bursts. Measure `items_per_second` per
subreddit from live data and re-tune nightly — a subreddit's rate varies by an
order of magnitude between 03:00 and 20:00 local time. Adaptive intervals are
worth building; a fixed cadence either wastes budget on quiet subreddits or
drops data on busy ones.

**Budget.** `2 calls × subreddits × (3600 / interval)` per hour. Twenty
subreddits at a 2-minute interval = 1,200 calls/hour ≈ 20 QPM — a fifth of the
budget for solid coverage of a mid-sized community set.

### 5.3 Global stream

Polling `/r/all/comments` continuously is theoretically possible but the
arithmetic is unforgiving: at 100 QPM with `limit=100` you can pull at most
10,000 comments/minute, and Reddit's global comment rate is of the same order.
You would spend the entire budget to capture a fraction of the firehose with no
headroom for anything else.

**Do not build global capture on the free tier.** Scope by subreddit and
keyword. Global capture is a data-licence purchase, not an engineering problem.

### 5.4 Revisit loop

Reddit content is mutable in ways that matter:

| Field | Behaviour | Consequence |
|---|---|---|
| `score` | Volatile for ~48h, then near-static; fuzzed by anti-spam | Never treat first-seen score as final |
| `num_comments` | Grows for days | Thread-importance ranking must be re-run |
| `body` / `selftext` | Editable indefinitely | Sentiment can invert after capture |
| `removed` / `deleted` | Any time, permanently | **Compliance obligation** |
| `edited` | Timestamp or `false` | The cheapest change-detection signal |

Schedule revisits on a decay curve — at +1h, +6h, +24h, +72h, +7d — and batch
100 fullnames per `/api/info` call. Ten thousand tracked items at five revisits
each is 500 calls total, spread across a week. Negligible.

### 5.5 Backfill

One-time, per new watch term or newly tracked subreddit:

1. Pull the subreddit's Arctic Shift Parquet dump (or query PullPush for
   cross-subreddit terms).
2. Filter locally against the watch terms — no API budget spent.
3. Insert with `provenance = 'archive'` so analytics can distinguish
   archive-derived rows (which may be stale or reflect since-deleted content)
   from live-captured ones.
4. Optionally re-hydrate the most recent 5,000 via `/api/info` to get current
   scores and to catch anything deleted since the dump.

Accept the 4–6 week archive lag: the live loop owns everything newer.

---

## 6. Data model

MySQL 8 DDL, matching this repository's existing conventions (`utf8mb4`,
InnoDB, surrogate `BIGINT` keys alongside natural keys).

```sql
-- What we are listening for.
CREATE TABLE listening_term (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    label           VARCHAR(120)  NOT NULL,
    match_type      ENUM('literal','phrase','regex','boolean') NOT NULL DEFAULT 'phrase',
    pattern         TEXT          NOT NULL,
    negative_pattern TEXT         NULL,       -- exclusions, to kill known false positives
    is_active       TINYINT(1)    NOT NULL DEFAULT 1,
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_listening_term_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Communities on the fast poll loop.
CREATE TABLE listening_subreddit (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    name                VARCHAR(80)  NOT NULL,        -- without the r/ prefix
    poll_interval_secs  INT          NOT NULL DEFAULT 300,
    observed_items_hour DECIMAL(10,2) NULL,           -- measured, drives the interval
    source              ENUM('manual','discovered')   NOT NULL DEFAULT 'manual',
    last_polled_at      DATETIME     NULL,
    last_fullname_seen  VARCHAR(20)  NULL,            -- cursor for gap detection
    is_active           TINYINT(1)   NOT NULL DEFAULT 1,
    UNIQUE KEY uq_listening_subreddit_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per captured post or comment. Reddit's fullname is the natural key.
CREATE TABLE listening_item (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    fullname        VARCHAR(20)  NOT NULL,        -- t3_abc123 / t1_def456
    kind            ENUM('post','comment') NOT NULL,
    subreddit       VARCHAR(80)  NOT NULL,
    author          VARCHAR(80)  NULL,            -- NULL once deleted
    author_hash     CHAR(64)     NULL,            -- for analytics without retaining the handle
    title           VARCHAR(400) NULL,            -- posts only
    body            MEDIUMTEXT   NULL,
    permalink       VARCHAR(400) NOT NULL,
    parent_fullname VARCHAR(20)  NULL,
    link_fullname   VARCHAR(20)  NULL,            -- owning submission, for comments
    created_utc     DATETIME     NOT NULL,
    first_seen_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at DATETIME     NULL,
    score           INT          NULL,
    num_comments    INT          NULL,
    edited_at       DATETIME     NULL,
    is_removed      TINYINT(1)   NOT NULL DEFAULT 0,
    is_deleted      TINYINT(1)   NOT NULL DEFAULT 0,
    purged_at       DATETIME     NULL,            -- when we tombstoned the content
    provenance      ENUM('live','archive','manual') NOT NULL DEFAULT 'live',
    UNIQUE KEY uq_listening_item_fullname (fullname),
    KEY ix_listening_item_created (created_utc),
    KEY ix_listening_item_sub_created (subreddit, created_utc),
    KEY ix_listening_item_revisit (last_checked_at, is_deleted),
    FULLTEXT KEY ft_listening_item_text (title, body)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Which terms an item matched. An item can match several.
CREATE TABLE listening_match (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    item_id      BIGINT NOT NULL,
    term_id      BIGINT NOT NULL,
    matched_text VARCHAR(400) NULL,     -- the span that hit, for reviewer context
    confidence   DECIMAL(4,3) NULL,
    UNIQUE KEY uq_listening_match (item_id, term_id),
    FOREIGN KEY (item_id) REFERENCES listening_item(id) ON DELETE CASCADE,
    FOREIGN KEY (term_id) REFERENCES listening_term(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Model output, versioned so a model change is re-runnable and auditable.
CREATE TABLE listening_enrichment (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    item_id         BIGINT       NOT NULL,
    model_name      VARCHAR(120) NOT NULL,
    model_version   VARCHAR(40)  NOT NULL,
    sentiment       ENUM('positive','neutral','negative','mixed') NULL,
    sentiment_score DECIMAL(4,3) NULL,
    severity        TINYINT      NULL,            -- 1..5, drives alert routing
    intent          VARCHAR(60)  NULL,            -- complaint / question / recommendation / …
    topics          JSON         NULL,
    is_spam         TINYINT(1)   NOT NULL DEFAULT 0,
    rationale       TEXT         NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_enrichment (item_id, model_name, model_version),
    FOREIGN KEY (item_id) REFERENCES listening_item(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Score/comment history, for velocity and spike detection.
CREATE TABLE listening_metric_snapshot (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    item_id      BIGINT   NOT NULL,
    observed_at  DATETIME NOT NULL,
    score        INT      NULL,
    num_comments INT      NULL,
    KEY ix_snapshot_item_time (item_id, observed_at),
    FOREIGN KEY (item_id) REFERENCES listening_item(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Design notes:

- **`fullname` is the deduplication key.** It is globally unique and stable
  across every endpoint, so the same item arriving from the discovery loop, the
  poll loop, and a retry all collapse to one row. Write with
  `INSERT … ON DUPLICATE KEY UPDATE` and let the database enforce it — every
  ingestion design generates duplicates by construction (retries, and the
  deliberately overlapping poll windows that guarantee no gaps), so dedupe must
  be structural, not best-effort.
- **Enrichment is versioned and separate from the item.** Re-classifying a
  year of history under a new model is then an insert, not a destructive update,
  and A/B comparison of two models over the same corpus is a `GROUP BY`.
- **`author_hash` alongside `author`.** Long-run analytics ("is this the same
  complainant?") work off the hash, so the raw handle can be dropped on
  deletion or on a privacy request without destroying the aggregates.
- **`purged_at` rather than row deletion.** See below.

---

## 7. Correctness concerns specific to Reddit

### 7.1 Ordering

`/new` is ordered by post time, but a listing cursor is not a clock. Sticky
posts, moderation, and removal all perturb listings. Track
`last_fullname_seen` per subreddit and detect gaps by checking whether that
fullname still appears in the next page; if it does not, widen the page or fall
back to a timestamp-bounded search.

### 7.2 Edits

`edited` is either `false` or a timestamp. Compare it against the stored
`edited_at` on each revisit; when it moves, re-run enrichment and keep the
prior enrichment row. Sentiment genuinely inverts — an edit that prepends
"EDIT: resolved, support was great" to a complaint is common and matters.

### 7.3 Score fuzzing

Reddit deliberately fuzzes vote counts to frustrate manipulation. Scores are
directionally meaningful and numerically approximate. Use them for ranking and
thresholds, never as a precise metric in a report. Velocity (Δscore/Δt from
`listening_metric_snapshot`) is more robust than any absolute score.

### 7.4 Delete compliance

**This is a contractual obligation, not a nicety.** Anyone accessing Reddit
public content is required to stop displaying or using content once it is
deleted by the user or by Reddit, and Reddit can and does revoke access for
non-compliance.

Implementation:

- A **compliance sweep** re-checks every retained item via `/api/info` in
  batches of 100, on a rolling cycle sized so the whole corpus is covered
  within 24 hours. Items absent from the response, or returned with
  `[deleted]`/`[removed]` bodies, are tombstoned.
- **Tombstone, do not delete the row:** null out `body`, `title`, `author`,
  set `is_deleted`/`is_removed` and `purged_at`. Aggregate counts survive; the
  content does not.
- **Purge derivatives too** — cached alert bodies, search index entries,
  exported CSVs, notification payloads. A tombstoned item that is still quoted
  in an old email is still a copy.
- Corpus sizing check: 24-hour full coverage of *N* items costs `N/100` calls
  per day. At 500,000 retained items that is 5,000 calls/day — about 3.5% of
  the free-tier budget. Comfortable, and it puts a real ceiling on how much
  history you can retain and still stay compliant.

### 7.5 Bots and noise

A material fraction of Reddit content is automated (AutoModerator, karma farms,
repost bots, link aggregators). Filter before enrichment, not after — spending
LLM tokens classifying AutoModerator boilerplate is pure waste.

Cheap, effective heuristics: author on a known-bot list; body contains
"I am a bot"; account age under a threshold; near-duplicate body text seen in
≥3 subreddits within an hour (shingle hash); comment is a direct child of the
submission and identical to a previous top-level comment.

---

## 8. Matching layer

Between capture and enrichment sits the question "does this item actually
mention what we care about?" Getting this wrong is the dominant source of user
complaints in every listening tool.

**Tiering.** Run cheap filters first and expensive ones only on survivors:

1. **Literal/phrase pass** — normalised casefold + Unicode NFKC, word-boundary
   aware. Rejects ~99% of the stream.
2. **Boolean expressions** — `("blue apron" OR blueapron) AND NOT "blue apron recipe card"`.
   Users need `NOT` more than they expect; a `negative_pattern` column exists
   for precisely this.
3. **Fuzzy/variant matching** — abbreviations, common misspellings, spacing
   variants. On the survivors only.
4. **LLM relevance adjudication** — for ambiguous hits, a single cheap
   classification call answering "is this about the entity in question, yes or
   no?" This is the lane that rescues a common-word brand name: a coined term
   never needs it, and a name made of two ordinary English words needs it on
   nearly every hit.

**Store the matched span** (`matched_text`). When a reviewer sees why an item
was flagged, false positives get reported and the negative patterns improve.
Without it, users lose trust in the feed and stop reading it.

---

## 9. Enrichment

### 9.1 Sentiment — the honest position

The published comparisons are less flattering than vendor material suggests.
On Reddit-style text, lexicon methods and transformers land closer together
than expected: one Reddit-focused study put VADER at 69% overall accuracy
against 66% for a RoBERTa model — though the aggregate hides that VADER has
markedly worse precision on positive and neutral classes and worse recall on
negatives, which is the class a reputation-monitoring tool most needs to catch.

Reddit is genuinely hard for sentiment: heavy sarcasm, in-group irony,
community-specific register, and negation-dense phrasing. Sarcasm-specific
work on the Self-Annotated Reddit Corpus reports fine-tuned BERT-family models
reaching ~0.92 F1, but that is a dedicated task-specific model, not a
general-purpose sentiment classifier.

**Recommended hybrid:**

| Lane | Handles | Cost |
|---|---|---|
| VADER (or similar lexicon) | First-pass triage on everything | ~free, microseconds |
| Transformer (`twitter-roberta-base-sentiment` class) | Items that matched a term | ~ms on GPU, ~50ms CPU |
| LLM | Ambiguous, high-severity, or sarcasm-suspect items | ~$0.001–0.01 per item |

Route to the LLM lane when the cheap lanes disagree, when the transformer's
confidence is low, or when severity is high. That keeps LLM spend to roughly
5–15% of matched volume while catching the cases that actually matter.

**Always emit a confidence score and always show it.** A negative-sentiment
alert on a sarcastic compliment erodes trust faster than a missed mention.

### 9.2 Beyond sentiment

Sentiment alone is a weak signal. The fields that make a listening tool
actionable:

- **Intent** — complaint · question · recommendation · comparison · news ·
  purchase-intent. Drives *who* gets the alert.
- **Severity (1–5)** — a rant with 3 upvotes in a dead subreddit is not a
  400-comment front-page thread. Combine sentiment, subreddit reach, and
  velocity.
- **Topic/aspect** — *what* the complaint is about. Aspect-based sentiment
  ("enrolment process: negative; faculty: positive") is far more useful than a
  document-level score.
- **Suggested action** — with a rationale. Even when nobody follows the
  suggestion, the rationale is what makes a reviewer trust or correct the
  classification.

Structured output (a constrained JSON schema) from a single LLM call yields all
four at once, which is cheaper than four separate models and easier to keep
consistent.

---

## 10. Alerting

Delivery is where most listening tools fail — not on capture, but by producing
a feed nobody reads.

- **Route by severity, not by volume.** Severity 4–5 pages a human; 2–3 lands
  in a daily digest; 1 is queryable but silent.
- **Deduplicate by thread, not by item.** Forty comments in one thread is one
  alert with a comment count, not forty alerts.
- **Spike detection** over a rolling baseline: alert when mentions of a term in
  a window exceed `mean + 3σ` of the trailing 14-day same-hour baseline.
  Absolute thresholds break the moment volume changes.
- **Cool-down per term** so a genuinely viral thread cannot produce a hundred
  notifications.
- **Every alert carries the permalink and the matched span.** A reviewer must
  be able to reach the source in one click and see why it fired.
- **Alerts must respect tombstones.** An alert queued for an item deleted
  before send should not go out.

---

## 11. Metrics

Definitions worth fixing early, because changing them later invalidates
history:

| Metric | Definition |
|---|---|
| Mention volume | Count of items matching a term in a window, tombstones excluded from content but **included in counts** |
| Share of voice | Term's mentions ÷ total mentions across the tracked competitor/peer set |
| Net sentiment | `(positive − negative) / total`, reported with the classifier's version |
| Reach (estimated) | Σ over threads of `f(subreddit subscribers, score, comments)` — an estimate, label it as one |
| Velocity | Δmentions/hour vs the trailing 14-day same-hour baseline |
| Response latency | First-seen → first human action, for items above a severity threshold |

Reach in particular should never be presented as a number without a
qualifier — Reddit gives no impression data, and any reach figure is a model,
not a measurement.

---

## 12. Cost and capacity

### 12.1 Free-tier budget (144,000 calls/day)

A concrete allocation for 20 tracked subreddits, 30 watch terms, ~500k
retained items:

| Loop | Cadence | Calls/day | Share |
|---|---|---|---|
| Subreddit poll (`/new` + `/comments`) | 20 subs × 2 endpoints, 2-min interval | 28,800 | 20% |
| Discovery search | 30 terms, every 20 min | 2,160 | 1.5% |
| Revisit hydration | decay curve, batched ×100 | ~1,000 | 0.7% |
| Compliance sweep | 500k items ÷ 100, daily | 5,000 | 3.5% |
| Retries + headroom | — | ~5,000 | 3.5% |
| **Total** | | **~42,000** | **~29%** |

**The free tier is not the binding constraint at this scale.** It supports
roughly 3× this footprint before the limiter starts shedding work. The binding
constraints are the *non-commercial* restriction and the approval process.

### 12.2 What costs money

| Item | Estimate |
|---|---|
| Reddit Data API (non-commercial) | $0 |
| Reddit Data API (commercial) | ~$0.24/1k calls; bundled tier ~$12,000/mo for 50M calls |
| LLM enrichment | At 2,000 matched items/day with 10% LLM-routed and ~800 tokens each: single-digit dollars/month |
| Transformer inference | CPU-adequate at this volume; no GPU needed below ~50k items/day |
| Storage | 500k items ≈ 1–2 GB with indexes. Trivial |
| Compute | One small worker VM plus Redis |

**The dominant cost of this system is not infrastructure — it is the licence
question.** If the tool is used for anything commercial, the economics change
by four orders of magnitude, and that is a business decision to make *before*
writing code, not after.

---

## 13. Compliance and privacy

1. **Reddit Developer Terms / Responsible Builder Policy.** Access requires
   approval. Commercial use requires written approval and a negotiated licence.
   Non-commercial use of the free tier must genuinely be non-commercial —
   internal reputation monitoring by an institution is a grey area worth a
   written clarification from Reddit rather than an assumption.
2. **Deletion propagation** (§7.4) is the obligation most likely to be breached
   by accident, via exports and cached alerts rather than the primary store.
3. **Local data-protection law.** Reddit usernames are pseudonymous but,
   combined with post content, can constitute personal information. Under the
   Philippine Data Privacy Act of 2012 (RA 10173), for instance, a deployment
   needs a lawful basis (legitimate interest is the usual one for
   public-content monitoring), a retention limit, and a documented purpose, and
   the processing activity must be registered. Equivalent obligations exist in
   most jurisdictions — establish yours before the corpus grows.
4. **GDPR**, if any monitored subject may be in the EU: public availability is
   not consent. Legitimate-interest assessment, retention limits, and an
   erasure path (`purged_at` already provides the mechanism).
5. **Never monitor individuals.** Watch terms should name organisations,
   products, and services — not named people. A tool that can be pointed at a
   person will eventually be pointed at one; constrain it in the
   term-validation layer, where it is enforced, not in a policy document, where
   it is not.
6. **Retention limit.** Twelve to twenty-four months of full content, with
   aggregates retained indefinitely, keeps the compliance sweep affordable and
   the privacy posture defensible.

---

## 14. Reference implementation stack

Nothing here is exotic. The recommendation is boring on purpose — the hard
parts of this system are the rate-limit budget and the compliance sweep, and
neither is made easier by an interesting stack.

| Concern | Recommendation | Why |
|---|---|---|
| Language | Python 3.12+ | `asyncpraw`/`praw` exist and are maintained; the NLP ecosystem is here |
| Reddit client | Raw `httpx` over `oauth.reddit.com` | PRAW hides the `X-Ratelimit-*` headers behind its own limiter, and those headers are the thing you most need to steer by. Use PRAW for exploration, your own client in production |
| Scheduler / limiter | Redis | The token bucket must be shared across workers; an in-process limiter breaks the moment you run two |
| Queue | Redis + RQ, or Celery | Only once enrichment volume justifies it. A single worker with an in-process scheduler carries the §12 volumes fine |
| Store | PostgreSQL 16, or MySQL 8 | §6 DDL is MySQL; Postgres is the better fit if you want `tsvector` search and `JSONB` topic queries |
| Search | The database's own full-text index first | Elasticsearch/OpenSearch only when corpus search becomes the bottleneck, which is later than you think |
| API / UI | FastAPI + a server-rendered review feed | The reviewable feed (§15 phase 2) matters more than a dashboard, and needs no SPA |
| Deploy | One worker process + one web process | A `Procfile`-shaped split; the poller cannot live in a request cycle |

**The one non-negotiable structural choice:** the poller runs as a long-lived
process with its own scheduler, separate from anything serving HTTP. Everything
else on this list can be swapped without touching the design.

### Configuration surface

```ini
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=server:<app-id>:<version> (by /u/<username>)
REDIS_URL=
DATABASE_URL=
LLM_API_KEY=
ALERT_WEBHOOK_URL=
RETENTION_MONTHS=18
COMPLIANCE_SWEEP_HOURS=24
```

## 15. Recommended phasing

| Phase | Deliverable | Proves |
|---|---|---|
| 0 | OAuth client approved; rate limiter reading live headers | Access is actually obtainable — the riskiest unknown, so do it first |
| 1 | Subreddit poll loop + normalise + dedupe + `listening_item` | Capture is lossless; measure real items/second |
| 2 | Matching layer + a reviewable feed UI | Precision is acceptable *before* any money goes to models |
| 3 | Compliance sweep | Legal position is sound before the corpus grows |
| 4 | Enrichment (lexicon → transformer → LLM lanes) | Classification is worth its cost |
| 5 | Alerting + severity routing | Someone actually reads it |
| 6 | Archive backfill + trend analytics | Historical context |

Phases 0 and 3 are ordered deliberately: access approval is the item most likely to
fail outright, and compliance is far cheaper to build before there is a corpus
to retrofit.

---

## 16. Open questions

1. **Is the intended use commercial?** This determines whether the project is a
   $0/month build or a $12,000/month one. Nothing else in this document matters
   as much.
2. **What is the actual watch list?** Term ambiguity drives the entire matching
   design. A distinctive coined name is a trivial matching problem; a name made
   of two common words is a hard one, and determines how much of §8 you need.
3. **How much history is required?** Live-only removes the archive dependency
   and its 4–6 week lag entirely.
4. **What latency does the use case need?** Sub-minute alerting costs
   substantially more API budget than a 15-minute cadence and is rarely
   necessary for reputation monitoring.
5. **Who acts on an alert, and within what SLA?** A listening tool with no
   downstream response process produces a dashboard nobody opens.

---

## Sources

Reddit primary documentation:
- [Reddit API access rules (User-Agent, rate limits, batching)](https://github.com/reddit-archive/reddit/wiki/API)
- [Reddit OAuth2 flows](https://github.com/reddit-archive/reddit/wiki/OAuth2)
- [Public Content Policy](https://support.reddithelp.com/hc/en-us/articles/26410290525844-Public-Content-Policy)
- [Do Reddit's data licensees have to stop using deleted data?](https://support.reddithelp.com/hc/en-us/articles/26417433892756-Do-Reddit-s-data-licensees-have-to-stop-using-data-deleted-from-Reddit)
- [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
- [Comment tree / morechildren reference notes](https://github.com/Pyprohly/reddit-api-doc-notes/blob/main/docs/api-reference/comment_tree.rst)

API limits and pricing (secondary; figures are widely reported rather than
officially published — verify against a quote before budgeting):
- [Reddit Data API Terms & Commercial Use (2026)](https://prowlo.com/blog/reddit-data-api)
- [Reddit API Pricing (2026): $0.24/1K calls, free tier & limits](https://prowlo.com/blog/reddit-api-pricing)
- [Reddit API in 2026: Pricing, Rate Limits & What Works](https://www.socialcrawl.dev/blog/reddit-data-api-2026)
- [Reddit API Rate Limits in 2026](https://www.painpointmap.com/blog/reddit-api-rate-limits-guide)
- [Reddit API Limitations: Complete Guide for Developers (2026)](https://painonsocial.com/blog/reddit-api-limitations)
- [Reddit API Limits](https://data365.co/blog/reddit-api-limits)

Historical archives:
- [Best Pushshift Alternatives 2026: PullPush, Arctic Shift](https://www.redditapis.com/blogs/best-pushshift-alternatives-2026)
- [How to Get Historical Reddit Data After Pushshift (2026)](https://www.xpoz.ai/blog/tutorials/how-to-get-historical-reddit-data-after-pushshift/)
- [Pushshift Alternative 2026 (PullPush, Arctic Shift)](https://think-pol.com/pushshift-alternative)

Market and tooling:
- [Best Reddit Monitoring Tools in 2026 (11 Compared)](https://snitchfeed.com/blog/best-reddit-monitoring-tools-2026)
- [Best Reddit Research Tools After GummySearch Shut Down (2026)](https://www.subredditsignals.com/blog/reddit-research-tools-2026-gummysearch-alternatives)
- [9 Best GummySearch Alternatives](https://prowlo.com/blog/gummysearch-shut-down-what-now)

Sentiment and classification:
- [VADER vs RoBERTa comparison](https://medium.com/@nityasav/enhancing-review-efficiency-through-sentiment-analysis-a-comparison-of-vader-and-roberta-c6920c9946f8)
- [Sentiment Analysis of Cybersecurity Content on Twitter and Reddit (arXiv 2204.12267)](https://arxiv.org/pdf/2204.12267)
- [Sarcasm Detection: A Comparative Study (arXiv 2107.02276)](https://arxiv.org/pdf/2107.02276)
- [Enhancing sarcasm detection on social media: LLMs and BERT on SARC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12617957/)
- [Sentiment analysis: why your model misses sarcasm](https://labelyourdata.com/articles/natural-language-processing/sentiment-analysis)

Pipeline architecture:
- [Build a Social-Listening Agent for Brand Monitoring](https://www.digitalapplied.com/blog/build-social-listening-agent-brand-monitoring-2026)
- [Social Media Monitoring API: Build Your Own in 5 Steps](https://www.socialcrawl.dev/blog/social-media-monitoring-api)
