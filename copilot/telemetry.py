"""Tracing and run metrics for every case.

Two kinds of tracing, both optional and both off unless configured:
- OpenTelemetry: one trace per case, a span per agent, per MCP tool call and per GPT call. Sent over OTLP/HTTP to
  OTEL_EXPORTER_OTLP_ENDPOINT (on AWS: the ADOT collector next to the app, which forwards to AWS X-Ray).
- LangSmith: set LANGSMITH_TRACING=true and LANGSMITH_API_KEY. LangGraph then traces every node itself, and
  llm.py wraps the OpenAI client so each GPT call shows its prompt, answer and tokens.

Run metrics are always on and need no service: each workflow node records how long it took and what its GPT calls
cost, and the case keeps them (screen, audit log, evals)."""
import contextvars
import functools
import inspect
import logging
import os
import time
from contextlib import contextmanager

from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer("loan-copilot")
_llm_calls: contextvars.ContextVar[list | None] = contextvars.ContextVar("llm_calls", default=None)
_configured = False


def setup_tracing(service_name: str = "loan-copilot") -> bool:
    """Starts exporting spans if OTEL_EXPORTER_OTLP_ENDPOINT is set. Safe to call more than once."""
    global _configured
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if _configured or not endpoint:
        return _configured
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    extra = {}
    if os.environ.get("OTEL_AWS_XRAY_IDS", "").lower() == "true":     # X-Ray-shaped trace ids (time in the id)
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        extra["id_generator"] = AwsXRayIdGenerator()
    name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": name}), **extra)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    _configured = True
    log.info("OpenTelemetry tracing on, exporting to %s", endpoint)
    return True


# ---------- GPT call records ----------

def price_per_million(model: str) -> tuple[float, float] | None:
    """(input, output) US dollars per million tokens. OPENAI_PRICE_PER_1M="0.15,0.60" overrides the table."""
    override = os.environ.get("OPENAI_PRICE_PER_1M")
    if override:
        try:
            i, o = (float(x) for x in override.split(","))
            return i, o
        except ValueError:
            log.warning("OPENAI_PRICE_PER_1M must look like 0.15,0.60; ignoring it")
    table = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00), "gpt-4.1-mini": (0.40, 1.60),
             "gpt-4.1": (2.00, 8.00)}                    # list prices, approximate: check your OpenAI plan
    for name, prices in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(name):
            return prices
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = price_per_million(model)
    if prices is None:
        return None
    return round((input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000, 6)


def record_llm_call(call: dict):
    """Adds one GPT call (model, tokens, cost, ms, purpose) to the node or tool that is running now."""
    calls = _llm_calls.get()
    if calls is not None:
        calls.append(call)


@contextmanager
def collect_llm_calls():
    """Everything record_llm_call sees inside this block, as a list."""
    calls: list = []
    token = _llm_calls.set(calls)
    try:
        yield calls
    finally:
        _llm_calls.reset(token)


def add_llm_span(call: dict):
    """A GPT call that ran in the tool server process: drawn as a child span with its real start and end."""
    if not {"started_ns", "ended_ns"} <= set(call):
        return
    span = tracer.start_span(f"gpt {call.get('purpose', 'call')}", start_time=call["started_ns"])
    _set_llm_attributes(span, call)
    span.end(end_time=call["ended_ns"])


def _set_llm_attributes(span, call: dict):
    span.set_attribute("gen_ai.system", "openai")
    span.set_attribute("gen_ai.request.model", call.get("model", ""))
    span.set_attribute("gen_ai.usage.input_tokens", call.get("input_tokens", 0))
    span.set_attribute("gen_ai.usage.output_tokens", call.get("output_tokens", 0))
    span.set_attribute("copilot.purpose", call.get("purpose", ""))
    span.set_attribute("copilot.pii_masked", call.get("pii_masked", 0))
    if call.get("cost_usd") is not None:
        span.set_attribute("copilot.cost_usd", call["cost_usd"])


# ---------- workflow nodes ----------

def traced_node(agent: str):
    """Wraps a LangGraph node: a span named after the agent, its time, and its GPT calls in state['metrics']."""
    def wrap(node):
        def finish(result: dict | None, started: float, calls: list) -> dict:
            entry = {"agent": agent, "ms": round((time.perf_counter() - started) * 1000, 1)}
            if calls:
                entry["llm"] = calls
            return {**(result or {}), "metrics": [entry]}

        if inspect.iscoroutinefunction(node):
            @functools.wraps(node)
            async def run_async(state):
                with tracer.start_as_current_span(f"agent {agent}") as span, collect_llm_calls() as calls:
                    started = time.perf_counter()
                    result = await node(state)
                    _mark_stop(span, result)
                    return finish(result, started, calls)
            return run_async

        @functools.wraps(node)
        def run_sync(state):
            with tracer.start_as_current_span(f"agent {agent}") as span, collect_llm_calls() as calls:
                started = time.perf_counter()
                result = node(state)
                _mark_stop(span, result)
                return finish(result, started, calls)
        return run_sync
    return wrap


def _mark_stop(span, result: dict | None):
    if result and result.get("status") and result["status"] != "done":
        span.set_attribute("copilot.stopped", result["status"])


def summarize(metrics: list[dict] | None) -> dict:
    """Totals for one case: machine time (the wait for the human is not counted), GPT calls, tokens, cost."""
    metrics = metrics or []
    calls = [c for m in metrics for c in m.get("llm", [])]
    costs = [c["cost_usd"] for c in calls if c.get("cost_usd") is not None]
    return {"total_ms": round(sum(m["ms"] for m in metrics), 1),
            "per_agent_ms": {m["agent"]: m["ms"] for m in metrics},
            "llm_calls": len(calls),
            "input_tokens": sum(c.get("input_tokens", 0) for c in calls),
            "output_tokens": sum(c.get("output_tokens", 0) for c in calls),
            "cost_usd": round(sum(costs), 6) if costs else (0.0 if not calls else None)}
