"""Deterministic policy: turns the model's verdict into the final decision and allowed proposals.

Pure standard-library code. The model's opinion is an INPUT here, never an instruction:
safety rules below always win over it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORIES = ("personal", "work", "newsletter", "automated_notification", "marketing",
              "invitation", "spam_suspicious", "manual_review")
BULK = {"newsletter", "marketing"}
HARD_PROTECT_FLAGS = {"account_or_security", "financial", "legal_or_official"}
PROTECT_FLAGS = HARD_PROTECT_FLAGS | {"time_sensitive"}

# Words that make a message "protected": it must never be suggested for archiving.
# Deliberately broad - the cost of a false "protected" is only a missed archive suggestion.
_PROTECT_WORDS = [
    # security / account
    "verification code", "verificatiecode", "one-time", "eenmalige code", "security alert",
    "beveiligingsmelding", "wachtwoord", "password", "inlogcode", "login attempt", "two-factor",
    "tweestaps", "2fa", "suspicious sign-in", "ongebruikelijke inlog", "digid",
    # financial
    "invoice", "factuur", "payment", "betaling", "betalingsherinnering", "aanmaning", "incasso",
    "overdue", "achterstallig", "iban", "creditcard", "afschrift", "refund", "terugbetaling",
    "belasting", "btw", "aangifte", "hypotheek", "polis",
    # legal / official
    "belastingdienst", "rijksoverheid", "deurwaarder", "dagvaarding", "subpoena", "opzegging",
    "termination",
]
_PROTECT_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _PROTECT_WORDS) + r")\b", re.I)


_URL = re.compile(r"https?://", re.I)
_THREAT = re.compile(
    r"\b(account|rekening|toegang)\b.{0,40}\b(geblokkeerd|blocked|suspended|locked|gedeactiveerd|"
    r"deactivated|opgeschort|beperkt|limited)\b|\b(geblokkeerd|blocked|suspended|locked|gedeactiveerd|"
    r"deactivated|opgeschort)\b.{0,40}\b(account|rekening)\b", re.I | re.S)
_URGENCY = re.compile(
    r"\b(binnen|within)\s+\d+\s*(uur|uren|hours?|dagen|days?|minuten|minutes?)\b|"
    r"\b(urgent|dringend|onmiddellijk|immediately|laatste waarschuwing|final warning|action required)\b", re.I)
_CRED = re.compile(r"\b(pincode|pin code|pin-code|pasnummer|cvv|bsn|inloggegevens|credentials|creditcard|"
                   r"card number|kaartnummer)\b", re.I)
_ASK = re.compile(r"\b(invoeren|invullen|vul|voer|enter|provide|verify|verifieer|bevestig|confirm|update|"
                  r"bijwerken|stuur)\b", re.I)


def phishing_indicators(subject: str, body: str) -> list[str]:
    """Deterministic phishing tell-tales (needs a link plus a threat/credential-request pattern).
    Model-independent on purpose: smaller models miss these."""
    text = f"{subject or ''}\n{(body or '')[:4000]}"
    if not _URL.search(text):
        return []
    found = {"account_threat": bool(_THREAT.search(text)), "urgency": bool(_URGENCY.search(text)),
             "credential_request": bool(_CRED.search(text) and _ASK.search(text))}
    suspicious = ((found["account_threat"] and (found["urgency"] or found["credential_request"]))
                  or (found["credential_request"] and found["urgency"]))
    return sorted(k for k, v in found.items() if v) if suspicious else []


@dataclass(frozen=True)
class Verdict:
    category: str
    importance: str
    confidence: float
    flags: tuple[str, ...]
    reason: str


@dataclass
class ActionSpec:
    action_type: str
    payload: dict
    reason: str


@dataclass
class Decision:
    category: str
    importance: str
    confidence: float
    reasons: str
    safety_flags: list[str] = field(default_factory=list)
    injection_flag: bool = False
    protected: bool = False
    actions: list[ActionSpec] = field(default_factory=list)
    source: str = "llm"  # "llm" or "rule" (model output unusable)


def decide(*, subject: str, body: str, verdict: Verdict | None, injection_signals: list[str],
           min_confidence: float = 0.6, archive_confidence: float = 0.85,
           newsletter_target: str = "Labels/Ads", bulk_headers: bool = False,
           known_correspondent: bool = False) -> Decision:
    text = f"{subject or ''}\n{(body or '')[:3000]}"
    keywords = sorted({m.group(0).lower() for m in _PROTECT_RE.finditer(text)})[:5]
    safety = [f"keyword:{k}" for k in keywords]
    injection = bool(injection_signals)
    phishing = phishing_indicators(subject, body)
    safety += [f"phishing:{k}" for k in phishing]
    keywords = keywords + (["phishing"] if phishing else [])  # counts as protected

    if verdict is None:  # model output unusable even after a retry: never guess
        if injection:
            safety += ["prompt_injection_suspected", *[f"injection:{s}" for s in injection_signals]]
        return Decision("manual_review", "normal", 0.0, "model output invalid after retry",
                        ["llm_output_invalid", *safety], injection, bool(keywords), [], "rule")

    flags = set(verdict.flags)
    if "contains_instructions_to_assistant" in flags:
        injection = True
        injection_signals = [*injection_signals, "model_flagged"]
    # A deadline protects a message, except in marketing, where "only today!" is just a sales tactic.
    protected = (bool(keywords) or bool(flags & HARD_PROTECT_FLAGS)
                 or ("time_sensitive" in flags and verdict.category != "marketing"))
    safety += [f"llm:{f}" for f in sorted(flags & PROTECT_FLAGS)]
    if "looks_like_phishing" in flags:
        safety.append("llm:looks_like_phishing")
    if injection:
        safety += ["prompt_injection_suspected", *[f"injection:{s}" for s in sorted(set(injection_signals))]]

    category, notes = verdict.category, []
    if phishing and category not in ("spam_suspicious", "manual_review"):
        notes.append(f"phishing indicators ({', '.join(phishing)}) contradict model category {category}")
        category = "manual_review"  # never auto-spam: a human decides
    if category == "spam_suspicious" and verdict.confidence < archive_confidence:
        notes.append(f"model guess {category} ({verdict.confidence:.2f}) too uncertain for spam")
        category = "manual_review"
    elif verdict.confidence < min_confidence and category != "manual_review":
        notes.append(f"model guess {category} ({verdict.confidence:.2f}) below threshold")
        category = "manual_review"
    # Bulk mail "asking" for something (rate us, donate...) is normal; only doubt it when nothing
    # in the headers says it is a mass mailing.
    if category in BULK and "requests_reply" in flags and not bulk_headers:
        notes.append("bulk category but a reply seems requested")
        category = "manual_review"
    person_like = known_correspondent and not bulk_headers
    if person_like:
        safety.append("known_correspondent")

    importance = verdict.importance
    if protected and importance == "low":
        importance = "normal"

    actions: list[ActionSpec] = []
    if (category in BULK and not protected and not injection and not person_like
            and "looks_like_phishing" not in flags and verdict.confidence >= archive_confidence):
        actions.append(ActionSpec(
            "move_to_folder", {"target": newsletter_target, "archive_from_inbox": True,
                               "category": category},
            f"{category} with confidence {verdict.confidence:.2f}, no protected content"))

    reason = (verdict.reason or "").strip()[:300]
    if notes:
        reason = (reason + " | " if reason else "") + "; ".join(notes)
    return Decision(category, importance, verdict.confidence, reason, safety, injection,
                    protected, actions, "llm")
