# Technical Specification: A YouTube Social Listening Tool

Research note. Last revised 17 September 2026.

**Status.** Implemented. Where the code and this document disagree, the code
wins and this document is wrong — say so. Figures from secondary reporting are
marked as such.

**History.** This project was first specified and built against Reddit. It was
rebuilt for YouTube because Reddit's Data API now gates access behind manual
approval with a non-commercial free tier and a ~$12,000/month commercial cliff,
while YouTube needs only a self-service API key. The Reddit specification is in
this repository's git history if the comparison is useful.

---

## 1. Executive summary

Five findings drive every design decision below.

1. **The scarce resource is a budget, not a rate.** YouTube charges requests
   rather than throttling them: 10,000 units per day, reset at midnight
   Pacific. This is the deepest difference from Reddit and it reshapes the
   whole pipeline.
2. **Cost asymmetry decides the capture strategy.** `search.list` costs **100
   units**; `commentThreads.list` costs **1**. Searching is ~100× more
   expensive than harvesting, so the pipeline discovers rarely and harvests
   generously. Getting this backwards spends the entire day in 100 calls.
3. **Retention is mandatory and short.** Stored API data must be **deleted or
   refreshed within 30 calendar days**, and kept consistent with what YouTube
   currently serves. A derived-metrics carve-out allows *counts* to live up to
   36 months, but not text. This is a stronger obligation than Reddit's
   delete-on-delete rule and it is an engine, not a checkbox.
4. **Access is not gated.** An API key is self-service. There is no approval
   process, no non-commercial restriction on basic read use, and no pricing
   cliff — which is why this platform, not Reddit, is where the project should
   have started.
5. **Comments-off is ordinary.** A large share of real videos have comments
   disabled, and the API returns `commentsDisabled` as a 403. Treating that as
   an error makes a listener that falls over constantly.

**Recommended shape:** track channels (1 unit per channel via their uploads
playlist), harvest comments freely, search sparingly and last, and run the
retention engine with a reserved slice of budget that capture is never allowed
to consume.

---

## 2. Data access

| Route | Coverage | Cost | Standing | Verdict |
|---|---|---|---|---|
| **YouTube Data API v3** | Public videos, comments, replies, stats | Free, 10k units/day | Sanctioned, self-service key | **The only route used** |
| Quota increase (audit form) | Same, higher ceiling | Free, but reviewed | Sanctioned | If 10k/day binds |
| Scraping | Anything visible | Infra only | **Violates the ToS** | Do not |

### Why not scraping

Beyond the terms problem, scraping YouTube is operationally worse than it
looks, and the API's free tier is more generous than an equivalent proxy
budget. There is also no legal basis for retaining what you collect. Use the
API.

### What the API will not give you

- **No firehose.** No streaming or webhook for public comments; everything is
  polling on a timer, exactly as on Reddit.
- **No global comment search.** `commentThreads.list` is per-video. Finding
  comments about a subject means finding the *videos* first, then reading them.
- **Only ~5 inline replies per thread.** Beyond that you get a count, and
  fetching the rest costs another call per thread.
- **`search.list` is not exhaustive** and is capped at 50 results a page.

---

## 3. Authentication

| Item | Value |
|---|---|
| Auth for public read | **API key**, as a `key=` query parameter |
| Endpoint base | `https://www.googleapis.com/youtube/v3` |
| OAuth required? | Only for private data on your own channel |
| Approval required? | No |

An API key is enough for everything this project does: it reads public videos,
comments and statistics, and never writes. That is the single biggest practical
advantage over Reddit, where the approval process is the riskiest unknown in
the whole plan.

**Key hygiene:** a YouTube API key is a bearer credential with a billing
consequence — an exposed key lets a stranger spend your daily quota. Restrict
it to the YouTube Data API in the Cloud console, keep it out of version
control, and rotate it if it leaks.

---

## 4. Quota: the real constraint

### 4.1 Documented costs

| Method | Units | Role in the pipeline |
|---|---|---|
| `search.list` | **100** | Discovery. Capped and run last |
| `videos.list` | 1 | Hydrate up to 50 ids per call |
| `channels.list` | 1 | Resolve a channel's uploads playlist |
| `playlistItems.list` | 1 | **A channel's uploads — 100× cheaper than searching for them** |
| `commentThreads.list` | 1 | The workhorse; 1 unit per page of 100 |
| `comments.list` | 1 | Retention refresh, by id |

Daily allowance is **10,000 units**, reset at **midnight America/Los_Angeles** —
not UTC and not local time.

### 4.2 What that buys

The arithmetic worth internalising:

```
10,000 units  =  100 keyword searches          (and nothing else)
              =  10,000 pages of comments      (~1,000,000 comments)
              =  or any mix, priced as above
```

Ten searches a day cost 1,000 units — 10% of everything — and return at most
500 videos. The same 1,000 units would read a million comments. **Discovery is
a luxury; harvesting is the product.**

### 4.3 Ledger design

- **Persisted in the database, not in memory.** An in-process counter forgets
  the day's spend on restart and then overspends, and on YouTube that means
  every call fails until midnight Pacific. `QuotaSpend` holds one row per
  method per quota day.
- **Charge after the call, not before.** YouTube bills a request whether or not
  the response was useful, so a call that 403s still cost units and must still
  be recorded.
- **Reserve before the call.** A call the budget cannot cover is refused rather
  than attempted.
- **Unpriced methods are rejected.** A method with no entry in the cost table
  raises rather than being guessed at — an unmeasured call is how a budget
  silently disappears.
- **A reserve is held back for retention.** Refreshing stored data is an
  obligation; capturing more of it is not. Capture stops before the reserve is
  touched.

Graceful degradation falls out of this: as the budget runs down, searches stop
first and comment harvesting continues, because the cheap call is also the
valuable one.

---

## 5. Ingestion architecture

Four jobs, run in a deliberate order. The order is the most important thing in
the design.

```
  1. RETENTION      refresh or purge. An obligation, never starved.
        |           comments.list / videos.list -- 1 unit per 50 records
        v
  2. CHANNEL SWEEP  tracked channels' uploads
        |           playlistItems.list -- 1 unit per channel
        v
  3. COMMENT HARVEST  the bulk of the useful data
        |           commentThreads.list -- 1 unit per page of 100
        v
  4. DISCOVERY      keyword search, capped and last
                    search.list -- 100 units per call
```

Running discovery first would let ten searches eat a fifth of the day before a
single comment was read.

### 5.1 Channel sweep

For each tracked channel, enumerate recent uploads via its **uploads playlist**
(`playlistItems.list`, 1 unit) rather than `search.list` (100 units). Tracking
a channel is therefore ~100× cheaper than searching for it, which is why the
configuration is channel-first.

### 5.2 Comment harvest

`commentThreads.list` at `maxResults=100`, `order=time`, `textFormat=plainText`.
Each page costs 1 unit. `textOriginal` is preferred over `textDisplay`, which
carries HTML that would pollute both matching and sentiment.

**Owned channels skip the keyword gate.** On a channel you own, every comment is
addressed to you — someone complaining under your own enrolment video rarely
names the institution — so requiring a term match there would discard most of
what you need to read. Terms are still recorded when they hit; they stop being
the admission test. This also means the spam lane earns its keep, because all
the sub-for-sub and crypto bait on your own videos now arrives.

### 5.3 Discovery

`search.list` per discovery-flagged term, `order=date`, windowed with
`publishedAfter`. Purpose is finding conversations on channels you do not track.
Hard-capped by a configurable share of the day's budget (default 20%), because
it is the only part of the pipeline that can exhaust the budget by itself.

### 5.4 Deduplication

`youtube_id` is the natural key and the database enforces it. Overlapping
harvest windows and retries guarantee duplicates by construction, so dedupe is
structural rather than best-effort. Re-ingesting updates the mutable fields
(like count, reply count, text, `updatedAt`) and restarts the retention clock.

---

## 6. Data model

```
Channel   a channel we poll; `is_owned` changes the capture rule
   |
Video     the container. Stats, and whether comments are even open.
   |
Mention   the unit of listening: a comment, a reply, or a video whose own
          title/description mentions a term. Everything downstream operates
          on Mention, so the pipeline never branches on kind.
```

Supporting tables: `WatchTerm`, `Match` (with the span that hit), `Enrichment`
(versioned), `MetricSnapshot` (**counts only, no text**), `Alert`, `QuotaSpend`.

Two deliberate choices:

- **`MetricSnapshot` holds no text at all.** That is precisely what the
  derived-metrics carve-out permits keeping for 36 months. A single text column
  here would forfeit it, so the separation is the mechanism rather than a note.
- **`author_hash` alongside `author_name`.** A SHA-256 of the author's channel
  id, so "is this the same commenter again?" keeps working after the display
  name and text have been purged.

See `social_listener/models.py` for the authoritative schema.

---

## 7. Retention: the 30-day engine

The obligation: stored API data must be **deleted or refreshed within 30
calendar days**, and stored data must track what YouTube currently serves.
Derived metrics may be kept up to 36 months.

Three jobs:

| Job | What it does |
|---|---|
| **Refresh** | Re-fetch records approaching expiry. Success restarts the 30-day clock — the policy's own remedy, and what makes long-running monitoring permissible at all |
| **Purge** | Anything that cannot be refreshed — expired, or gone from YouTube — has its text, title and author name nulled. The row survives so aggregates stay valid |
| **Reconcile** | Records not verified in 7 days are re-checked regardless of expiry. Without this a deletion could sit unnoticed for ~25 days, and the policy asks for consistency "as quickly as possible" |

Implementation notes that matter:

- **The clock runs from when *we* stored the record**, not from `publishedAt`. A
  freshly captured five-year-old comment has a full 30 days.
- **Refresh runs before purge.** In a healthy system with budget to spare,
  almost nothing is purged for age — purge-for-age is what happens when refresh
  is *impossible*.
- **Derivatives are purged too.** A comment scrubbed from `mention.text` but
  still quoted in a cached alert body is still a retained copy. Alert headlines
  and details are scrubbed in the same pass.
- **A refreshed video renews its video-kind mention.** They carry the same
  title and description, so refreshing one without the other would purge a
  record whose source had just been confirmed live.
- **Corpus sizing.** Refreshing *N* records costs `N/50` units. 100,000
  retained records is 2,000 units — 20% of a day. That, not disk, is what caps
  how much history you can hold and stay compliant.

---

## 8. Matching

Tiered, cheapest first: literal/phrase (word-boundary aware, NFKC-normalised)
→ boolean (`AND`/`OR`/`NOT` over phrases) → regex → per-term negative patterns
that veto an otherwise-good match.

Two notes:

- **Not casefolded.** Every search is already case-insensitive; casefolding
  would only mean the span shown to a reviewer comes back in lower case, which
  reads as a bug.
- **The matched span is stored.** A reviewer who cannot see *why* an item was
  flagged stops trusting the feed, and a feed nobody trusts is a feed nobody
  reads.

An LLM relevance-adjudication tier is specified but not implemented; it needs a
live corpus to be worth its cost. `Match.confidence` is the field it populates.

---

## 9. Classification

### 9.1 Spam is the headline problem

Reddit's automated content is mostly AutoModerator boilerplate — uniform and
trivially matched. YouTube comment spam is an industry: crypto bait,
sub-for-sub, prize scams, engagement farming, phone numbers, off-platform
contact details. It is both a larger share of the stream and far more varied.

So the detector is **scored, not boolean**: any single signal (a link, shouting,
a phone number) is weak alone but decisive in company. It runs **before**
anything expensive, because spending model budget classifying sub-for-sub is
pure waste.

The false positive that matters most is a helpful person posting a link. The
scoring threshold is set so that a single link does not condemn a comment.

### 9.2 Sentiment — the honest position

Unchanged from the Reddit build, because the finding is about the text, not the
platform: on social-media text, lexicon methods and transformers land closer
together than vendor material suggests — one study put VADER at 69% accuracy
against RoBERTa's 66% — and the aggregate hides that the lexicon is markedly
worse on the negative class, which is the class a reputation tool exists to
catch. Sarcasm is a genuine, unsolved weakness.

The response is to report confidence everywhere and route low-confidence items
to a human rather than asserting them. A negative-sentiment alert on a
sarcastic compliment erodes trust faster than a missed mention.

Three lanes, cheapest first:

| Lane | Handles | Cost |
|---|---|---|
| Lexicon + rules | Everything | Free |
| Transformer | Matched items | ~50ms CPU |
| LLM | Ambiguous, severe, sarcasm-suspect | ~$0.001–0.01 each |

Only the lexicon lane is implemented. The others are `Enricher` implementations
with the same interface, and `route_to_expensive_lane()` already computes what
*would* escalate — which is what makes the cost projection real.

### 9.3 Severity

1–5, combining sentiment, reach and consequence. YouTube gives real like and
reply counts — no vote fuzzing as on Reddit — so reach is a more trustworthy
input here, and a liked complaint genuinely is more urgent than an ignored one.
Consequence language ("lawyer", "CHED", "journalist") raises severity
independent of sentiment score, because it describes outcomes rather than
feelings.

---

## 10. Alerting

- **Route by severity, not volume.** 4–5 pages a human; 2–3 is a digest; 1 is
  queryable and silent.
- **One alert per video, not per comment.** A review-bombed video produces one
  alert with a count, not eighty.
- **Spike detection** against a rolling 14-day baseline at mean + 3σ. Leading
  empty days are excluded, because they mean "capture had not started" rather
  than "volume was zero" — including them makes a freshly deployed system alarm
  on its own arrival.
- **Alerts respect purges.** A queued alert for purged content does not go out,
  and existing alert bodies are scrubbed.

---

## 11. Metrics

| Metric | Definition |
|---|---|
| Mention volume | Mentions in a window. **Purged items keep their count** |
| Share of voice | A term's mentions ÷ all matched mentions |
| Net sentiment | `(positive − negative) / classified`, reported with the model version |
| Video views | A real number from YouTube — but **not** comment reach |
| Spike | Daily volume above mean + 3σ of the trailing 14 days |

Reach deserves the same caution as on Reddit. YouTube view counts are genuine,
unlike Reddit's fuzzed scores, but a view is not a read of any particular
comment. Label it as what it is.

---

## 12. Cost and capacity

A worked day for 3 tracked channels, ~7 active videos, 3 watch terms:

| Job | Calls | Units | Share |
|---|---|---|---|
| Channel sweep | 3 × `playlistItems.list` | 3 | 0.03% |
| Comment harvest | 7 videos × 3 pages | 21 | 0.2% |
| Retention refresh | ~100 records ÷ 50 | 2 | 0.02% |
| Discovery | 3 × `search.list` | 300 | 3% |
| **Total** | | **~326** | **~3%** |

**The free quota is not the binding constraint at this scale** — it supports
roughly 30× this footprint. It becomes binding when discovery is run
aggressively or when the retained corpus grows into six figures.

| Item | Cost |
|---|---|
| YouTube Data API | $0 |
| LLM enrichment | At 2,000 matched items/day, 10% escalated, ~800 tokens: single-digit dollars/month |
| Transformer inference | CPU-adequate below ~50k items/day |
| Storage | Capped by the 30-day rule, so it stays small by construction |

---

## 13. Compliance and privacy

1. **YouTube API Services Terms and Developer Policies.** The 30-day
   refresh-or-delete rule (§7) is the operative one, plus the consistency
   requirement. The derived-metrics carve-out is the only extension, and it
   covers counts, not text.
2. **Purging derivatives** is the obligation most likely to be breached by
   accident — via exports and cached alert bodies rather than the primary
   store.
3. **Local data-protection law.** Comment author names and channel ids are
   personal information. Under the Philippine Data Privacy Act (RA 10173), for
   instance, a deployment needs a lawful basis, a retention limit and a
   documented purpose, and the processing must be registered. Equivalent
   obligations exist in most jurisdictions. The 30-day rule conveniently forces
   a defensible retention posture.
4. **Never monitor individuals.** Watch terms should name organisations,
   products and services — not named people. A tool that can be pointed at a
   person will eventually be pointed at one; constrain it in term validation,
   where it is enforced, not in a policy document, where it is not.
5. **Commenters are not public figures.** A YouTube comment is public but its
   author usually did not expect to be catalogued. Keep the author hash for
   analytics and let the name go.

---

## 14. Implementation stack

Boring on purpose. The hard parts are the quota ledger and the retention
engine, and neither is made easier by an interesting stack.

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.9+ | Tested on 3.11; 3.9 supported (see `tests/test_python_compat.py`) |
| API client | `httpx`, hand-rolled | The official client library hides the quota accounting this design is built around |
| Store | SQLite (demo) / PostgreSQL or MySQL | The quota ledger and retention clock need real transactions once processes multiply |
| Web | FastAPI + Jinja, server-rendered SVG charts | No build step, no CDN, no external scripts |
| Scheduler | One long-lived worker process | The one non-negotiable: harvesting cannot live in a request cycle |

---

## 15. What is not built

- **Transformer and LLM lanes.** Interfaces exist; escalation is computed; only
  the lexicon lane runs.
- **A scheduler.** `harvest`, `discover` and `retention` are one-shot commands.
  Retention on a timer is the one that carries policy exposure.
- **Alert delivery.** Alerts are raised and displayed, not emailed or texted.
- **A terms/channels CLI.** Configuration is seeded from the demo module.
- **OAuth for owned-channel private data** (unpublished comments, held for
  review).

---

## 16. Open questions

1. **Which channels matter?** Channel-first configuration is ~100× cheaper than
   search, so the tracked list is the single highest-leverage decision.
2. **Is 30 days of retained content enough?** If longer trend history is needed,
   the answer is more `MetricSnapshot` rows, not longer text retention.
3. **Does the 10,000-unit ceiling bind?** If discovery needs to be aggressive,
   file the quota-increase audit form early.
4. **Who acts on an alert, within what SLA?** A listening tool with no
   downstream response process produces a dashboard nobody opens.

---

## Sources

YouTube primary documentation (referenced; developers.google.com was not
directly reachable from the build environment, so quota figures were
cross-checked against multiple secondary sources):
- [YouTube API Services — Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- [Additional policies for derived metrics and data storage](https://developers.google.com/youtube/terms/derived-metrics-policy)
- [Determine quota cost](https://developers.google.com/youtube/v3/determine_quota_cost)
- [CommentThreads: list](https://developers.google.com/youtube/v3/docs/commentThreads/list)

Quota costs and limits (secondary):
- [YouTube API quota limits, costs and how to get more](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota)
- [100 searches burn 10,000 units](https://www.socialcrawl.dev/blog/youtube-data-api-2026)
- [YouTube API quota explained](https://outlierkit.com/resources/youtube-api-quota/)
- [YouTube API pricing: is it free?](https://outlierkit.com/resources/youtube-api-pricing/)
- [commentThreads.list guide](https://www.commentshark.com/blog/youtube-data-api-commentthreads-list-guide)

Platform comparison (why not Facebook):
- [Meta Content Library](https://transparency.meta.com/researchtools/meta-content-library/)
- [Facebook data APIs and access limits](https://www.socialfetch.dev/blog/best-facebook-data-apis-2026)

Sentiment and sarcasm:
- [VADER vs RoBERTa comparison](https://medium.com/@nityasav/enhancing-review-efficiency-through-sentiment-analysis-a-comparison-of-vader-and-roberta-c6920c9946f8)
- [Sarcasm detection: a comparative study (arXiv 2107.02276)](https://arxiv.org/pdf/2107.02276)
- [LLMs and BERT on SARC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12617957/)
