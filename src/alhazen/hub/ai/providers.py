"""Provider clients: one small HTTP client per AI provider, one interface.

Hides: each provider's wire format (OpenAI and OpenRouter chat completions,
Anthropic messages, Google generateContent), authentication headers,
timeouts, the single retry, response size caps and the mapping of provider
failures onto `ProviderError` kinds.

The interface (`ProviderClient`) is the seam the authoring kit
(``alhazen.hub.ai.author``) calls; it never sees a key or a URL. A client is
built per job from the user's decrypted key and dropped afterwards. Keys are
sent only in request headers (never in a URL, so they cannot reach an
access log), and no error message ever contains a key, a header or a
response body: provider bodies are untrusted text and may echo a request.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# Built-in defaults; an operator may override base_url, models and
# default_model per provider in [ai.providers.<id>].
_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ("gpt-4.1", "gpt-4.1-mini"),
    },
    "anthropic": {
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
    },
    "google": {
        "name": "Google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": ("gemini-2.5-pro", "gemini-2.5-flash"),
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ("openai/gpt-4.1", "anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"),
    },
}

ANTHROPIC_VERSION = "2023-06-01"
# The longest a Retry-After is honoured before the one retry.
MAX_RETRY_WAIT_SECONDS = 20.0


@dataclass(frozen=True)
class Completion:
    """One model answer: its text, normalized token usage, the model that answered."""

    text: str
    usage: dict[str, int]
    model: str


class ProviderError(Exception):
    """A provider call failed. ``kind``: quota | auth | timeout | invalid | other.

    ``message`` is written for a person and safe to store and show: it never
    holds the key, request headers or the provider's response body.
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


class ProviderClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Completion: ...


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    base_url: str
    models: tuple[str, ...]
    default_model: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "models": list(self.models),
            "default_model": self.default_model,
        }


def provider_infos(overrides: Mapping[str, Any]) -> dict[str, ProviderInfo]:
    """Every provider with the operator's overrides applied (settings.AIProviderSettings)."""
    out: dict[str, ProviderInfo] = {}
    for pid, base in _DEFAULTS.items():
        o = overrides.get(pid)
        models = tuple(o.models) if o is not None and o.models else tuple(base["models"])
        default = o.default_model if o is not None and o.default_model else models[0]
        out[pid] = ProviderInfo(
            id=pid,
            name=str(base["name"]),
            base_url=(o.base_url if o is not None and o.base_url else str(base["base_url"])),
            models=models,
            default_model=default,
        )
    return out


@dataclass
class HttpBudget:
    connect_seconds: float = 10.0
    read_seconds: float = 120.0
    max_response_bytes: int = 8 * 1024 * 1024
    sleep: Callable[[float], None] = field(default=time.sleep)


class _HttpClient:
    """Shared HTTP behaviour; subclasses build requests and parse answers."""

    def __init__(
        self,
        info: ProviderInfo,
        key: str,
        model: str,
        budget: HttpBudget,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.info = info
        self.model = model
        self._key = key
        self._budget = budget
        self._transport = transport

    def __repr__(self) -> str:  # never the key
        return f"<{type(self).__name__} {self.info.id} {self.model}>"

    # -- subclass hooks ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request(
        self,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def _parse(self, body: dict[str, Any]) -> Completion:
        raise NotImplementedError

    def _verify_path(self) -> str:
        raise NotImplementedError

    # -- public --------------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Completion:
        _check_messages(messages)
        path, payload = self._request(messages, json_schema, max_tokens, temperature)
        body = self._send("POST", path, payload)
        try:
            return self._parse(body)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(
                "other", f"{self.info.name} returned an answer this hub cannot read"
            ) from None

    def verify(self) -> None:
        """One cheap authenticated call (list models); ProviderError if refused."""
        self._send("GET", self._verify_path(), None)

    # -- transport -----------------------------------------------------------

    def _send(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = self.info.base_url.rstrip("/") + path
        timeout = httpx.Timeout(self._budget.read_seconds, connect=self._budget.connect_seconds)
        attempt = 0
        while True:
            attempt += 1
            try:
                with (
                    httpx.Client(
                        timeout=timeout, transport=self._transport, follow_redirects=False
                    ) as client,
                    client.stream(method, url, headers=self._headers(), json=payload) as response,
                ):
                    status = response.status_code
                    retry_after = response.headers.get("retry-after")
                    raw = self._read_capped(response)
            except httpx.TimeoutException:
                raise ProviderError("timeout", f"{self.info.name} did not answer in time") from None
            except httpx.HTTPError as exc:
                raise ProviderError(
                    "other", f"{self.info.name} could not be reached ({type(exc).__name__})"
                ) from None
            if status < 300:
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ProviderError(
                        "other", f"{self.info.name} returned a response that is not JSON"
                    ) from None
                if not isinstance(value, dict):
                    raise ProviderError("other", f"{self.info.name} returned an unexpected answer")
                return value
            kind = _classify(status, raw)
            retryable = status == 429 or status >= 500
            if retryable and kind != "quota" and attempt == 1:
                self._budget.sleep(_retry_wait(retry_after))
                continue
            raise ProviderError(kind, _message(self.info.name, kind, status), status=status)

    def _read_capped(self, response: httpx.Response) -> bytes:
        limit = self._budget.max_response_bytes
        buffer = bytearray()
        for part in response.iter_bytes():
            buffer += part
            if len(buffer) > limit:
                raise ProviderError(
                    "other", f"{self.info.name} sent more than {limit} bytes; answer discarded"
                )
        return bytes(buffer)


def _check_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise ValueError("at least one message is required")
    for message in messages:
        if message.get("role") not in ("system", "user", "assistant"):
            raise ValueError("message role must be system, user or assistant")
        if not isinstance(message.get("content"), str):
            raise ValueError("message content must be text")


def _retry_wait(header: str | None) -> float:
    if header is not None:
        try:
            return max(0.0, min(float(header.strip()), MAX_RETRY_WAIT_SECONDS))
        except ValueError:
            pass
    return 2.0


_QUOTA_WORDS = ("insufficient_quota", "credit", "billing", "quota", "payment")


def _classify(status: int, raw: bytes) -> str:
    """Kind of a failed answer. The body is read only for a few known words
    and never kept."""
    text = raw[:4096].decode("utf-8", "replace").lower()
    if status == 402:
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 429 and any(word in text for word in _QUOTA_WORDS):
        return "quota"
    if status == 408 or status == 504:
        return "timeout"
    if status in (400, 404, 413, 422):
        return "invalid"
    return "other"


def _message(name: str, kind: str, status: int) -> str:
    return {
        "quota": f"{name} reports the account is out of credit or quota (HTTP {status})",
        "auth": f"{name} refused the API key (HTTP {status}); replace it",
        "timeout": f"{name} timed out (HTTP {status})",
        "invalid": f"{name} rejected the request (HTTP {status}); check the model name",
    }.get(kind, f"{name} failed (HTTP {status})")


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Answer with one JSON object only, no prose and no code fences, matching this JSON "
        "Schema:\n" + json.dumps(schema, sort_keys=True)
    )


def _usage(prompt: Any, output: Any) -> dict[str, int]:
    return {
        "input_tokens": int(prompt or 0),
        "output_tokens": int(output or 0),
    }


class OpenAIClient(_HttpClient):
    """OpenAI chat completions (also any OpenAI-compatible endpoint)."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _request(
        self,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": json_schema, "strict": False},
            }
        return "/chat/completions", payload

    def _parse(self, body: dict[str, Any]) -> Completion:
        text = body["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("no text")
        usage = body.get("usage") or {}
        return Completion(
            text=text,
            usage=_usage(usage.get("prompt_tokens"), usage.get("completion_tokens")),
            model=str(body.get("model") or self.model),
        )

    def _verify_path(self) -> str:
        return "/models"


class OpenRouterClient(OpenAIClient):
    """OpenRouter: OpenAI-compatible; max_tokens instead of max_completion_tokens."""

    def _request(
        self,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, Any]]:
        path, payload = super()._request(messages, json_schema, max_tokens, temperature)
        payload["max_tokens"] = payload.pop("max_completion_tokens")
        return path, payload

    def _verify_path(self) -> str:
        return "/key"


class AnthropicClient(_HttpClient):
    """Anthropic messages API. No native JSON-schema mode: the schema is
    stated in the system prompt and the authoring kit validates the answer."""

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _request(
        self,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, Any]]:
        system = [m["content"] for m in messages if m["role"] == "system"]
        if json_schema is not None:
            system.append(_schema_instruction(json_schema))
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m["role"] != "system"
            ],
        }
        if system:
            payload["system"] = "\n\n".join(system)
        return "/v1/messages", payload

    def _parse(self, body: dict[str, Any]) -> Completion:
        parts = [b["text"] for b in body["content"] if b.get("type") == "text"]
        if not parts:
            raise ValueError("no text")
        usage = body.get("usage") or {}
        return Completion(
            text="".join(parts),
            usage=_usage(usage.get("input_tokens"), usage.get("output_tokens")),
            model=str(body.get("model") or self.model),
        )

    def _verify_path(self) -> str:
        return "/v1/models"


class GoogleClient(_HttpClient):
    """Google Gemini generateContent; the key goes in x-goog-api-key, never the URL."""

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    def _request(
        self,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, dict[str, Any]]:
        system = [m["content"] for m in messages if m["role"] == "system"]
        if json_schema is not None:
            system.append(_schema_instruction(json_schema))
        config: dict[str, Any] = {"maxOutputTokens": max_tokens, "temperature": temperature}
        if json_schema is not None:
            config["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in messages
                if m["role"] != "system"
            ],
            "generationConfig": config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        return f"/models/{self.model}:generateContent", payload

    def _parse(self, body: dict[str, Any]) -> Completion:
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise ValueError("no text")
        usage = body.get("usageMetadata") or {}
        return Completion(
            text=text,
            usage=_usage(usage.get("promptTokenCount"), usage.get("candidatesTokenCount")),
            model=str(body.get("modelVersion") or self.model),
        )

    def _verify_path(self) -> str:
        return "/models?pageSize=1"


_CLIENTS: dict[str, type[_HttpClient]] = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "google": GoogleClient,
    "openrouter": OpenRouterClient,
}


def make_client(
    info: ProviderInfo,
    key: str,
    model: str,
    budget: HttpBudget,
    transport: httpx.BaseTransport | None = None,
) -> _HttpClient:
    """A client for one provider, key and model. ``transport`` is for tests."""
    return _CLIENTS[info.id](info, key, model, budget, transport)
