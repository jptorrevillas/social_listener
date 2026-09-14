from social_listener.enrichment import (
    DEFAULT_ENRICHER,
    detect_intent,
    extract_topics,
    looks_automated,
    route_to_expensive_lane,
)


def enrich(body, **kw):
    payload = {"title": None, "body": body, "author": "someone", "score": 1, "num_comments": 0}
    payload.update(kw)
    return DEFAULT_ENRICHER.enrich(payload)


def test_bot_authors_are_filtered():
    assert looks_automated("AutoModerator", "anything")


def test_bot_boilerplate_is_filtered_regardless_of_author():
    assert looks_automated("realperson", "I am a bot, and this action was performed automatically.")


def test_spam_is_flagged_and_never_escalated():
    result = enrich("I am a bot. Beep boop.", author="AutoModerator")
    assert result.is_spam
    assert result.needs_expensive_lane is False


def test_clear_negative_is_scored_negative():
    result = enrich("This is terrible, absolutely ridiculous and I am fed up.")
    assert result.sentiment == "negative"


def test_clear_positive_is_scored_positive():
    result = enrich("Wonderful experience, highly recommend, very grateful.")
    assert result.sentiment == "positive"


def test_sarcasm_caps_confidence_and_escalates():
    # The lexicon reads this as positive. The point is that it says so with low
    # confidence and routes the item onward rather than asserting it.
    result = enrich("Oh great, the portal is down again during enrolment week. Thanks a lot /s")
    assert result.confidence <= 0.35
    assert result.needs_expensive_lane is True


def test_complaint_outranks_question_when_both_cues_are_present():
    assert detect_intent("Why is this terrible service still unacceptable?") == "complaint"


def test_topics_are_extracted():
    topics = extract_topics("the tuition invoice and the enrolment portal")
    assert "billing" in topics and "enrolment" in topics


def test_severity_rises_with_reach():
    quiet = enrich("This is terrible and unacceptable.", score=1, num_comments=0)
    loud = enrich("This is terrible and unacceptable.", score=500, num_comments=200)
    assert loud.severity > quiet.severity


def test_severity_rises_on_escalation_language():
    plain = enrich("This is terrible.", score=1)
    legal = enrich("This is terrible, I am talking to a lawyer about this fraud.", score=1)
    assert legal.severity > plain.severity


def test_severity_is_capped_at_five():
    result = enrich(
        "Absolutely terrible unacceptable fraud, contacting a lawyer and a journalist.",
        score=5000,
        num_comments=2000,
    )
    assert result.severity == 5


def test_positive_items_are_never_high_severity():
    result = enrich("Wonderful, highly recommend!", score=5000, num_comments=2000)
    assert result.severity == 1


def test_routing_escalates_low_confidence():
    result = enrich("It is fine I guess.")
    result.confidence = 0.2
    assert route_to_expensive_lane(result) is True
