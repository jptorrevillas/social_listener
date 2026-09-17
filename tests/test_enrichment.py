"""Classification. The spam detector carries most of the weight on YouTube."""

from social_listener.enrichment import (
    DEFAULT_ENRICHER,
    detect_intent,
    extract_topics,
    route_to_expensive_lane,
    score_spam,
)


def enrich(text, **kw):
    payload = {"title": None, "text": text, "author_name": "someone"}
    payload.update(kw)
    return DEFAULT_ENRICHER.enrich(payload)


# -- spam ------------------------------------------------------------------


def test_crypto_bait_is_spam():
    assert enrich(
        "Make $5000 a week from home, DM me on WhatsApp +639171234567",
        author_name="CryptoWealthNow",
    ).is_spam


def test_sub_for_sub_is_spam():
    assert enrich("SUBSCRIBE TO MY CHANNEL AND I SUB BACK, link in bio").is_spam


def test_engagement_filler_is_spam():
    assert enrich("who else is watching this in 2026?? like if you agree").is_spam


def test_a_legitimate_comment_with_a_link_is_not_spam():
    # The false positive that would matter most: helpful people post links.
    result = enrich("The enrolment guide is at https://example.edu/enrol if it helps")
    assert result.is_spam is False


def test_a_plain_complaint_is_not_spam():
    assert enrich("The enrolment portal timed out four times. Ridiculous.").is_spam is False


def test_spam_scoring_is_additive_not_a_single_trigger():
    one_signal = score_spam("check out my channel")
    many = score_spam("CHECK OUT MY CHANNEL, link in bio, DM me +639171234567")
    assert many > one_signal


def test_a_spam_author_name_contributes():
    assert score_spam("hello there", "CryptoEarningsBot") > score_spam("hello there", "student")


def test_shouting_contributes():
    assert score_spam("THIS IS ABSOLUTELY OUTRAGEOUS BEHAVIOUR") > score_spam(
        "this is absolutely outrageous behaviour"
    )


def test_empty_text_is_not_spam():
    assert score_spam("") == 0.0


def test_spam_is_never_escalated_to_a_paid_lane():
    result = enrich("SUBSCRIBE TO MY CHANNEL AND I SUB BACK, link in bio")
    assert result.needs_expensive_lane is False


def test_spam_is_never_high_severity():
    result = enrich(
        "SUBSCRIBE AND I SUB BACK, link in bio", like_count=99999, reply_count=99999
    )
    assert result.severity == 1


# -- sentiment -------------------------------------------------------------


def test_clear_negative_scores_negative():
    assert enrich("This is terrible, absolutely ridiculous, I am fed up.").sentiment == "negative"


def test_clear_positive_scores_positive():
    assert enrich("Wonderful experience, grateful, highly recommend.").sentiment == "positive"


def test_sarcasm_caps_confidence_and_escalates():
    # The lexicon reads this as positive. It is wrong. What matters is that it
    # says so quietly and hands the item to a human.
    result = enrich("Oh brilliant, the portal is down again. Thanks a lot /s")
    assert result.confidence <= 0.35
    assert result.needs_expensive_lane is True


def test_confidence_is_lower_near_the_decision_boundary():
    faint = enrich("It was ok I suppose.")
    strong = enrich("Absolutely dreadful, awful, appalling service.")
    assert faint.confidence < strong.confidence


# -- intent and severity ---------------------------------------------------


def test_a_complaint_phrased_as_a_question_is_a_complaint():
    assert detect_intent("Why is this ridiculous service still unacceptable?") == "complaint"


def test_abuse_outranks_other_intents():
    assert detect_intent("You are an idiot, how do I get a refund") == "abuse"


def test_topics_are_extracted():
    topics = extract_topics("the tuition invoice and the enrolment portal")
    assert "billing" in topics and "enrolment" in topics


def test_severity_rises_with_likes():
    quiet = enrich("This is terrible and unacceptable.", like_count=0)
    loud = enrich("This is terrible and unacceptable.", like_count=500)
    assert loud.severity > quiet.severity


def test_severity_rises_on_consequence_language():
    plain = enrich("This is terrible.")
    legal = enrich("This is terrible, I am contacting a lawyer about this fraud.")
    assert legal.severity > plain.severity


def test_severity_is_capped_at_five():
    result = enrich(
        "Absolutely terrible unacceptable fraud, contacting a lawyer and a journalist",
        like_count=99999,
        reply_count=99999,
    )
    assert result.severity == 5


def test_positive_comments_are_never_high_severity():
    assert enrich("Wonderful, highly recommend!", like_count=99999).severity == 1


def test_low_confidence_routes_to_the_expensive_lane():
    result = enrich("It is fine I guess.")
    result.confidence = 0.2
    assert route_to_expensive_lane(result) is True
