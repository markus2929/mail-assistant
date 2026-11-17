"""Structured LLM calls: schema-constrained request, strict validation, one repair retry."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from mailassistant.llm.client import LLMReply
from mailassistant.llm.jsonutil import extract_json

T = TypeVar("T", bound=BaseModel)
Recorder = Callable[[str, list, LLMReply, bool, str | None], None]


class StructuredOutputError(Exception):
    """The model never produced valid output (after the retry)."""


def run_structured(client, messages: list[dict], model_cls: type[T], *, task: str,
                   record: Recorder | None = None, retries: int = 1) -> T:
    schema = model_cls.model_json_schema()
    msgs, last_error = list(messages), "unknown"
    for _ in range(retries + 1):
        reply = client.chat(msgs, schema)  # LLMUnavailable / LLMModelMissing propagate to the caller
        try:
            obj = model_cls.model_validate_json(extract_json(reply.content))
        except ValueError as e:  # includes pydantic.ValidationError
            last_error = str(e)[:500]
            if record:
                record(task, msgs, reply, False, last_error)
            msgs = [*messages, {"role": "assistant", "content": reply.content[:2000]},
                    {"role": "user", "content": "That output was invalid (" + last_error[:300]
                     + "). Reply with ONLY a JSON object that matches the schema."}]
            continue
        if record:
            record(task, msgs, reply, True, None)
        return obj
    raise StructuredOutputError(last_error)
