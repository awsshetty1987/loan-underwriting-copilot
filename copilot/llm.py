"""The only place that talks to OpenAI. Every call: personal data masked first, a trace span, tokens and cost
recorded for the case, and (if LangSmith is on) the call shown in LangSmith too."""
import json
import os
import re
import time

from . import pii, telemetry
from .config import settings

_client = None


def _openai():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(timeout=60)
        if os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and os.environ.get("LANGSMITH_API_KEY"):
            from langsmith.wrappers import wrap_openai
            _client = wrap_openai(_client)
    return _client


def call_llm(system_prompt: str, user_prompt: str, json_mode: bool = False, purpose: str = "call",
             pii_names: list[str | None] = ()) -> str | None:
    """GPT's answer as text, or None when no OpenAI key is set (callers then use plain code)."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    mapping: dict[str, str] = {}
    if settings().pii_masking:
        user_prompt, mapping = pii.mask(user_prompt, pii_names)
    model = settings().openai_model
    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
    with telemetry.tracer.start_as_current_span(f"gpt {purpose}") as span:
        started_ns, t0 = time.time_ns(), time.perf_counter()
        resp = _openai().chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            **extra)
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        call = {"purpose": purpose, "model": getattr(resp, "model", None) or model,
                "input_tokens": tokens_in, "output_tokens": tokens_out,
                "cost_usd": telemetry.cost_usd(model, tokens_in, tokens_out),
                "ms": round((time.perf_counter() - t0) * 1000, 1), "pii_masked": len(mapping),
                "started_ns": started_ns, "ended_ns": time.time_ns()}
        telemetry._set_llm_attributes(span, call)
    telemetry.record_llm_call(call)
    return pii.unmask(resp.choices[0].message.content, mapping)


def parse_json(text: str) -> dict:
    """json.loads that also copes with ```json ... ``` fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)
