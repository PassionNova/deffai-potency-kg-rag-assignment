"""Claude via the Anthropic Messages API with schema-constrained structured output.

Preferred mechanism: forced tool use (tool_choice=tool). Models that reject forced tool choice use native
structured outputs (output_config.format = json_schema) with the same schema. The mechanism actually used is
returned with every call and recorded in traces. Production would swap in a Bedrock adapter with the same contract."""

import copy
import json
import time
from dataclasses import dataclass
from functools import lru_cache

from potency.observability.usage import check_budget
from potency.settings import get_settings

FORCED_TOOL = "forced_tool_use"
JSON_SCHEMA_OUTPUT = "output_config.json_schema"


@dataclass
class ToolCallResult:
    output: dict
    usage: dict
    model: str
    elapsed_ms: int
    stop_reason: str | None
    request_id: str | None
    mechanism: str = FORCED_TOOL


def strict_schema(schema: dict) -> dict:
    """JSON-schema output requires closed objects; a nullable enum is expressed as anyOf[enum, null]."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
            t = node.get("type")
            if isinstance(t, list) and "null" in t and "enum" in node:
                base = [x for x in t if x != "null"]
                enum = [x for x in node.pop("enum") if x is not None]
                node.pop("type")
                node["anyOf"] = [{"type": base[0] if len(base) == 1 else base, "enum": enum}, {"type": "null"}]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


class AnthropicLLM:
    is_test_double = False

    def __init__(self, model: str, api_key: str):
        import anthropic

        self.model_id = model
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=6, timeout=300)
        self.mechanism = FORCED_TOOL

    def _kwargs(self, system: str, user: str, tool: dict, max_tokens: int, mechanism: str) -> dict:
        kwargs = dict(
            model=self.model_id,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if mechanism == FORCED_TOOL:
            kwargs.update(tools=[tool], tool_choice={"type": "tool", "name": tool["name"]})
        else:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": strict_schema(tool["input_schema"])}}
        return kwargs

    def call_tool(self, *, system: str, user: str, tool: dict, max_tokens: int) -> ToolCallResult:
        import anthropic

        check_budget()
        t0 = time.perf_counter()
        resp = None
        used = self.mechanism
        for _ in range(2):
            used = self.mechanism
            try:
                resp = self._client.messages.create(**self._kwargs(system, user, tool, max_tokens, used))
                break
            except anthropic.BadRequestError as exc:
                if "tool_choice" in str(exc).lower() and used == FORCED_TOOL:
                    self.mechanism = JSON_SCHEMA_OUTPUT
                else:
                    raise
        if resp is None:
            raise RuntimeError("model request could not be configured")
        elapsed = int((time.perf_counter() - t0) * 1000)
        if used == FORCED_TOOL:
            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if block is None:
                raise RuntimeError(f"model returned no tool_use block (stop_reason={resp.stop_reason})")
            output = dict(block.input)
        else:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            try:
                output = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"structured output was not valid JSON (stop_reason={resp.stop_reason})") from exc
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return ToolCallResult(
            output=output,
            usage=usage,
            model=resp.model,
            elapsed_ms=elapsed,
            stop_reason=resp.stop_reason,
            request_id=getattr(resp, "_request_id", None),
            mechanism=used,
        )


@lru_cache
def get_llm() -> AnthropicLLM:
    s = get_settings()
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value():
        raise RuntimeError("ANTHROPIC_API_KEY is not set; add it to .env (git-ignored)")
    return AnthropicLLM(s.generation_model, s.anthropic_api_key.get_secret_value())
