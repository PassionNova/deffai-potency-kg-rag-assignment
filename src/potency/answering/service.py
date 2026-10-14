"""Question answering over one scope: retrieve (vector | graph) -> pack -> generate -> verify -> persist trace."""

import subprocess
import time
from functools import lru_cache

from psycopg.types.json import Jsonb

from potency import __version__, ids
from potency.adapters.embedders import get_embedder
from potency.answering.generator import get_generator
from potency.answering.verifier import verify
from potency.db import scoped_session
from potency.graph.builder import BUILDER_VERSION, latest_graph_build
from potency.indexing.embeddings import active_index_version
from potency.observability.usage import record_usage
from potency.retrieval.graph import GRAPH_RETRIEVER_VERSION, TraversalLimits, graph_retrieve
from potency.retrieval.packer import PACKER_VERSION, Candidate, pack
from potency.retrieval.vector import VECTOR_RETRIEVER_VERSION, vector_search
from potency.settings import REPO_ROOT
from potency.tenancy import Scope

VECTOR_K = 24
GRAPH_WEIGHT = 0.5
DEFAULT_BUDGET = 12000


@lru_cache
def code_version() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "src", "prompts", "configs"], cwd=REPO_ROOT,
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        commit, dirty = None, None
    return {"package": f"potency {__version__}", "git_commit": commit, "uncommitted_changes": dirty}


def answer_question(
    scope: Scope,
    *,
    question_id: str,
    question: str,
    mode: str,
    run_id: str,
    corpus_snapshot: str | None,
    case_context: str,
    budget: int = DEFAULT_BUDGET,
    extra: dict | None = None,
    persist: bool = True,
) -> tuple[dict, dict]:
    if mode not in ("vector", "graph"):
        raise ValueError(f"unknown mode {mode}")
    embedder = get_embedder()
    generator = get_generator()
    t0 = time.perf_counter()
    qvec = embedder.embed_query(question)
    seeds: list[dict] = []
    traversed: list[dict] = []
    limits: dict | None = None
    with scoped_session(scope) as conn:
        index_version = active_index_version(conn)
        graph_build = latest_graph_build(conn)
        if mode == "vector":
            hits = vector_search(conn, qvec, embedder.model_id, VECTOR_K)
            candidates = [
                Candidate(h["passage_fk"], round(float(h["sim"]), 4), None, (h["char_start"], h["char_end"]),
                          {"vector_sim": round(float(h["sim"]), 4)})
                for h in hits
            ]
            retriever_version = f"{VECTOR_RETRIEVER_VERSION}+{PACKER_VERSION}"
            limits = {"mode": "vector", "top_k_passages": VECTOR_K, "graph_traversal": None}
        else:
            gr = graph_retrieve(conn, question, qvec, embedder.model_id, TraversalLimits())
            seeds, traversed, limits = gr.seeds, gr.traversed_edges, gr.limits
            max_g = max((c["graph_score"] for c in gr.candidates.values()), default=1.0) or 1.0
            candidates = []
            for pk, c in gr.candidates.items():
                score = GRAPH_WEIGHT * (c["graph_score"] / max_g) + (1 - GRAPH_WEIGHT) * c["vector_sim"]
                candidates.append(Candidate(pk, round(score, 4), c["quotes"][0] if c["quotes"] else None,
                                            c["best_chunk"],
                                            {k: c[k] for k in ("graph_score", "supporting_origins", "vector_sim", "via")}))
            candidates.sort(key=lambda c: (-c.score, c.passage_pk))
            retriever_version = f"{GRAPH_RETRIEVER_VERSION}+{BUILDER_VERSION}+{PACKER_VERSION}"
            limits = {**limits, "candidate_scoring": f"{GRAPH_WEIGHT}*normalized_graph_score + "
                                                     f"{1 - GRAPH_WEIGHT}*vector_similarity"}
        packed = pack(conn, candidates, budget)
        passage_texts = {
            r["id"]: r["text"]
            for r in conn.execute(
                "SELECT id, text FROM passage WHERE id = ANY(%s)", ([b.passage_pk for b in packed.blocks],)
            ).fetchall()
        }
    retrieval_ms = int((time.perf_counter() - t0) * 1000)

    gen = generator.generate(question, case_context, packed.evidence_text)
    verification = verify(gen.output, packed.blocks, passage_texts, scope)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    usd = record_usage(scope, run_id, f"answer:{question_id}:{mode}", gen.model, gen.usage) if gen.usage else 0.0

    trace_id = f"trace-{ids.short_hash(scope.prefix, run_id, question_id, mode, n=20)}"
    extra = extra or {}
    trace = {
        "trace_id": trace_id,
        "run_id": run_id,
        "question_id": question_id,
        "question": question,
        "mode": mode,
        "scope": scope.as_dict(),
        "corpus_snapshot": corpus_snapshot,
        "index_version": index_version,
        "graph_build_id": graph_build,
        "seed_node_ids": [s["node_id"] for s in seeds],
        "seeds": seeds,
        "traversed_edge_ids": [t["edge_id"] for t in traversed],
        "traversed_edges": traversed,
        "traversal_limits": limits,
        "candidates_considered": len(candidates),
        "candidate_scores": [{"passage_pk": c.passage_pk, "score": c.score, **c.detail} for c in candidates[:40]],
        "selected_passage_ids": [b.passage_pk for b in packed.blocks],
        "selected_passages": [
            {"evidence_id": b.evidence_id, "scope": scope.as_dict(), "source_id": b.source_id,
             "source_version": b.source_version, "passage_id": b.passage_id, "page": b.page, "section": b.section,
             "score": b.score}
            for b in packed.blocks
        ],
        "packing_skipped": packed.skipped[:40],
        "evidence_text": packed.evidence_text,
        "evidence_chars": packed.chars,
        "evidence_budget_chars": budget,
        "char_count_method": "Unicode code points (Python len) over the full evidence_text sent to generation",
        "retriever_version": retriever_version,
        "prompt_version": generator.prompt_version,
        "model": gen.model,
        "embedding_model": embedder.model_id,
        "is_test_double": {"generator": gen.is_test_double, "embedder": embedder.is_test_double},
        "elapsed_ms": elapsed_ms,
        "timings_ms": {"retrieval_and_packing": retrieval_ms, "generation": gen.elapsed_ms},
        "usage": {**gen.usage, "estimated_usd": usd, "request_id": gen.request_id},
        "generation_output": gen.output,
        "verification": verification,
        "system_versions": {**code_version(), "retriever": retriever_version, "prompt": generator.prompt_version,
                            "generation_model": gen.model, "embedding_model": embedder.model_id,
                            "graph_builder": BUILDER_VERSION},
        "created_at": ids.utcnow().isoformat(),
        **extra,
    }
    answer = {
        "question_id": question_id,
        "mode": mode,
        "answer": gen.output.get("answer", ""),
        "claims": verification["claims"],
        "citations": verification["citations"],
        "not_found": gen.output.get("not_found", []),
        "conflicts": gen.output.get("conflicts", []),
        "uncertainty": gen.output.get("uncertainty", ""),
        "citation_check": verification["summary"],
        "trace_id": trace_id,
        "run_id": run_id,
        "scope": scope.as_dict(),
        "corpus_snapshot": corpus_snapshot,
        "index_version": index_version,
        "model": gen.model,
        "is_test_double": gen.is_test_double,
        **extra,
    }
    if persist:
        with scoped_session(scope) as conn:
            conn.execute(
                "INSERT INTO trace (id, tenant_id, project_id, run_id, question_id, mode, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
                (trace_id, scope.tenant_id, scope.project_id, run_id, question_id, mode, Jsonb(trace)),
            )
            conn.execute(
                "INSERT INTO answer (id, tenant_id, project_id, trace_id, run_id, question_id, mode, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
                (f"answer-{trace_id[6:]}", scope.tenant_id, scope.project_id, trace_id, run_id, question_id, mode,
                 Jsonb(answer)),
            )
    return answer, trace
