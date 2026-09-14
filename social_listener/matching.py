"""The tiered matching layer (§8).

Between capture and enrichment sits "does this item actually mention what we
care about?". Getting it wrong is the dominant source of user complaints in
every listening tool, so the design is:

  1. literal / phrase  -- normalised, word-boundary aware. Rejects ~99%.
  2. boolean           -- AND / OR / NOT over phrases. Users need NOT more than
                          they expect.
  3. regex             -- an escape hatch for variants.
  4. negative patterns -- per-term exclusions that kill known false positives.

Every match carries the span that hit, because a reviewer who cannot see why an
item was flagged stops trusting the feed.

A fifth tier -- LLM relevance adjudication for ambiguous hits -- is specified in
§8 but deliberately not implemented here: it needs a live corpus to be worth
its cost. `MatchResult.confidence` is the field it would populate.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SPAN_CONTEXT_CHARS = 60


@dataclass(frozen=True)
class MatchResult:
    term_label: str
    matched_text: str
    confidence: float


def normalise_text(text: str) -> str:
    """Unicode NFKC, so smart quotes and full-width forms match.

    Deliberately NOT casefolded: every search below is already IGNORECASE, and
    casefolding here would only mean the span shown to a reviewer comes back in
    lower case, which reads as a bug.
    """
    return unicodedata.normalize("NFKC", text or "")


def _span_with_context(haystack: str, start: int, end: int) -> str:
    left = max(start - SPAN_CONTEXT_CHARS, 0)
    right = min(end + SPAN_CONTEXT_CHARS, len(haystack))
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(haystack) else ""
    return f"{prefix}{haystack[left:right].strip()}{suffix}"


def find_phrase(haystack: str, phrase: str) -> tuple[int, int] | None:
    """Word-boundary-aware phrase search over normalised text."""
    if not phrase.strip():
        return None
    pattern = r"\b" + r"\s+".join(re.escape(word) for word in phrase.split()) + r"\b"
    hit = re.search(pattern, haystack, flags=re.IGNORECASE)
    return (hit.start(), hit.end()) if hit else None


# -- boolean expressions ----------------------------------------------------
#
# Grammar is deliberately small: quoted phrases or bare words, combined with
# AND / OR / NOT and parentheses. Anything more expressive belongs in regex.

_TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+', re.IGNORECASE)


def _tokenise(expression: str) -> list[str]:
    return _TOKEN_RE.findall(expression)


class _BooleanParser:
    """Recursive-descent parser: OR binds loosest, then AND, then NOT."""

    def __init__(self, tokens: list[str], haystack: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.haystack = haystack
        self.hits: list[tuple[int, int]] = []

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str | None:
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def parse(self) -> bool:
        result = self.parse_or()
        return result

    def parse_or(self) -> bool:
        result = self.parse_and()
        while (token := self.peek()) and token.upper() == "OR":
            self.next()
            right = self.parse_and()
            result = result or right
        return result

    def parse_and(self) -> bool:
        result = self.parse_not()
        while (token := self.peek()) and token.upper() == "AND":
            self.next()
            right = self.parse_not()
            result = result and right
        return result

    def parse_not(self) -> bool:
        token = self.peek()
        if token and token.upper() == "NOT":
            self.next()
            return not self.parse_not()
        return self.parse_atom()

    def parse_atom(self) -> bool:
        token = self.next()
        if token is None:
            return False
        if token == "(":
            result = self.parse_or()
            if self.peek() == ")":
                self.next()
            return result
        phrase = token.strip('"')
        span = find_phrase(self.haystack, phrase)
        if span:
            self.hits.append(span)
            return True
        return False


def evaluate_boolean(haystack: str, expression: str) -> tuple[bool, list[tuple[int, int]]]:
    parser = _BooleanParser(_tokenise(expression), haystack)
    result = parser.parse()
    return result, parser.hits


# -- the matcher ------------------------------------------------------------


class Matcher:
    """Evaluates one item against a set of terms."""

    def __init__(self, terms) -> None:
        self.terms = [t for t in terms if getattr(t, "is_active", True)]

    def match(self, text: str) -> list[MatchResult]:
        haystack = normalise_text(text)
        if not haystack.strip():
            return []

        results: list[MatchResult] = []
        for term in self.terms:
            hit = self._match_one(term, haystack)
            if hit is None:
                continue
            if self._excluded(term, haystack):
                continue
            results.append(hit)
        return results

    def _match_one(self, term, haystack: str) -> MatchResult | None:
        match_type = (term.match_type or "phrase").lower()
        pattern = term.pattern or ""

        if match_type == "boolean":
            ok, spans = evaluate_boolean(haystack, pattern)
            if ok and spans:
                start, end = spans[0]
                return MatchResult(term.label, _span_with_context(haystack, start, end), 1.0)
            return None

        if match_type == "regex":
            try:
                hit = re.search(pattern, haystack, flags=re.IGNORECASE)
            except re.error:
                return None
            if hit:
                return MatchResult(
                    term.label, _span_with_context(haystack, hit.start(), hit.end()), 0.9
                )
            return None

        # literal and phrase share the word-boundary path; a "literal" term is
        # simply a phrase with no internal whitespace.
        span = find_phrase(haystack, pattern)
        if span:
            return MatchResult(term.label, _span_with_context(haystack, *span), 1.0)
        return None

    @staticmethod
    def _excluded(term, haystack: str) -> bool:
        """A negative pattern vetoes an otherwise-good match."""
        negative = getattr(term, "negative_pattern", None)
        if not negative:
            return False
        for clause in filter(None, (c.strip() for c in negative.split("|"))):
            if find_phrase(haystack, clause):
                return True
        return False
