# social_listener

Technical research and specification for a system that watches Reddit for
mentions of a set of terms, classifies them, stores them, and alerts a human.

**Status: research only.** No implementation yet. The specification is a design
position to argue with, not a description of shipped code.

📄 **[docs/SPECIFICATION.md](docs/SPECIFICATION.md)** — the full technical
specification.

---

## Why this document exists

Reddit is the platform where people say what they actually think about an
organisation, and it is also the platform with the least tractable API for
finding out. Between the 2023 pricing change, the Pushshift shutdown, and the
2026 Responsible Builder Policy, most of the off-the-shelf tooling in this
category either died or repriced into enterprise territory. So the build-vs-buy
question is live again, and it deserves an honest technical answer before
anyone writes code.

## The five findings that drive the design

1. **There is no firehose.** Reddit exposes no streaming or webhook API for
   public content. Every "real-time" Reddit tool is polling listing endpoints
   on a timer. Latency is a budget decision, not a platform feature.

2. **The rate limit *is* the architecture.** 100 queries per minute per OAuth
   client — roughly 144,000 calls/day — averaged over a 10-minute window, and
   scoped **per API key, not per end user**. A multi-tenant product gets no
   extra headroom by adding customers. The entire ingestion design is an
   exercise in spending those calls well.

3. **Listings terminate at ~1,000 items.** Ten pages of 100. The Data API
   structurally cannot answer "everything ever said about X", so historical
   coverage is a separate problem needing an external archive with weaker
   guarantees.

4. **Access is gated.** Since the Responsible Builder Policy update of 5 June
   2026, new OAuth clients go through manual approval rather than self-service
   registration.

5. **The commercial cliff is the real risk.** The free tier is non-commercial.
   Commercial terms are reported at ~$0.24 per 1,000 calls with a bundled tier
   near $12,000/month. There is nothing in between — which is why GummySearch,
   the best-known tool in this category, stopped new signups in November 2025.

**Whether the intended use is commercial is a four-orders-of-magnitude
question, and it should be settled before any code is written.**

## What the specification covers

| § | Section | |
|---|---|---|
| 1–2 | Executive summary, data acquisition options | Why the Data API, why not scraping, what the archives can and cannot do |
| 3–4 | Auth, rate limits, endpoint inventory | OAuth mechanics, the `X-Ratelimit-*` headers, and why `/api/info` is the efficiency lever |
| 5 | Ingestion architecture | Four loops — discovery, poll, revisit, compliance — each with its own budget and shed priority |
| 6 | Data model | DDL, with `fullname` as the structural dedupe key and versioned enrichment |
| 7 | Reddit-specific correctness traps | Edits inverting sentiment, deliberate score fuzzing, deletion propagation |
| 8–9 | Matching and enrichment | Tiered matching; a hybrid sentiment approach with honest accuracy numbers |
| 10–11 | Alerting and metrics | Severity routing, spike detection, and metric definitions worth fixing early |
| 12 | Cost and capacity | A worked API-call budget showing a 20-subreddit footprint at ~29% of the free tier |
| 13 | Compliance and privacy | Delete propagation as a contractual obligation, plus the data-protection position |
| 14–16 | Stack, phasing, open questions | What to build first, and what nobody has answered yet |

## Two things worth flagging up front

**The pricing figures are widely reported but not officially published.** Reddit
does not publish a self-serve commercial rate card. Get a quote before
budgeting against any number in this document.

**Sentiment accuracy is worse than vendor material suggests.** One
Reddit-focused study put VADER at 69% overall accuracy against RoBERTa's 66% —
and the aggregate hides that the lexicon method is markedly worse on the
negative class, which is exactly the class a reputation-monitoring tool exists
to catch. Reddit's sarcasm density is a genuine, unsolved problem. §9 takes a
position on it rather than papering over it.

## Suggested first step

Phase 0 in §15: get an OAuth client approved and stand up a rate limiter that
steers by the live `X-Ratelimit-*` headers. Access approval is the single
riskiest unknown in the whole plan and the cheapest to test.
