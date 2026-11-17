"""Classification without any database: used by the service and by the evaluation tool."""
from __future__ import annotations

from dataclasses import dataclass

from mailassistant.classify.policy import Decision, Verdict, decide
from mailassistant.classify.prompt import PROMPT_VERSION, EmailView, build_messages
from mailassistant.llm.runner import Recorder, StructuredOutputError, run_structured
from mailassistant.llm.schemas import ClassificationOutput
from mailassistant.security import injection


@dataclass
class ClassifyConfig:
    context: str = ""
    body_chars: int = 6000
    min_confidence: float = 0.6
    archive_confidence: float = 0.85
    newsletter_target: str = "Labels/Ads"

    @classmethod
    def from_settings(cls, s) -> ClassifyConfig:
        return cls(s.classify_context, s.classify_body_chars, s.classify_min_confidence,
                   s.classify_archive_confidence, s.folder_newsletters)


@dataclass
class ClassifyResult:
    decision: Decision
    verdict: Verdict | None
    prompt_version: str = PROMPT_VERSION


def has_bulk_headers(headers: dict) -> bool:
    h = headers or {}
    return bool(h.get("list-unsubscribe") or h.get("list-id")
                or str(h.get("precedence", "")).lower() in ("bulk", "list", "junk"))


def classify_view(client, view: EmailView, cfg: ClassifyConfig,
                  record: Recorder | None = None, *, known_correspondent: bool = False) -> ClassifyResult:
    """May raise LLMUnavailable / LLMModelMissing (caller defers); never raises for bad output."""
    signals = injection.detect(f"{view.subject or ''}\n{view.body_text}")
    messages = build_messages(view, body_chars=cfg.body_chars, context=cfg.context)
    try:
        out = run_structured(client, messages, ClassificationOutput, task="classify", record=record)
        verdict = Verdict(out.category, out.importance, out.confidence, tuple(out.flags), out.reason)
    except StructuredOutputError:
        verdict = None
    decision = decide(subject=view.subject or "", body=view.body_text, verdict=verdict,
                      injection_signals=signals, min_confidence=cfg.min_confidence,
                      archive_confidence=cfg.archive_confidence,
                      newsletter_target=cfg.newsletter_target,
                      bulk_headers=has_bulk_headers(view.headers),
                      known_correspondent=known_correspondent)
    return ClassifyResult(decision, verdict)
