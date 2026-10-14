"""Main-case pipeline steps for a configured scope (the same generic steps run for fixture scopes)."""

from collections import defaultdict

from psycopg.types.json import Jsonb

from potency import ids
from potency.answering.service import answer_question
from potency.db import scoped_session
from potency.extraction.runner import run_extraction
from potency.graph.builder import build_graph
from potency.graph.export import export_graph
from potency.indexing.embeddings import build_index
from potency.sources.importer import import_records
from potency.workflows.config import CaseConfig, case_context_text
from potency.workflows.io import RESULTS, write_json, write_jsonl

DEFAULT_MODE = "graph"


def _record_run(scope, kind: str, meta: dict) -> str:
    run_id = meta.get("run_id") or ids.new_run_id(kind)
    with scoped_session(scope) as conn:
        conn.execute(
            "INSERT INTO run_record (id, tenant_id, project_id, kind, meta) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET meta=EXCLUDED.meta",
            (run_id, scope.tenant_id, scope.project_id, kind, Jsonb(meta)),
        )
    return run_id


def step_import(cfg: CaseConfig) -> dict:
    return import_records(
        [cfg.records_path], expected_scope=cfg.scope, expected_snapshot=cfg.corpus_snapshot, manifest=cfg.manifest,
        corrections=cfg.corrections,
    )


def step_index(cfg: CaseConfig) -> dict:
    stats = build_index(cfg.scope, corpus_snapshot=cfg.corpus_snapshot, log_progress=True)
    _record_run(cfg.scope, "index_build", {**stats, "run_id": ids.new_run_id("index")})
    return stats


def step_extract(cfg: CaseConfig) -> dict:
    return run_extraction(cfg.scope)


def step_graph(cfg: CaseConfig, out_dir=None) -> dict:
    stats = build_graph(cfg.scope)
    stats["export"] = export_graph(cfg.scope, out_dir or RESULTS / "graph")
    return stats


def step_answer(cfg: CaseConfig, question_ids: list[str] | None = None, mode: str = DEFAULT_MODE) -> dict:
    run_id = ids.new_run_id("answers")
    answers, traces = [], []
    context = case_context_text(cfg.case)
    for q in cfg.questions:
        if question_ids and q["id"] not in question_ids:
            continue
        modes = ["vector", "graph"] if q.get("compare_modes") else [mode]
        budget = q.get("max_evidence_chars_per_mode") or 12000
        for m in modes:
            a, t = answer_question(
                cfg.scope, question_id=q["id"], question=q["question"], mode=m, run_id=run_id,
                corpus_snapshot=cfg.corpus_snapshot, case_context=context, budget=budget,
            )
            answers.append(a)
            traces.append(t)
    write_jsonl(RESULTS / "answers.jsonl", answers)
    write_jsonl(RESULTS / "traces.jsonl", traces)
    _record_run(cfg.scope, "answers", {"run_id": run_id, "questions": [a["question_id"] + ":" + a["mode"] for a in answers]})
    return {"run_id": run_id, "answers": len(answers)}


def import_report(cfg: CaseConfig) -> dict:
    scope = cfg.scope
    with scoped_session(scope) as conn:
        runs = conn.execute("SELECT id, started_at, counts, report FROM import_run ORDER BY started_at").fetchall()
        index = conn.execute(
            "SELECT id, embedding_model, dims, chunker_version, stats, created_at FROM index_version "
            "WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        index_builds = conn.execute(
            "SELECT id, created_at, meta FROM run_record WHERE kind='index_build' ORDER BY created_at"
        ).fetchall()
        ext_run = conn.execute(
            "SELECT id, stats, policy, model, extractor_version, prompt_version FROM extraction_run "
            "WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        coverage = conn.execute(
            "SELECT source_id, outcome, reason, selected FROM extraction_coverage WHERE run_id=%s",
            (ext_run["id"] if ext_run else None,),
        ).fetchall()
        store = conn.execute(
            "SELECT source_id, count(*) AS n, count(*) FILTER (WHERE is_current) AS current FROM passage GROUP BY source_id"
        ).fetchall()
        doc_nodes = {
            r["label"] for r in conn.execute(
                "SELECT attributes->>'source_id' AS label FROM kg_node WHERE type='SourceDocument'"
            ).fetchall()
        }
        graph = conn.execute("SELECT id, stats FROM graph_build ORDER BY created_at DESC LIMIT 1").fetchone()
        chunk_counts = {
            r["source_id"]: r["n"] for r in conn.execute(
                "SELECT p.source_id, count(*) AS n FROM passage_chunk pc JOIN passage p ON p.id = pc.passage_fk "
                "GROUP BY p.source_id"
            ).fetchall()
        }
    if not runs:
        raise RuntimeError("no import runs recorded for scope")
    initial = next((r for r in runs if r["counts"].get("imported", 0) > 0), runs[0])
    latest = runs[-1]
    expected = {s["source_id"]: s for s in cfg.manifest.get("sources", [])}
    cov = defaultdict(lambda: defaultdict(int))
    for c in coverage:
        cov[c["source_id"]]["passages"] += 1
        cov[c["source_id"]]["selected"] += int(c["selected"])
        cov[c["source_id"]][f"outcome:{c['outcome']}"] += 1
        if c["reason"] and not c["selected"]:
            cov[c["source_id"]][f"skip_reason:{c['reason']}"] += 1
    ext_per_source = (ext_run["stats"] or {}).get("per_source", {}) if ext_run else {}
    per_source = {}
    for s in sorted({*expected, *(r["source_id"] for r in store)}):
        st = next((r for r in store if r["source_id"] == s), None)
        per_source[s] = {
            "manifest_passage_count": expected.get(s, {}).get("passage_count"),
            "import_outcomes_initial_run": initial["report"]["per_source"].get(s),
            "import_outcomes_latest_run": latest["report"]["per_source"].get(s),
            "stored_passages": st["n"] if st else 0,
            "current_passages": st["current"] if st else 0,
            "indexed_chunks": chunk_counts.get(s, 0),
            "source_document_node": s in doc_nodes,
            "semantic_extraction": {
                **dict(cov.get(s, {})),
                "assertions_accepted": ext_per_source.get(s, {}).get("assertions_accepted", 0),
                "assertions_rejected": ext_per_source.get(s, {}).get("assertions_rejected", 0),
            },
        }
    ic = initial["counts"]
    totals = (ext_run["stats"] or {}).get("totals", {}) if ext_run else {}
    report = {
        "corpus_snapshot": cfg.corpus_snapshot,
        "scope": scope.as_dict(),
        "index_version": index["id"] if index else None,
        "count_semantics": "Top-level imported/unchanged/failed/omitted are the initial import run that loaded the "
                           "store. restart_reimport shows the latest re-import (expected: all unchanged, zero new "
                           "embeddings). Ingestion/indexing counts are separate from semantic_extraction_coverage.",
        "imported": ic.get("imported", 0),
        "unchanged": ic.get("unchanged", 0),
        "updated": ic.get("updated", 0),
        "failed": ic.get("failed", 0),
        "omitted": ic.get("omitted", 0),
        "records_read": ic.get("records_read", 0),
        "reconciled": ic.get("reconciled", False),
        "complete_import": ic.get("reconciled", False) and ic.get("failed", 0) == 0
        and (ic.get("imported", 0) + ic.get("unchanged", 0) + ic.get("updated", 0)) == cfg.manifest.get("record_count"),
        "errors": initial["report"].get("errors", []),
        "initial_import_run": {"run_id": initial["id"], "started_at": str(initial["started_at"]), "counts": ic},
        "restart_reimport": {
            "run_id": latest["id"], "started_at": str(latest["started_at"]), "counts": latest["counts"],
            "latest_index_build": index_builds[-1]["meta"] if index_builds else None,
            "is_distinct_from_initial": latest["id"] != initial["id"],
        },
        "index_builds": [
            {"run_id": b["id"], "created_at": str(b["created_at"]),
             **{k: b["meta"].get(k) for k in ("index_version", "embeddings_computed", "embedding_cache_hits",
                                              "chunks", "passages_indexed", "elapsed_s")}}
            for b in index_builds
        ],
        "manifest_check": initial["report"].get("manifest_check"),
        "corrections_check": latest["report"].get("corrections_check"),
        "indexing": {
            "index_version": index["id"] if index else None,
            "embedding_model": index["embedding_model"] if index else None,
            "dims": index["dims"] if index else None,
            "chunker_version": index["chunker_version"] if index else None,
            "stats": index["stats"] if index else None,
            "all_records_indexed": bool(index) and index["stats"].get("passages_indexed") == sum(r["n"] for r in store),
        },
        "graph": {"graph_build_id": graph["id"] if graph else None, "stats": graph["stats"] if graph else None,
                  "source_document_nodes": len(doc_nodes),
                  "all_sources_have_document_node": all(v["source_document_node"] for v in per_source.values())},
        "semantic_extraction_coverage": {
            "run_id": ext_run["id"] if ext_run else None,
            "extractor": (ext_run["stats"] or {}).get("extractor") if ext_run else None,
            "selection_policy": ext_run["policy"] if ext_run else None,
            "selection_method": "Every current passage is scored with the generic relevance policy "
                                "(configs/extraction-rules.yaml); passages at or above min_score are selected, "
                                "capped per source and in total, and each source keeps its top-scoring passage. "
                                "Skipped passages keep structural coverage (SourcePassage node, embeddings, "
                                "redaction placeholders) but receive no semantic assertions.",
            "totals": totals,
            "complete": (ext_run["stats"] or {}).get("complete") if ext_run else False,
        },
        "per_source": per_source,
        "import_run_history": [
            {"run_id": r["id"], "started_at": str(r["started_at"]), "counts": r["counts"]} for r in runs
        ],
    }
    write_json(RESULTS / "import-report.json", report)
    return report
