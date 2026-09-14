from types import SimpleNamespace as T

from social_listener.matching import Matcher, evaluate_boolean, find_phrase, normalise_text


def term(**kw):
    base = dict(
        label="T", match_type="phrase", pattern="", negative_pattern=None, is_active=True
    )
    base.update(kw)
    return T(**base)


def test_phrase_respects_word_boundaries():
    assert find_phrase("the enrolment portal", "enrolment")
    # "enrol" must not match inside "enrolment".
    assert find_phrase("the enrolment portal", "enrol") is None


def test_phrase_tolerates_multiple_spaces():
    assert find_phrase("northbridge   college is open", "northbridge college")


def test_normalise_preserves_case():
    # Casefolding here would make every span shown to a reviewer lower case.
    assert normalise_text("Northbridge College") == "Northbridge College"


def test_matching_is_case_insensitive_anyway():
    matcher = Matcher([term(label="B", pattern="northbridge college")])
    assert matcher.match("I went to NORTHBRIDGE COLLEGE")


def test_span_keeps_original_casing():
    matcher = Matcher([term(label="B", pattern="northbridge college")])
    hit = matcher.match("Applying to Northbridge College soon")[0]
    assert "Northbridge College" in hit.matched_text


def test_boolean_not_excludes():
    ok, _ = evaluate_boolean("northbridge road is closed", '"northbridge" AND NOT "northbridge road"')
    assert ok is False


def test_boolean_or_includes():
    ok, _ = evaluate_boolean("nbc open day", '("northbridge college" OR nbc)')
    assert ok is True


def test_negative_pattern_vetoes_a_good_match():
    matcher = Matcher(
        [term(label="B", pattern="northbridge", negative_pattern="northbridge road")]
    )
    assert matcher.match("traffic on Northbridge Road") == []
    assert matcher.match("applying to Northbridge next year")


def test_multiple_negative_clauses_are_pipe_separated():
    matcher = Matcher(
        [term(label="B", pattern="northbridge", negative_pattern="northbridge road | northbridge mall")]
    )
    assert matcher.match("meet me at Northbridge Mall") == []


def test_inactive_terms_are_ignored():
    matcher = Matcher([term(label="B", pattern="northbridge", is_active=False)])
    assert matcher.match("northbridge college") == []


def test_invalid_regex_does_not_raise():
    matcher = Matcher([term(label="B", match_type="regex", pattern="(unclosed")])
    assert matcher.match("anything") == []


def test_empty_text_matches_nothing():
    matcher = Matcher([term(label="B", pattern="northbridge")])
    assert matcher.match("") == []
