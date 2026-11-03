"""The only place that talks to OpenAI."""
import json
import os
import re

from .config import settings

_client = None


def call_llm(system_prompt: str, user_prompt: str, json_mode: bool = False) -> str | None:
    """GPT's answer as text, or None when no OpenAI key is set (callers then use plain code)."""
    global _client
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(timeout=60)
    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
    resp = _client.chat.completions.create(
        model=settings().openai_model, temperature=0,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        **extra)
    return resp.choices[0].message.content


def parse_json(text: str) -> dict:
    """json.loads that also copes with ```json ... ``` fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)
