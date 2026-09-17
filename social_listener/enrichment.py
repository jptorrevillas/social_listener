"""Classification: spam, sentiment, intent, severity, topics.

The honest position on sentiment carries over from the Reddit build: on
social-media text, lexicon methods and transformers land closer together than
vendor material suggests, and both are weak on sarcasm. So confidence is
reported everywhere and low-confidence items are routed to a human rather than
asserted.

What changes for YouTube is the noise. Reddit's automated content is mostly
AutoModerator boilerplate -- uniform, trivially matched. YouTube comment spam is
an industry: crypto bait, sub-for-sub, prize scams, engagement farming, phone
numbers and off-platform contact details. It is both a larger share of the
stream and more varied, so the spam lane here is a scored detector rather than
a phrase list, and it runs before anything expensive.

Three lanes, cheapest first, exactly as specified:

    lexicon      VADER + rules, on everything      free
    transformer  on matched items                  ~50ms CPU
    llm          ambiguous / severe / sarcastic     ~$0.001-0.01

Only the lexicon lane ships. The others are `Enricher` implementations with the
same interface; `route_to_expensive_lane()` already computes what would be
escalated, which is what makes the cost projection real rather than notional.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# -- spam signals -----------------------------------------------------------

SPAM_PHRASES = (
    "check out my channel", "sub for sub", "i sub back", "subscribe to my channel",
    "link in bio", "link in my profile", "dm me", "whatsapp", "telegram",
    "make $", "earn $", "free gift", "claim your prize", "you have been selected",
    "no experience needed", "from home", "crypto", "forex", "binary options",
    "i am a bot", "generated automatically", "let's grow together",
)
SPAM_PATTERNS = (
    re.compile(r"\+\d{10,}"),                  # phone numbers
    re.compile(r"(?:https?://|www\.)\S+"),     # bare links
    re.compile(r"\b\d+\s*(?:usd|php|\$)\s*(?:a|per)\s*(?:day|week|month)\b", re.I),
    re.compile(r"(.)\1{6,}"),                  # aaaaaaa / !!!!!!!
)
# Engagement farming: low-value, high-volume, not malicious but pure noise.
FILLER_PHRASES = ("first!", "who else is watching", "like if you agree", "early squad")

SPAM_AUTHOR_HINTS = ("crypto", "giftcard", "gift card", "subforsub", "bot", "earnings", "promo")

SARCASM_CUES = ("/s", "yeah right", "sure thing", "oh brilliant", "oh great",
                "thanks a lot", "very reassuring", "i'll believe it")

INTENT_CUES = {
    "complaint": (
        "still waiting", "no response", "nobody replied", "terrible", "awful",
        "ridiculous", "unacceptable", "fed up", "worst", "scam", "rude",
        "ignored", "timed out", "broken", "refund", "double-charged", "shameful",
        "fobbed off", "cancelling",
    ),
    "question": (
        "does anyone", "anyone know", "how do i", "how long", "is it worth",
        "should i", "what happens", "can someone", "any idea", "deciding",
    ),
    "praise": (
        "credit to", "grateful", "thank you", "thanks to", "excellent",
        "best decision", "earns its keep", "shout out",
    ),
    "recommendation": ("highly recommend", "would recommend", "go for it", "worth it"),
    "comparison": ("better than", "compared to", "versus", " vs ", "instead of"),
    "enquiry": ("accept transferees", "how do i pay", "do units carry", "applies to"),
    "abuse": ("idiot", "stupid", "shut up", "trash"),
}

# Consequence language: raises severity regardless of sentiment score.
ESCALATION_CUES = (
    "lawyer", "legal", "sue", "lawsuit", "ombudsman", "ched", "deped",
    "journalist", "news", "complaint filed", "data breach", "leaked",
    "harassment", "discrimination", "safety", "fraud", "cannot be legal",
)

ASPECTS = {
    "enrolment": ("enrol", "enroll", "registration", "admission"),
    "billing": ("tuition", "fee", "payment", "invoice", "refund", "billing", "charged"),
    "faculty": ("professor", "teacher", "instructor", "faculty", "prof "),
    "facilities": ("campus", "library", "canteen", "wifi", "laboratory", "building"),
    "support": ("registrar", "helpdesk", "support", "hotline", "window", "ticket"),
    "academics": ("grade", "exam", "syllabus", "course", "subject", "thesis", "units"),
    "outcomes": ("job", "placement", "employed", "career", "graduate"),
}


@dataclass
class EnrichmentResult:
    model_name: str
    model_version: str
    sentiment: str
    sentiment_score: float
    confidence: float
    severity: int
    intent: str
    topics: list = field(default_factory=list)
    is_spam: bool = False
    spam_score: float = 0.0
    rationale: str = ""
    needs_expensive_lane: bool = False

    def topics_json(self) -> str:
        return json.dumps(self.topics)


class Enricher(ABC):
    model_name = "abstract"
    model_version = "0"

    @abstractmethod
    def enrich(self, item: dict) -> EnrichmentResult: ...


def score_spam(text: str, author_name: Optional[str] = None) -> float:
    """0..1. Additive evidence rather than a single trigger phrase.

    Scored rather than boolean because YouTube spam is varied: any one signal
    (a link, shouting, a phone number) is weak alone but decisive in company.
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return 0.0

    score = 0.0
    score += 0.45 * sum(1 for phrase in SPAM_PHRASES if phrase in lowered)
    score += 0.30 * sum(1 for pattern in SPAM_PATTERNS if pattern.search(text or ""))
    score += 0.35 * sum(1 for phrase in FILLER_PHRASES if phrase in lowered)

    if author_name:
        author = author_name.lower()
        if any(hint in author for hint in SPAM_AUTHOR_HINTS):
            score += 0.4

    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) >= 12:
        shouting = sum(1 for c in letters if c.isupper()) / len(letters)
        if shouting > 0.6:
            score += 0.3

    return min(score, 1.0)


def detect_intent(text: str) -> str:
    lowered = (text or "").lower()
    scores = {}
    for intent, cues in INTENT_CUES.items():
        hits = sum(1 for cue in cues if cue in lowered)
        if hits:
            scores[intent] = hits
    if not scores:
        return "other"
    # Consequence outranks grammar: a complaint phrased as a question is a
    # complaint, and needs routing as one.
    for priority in ("abuse", "complaint"):
        if priority in scores:
            return priority
    return max(scores.items(), key=lambda kv: kv[1])[0]


def extract_topics(text: str) -> list:
    lowered = (text or "").lower()
    return [name for name, cues in ASPECTS.items() if any(c in lowered for c in cues)][:4]


class LexiconEnricher(Enricher):
    model_name = "vader-rules-yt"
    model_version = "0.1.0"

    SPAM_THRESHOLD = 0.5

    def __init__(self) -> None:
        self._analyzer = SentimentIntensityAnalyzer()

    def enrich(self, item: dict) -> EnrichmentResult:
        text = " ".join(
            filter(None, (item.get("title"), item.get("text"), item.get("description")))
        ).strip()
        author = item.get("author_name")

        spam_score = score_spam(text, author)
        if spam_score >= self.SPAM_THRESHOLD:
            return EnrichmentResult(
                model_name=self.model_name,
                model_version=self.model_version,
                sentiment="neutral",
                sentiment_score=0.0,
                confidence=round(min(0.5 + spam_score / 2, 0.98), 3),
                severity=1,
                intent="other",
                is_spam=True,
                spam_score=round(spam_score, 3),
                rationale=f"Spam score {spam_score:.2f}; dropped before sentiment scoring.",
            )

        scores = self._analyzer.polarity_scores(text)
        compound = scores["compound"]
        sentiment = (
            "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
        )

        confidence = min(abs(compound) * 1.4 + 0.3, 0.95)
        sarcastic = any(cue in text.lower() for cue in SARCASM_CUES)
        if sarcastic:
            # The lexicon's documented blind spot. Say so rather than assert.
            confidence = min(confidence, 0.35)

        intent = detect_intent(text)
        topics = extract_topics(text)
        severity = self._severity(item, sentiment, compound, text, intent)

        bits = [f"VADER compound {compound:+.2f}"]
        if intent != "other":
            bits.append(f"intent={intent}")
        if sarcastic:
            bits.append("sarcasm cue -- confidence capped, escalated")
        if spam_score:
            bits.append(f"spam score {spam_score:.2f} (below threshold)")
        if topics:
            bits.append("topics: " + ", ".join(topics))

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
            spam_score=round(spam_score, 3),
            rationale="; ".join(bits),
        )
        result.needs_expensive_lane = route_to_expensive_lane(result)
        return result

    @staticmethod
    def _severity(item: dict, sentiment: str, compound: float, text: str, intent: str) -> int:
        """1-5, combining sentiment, reach and consequence.

        YouTube gives real like and reply counts -- no vote fuzzing as on
        Reddit -- so reach is a more trustworthy input here, and a liked
        complaint genuinely is more urgent than an ignored one.
        """
        if sentiment == "positive":
            return 1
        severity = 2 if sentiment == "negative" else 1

        if compound <= -0.6:
            severity += 1
        if intent in ("complaint", "abuse"):
            severity += 1

        likes = item.get("like_count") or 0
        replies = item.get("reply_count") or 0
        if likes >= 50 or replies >= 15:
            severity += 1

        if any(cue in text.lower() for cue in ESCALATION_CUES):
            severity += 1

        return max(1, min(severity, 5))


def route_to_expensive_lane(result: EnrichmentResult) -> bool:
    """Escalate on low confidence or high severity. Never on spam."""
    if result.is_spam:
        return False
    return result.confidence < 0.5 or result.severity >= 4


DEFAULT_ENRICHER = LexiconEnricher()
