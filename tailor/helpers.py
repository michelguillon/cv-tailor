"""tailor/helpers.py — provider clients + call_with_retry() (R-05).

The ONE module that touches a provider SDK directly. Everything else — the
orchestrator, phases, tools, corpus ingestion — calls through here, so a
transient 429/5xx never aborts a run and provider details stay in one place
(the Week 1/2 `call_with_retry` pattern, generalised across providers).

Step 1 wires only Mistral (embeddings, Week 1 reuse). The Anthropic and OpenAI
clients are added in later steps behind the same `call_with_retry`.

mistralai 2.4.9 note (F-07): the SDK's public surface is under
`mistralai.client` (not top-level `mistralai`), the base error is `MistralError`
(with `SDKError` etc. beneath it), and HTTP status/headers live on
`exc.raw_response`. The embeddings call is `client.embeddings.create(model=...,
inputs=[...]) -> resp.data[i].embedding`.
"""

from __future__ import annotations

import logging
import os
import time

import anthropic
import openai
from mistralai.client import Mistral
from mistralai.client.errors import MistralError

__all__ = [
    "get_mistral_client",
    "get_anthropic_client",
    "get_openai_client",
    "call_with_retry",
    "embed_texts",
    "embed_query",
    "claude_complete",
    "gpt_complete",
    "cached",
    "RETRYABLE_STATUS",
]

log = logging.getLogger("tailor.helpers")

# 429 = rate limit; 5xx = transient server errors. 4xx (our bug) must NOT retry.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

EMBED_BATCH = 64  # Mistral embeds many inputs per call; batch to cut round-trips.


# --------------------------------------------------------------------------- #
# Clients                                                                     #
# --------------------------------------------------------------------------- #

def get_mistral_client(api_key: str | None = None) -> Mistral:
    """Build the Mistral client. Fail loud and early on a missing key."""
    key = api_key or os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise EnvironmentError(
            "MISTRAL_API_KEY is not set. Add it to .env and run via "
            "`docker compose run --rm cli ...` (Compose loads .env automatically)."
        )
    return Mistral(api_key=key)


def get_anthropic_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Build the Anthropic client. Fail loud and early on a missing key."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and run via "
            "`docker compose run --rm cli ...`."
        )
    return anthropic.Anthropic(api_key=key)


def get_openai_client(api_key: str | None = None) -> openai.OpenAI:
    """Build the OpenAI client. Fail loud and early on a missing key."""
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise EnvironmentError(
            "OPENAI_API_KEY is not set. Add it to .env and run via "
            "`docker compose run --rm cli ...`."
        )
    return openai.OpenAI(api_key=key)


# --------------------------------------------------------------------------- #
# Retry wrapper (provider-agnostic)                                           #
# --------------------------------------------------------------------------- #

def _status_of(exc: Exception) -> int | None:
    """HTTP status from a provider exception, across SDK shapes, or None."""
    raw = getattr(exc, "raw_response", None)          # mistralai 2.x SDKError
    if raw is not None and getattr(raw, "status_code", None) is not None:
        return raw.status_code
    return getattr(exc, "status_code", None)          # openai / anthropic style


def _retry_after_seconds(exc: Exception) -> float | None:
    """Honour a Retry-After header if the server sent one — it knows its load."""
    raw = getattr(exc, "raw_response", None)
    headers = getattr(raw, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after")
    return float(value) if value and str(value).isdigit() else None


def _should_retry(exc: Exception, status: int | None) -> bool:
    if status is not None:
        return status in RETRYABLE_STATUS
    # No status → a transport-level failure (no response). Retry those; do NOT
    # retry response-validation errors (those are a schema bug, not transient).
    name = type(exc).__name__.lower()
    return any(k in name for k in ("noresponse", "connection", "timeout"))


def call_with_retry(
    func,
    *args,
    max_retries: int = 5,
    base_delay: float = 1.0,
    retryable_exc: type[Exception] | tuple[type[Exception], ...] = MistralError,
    **kwargs,
):
    """Call a provider SDK method, retrying transient failures with backoff.

    Returns whatever ``func`` returns. Re-raises immediately on a non-retryable
    error (a bug on our side) or once ``max_retries`` is exhausted. Backoff is
    exponential unless the server sends Retry-After. ``retryable_exc`` scopes
    which exception family is even considered for retry (default: Mistral's).
    """
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except retryable_exc as exc:
            attempt += 1
            status = _status_of(exc)
            if not _should_retry(exc, status) or attempt > max_retries:
                raise
            delay = _retry_after_seconds(exc) or base_delay * (2 ** (attempt - 1))
            log.warning(
                "%s (status=%s) on attempt %d/%d — retrying in %.1fs",
                type(exc).__name__, status, attempt, max_retries, delay,
            )
            time.sleep(delay)


# --------------------------------------------------------------------------- #
# Embeddings (Mistral — provider hidden from callers, D-02)                   #
# --------------------------------------------------------------------------- #

def embed_texts(
    texts: list[str],
    *,
    model: str,
    client: Mistral | None = None,
) -> tuple[list[list[float]], int]:
    """Embed texts, batched, every call retry-wrapped. Returns (vectors, tokens).

    Callers (corpus ingestion, Phase 1 retrieval) pass the model from config and
    never see that Mistral is the provider.
    """
    if not texts:
        return [], 0
    client = client or get_mistral_client()
    vectors: list[list[float]] = []
    total_tokens = 0
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        resp = call_with_retry(client.embeddings.create, model=model, inputs=batch)
        vectors.extend(item.embedding for item in resp.data)
        usage = getattr(resp, "usage", None)
        total_tokens += getattr(usage, "total_tokens", 0) or 0
    return vectors, total_tokens


def embed_query(text: str, *, model: str, client: Mistral | None = None) -> list[float]:
    """Embed a single query string (Phase 1 retrieval)."""
    vectors, _ = embed_texts([text], model=model, client=client)
    return vectors[0]


# --------------------------------------------------------------------------- #
# Claude (Anthropic — orchestrator/reasoner; Haiku in dev/demo, Sonnet in full) #
# --------------------------------------------------------------------------- #

def cached(text: str) -> dict:
    """An Anthropic system/content text block marked for ephemeral prompt caching (D-31).

    Pass a list of these as `system=` to set cache breakpoints on a stable prefix
    (e.g. `[cached(SYSTEM), cached(jd_rubric_block)]` → two breakpoints). Anthropic
    caches the cumulative prefix up to each marked block; blocks below the model's
    minimum (1024 tokens for Sonnet, 2048 for Haiku) are simply not cached — no
    error, just `cache_creation_input_tokens == 0` (F-22). OpenAI needs no equivalent:
    it caches qualifying prefixes automatically.
    """
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def claude_complete(
    *,
    model: str,
    messages: list[dict],
    system: str | list[dict] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    client: anthropic.Anthropic | None = None,
):
    """Call the Claude Messages API through call_with_retry. Returns the raw Message.

    The provider is hidden from callers (D-02). `tools`+`tool_choice` let a caller
    force structured output (a tool call), which is more reliable than free-form
    JSON — especially from Haiku in dev (D-26).
    """
    client = client or get_anthropic_client()
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    return call_with_retry(client.messages.create, retryable_exc=anthropic.APIError, **kwargs)


# --------------------------------------------------------------------------- #
# GPT (OpenAI — section critique tool; GPT-4o-mini, D-03)                     #
# --------------------------------------------------------------------------- #

def gpt_complete(
    *,
    model: str,
    messages: list[dict],
    response_format: dict | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    client: openai.OpenAI | None = None,
):
    """Call the OpenAI Chat Completions API through call_with_retry.

    Returns the raw response; callers read `resp.choices[0].message.content`.
    `response_format` enables JSON / strict structured outputs.
    """
    client = client or get_openai_client()
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return call_with_retry(client.chat.completions.create, retryable_exc=openai.APIError, **kwargs)
