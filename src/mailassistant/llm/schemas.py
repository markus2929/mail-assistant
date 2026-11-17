"""Pydantic schemas for model output. Used both as the JSON schema sent to Ollama (constrained
decoding) and to validate everything that comes back before any code acts on it."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["personal", "work", "newsletter", "automated_notification", "marketing",
                   "invitation", "spam_suspicious", "manual_review"]
Flag = Literal["account_or_security", "financial", "legal_or_official", "time_sensitive",
               "requests_reply", "looks_like_phishing", "contains_instructions_to_assistant"]


class ClassificationOutput(BaseModel):
    """Field order matters: the reason is generated before the decision."""
    model_config = ConfigDict(extra="forbid")

    reason: str
    category: Category
    importance: Literal["low", "normal", "high"]
    flags: list[Flag] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)


class EventExtraction(BaseModel):
    """What the model may copy out of an email. Dates/times stay TEXT; Python resolves them."""
    model_config = ConfigDict(extra="forbid")

    reason: str
    kind: Literal["invitation", "proposed_date", "mention", "none"]
    title: str
    date_text: str
    start_time_text: str
    end_time_text: str
    duration_minutes: int | None = Field(ge=1, le=1440)
    timezone_text: str
    location: str
    meeting_url: str
    organiser: str
    evidence_quote: str
    all_day: bool
    confidence: float = Field(ge=0.0, le=1.0)


class FollowupItem(BaseModel):
    """One commitment. Who owes it is relative to the user; Python maps it to I_OWE / THEY_OWE."""
    model_config = ConfigDict(extra="forbid")

    who_owes: Literal["user", "other"]
    action: str = Field(max_length=200)     # short description, shown to the user
    evidence_quote: str = Field(max_length=600)
    deadline_text: str = Field(max_length=100)  # copied verbatim, "" if none
    confidence: float = Field(ge=0.0, le=1.0)


class FollowupExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    items: list[FollowupItem] = Field(default_factory=list, max_length=5)


class ClosureCheck(BaseModel):
    """Does a newer email fulfil an open commitment?"""
    model_config = ConfigDict(extra="forbid")

    reason: str
    fulfils: Literal["yes", "partly", "no"]
    evidence_quote: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
