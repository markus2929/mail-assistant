from mailassistant.classify.policy import Verdict, decide


def v(category="newsletter", conf=0.92, flags=(), importance="low", reason="because"):
    return Verdict(category, importance, conf, tuple(flags), reason)


def run(verdict, subject="Weekly digest", body="Some news.", signals=(), **kw):
    return decide(subject=subject, body=body, verdict=verdict, injection_signals=list(signals), **kw)


def test_clean_newsletter_gets_archive_suggestion():
    d = run(v())
    assert d.category == "newsletter" and not d.protected and not d.injection_flag
    assert [a.action_type for a in d.actions] == ["move_to_folder"]
    assert d.actions[0].payload == {"target": "Labels/Ads", "archive_from_inbox": True, "category": "newsletter"}


def test_not_confident_enough_to_archive():
    d = run(v(conf=0.7))
    assert d.category == "newsletter" and d.actions == []


def test_protected_keywords_block_archiving_and_raise_importance():
    d = run(v(), subject="Uw factuur", body="Wij sturen u deze nieuwsbrief")
    assert d.protected and d.actions == [] and d.importance == "normal"
    assert any(f.startswith("keyword:") for f in d.safety_flags)


def test_protected_by_model_flag():
    d = run(v(flags=["financial"]))
    assert d.protected and d.actions == [] and "llm:financial" in d.safety_flags


def test_bulk_but_reply_requested_goes_to_manual_review():
    d = run(v(flags=["requests_reply"]))
    assert d.category == "manual_review" and d.actions == []


def test_low_confidence_becomes_manual_review_and_keeps_the_guess_visible():
    d = run(v(category="work", conf=0.4, importance="normal"))
    assert d.category == "manual_review" and "work" in d.reasons and d.confidence == 0.4


def test_spam_is_never_acted_on_and_needs_high_confidence():
    assert run(v(category="spam_suspicious", conf=0.7)).category == "manual_review"
    d = run(v(category="spam_suspicious", conf=0.97))
    assert d.category == "spam_suspicious" and d.actions == []


def test_injection_signals_block_all_actions():
    d = run(v(), signals=["ignore_previous_instructions"])
    assert d.injection_flag and d.actions == []
    assert "prompt_injection_suspected" in d.safety_flags
    assert "injection:ignore_previous_instructions" in d.safety_flags


def test_model_flagged_injection_counts():
    d = run(v(flags=["contains_instructions_to_assistant"]))
    assert d.injection_flag and d.actions == []


def test_phishing_flag_blocks_archive():
    assert run(v(flags=["looks_like_phishing"])).actions == []


def test_invalid_model_output_is_manual_review_from_rules():
    d = run(None, subject="Aanmaning", body="betaal nu")
    assert (d.category, d.source, d.confidence) == ("manual_review", "rule", 0.0)
    assert "llm_output_invalid" in d.safety_flags and d.protected and d.actions == []


def test_personal_mail_never_gets_actions():
    assert run(v(category="personal", conf=0.95, importance="normal")).actions == []


def test_marketing_deadline_does_not_protect_but_newsletter_deadline_does():
    m = run(v(category="marketing", flags=["time_sensitive"]))
    assert not m.protected and [a.action_type for a in m.actions] == ["move_to_folder"]
    n = run(v(category="newsletter", flags=["time_sensitive"]))
    assert n.protected and n.actions == []


def test_marketing_with_a_real_financial_flag_stays_protected():
    assert run(v(category="marketing", flags=["time_sensitive", "financial"])).actions == []


def test_bulk_call_to_action_is_fine_when_headers_show_a_mass_mailing():
    d = run(v(category="marketing", flags=["requests_reply"]), bulk_headers=True)
    assert d.category == "marketing" and [a.action_type for a in d.actions] == ["move_to_folder"]


def test_known_correspondent_without_bulk_headers_is_never_suggested_for_archive():
    d = run(v(), known_correspondent=True)
    assert d.category == "newsletter" and d.actions == [] and "known_correspondent" in d.safety_flags
    assert run(v(), known_correspondent=True, bulk_headers=True).actions != []


PHISH = ("Uw account is geblokkeerd. Klik binnen 24 uur op de link en voer uw gegevens en pincode in: "
         "http://secure-bank-login.example/verify")


def test_phishing_indicators_override_a_confident_wrong_model():
    d = run(v(category="automated_notification", conf=1.0, importance="high"), body=PHISH)
    assert d.category == "manual_review" and d.protected and d.actions == []
    assert "phishing:account_threat" in d.safety_flags and "phishing" in d.reasons


def test_phishing_check_needs_a_link_and_does_not_hit_normal_security_mail():
    from mailassistant.classify.policy import phishing_indicators
    assert phishing_indicators("", PHISH.replace("http://secure-bank-login.example/verify", "")) == []
    legit = [
        "Er is zojuist ingelogd op uw rekening vanaf een nieuw apparaat. Neem contact op als u dit niet was.",
        "Je hebt gevraagd om je wachtwoord opnieuw in te stellen. Klik binnen 24 uur: https://shop.example/reset",
        "Your verification code is 482913. https://service.example/help",
        "Uw factuur staat klaar. Betaal binnen 14 dagen: https://leverancier.example/pay",
    ]
    for text in legit:
        assert phishing_indicators("", text) == [], text


def test_spam_category_from_the_model_is_left_alone_by_the_phishing_rule():
    d = run(v(category="spam_suspicious", conf=0.97), body=PHISH)
    assert d.category == "spam_suspicious" and d.actions == []
