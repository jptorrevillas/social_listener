# social_listener

A **YouTube** social listening tool: harvests comments from tracked channels and
keyword-discovered videos, classifies them, enforces YouTube's 30-day retention
rule, and alerts a human.

**It runs with no API key.** Set one and the same pipeline captures live data —
no code change.

📄 **[docs/SPECIFICATION.md](docs/SPECIFICATION.md)** — the design rationale.
Section references in the code (§4.3, §7) point into it.

---

## Run the demo

**Requires Python 3.9 or newer.** Tested on 3.11; 3.9 works, but it reached end
of life in October 2025, so prefer 3.11+ if you get to choose.

**Windows (PowerShell)**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m social_listener seed --reset
python -m social_listener serve      # http://127.0.0.1:8000
```

If PowerShell blocks the activate script, use `.venv\Scripts\activate.bat` or
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m social_listener seed --reset
python -m social_listener serve
```

```bash
python -m social_listener stats       # what is in the database
python -m social_listener harvest     # channel uploads + comments (1-unit calls)
python -m social_listener discover    # keyword search (100 units each)
python -m social_listener retention   # refresh or purge, per the 30-day rule
python -m social_listener quota       # where today's units went
pytest -q                             # 145 tests
```

Requirements are **floors** (`>=`), not pins, so they resolve against whatever
your package index carries.

### ⚠️ The demo data is fabricated

"Northbridge College" is a **fictional institution**; every video and comment is
generated from templates. None of it came from YouTube. The UI carries a
permanent banner saying so — **keep it**. A screenshot of this feed must never
be mistakable for real sentiment about a real place.

## Going live

```bash
cp .env.example .env     # add YOUTUBE_API_KEY
python -m social_listener doctor
```

`doctor` is the first thing to run if the banner still says DEMO. It reports
where it looked for `.env`, whether the key parsed, and a fingerprint of the key
(never the key itself), and it names the specific problem. The ways this fails
are all silent:

| Symptom | Cause |
|---|---|
| `.env present NO` but you made one | It is named `.env.txt` — Windows hides known extensions |
| `.env` unreadable | UTF-16, which is what PowerShell 5.1 `>` redirection writes. Use `Set-Content -Encoding utf8` |
| Key present but empty | `YOUTUBE_API_KEY` appears twice; `.env.example` ships an empty one and the **last** wins |
| Worked yesterday, not today | You launched from a different directory (fixed — `.env` is now anchored to the project root) |

To skip `.env` entirely for one session:

```powershell
$env:YOUTUBE_API_KEY="AIza..."
```

An API key is self-service from the Google Cloud console — **no approval
process, no non-commercial restriction, no pricing cliff.** That is the whole
reason this is a YouTube tool and not a Reddit one.

Then tell it what to watch: tracked channels and watch terms. There is no CLI
for that yet (see *What is not built*), so it is seeded from the demo module
today.

## The thing to understand before anything else

YouTube does not throttle requests, it **charges** them. 10,000 units a day,
reset at midnight Pacific:

```
search.list           100 units      commentThreads.list      1 unit
```

So:

```
10,000 units  =  100 keyword searches, and nothing else
              =  or ~1,000,000 comments
```

Ten searches cost 10% of the day and return at most 500 videos. The same units
would read a million comments. **Discovery is a luxury; harvesting is the
product** — and that single ratio explains most of the architecture:

- Channels are enumerated via their uploads playlist (1 unit), never by search
  (100 units) — tracking a channel is ~100× cheaper than searching for it.
- Discovery runs **last** and is capped at 20% of the budget.
- The ledger is **persisted in the database**, because an in-memory counter
  forgets the day's spend on restart and then overspends until midnight.
- A reserve is held back so retention can always run: refreshing stored data is
  an obligation, capturing more of it is not.

## What to show in a demo

1. **The quota panel.** 10,000 units, what a search costs, what a comment page
   costs, and where today's units went. This is the constraint the whole design
   answers to, and it is the thing an operator actually watches.
2. **The retention panel.** YouTube requires stored data to be deleted or
   refreshed within 30 days. Press **Run retention pass**: records get
   refreshed (clock restarts), anything gone from YouTube is purged — text,
   title and author nulled, row kept so counts stay valid — and cached alert
   bodies are scrubbed in the same pass. Every feed row shows its own
   "purges in Nd" countdown.
3. **Spam filtered before any model ran.** YouTube comment spam is an industry,
   not boilerplate. The detector is scored rather than boolean, because one
   signal is weak but several are decisive. Filter the feed by **Show spam** to
   see what was caught — and note a helpful comment containing a link is *not*
   caught.
4. **Sarcasm handled by admitting ignorance.** Find a row tagged *low
   confidence — needs a human*. The lexicon scores "Oh brilliant, the portal is
   down again. Thanks a lot /s" as **positive**. It is wrong. What matters is
   that it says so at 35% confidence and routes the item to a person.
5. **Severity is not sentiment.** "double-charged my tuition… talking to a
   lawyer" scores near-neutral on the lexicon, but lands at **severity 4** via
   complaint intent, reach, and consequence language. The rules catch what the
   lexicon misses.
6. **Comments disabled is ordinary.** One demo video has them off. The
   harvester records it and moves on, because a listener that raises on
   `commentsDisabled` falls over constantly.

## How it fits together

```
DemoSource / LiveYouTubeSource     one interface, chosen by whether a key exists
        |
   retention        refresh or purge FIRST -- an obligation, never starved
        |
   channel sweep    uploads playlist, 1 unit per channel
        |
   comment harvest  1 unit per page of 100
        |
   normalise        four endpoint shapes -> one internal shape
        |
   dedupe           youtube_id is the key; the database enforces it
        |
   match            literal -> phrase -> boolean -> negative patterns
        |            (skipped entirely on channels you own -- see below)
   classify         spam -> sentiment -> intent -> severity -> topics
        |
   store + alert    severity 4-5 pages a human, one alert per video
        |
   discovery        keyword search LAST, capped at 20% of the budget
```

**Owned channels skip the keyword gate.** On a channel you own every comment is
addressed to you — someone complaining under your own enrolment video rarely
names the institution — so requiring a term match there would throw away most of
what you need to read.

| Module | Does |
|---|---|
| `quota.py` | Cost table, Pacific reset arithmetic, budget status |
| `ledger.py` | Persisted spend, per method per quota day |
| `youtube/client.py` | Quota-aware API client; `commentsDisabled` is normal |
| `youtube/normalise.py` | Four endpoint shapes → one internal shape |
| `youtube/source.py` | The demo/live seam |
| `matching.py` | Tiered matcher with a hand-written boolean parser |
| `enrichment.py` | Scored spam detector, sentiment, intent, severity, topics |
| `harvest.py` | The pipeline; upsert, match, classify, alert |
| `retention.py` | The 30-day refresh-or-purge engine |
| `analytics.py` | Metric definitions — fixed early, on purpose |
| `web/` | Dashboard, review feed, configuration |

## What is deliberately not built

- **Transformer and LLM classification lanes.** `enrichment.py` defines the
  `Enricher` interface and already computes which items *would* escalate, so
  the cost projection is real. Only the lexicon lane runs.
- **A scheduler.** `harvest`, `discover` and `retention` are one-shot commands.
  **Retention on a timer is the one with policy exposure** — that is the first
  thing to build.
- **Alert delivery.** Alerts are raised and displayed, not sent.
- **A terms/channels CLI.** Configuration is seeded from the demo module.
- **OAuth for owned-channel private data** (held-for-review comments).

### Do not lead with net sentiment

The dashboard's net-sentiment tile reads **positive** on a corpus that is
deliberately complaint-heavy, and that is the lexicon's documented weakness
showing through rather than a bug. Measured on the demo's own templates:

| Comment | Lexicon score | Intent |
|---|---|---|
| "portal timed out four times… absolutely ridiculous" | −0.42 | complaint |
| "queued two and a half hours… worst experience of my life" | −0.62 | complaint |
| "a fee nobody can explain… felt fobbed off" | **0.00 (neutral)** | complaint |
| "genuinely the best decision I made" | **+0.85** | praise |
| "the new library is excellent" | +0.57 | praise |

Praise uses words a sentiment lexicon weights heavily ("excellent", "best",
"grateful"). Complaints often do not — "queued two hours", "a fee nobody can
explain", "timed out" are damning in context and almost neutral in a lexicon.
So the aggregate tilts positive while the actual grievances go under-weighted,
which is precisely why §9.2 says the lexicon is worst on the class this tool
exists to catch.

**The intent and severity signals get all three complaints right regardless**,
which is why the feed is ranked by severity and not by sentiment. Read the alert
count and the severity ranking; treat net sentiment as a weak trend indicator,
not a headline. Adding the transformer or LLM lane is what would fix the
aggregate.

### One number that will not match the specification

§9 budgets the expensive classification lane at 5–15% of matched volume. The
demo reports **~45%**, because the synthetic corpus is deliberately
complaint-heavy — it exists to exercise the severity and escalation paths, not
to model a realistic comment mix. On a real corpus, where most comments are
neutral chatter or spam, expect the figure to fall toward the specification's
range. Worth saying before someone does the arithmetic on the LLM bill.

### A note on Facebook

Facebook was considered and rejected: the Graph API only reads Pages you
manage, public keyword search was removed in 2018, and Meta Content Library is
restricted to approved academic researchers. Monitoring your **own** Facebook
Page is achievable and valuable; monitoring Facebook at large, via any
sanctioned route, is not. See §2 of the specification.
