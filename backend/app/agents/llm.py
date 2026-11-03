"""Language model providers, as an ordered chain.

Gemini first, Bedrock second, and a deterministic fallback whenever neither can
answer — quota exhausted, network gone, key missing.

The chain matters because the most likely failure by far is not an outage, it is
a free-tier quota running out halfway through a demo. When that happens Gemini
returns 429 for hours, so a single-provider design degrades to deterministic
rules for the rest of the session even though a perfectly good second provider
is configured and idle. Each provider therefore carries its own circuit breaker,
and a provider that is cooling down is skipped rather than waited on.

Circuits reopen on a timer rather than staying latched, so a quota that resets at
midnight is picked up on its own without anybody calling the health endpoint.

The fallback is not a degraded imitation of the model. It is a set of rules
over the same structured inputs, so the system keeps giving correct answers
without a model at all. That property is what lets a demo run on venue wifi,
and it is also the honest answer to "what happens when the LLM is wrong or
absent?": the numbers come from the solver and the scorer, never from here.

Since 2026-10 `complete` is also the **gateway**: every call, from every caller,
passes the prompt guardrail (PII redaction, size cap), the cost controls in
`app/agents/llm_cost.py` (task profile -> model tier and output cap, response
cache with single-flight, token budget, bulkhead) and the output guardrail.
A caller names its `task`; it cannot skip a layer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Hashable, Literal, Protocol

from app.agents import guardrails, llm_cost
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

Engine = Literal["gemini", "bedrock", "fallback"]

#: A single slow model call must not hold up a dispatch decision.
LLM_TIMEOUT_S = 8.0


@dataclass(slots=True)
class Completion:
    text: str
    engine: Engine
    #: Set when the primary provider failed and we degraded.
    degraded_reason: str | None = None
    #: hit | shared | coalesced | computed | uncached | skipped
    cache: str = "skipped"
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    model: str = ""
    latency_ms: float = 0.0


@dataclass(slots=True)
class ProviderResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0


class Provider(Protocol):
    engine: Engine

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 600,
                       tier: str = "large") -> ProviderResult: ...


class GeminiProvider:
    engine: Engine = "gemini"

    def __init__(self, api_key: str, model: str, small_model: str = "") -> None:
        self._api_key = api_key
        self._model = model
        self._small = small_model or model
        self._client = None

    def _ensure(self):
        if self._client is None:
            from google import genai  # imported lazily so the app boots without it

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 600,
                       tier: str = "large") -> ProviderResult:
        """The system prompt goes in `system_instruction`, not glued onto the
        user text: a stable prefix is what the provider's implicit prompt cache
        can reuse, and the separation is also what tells the model which part
        is ours. Async client, so a burst does not queue on a thread pool."""
        from google.genai import types

        client = self._ensure()
        model = self._small if tier == "small" else self._model
        cfg: dict[str, Any] = {"system_instruction": system, "max_output_tokens": max_tokens,
                               "temperature": 0.2}
        # 2.5 Flash models think by default and bill for it. Short structured
        # tasks do not need it; Pro cannot turn it off, so only Flash is told.
        if "2.5-flash" in model and tier == "small":
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        response = await client.aio.models.generate_content(
            model=model, contents=prompt, config=types.GenerateContentConfig(**cfg))
        usage = getattr(response, "usage_metadata", None)
        return ProviderResult(
            text=(response.text or "").strip(), model=model,
            tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
            tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
            tokens_cached=int(getattr(usage, "cached_content_token_count", 0) or 0),
        )


class BedrockProvider:
    """Bedrock via a Bedrock API key.

    AWS issues two credential shapes for Bedrock. Classic IAM keys go through
    the usual boto3 chain; a Bedrock API key (the `ABSK...` form) is a bearer
    token that botocore reads from `AWS_BEARER_TOKEN_BEDROCK`. We export it
    here so either shape works without the caller caring which they hold.
    """

    engine: Engine = "bedrock"

    def __init__(self, model_id: str, region: str, api_key: str = "", small_model_id: str = "") -> None:
        self._model_id = model_id
        self._small = small_model_id or model_id
        self._region = region
        self._api_key = api_key
        self._client = None

    def _ensure(self):
        if self._client is None:
            import os

            import boto3

            if self._api_key and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self._api_key

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 600,
                       tier: str = "large") -> ProviderResult:
        client = self._ensure()
        model = self._small if tier == "small" else self._model_id
        system_blocks: list[dict] = [{"text": system}]
        if settings.bedrock_prompt_cache:
            # Everything before the cache point is reused across calls at a
            # fraction of the input price, on models that support it.
            system_blocks.append({"cachePoint": {"type": "default"}})

        def _call() -> ProviderResult:
            response = client.converse(
                modelId=model,
                system=system_blocks,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
            )
            blocks = response["output"]["message"]["content"]
            usage = response.get("usage") or {}
            return ProviderResult(
                text="".join(b.get("text", "") for b in blocks).strip(), model=model,
                tokens_in=int(usage.get("inputTokens") or 0),
                tokens_out=int(usage.get("outputTokens") or 0),
                tokens_cached=int(usage.get("cacheReadInputTokens") or 0),
            )

        return await asyncio.to_thread(_call)


#: Errors that mean "this provider is out for a while", as opposed to "that one
#: request went wrong". Matched on the text because the three SDKs involved raise
#: entirely different exception types for the same condition, and importing all
#: of them to catch them properly would defeat the lazy imports that let this
#: module load with neither installed.
_EXHAUSTED = (
    "429", "quota", "resource_exhausted", "resourceexhausted",
    "throttling", "throttled", "too many requests", "rate limit",
    "insufficient_quota", "serviceunavailable", "503",
)


def _is_exhausted(exc: Exception) -> bool:
    return any(m in f"{type(exc).__name__} {exc}".lower() for m in _EXHAUSTED)


def _build_chain() -> list[Provider]:
    """Every provider that is configured, best first.

    `llm_provider` chooses the head of the chain rather than the only member of
    it. Anything else that has credentials follows, because a configured
    provider sitting idle while the system degrades to rules is the worst of
    both worlds — it is the outcome that made this a chain.
    """
    built: dict[Engine, Provider] = {}
    if settings.gemini_api_key:
        built["gemini"] = GeminiProvider(settings.gemini_api_key, settings.gemini_model,
                                         settings.gemini_model_small)
    if settings.bedrock_model_id:
        built["bedrock"] = BedrockProvider(
            settings.bedrock_model_id,
            settings.aws_region,
            settings.aws_api_key_bedrock_for_xai,
            settings.bedrock_model_id_small,
        )

    order: list[Engine] = ["gemini", "bedrock"]
    if settings.llm_provider in order:
        order.remove(settings.llm_provider)
        order.insert(0, settings.llm_provider)
    return [built[e] for e in order if e in built]


_chain: list[Provider] | None = None

#: Consecutive failures per provider, and the monotonic time each one may be
#: tried again. Per provider, not global: Gemini being out of quota says nothing
#: whatever about whether Bedrock can answer.
_failures: dict[Engine, int] = {}
_cooldown_until: dict[Engine, float] = {}

_FAILURE_THRESHOLD = 3
#: A provider that merely erred is rested briefly. One that reported an
#: exhausted quota is rested for long enough that we are not hammering a limit
#: that resets on the hour, but not so long that a demo cannot recover.
_COOLDOWN_S = 60.0
_EXHAUSTED_COOLDOWN_S = 900.0


def _available(provider: Provider) -> bool:
    """Is this provider worth trying right now?

    Half-open by design: once the cooldown passes the provider is tried again on
    the next request, and one success clears its failure count. There is no
    separate probe and nothing to call by hand.
    """
    until = _cooldown_until.get(provider.engine, 0.0)
    return time.monotonic() >= until


def _get_chain() -> list[Provider]:
    global _chain
    if _chain is None:
        _chain = _build_chain()
        log.info(
            "llm_chain",
            preferred=settings.llm_provider,
            chain=[p.engine for p in _chain] or ["fallback"],
        )
    return _chain


def current_engine() -> Engine:
    """Which engine would answer right now."""
    for provider in _get_chain():
        if _available(provider):
            return provider.engine
    return "fallback"


def engine_note() -> str:
    chain = _get_chain()
    if not chain:
        return "No model configured; deterministic rules in use."

    live = [p for p in chain if _available(p)]
    if not live:
        return "Every provider is cooling down; deterministic rules in use."

    name = {
        "gemini": settings.gemini_model,
        "bedrock": settings.bedrock_model_id,
    }.get(live[0].engine, live[0].engine)

    resting = [p.engine for p in chain if not _available(p)]
    if resting:
        return f"{name} responding — {', '.join(resting)} cooling down"
    spare = [p.engine for p in chain[1:]]
    return f"{name} responding" + (f", {', '.join(spare)} standing by" if spare else "")


async def complete(
    system: str,
    prompt: str,
    *,
    fallback: str,
    task: str = "general",
    cache_key: Hashable | None = None,
    max_tokens: int | None = None,
) -> Completion:
    """The gateway. Guard, then cache, then budget, then the provider chain,
    then guard again; hand back `fallback` whenever any of them says no.

    Callers always supply a usable fallback string; there is no code path where
    a missing model produces a missing answer, and none where a model produces
    an operational number — the figures come from the solver and the scorer.
    """
    prof = llm_cost.profile(task)
    guarded = guardrails.guard_prompt(system, prompt, task=task)
    if not _get_chain():
        llm_cost.record(task, how="skipped", fallback_reason="no provider")
        return Completion(fallback, "fallback", "no provider configured")

    ran: dict[str, Completion] = {}  # set by compute() when this call ran the chain

    async def compute() -> tuple[str, bool]:
        result = await _through_chain(guarded.system, guarded.prompt, prof,
                                      max_tokens or prof.max_tokens, task)
        ran["result"] = result
        return result.text, result.engine != "fallback"

    if settings.llm_cache_enabled and prof.cache_ttl_s > 0:
        key = llm_cost.ResponseCache.key(
            ("v1", task, prof.tier, cache_key) if cache_key is not None
            else ("v1", task, prof.tier, guarded.system, guarded.prompt))
        text, how = await llm_cost.cache.get_or_compute(key, prof.cache_ttl_s, compute)
    else:
        text, _ = await compute()
        how = "uncached"

    result = ran.get("result")
    if result is None:
        # Served from cache or by another caller's in-flight request: no model
        # call, no spend. A coalesced fallback comes back as an empty string.
        llm_cost.record(task, how=how)
        if not text:
            return Completion(fallback, "fallback", "coalesced onto a call that degraded", cache=how)
        return Completion(text, _get_chain()[0].engine, None, cache=how)
    llm_cost.record(task, how=how, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                    tokens_cached=result.tokens_cached, model=result.model,
                    latency_ms=result.latency_ms,
                    fallback_reason=result.degraded_reason if result.engine == "fallback" else None)
    if result.engine == "fallback":
        return Completion(fallback, "fallback", result.degraded_reason, cache=how)
    result.cache = how
    return result


async def _through_chain(system: str, prompt: str, prof: llm_cost.TaskProfile,
                         max_tokens: int, task: str) -> Completion:
    """Budget, bulkhead, then each provider in turn. Returns a Completion whose
    engine is "fallback" (and text empty) when nothing answered."""
    ok, why = await llm_cost.budget.admit(
        prof.priority, llm_cost.estimate_tokens(system, prompt) + max_tokens)
    if not ok:
        guardrails.trip("budget.tokens", f"{task}: {why}", blocking=False, persist=False)
        return Completion("", "fallback", f"budget: {why}")
    if llm_cost.bulkhead.full_for(prof.priority):
        llm_cost.bulkhead.shed += 1
        guardrails.trip("budget.concurrency", f"{task}: {llm_cost.bulkhead.in_flight} calls in flight",
                        blocking=False, persist=False)
        return Completion("", "fallback", "bulkhead: model capacity reserved for life-safety work")

    chain = _get_chain()
    tried: list[str] = []
    first_reason: str | None = None

    async with llm_cost.bulkhead:
        for provider in chain:
            engine = provider.engine
            if not _available(provider):
                tried.append(f"{engine}:cooling")
                continue

            started = time.perf_counter()
            try:
                got = await asyncio.wait_for(
                    provider.complete(system, prompt, max_tokens=max_tokens, tier=prof.tier),
                    timeout=LLM_TIMEOUT_S,
                )
                if isinstance(got, str):          # a provider (or test double) of the old shape
                    got = ProviderResult(text=got, model=engine)
                if not got.text:
                    raise ValueError("empty completion")
            except Exception as exc:  # noqa: BLE001 - degrade, never fail
                exhausted = _is_exhausted(exc)
                n = _failures.get(engine, 0) + 1
                _failures[engine] = n
                reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                first_reason = first_reason or f"{engine} {reason}"
                tried.append(f"{engine}:{'exhausted' if exhausted else 'error'}")

                # An exhausted quota is not a flaky request and there is no point
                # spending two more attempts proving it. Rest this provider at once
                # and move down the chain.
                if exhausted or n >= _FAILURE_THRESHOLD:
                    rest = _EXHAUSTED_COOLDOWN_S if exhausted else _COOLDOWN_S
                    _cooldown_until[engine] = time.monotonic() + rest
                    log.warning("llm_provider_resting", engine=engine,
                                seconds=rest, failures=n, error=reason)
                else:
                    log.warning("llm_unavailable", engine=engine,
                                failures=n, error=reason)
                continue

            # Success. Clear this provider's history, and say plainly when the
            # answer came from somewhere other than the preferred provider — a
            # silent failover is how you discover in March that the primary has
            # been dead since January.
            _failures[engine] = 0
            _cooldown_until.pop(engine, None)
            tokens_in = got.tokens_in or llm_cost.estimate_tokens(system, prompt)
            tokens_out = got.tokens_out or llm_cost.estimate_tokens(got.text)
            await llm_cost.budget.record(tokens_in + tokens_out)
            checked = guardrails.guard_output(got.text, task=task)
            if not checked.ok:
                return Completion("", "fallback", f"guardrail: {checked.reason}")
            degraded = None
            if engine != chain[0].engine or tried:
                degraded = f"failed over to {engine} after {', '.join(tried)}"
                log.info("llm_failover", engine=engine, after=tried)
            return Completion(checked.text, engine, degraded, tokens_in=tokens_in,
                              tokens_out=tokens_out, tokens_cached=got.tokens_cached,
                              model=got.model, latency_ms=(time.perf_counter() - started) * 1000)

    return Completion(
        "", "fallback",
        first_reason or f"all providers cooling down ({', '.join(tried)})",
    )


def reset_circuit() -> None:
    """Forget every cooldown. Called by the health endpoint, and safe to call
    at any time — the timers would expire on their own regardless."""
    _failures.clear()
    _cooldown_until.clear()


def provider_status() -> list[dict]:
    """What each provider is doing, for the health endpoint and the console.

    Worth exposing rather than keeping internal: "which model answered, and did
    anything fail over" is the first question when an explanation reads oddly.
    """
    now = time.monotonic()
    return [
        {
            "engine": p.engine,
            "preferred": i == 0,
            "available": _available(p),
            "consecutiveFailures": _failures.get(p.engine, 0),
            "restingForSeconds": max(0, round(_cooldown_until.get(p.engine, 0.0) - now)),
        }
        for i, p in enumerate(_get_chain())
    ]
