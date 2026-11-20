from types import SimpleNamespace

import pytest

from mailassistant.llm import fallback as fb
from mailassistant.llm.client import LLMModelMissing, LLMReply, LLMUnavailable, OllamaClient
from mailassistant.llm.fallback import FallbackClient, llm_client


class FakeClient:
    def __init__(self, base_url, model, *, chat_error=None, healthy=True):
        self.base_url, self.model = base_url, model
        self.chat_error, self._healthy = chat_error, healthy
        self.chats = self.health_checks = 0

    def chat(self, messages, schema=None, *, temperature=0.0):
        self.chats += 1
        if self.chat_error:
            raise self.chat_error
        return LLMReply("{}", 1, self.model)

    def healthy(self):
        self.health_checks += 1
        return self._healthy


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _reset():
    fb.reset_state()
    yield
    fb.reset_state()


def make(primary_kw=None, fallback_kw=None):
    clock = Clock()
    p = FakeClient("https://desktop:11435", "desk-model", **(primary_kw or {}))
    f = FakeClient("http://mailassistant-ollama:11434", "small-model", **(fallback_kw or {}))
    return FallbackClient(p, f, clock=clock), p, f, clock


def test_desktop_answers_when_up():
    c, p, f, _ = make()
    reply = c.chat([{"role": "user", "content": "x"}])
    assert reply.model == "desk-model" and c.model == "desk-model" and c.served_by == "desktop"
    assert p.chats == 1 and f.chats == 0


def test_recent_success_skips_health_check():
    c, p, _, clock = make()
    c.chat([])
    clock.t += 30
    c.chat([])
    assert p.health_checks == 1 and p.chats == 2


@pytest.mark.parametrize("err", [LLMUnavailable("down"), LLMModelMissing("no model")])
def test_desktop_error_falls_back_and_is_skipped_for_a_while(err):
    c, p, f, clock = make(primary_kw={"chat_error": err})
    reply = c.chat([])
    assert reply.model == "small-model" and c.served_by == "server" and c.model == "small-model"
    clock.t += fb.DOWN_SECONDS - 1
    c.chat([])
    assert p.chats == 1 and f.chats == 2          # desktop not tried again while marked down


def test_failed_health_check_skips_the_slow_chat():
    c, p, f, _ = make(primary_kw={"healthy": False})
    c.chat([])
    assert p.chats == 0 and f.chats == 1 and c.served_by == "server"


def test_desktop_used_again_after_recovery():
    c, p, f, clock = make(primary_kw={"healthy": False})
    c.chat([])
    p._healthy = True
    clock.t += fb.DOWN_SECONDS + 1
    c.chat([])
    assert c.served_by == "desktop" and p.chats == 1 and f.chats == 1


def test_both_down_propagates():
    c, *_ = make(primary_kw={"healthy": False}, fallback_kw={"chat_error": LLMUnavailable("server down")})
    with pytest.raises(LLMUnavailable):
        c.chat([])


def test_state_is_shared_between_clients():
    c1, p1, _, clock = make(primary_kw={"chat_error": LLMUnavailable("down")})
    c1.chat([])
    p2 = FakeClient(p1.base_url, "other-desk-model")
    f2 = FakeClient("http://mailassistant-ollama:11434", "small-model")
    FallbackClient(p2, f2, clock=clock).chat([])
    assert p2.health_checks == 0 and p2.chats == 0 and f2.chats == 1


def settings(**kw):
    base = dict(ollama_base_url="https://desktop:11435", ollama_model="classify-m", followup_ollama_model="fu-m",
                capture_voice_followup_model=None, ollama_timeout_seconds=180.0, ollama_num_ctx=8192,
                ollama_token=None, ollama_cert_sha256=None, ollama_fallback_url="", ollama_fallback_model="",
                ollama_fallback_timeout_seconds=600.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_fallback_configured_gives_plain_client():
    c = llm_client(settings(), "followup")
    assert isinstance(c, OllamaClient) and c.model == "fu-m"


def test_task_models_on_both_sides():
    s = settings(ollama_fallback_url="http://mailassistant-ollama:11434", ollama_fallback_model="small",
                 capture_voice_followup_model="voice-m")
    got = {t: llm_client(s, t) for t in fb.TASKS}
    assert {t: c.primary.model for t, c in got.items()} == {
        "classify": "classify-m", "event": "classify-m", "followup": "fu-m", "voice": "voice-m"}
    assert {c.fallback.model for c in got.values()} == {"small"}
    assert all(c.fallback.timeout == 600.0 and c.fallback.token is None for c in got.values())


def test_unknown_task_rejected():
    with pytest.raises(ValueError):
        llm_client(settings(), "summarise")
