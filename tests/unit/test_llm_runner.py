import pytest
from pydantic import ValidationError

from mailassistant.llm.client import LLMReply
from mailassistant.llm.runner import StructuredOutputError, run_structured
from mailassistant.llm.schemas import ClassificationOutput

GOOD = ('{"reason": "digest with unsubscribe link", "category": "newsletter", "importance": "low", '
        '"flags": [], "confidence": 0.9}')


class Scripted:
    model = "fake"

    def __init__(self, *contents):
        self.contents, self.calls = list(contents), []

    def chat(self, messages, schema=None, **kw):
        self.calls.append((list(messages), schema))
        return LLMReply(self.contents.pop(0), 5, "fake")


def test_valid_output_is_parsed_and_recorded():
    seen = []
    c = Scripted("```json\n" + GOOD + "\n```")
    out = run_structured(c, [{"role": "user", "content": "x"}], ClassificationOutput, task="t",
                         record=lambda *a: seen.append(a))
    assert out.category == "newsletter" and out.confidence == 0.9
    assert c.calls[0][1]["properties"]["category"]["enum"][0] == "personal"   # schema was sent
    assert [s[3] for s in seen] == [True]


def test_one_repair_retry_then_success():
    seen = []
    c = Scripted('{"category": "nonsense"}', GOOD)
    out = run_structured(c, [{"role": "user", "content": "x"}], ClassificationOutput, task="t",
                         record=lambda *a: seen.append(a))
    assert out.category == "newsletter" and [s[3] for s in seen] == [False, True]
    assert "invalid" in c.calls[1][0][-1]["content"] and len(c.calls[1][0]) == 3


def test_gives_up_after_the_retry():
    with pytest.raises(StructuredOutputError):
        run_structured(Scripted("nope", "still nope"), [], ClassificationOutput, task="t")


@pytest.mark.parametrize("bad", [
    '{"reason": "r", "category": "personal", "importance": "low", "flags": [], "confidence": 1.5}',
    '{"reason": "r", "category": "other", "importance": "low", "flags": [], "confidence": 0.5}',
    '{"reason": "r", "category": "personal", "importance": "urgent", "flags": [], "confidence": 0.5}',
    '{"reason": "r", "category": "personal", "importance": "low", "flags": ["delete_all"], "confidence": 0.5}',
    '{"reason": "r", "category": "personal", "importance": "low", "flags": [], "confidence": 0.5, "action": "send_email"}',
])
def test_schema_rejects_bad_or_extra_fields(bad):
    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate_json(bad)
