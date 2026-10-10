"""Provider clients against httpx.MockTransport: wire formats, the single
retry, failure kinds, caps, and that no key or response body ever reaches
an error message or a URL. No network."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alhazen.hub.ai.providers import (
    HttpBudget,
    ProviderError,
    make_client,
    provider_infos,
)
from alhazen.hub.settings import AIProviderSettings

KEY = "sk-secret-key-value-123456"
MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "hello"},
]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def client(provider: str, recorder: Recorder, **budget: Any) -> Any:
    info = provider_infos({})[provider]
    sleeps: list[float] = []
    b = HttpBudget(sleep=sleeps.append, **budget)
    c = make_client(info, KEY, "m-1", b, transport=httpx.MockTransport(recorder))
    c.sleeps = sleeps
    return c


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


OPENAI_OK = {
    "model": "m-1",
    "choices": [{"message": {"content": '{"a": 1}'}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


def test_openai_request_and_answer() -> None:
    rec = Recorder(ok(OPENAI_OK))
    answer = client("openai", rec).complete(MESSAGES, json_schema={"type": "object"}, max_tokens=50)
    assert answer.text == '{"a": 1}' and answer.usage == {"input_tokens": 7, "output_tokens": 3}
    request = rec.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = rec.body()
    assert body["model"] == "m-1" and body["max_completion_tokens"] == 50
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"] == MESSAGES


def test_openrouter_uses_max_tokens_and_its_base_url() -> None:
    rec = Recorder(ok(OPENAI_OK))
    client("openrouter", rec).complete(MESSAGES, json_schema=None, max_tokens=9)
    assert str(rec.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert rec.body()["max_tokens"] == 9 and "response_format" not in rec.body()


def test_anthropic_moves_system_and_states_the_schema() -> None:
    rec = Recorder(
        ok(
            {
                "model": "m-1",
                "content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        )
    )
    answer = client("anthropic", rec).complete(
        MESSAGES, json_schema={"type": "object"}, max_tokens=8
    )
    assert answer.usage == {"input_tokens": 4, "output_tokens": 2}
    request = rec.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY and "anthropic-version" in request.headers
    body = rec.body()
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["system"].startswith("be brief") and "JSON Schema" in body["system"]


def test_google_key_in_header_never_in_url() -> None:
    rec = Recorder(
        ok(
            {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
            }
        )
    )
    answer = client("google", rec).complete(MESSAGES, json_schema={"type": "object"}, max_tokens=8)
    assert answer.usage == {"input_tokens": 5, "output_tokens": 1}
    request = rec.requests[0]
    assert KEY not in str(request.url) and request.headers["x-goog-api-key"] == KEY
    assert str(request.url).endswith("/models/m-1:generateContent")
    body = rec.body()
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_one_retry_on_429_honouring_retry_after() -> None:
    rec = Recorder(httpx.Response(429, headers={"Retry-After": "3"}), ok(OPENAI_OK))
    c = client("openai", rec)
    assert c.complete(MESSAGES, json_schema=None, max_tokens=5).text == '{"a": 1}'
    assert c.sleeps == [3.0] and len(rec.requests) == 2


def test_retry_after_is_capped_and_only_once() -> None:
    rec = Recorder(
        httpx.Response(503, headers={"Retry-After": "999"}),
        httpx.Response(503, text=f"echo {KEY}"),
    )
    c = client("openai", rec)
    with pytest.raises(ProviderError) as caught:
        c.complete(MESSAGES, json_schema=None, max_tokens=5)
    assert caught.value.kind == "other" and c.sleeps == [20.0]
    assert KEY not in caught.value.message and "echo" not in caught.value.message


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, "bad key", "auth"),
        (403, "nope", "auth"),
        (402, "pay", "quota"),
        (429, '{"error": {"code": "insufficient_quota"}}', "quota"),
        (400, "bad model", "invalid"),
        (404, "no model", "invalid"),
        (504, "slow", "timeout"),
    ],
)
def test_failure_kinds(status: int, body: str, kind: str) -> None:
    responses = [httpx.Response(status, text=body)] * 2
    rec = Recorder(*responses)
    with pytest.raises(ProviderError) as caught:
        client("openai", rec).complete(MESSAGES, json_schema=None, max_tokens=5)
    assert caught.value.kind == kind and caught.value.status == status
    assert body not in caught.value.message
    if kind == "quota":
        assert len(rec.requests) == 1  # quota is not retried


def test_transport_timeout_and_connection_errors() -> None:
    rec = Recorder(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as caught:
        client("openai", rec).complete(MESSAGES, json_schema=None, max_tokens=5)
    assert caught.value.kind == "timeout"
    rec = Recorder(httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as caught:
        client("openai", rec).complete(MESSAGES, json_schema=None, max_tokens=5)
    assert caught.value.kind == "other"


def test_response_cap_and_unreadable_answers() -> None:
    rec = Recorder(httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(ProviderError, match="more than 1000 bytes"):
        client("openai", rec, max_response_bytes=1000).complete(
            MESSAGES, json_schema=None, max_tokens=5
        )
    rec = Recorder(httpx.Response(200, content=b"not json"))
    with pytest.raises(ProviderError, match="not JSON"):
        client("openai", rec).complete(MESSAGES, json_schema=None, max_tokens=5)
    rec = Recorder(ok({"choices": []}))
    with pytest.raises(ProviderError, match="cannot read"):
        client("openai", rec).complete(MESSAGES, json_schema=None, max_tokens=5)


def test_verify_and_repr_never_show_the_key() -> None:
    rec = Recorder(ok({"data": []}))
    c = client("openai", rec)
    c.verify()
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/models"
    assert KEY not in repr(c)


def test_operator_overrides() -> None:
    infos = provider_infos(
        {
            "openai": AIProviderSettings(
                base_url="http://127.0.0.1:9/v1", models=("a", "b"), default_model="b"
            )
        }
    )
    assert infos["openai"].base_url == "http://127.0.0.1:9/v1"
    assert infos["openai"].public() == {
        "id": "openai",
        "name": "OpenAI",
        "models": ["a", "b"],
        "default_model": "b",
    }
    assert infos["google"].default_model == infos["google"].models[0]


def test_bad_messages_are_refused_before_any_request() -> None:
    rec = Recorder()
    with pytest.raises(ValueError):
        client("openai", rec).complete(
            [{"role": "tool", "content": "x"}], json_schema=None, max_tokens=1
        )
    assert rec.requests == []
