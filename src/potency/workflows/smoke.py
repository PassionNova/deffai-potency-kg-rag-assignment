"""The two pre-start access checks from MODEL_ACCESS.md, using tiny synthetic input only."""

import math

from potency import ids
from potency.settings import get_settings
from potency.workflows.io import RESULTS, write_json

SENTENCE = "Access check: a fictional assay measures a fictional attribute."


def run_smoke() -> dict:
    s = get_settings()
    out: dict = {"package_edition": "2026-09-21-potency-rag-v2", "corpus_snapshot": "potency-assignment-2026-09-21-v3"}
    from potency.adapters.embedders import get_embedder

    emb = get_embedder()
    vec = emb.embed_documents([SENTENCE])[0]
    out["embedding"] = {
        "provider": "local fastembed (ONNX Runtime, CPU)" if not emb.is_test_double else "test double",
        "model": emb.model_id,
        "dimension": int(vec.shape[0]),
        "finite_nonempty": bool(vec.size) and all(math.isfinite(float(x)) for x in vec),
        "success_at": ids.utcnow().isoformat(),
    }
    import anthropic

    client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value())
    resp = client.messages.create(
        model=s.generation_model, max_tokens=60,
        messages=[{"role": "user", "content": f"Reply with one short sentence acknowledging: {SENTENCE}"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    out["generation"] = {
        "provider": "Anthropic Messages API",
        "requested_model": s.generation_model,
        "response_model": resp.model,
        "nonempty_content": bool(text.strip()),
        "sanitized_response_excerpt": text.strip()[:160],
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
        "success_at": ids.utcnow().isoformat(),
    }
    tool = {
        "name": "ack", "description": "Acknowledge.",
        "input_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                         "required": ["ok", "word"]},
    }
    from potency.adapters.anthropic_llm import AnthropicLLM

    llm = AnthropicLLM(s.generation_model, s.anthropic_api_key.get_secret_value())
    res = llm.call_tool(system="You return structured access-check data.",
                        user="Return ok=true and word='fixture'.", tool=tool, max_tokens=400)
    out["structured_output"] = {
        "mechanism": res.mechanism,
        "note": "forced tool use is tried first; models that reject tool_choice=tool use output_config json_schema "
                "with the same schema. Outputs are validated locally (Pydantic / verifier).",
        "capability": "json_schema" if res.output.get("ok") is True and res.output.get("word") == "fixture" else "none",
        "observed": res.output,
        "usage": res.usage,
        "success_at": ids.utcnow().isoformat(),
    }
    out["credentials"] = "ANTHROPIC_API_KEY read from git-ignored .env; value never written to artifacts"
    write_json(RESULTS / "access-check.json", out)
    return out
