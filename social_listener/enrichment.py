"""Classification: sentiment, intent, severity, spam (§9).

The specification takes an honest position on sentiment and this module keeps
it. On Reddit-style text, lexicon methods and transformers land closer together
than vendor material suggests -- one Reddit-focused study put VADER at 69%
accuracy against RoBERTa's 66%, and the aggregate hides that the lexicon is
markedly worse on the negative class, which is the class a reputation tool most
needs to catch.

So the design is three lanes, cheapest first:

    lexicon      VADER, on everything                  free, microseconds
    transformer  on items that matched a term          ~50ms CPU
    llm          ambiguous / high-severity / sarcastic ~$0.001-0.01 per item

Only the lexicon lane ships here. The other two are defined as pluggable
`Enricher` implementations with the same interface, so adding them is a
registration, not a rewrite. `route_to_expensive_lane()` already computes which
items would be escalated, which is what makes the cost projection in §12 real
rather than hypothetical.

Every result carries a confidence, and the UI always shows it. A
negative-sentiment alert on a sarcastic compliment erodes trust faster than a
missed mention.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# -- signals ----------------------------------------------------------------

# §7.5: filter bots before enrichment, not after. Spending model budget
# classifying AutoModerator boilerplate is pure waste.
BOT_AUTHORS = {"automoderator", "reddit", "botrank", "repostsleuthbot", "totesmessenger"}
BOT_PHRASES = (
    "i am a bot",
    "this action was performed automatically",
    "beep boop",
    "^(i am a bot)",
    "please contact the moderators of this subreddit",
)

INTENT_CUES: dict[str, tuple[str, ...]] = {
    "complaint": (
        "still waiting", "no response", "nobody replied", "terrible", "awful",
        "ridiculous", "unacceptable", "fed up", "worst", "scam", "rude",
        "ignored", "keeps crashing", "broken", "refund",
    ),
    "question": (
        "does anyone", "anyone know", "how do i", "how long", "is it worth",
        "should i", "what happens", "can someone", "any idea", "?",
    ),
    "recommendation": (
        "highly recommend", "would recommend", "go for it", "worth it",
        "shout out", "kudos", "thank you", "grateful", "best decision",
    ),
    "comparison": ("versus", " vs ", "compared to", "better than", "instead of"),
    "purchase_intent": (
        "about to enrol", "about to enroll", "signing up", "planning to apply",
        "thinking of applying", "ready to pay", "how do i pay",
    ),
    "news": ("announced", "press release", "reported that", "according to"),
}

# Words that raise severity independent of sentiment score: these describe
# consequences, not feelings.
ESCALATION_CUES = (
    "lawyer", "legal", "sue", "lawsuit", "ombudsman", "news", "journalist",
    "deped", "ched", "complaint filed", "data breach", "leaked", "harassment",
    "discrimination", "safety", "injured", "fraud",
)

SARCASM_CUES = ("/s", "yeah right", "sure thing", "oh great", "thanks a lot", "genius")


@dataclass
class EnrichmentResult:
    model_name: str
    model_version: str
    sentiment: str
    sentiment_score: float
    confidence: float
    severity: int
    intent: str
    topics: list[str] = field(default_factory=list)
    is_spam: bool = False
    rationale: str = ""
    needs_expensive_lane: bool = False

    def topics_json(self) -> str:
        return json.dumps(self.topics)


class Enricher(ABC):
    """One classification lane."""

    model_name: str = "abstract"
    model_version: str = "0"

    @abstractmethod
    def enrich(self, item: dict) -> EnrichmentResult: ...


def looks_automated(author: str | None, text: str) -> bool:
    if author and author.lower() in BOT_AUTHORS:
        return True
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in BOT_PHRASES)


def detect_intent(text: str) -> str:
    lowered = (text or "").lower()
    scores: dict[str, int] = {}
    for intent, cues in INTENT_CUES.items():
        hits = sum(1 for cue in cues if cue in lowered)
        if hits:
            scores[intent] = hits
    if not scores:
        return "other"
    # A complaint that also asks a question is still a complaint -- consequence
    # outranks grammar.
    if "complaint" in scores:
        return "complaint"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def extract_topics(text: str) -> list[str]:
    """Crude aspect extraction. A real deployment trains this on its own corpus."""
    lowered = (text or "").lower()
    aspects = {
        "enrolment": ("enrol", "enroll", "registration", "admission"),
        "billing": ("tuition", "fee", "payment", "invoice", "refund", "billing"),
        "faculty": ("professor", "teacher", "instructor", "faculty", "prof "),
        "facilities": ("campus", "library", "canteen", "wifi", "aircon", "building"),
        "support": ("registrar", "helpdesk", "support", "hotline", "email them"),
        "academics": ("grade", "exam", "syllabus", "course", "subject", "thesis"),
    }
    found = [name for name, cues in aspects.items() if any(c in lowered for c in cues)]
    return found[:4]


class LexiconEnricher(Enricher):
    """VADER plus rule-based intent, severity and spam. The always-on lane."""

    model_name = "vader-rules"
    model_version = "0.1.0"

    def __init__(self) -> None:
        self._analyzer = SentimentIntensityAnalyzer()

    def enrich(self, item: dict) -> EnrichmentResult:
        text = " ".join(filter(None, (item.get("title"), item.get("body")))).strip()
        author = item.get("author")

        if looks_automated(author, text):
            return EnrichmentResult(
                model_name=self.model_name,
                model_version=self.model_version,
                sentiment="neutral",
                sentiment_score=0.0,
                confidence=0.95,
                severity=1,
                intent="other",
                is_spam=True,
                rationale="Automated account or bot boilerplate; skipped before scoring.",
            )

        scores = self._analyzer.polarity_scores(text)
        compound = scores["compound"]
        sentiment = (
            "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
        )

        # Confidence falls away near the decision boundary, and falls hard when
        # sarcasm cues are present -- the lexicon's documented blind spot.
        confidence = min(abs(compound) * 1.4 + 0.3, 0.95)
        sarcastic = any(cue in text.lower() for cue in SARCASM_CUES)
        if sarcastic:
            confidence = min(confidence, 0.35)

        intent = detect_intent(text)
        topics = extract_topics(text)
        severity = self._severity(item, sentiment, compound, text, intent)

        rationale_bits = [f"VADER compound {compound:+.2f}"]
        if intent != "other":
            rationale_bits.append(f"intent={intent}")
        if sarcastic:
            rationale_bits.append("sarcasm cue present -- confidence capped, escalate")
        if topics:
            rationale_bits.append("topics: " + ", ".join(topics))

        result = EnrichmentResult(
            model_name=self.model_name,
            model_version=self.model_version,
            sentiment=sentiment,
            sentiment_score=round(compound, 3),
            confidence=round(confidence, 3),
            severity=severity,
            intent=intent,
            topics=topics,
            is_spam=False,
            rationale="; ".join(rationale_bits),
        )
        result.needs_expensive_lane = route_to_expensive_lane(result)
        return result

    @staticmethod
    def _severity(item: dict, sentiment: str, compound: float, text: str, intent: str) -> int:
        """1-5. Combines sentiment, reach and consequence (§9.2).

        A rant with 3 upvotes in a dead subreddit is not a 400-comment front-page
        thread, and the score is fuzzed anyway (§7.3) -- so reach contributes but
        never dominates.
        """
        if sentiment == "positive":
            return 1
        severity = 2 if sentiment == "negative" else 1

        if compound <= -0.6:
            severity += 1
        if intent == "complaint":
            severity += 1

        score = item.get("score") or 0
        comments = item.get("num_comments") or 0
        if score >= 100 or comments >= 50:
            severity += 1

        if any(cue in text.lower() for cue in ESCALATION_CUES):
            severity += 1

        return max(1, min(severity, 5))


def route_to_expensive_lane(result: EnrichmentResult) -> bool:
    """Would this item be escalated to the transformer or LLM lane? (§9.1)

    Escalate on low confidence, on high severity, or on sarcasm cues. §12
    budgets this at 5-15% of matched volume; the demo reports the real figure.
    """
    if result.is_spam:
        return False
    return result.confidence < 0.5 or result.severity >= 4


DEFAULT_ENRICHER = LexiconEnricher()
