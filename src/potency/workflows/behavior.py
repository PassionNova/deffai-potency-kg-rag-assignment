"""Source-correction and unresolved-conflict fixtures, executed through the same generic import/index/extract/
graph/answer pipeline. Scenario metadata drives scope and declared revision operations only; expected values
are read here solely to score checks after the run."""

import json
import re
import shutil

from potency import ids
from potency.answering.service import answer_question
from potency.extraction.runner import run_extraction
from potency.graph.builder import build_graph
from potency.graph.export import export_graph
from potency.indexing.embeddings import build_index
from potency.settings import REPO_ROOT
from potency.sources.admin import delete_scope, export_source_records
from potency.sources.importer import import_records
from potency.tenancy import Scope
from potency.textutil import find_exact_span
from potency.workflows.io import RESULTS, read_jsonl, rel_results, write_json, write_jsonl

CONTRACT = REPO_ROOT / "fixtures" / "behavior-cases.json"
OUT = RESULTS / "fixtures"
MODE = "graph"
CONTEXT = "Synthetic behavior-fixture scope; no case configuration. Treat the notes as the only evidence."
OVERRIDE_RE = re.compile(
    r"(later|newer|more recent|most recent)[^.]{0,60}(supersede|override|replace|take[s]? precedence|controls|governs)",
    re.I,
)


def _ref_key(r: dict) -> str:
    s = r["scope"]
    return ids.passage_pk(s["tenant_id"], s["project_id"], r["source_id"], r["source_version"], r["passage_id"])


def run_behavior_fixtures(out_dir=OUT) -> dict:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    answers, traces, history, checks = [], [], [], []
    for sc in contract["scenarios"]:
        scope = Scope.from_dict(sc["query_scope"])
        reset = delete_scope(scope)
        for stage in sc["stages"]:
            run_id = ids.new_run_id(f"fx-{sc['id']}-{stage['id']}")
            paths = [REPO_ROOT / p for p in stage["input_records"]]
            supersedes = stage.get("supersedes_source_refs") if stage["operation"] == "replace_source_revision" else None
            imp = import_records(paths, expected_scope=scope, expected_snapshot=stage["corpus_snapshot"],
                                 supersedes=supersedes, replaces_with=stage.get("current_source_refs"),
                                 run_label=f"fx-import-{stage['id']}")
            idx = build_index(scope, corpus_snapshot=stage["corpus_snapshot"])
            ext = run_extraction(scope)
            gb = build_graph(scope)
            extra = {"scenario_id": sc["id"], "stage": stage["id"], "run_id": run_id}
            ans, tr = answer_question(scope, question_id=sc["question_id"], question=sc["question"], mode=MODE,
                                      run_id=run_id, corpus_snapshot=stage["corpus_snapshot"], case_context=CONTEXT,
                                      budget=6000, extra=extra)
            answers.append(ans)
            traces.append(tr)
            hdir = out_dir / "history" / sc["id"] / stage["id"]
            if hdir.exists():
                shutil.rmtree(hdir)
            (hdir / "inputs").mkdir(parents=True)
            for p in paths:
                shutil.copy2(p, hdir / "inputs" / p.name)
            records = export_source_records(scope, hdir / "source_records.jsonl")
            gexp = export_graph(scope, hdir / "graph")
            write_jsonl(hdir / "answers.jsonl", [ans])
            write_jsonl(hdir / "traces.jsonl", [tr])
            write_json(hdir / "run.json", {"run_id": run_id, "scenario_id": sc["id"], "stage": stage["id"],
                                           "operation": stage["operation"], "import": imp["counts"],
                                           "supersession": imp["supersession"], "index": idx,
                                           "extraction": ext["totals"], "graph": gb,
                                           "scope_reset_before_scenario": reset if stage is sc["stages"][0] else None})
            history.append({
                "scenario_id": sc["id"], "stage": stage["id"], "run_id": run_id,
                "corpus_snapshot": stage["corpus_snapshot"], "index_version": idx["index_version"],
                "graph_build_id": gb["graph_build_id"],
                "source_refs": stage["current_source_refs"],
                "superseded_source_refs": stage.get("supersedes_source_refs", []),
                "source_records_path": rel_results(hdir / "source_records.jsonl"),
                "input_copies": [rel_results(hdir / "inputs" / p.name) for p in paths],
                "graph_nodes_path": rel_results(hdir / "graph" / "nodes.jsonl"),
                "graph_edges_path": rel_results(hdir / "graph" / "edges.jsonl"),
                "answers_path": rel_results(hdir / "answers.jsonl"),
                "traces_path": rel_results(hdir / "traces.jsonl"),
                "run_metadata_path": rel_results(hdir / "run.json"),
                "graph_sha256": {"nodes": gexp["nodes_sha256"], "edges": gexp["edges_sha256"]},
            })
            checks.extend(_stage_checks(sc, stage, ans, tr, records, hdir))
        if sc["id"] == "source-correction":
            checks.extend(_correction_history_checks(sc, out_dir))

    write_jsonl(out_dir / "answers.jsonl", answers)
    write_jsonl(out_dir / "traces.jsonl", traces)
    report = {
        "contract": {"id": contract["id"], "version": contract["version"], "path": "fixtures/behavior-cases.json"},
        "scenarios": [{"scenario_id": s["id"], "question_id": s["question_id"], "mode": MODE,
                       "stages": [st["id"] for st in s["stages"]], "scope": s["query_scope"]}
                      for s in contract["scenarios"]],
        "commands": ["docker compose up -d", "uv run alembic upgrade head", "uv run potency fixtures"],
        "execution": {
            "models": sorted({t["model"] for t in traces}),
            "embedding_model": sorted({t["embedding_model"] for t in traces}),
            "test_doubles_used": any(t["is_test_double"]["generator"] or t["is_test_double"]["embedder"] for t in traces),
            "note": "Each scenario scope is deleted (RLS-scoped) before its first stage so the run is reproducible; "
                    "pre-update artifacts are preserved as files under history/.",
        },
        "history_artifacts": history,
        "checks": checks,
        "failures": [c for c in checks if c["status"] != "passed"],
        "answers_path": "fixtures/answers.jsonl",
        "traces_path": "fixtures/traces.jsonl",
        "origin": "supplied visible acceptance fixtures (not candidate-authored holdout)",
    }
    write_json(out_dir / "report.json", report)
    return report


def _check(sc, stage, check_id, ok, observed, refs) -> dict:
    return {"scenario_id": sc["id"], "stage": stage["id"] if isinstance(stage, dict) else stage, "check_id": check_id,
            "status": "passed" if ok else "failed", "observed": observed, "artifact_refs": refs}


def _citations_resolve(ans: dict, records: list[dict]) -> tuple[bool, list]:
    by_key = {(r["source_id"], r["source_version"], r["passage_id"]): r["text"] for r in records}
    detail = []
    for c in ans["citations"]:
        text = by_key.get((c["source_id"], c["source_version"], c["passage_id"]))
        ok = text is not None and c["quote"] in text
        detail.append({"passage": f"{c['source_id']}@{c['source_version']}#{c['passage_id']}", "exact": ok})
    return bool(detail) and all(d["exact"] for d in detail), detail


def _stage_checks(sc, stage, ans, tr, records, hdir) -> list[dict]:
    refs = [rel_results(hdir / "answers.jsonl"), rel_results(hdir / "traces.jsonl")]
    current = {_ref_key(r) for r in stage["current_source_refs"]}
    selected = set(tr["selected_passage_ids"])
    cited_versions = {c["source_version"] for c in ans["citations"]}
    text = (ans["answer"] + " " + " ".join(c["text"] for c in ans["claims"])).lower()
    out = []
    if sc["id"] == "source-correction":
        exp = stage["expected_attribute"].lower()
        others = {s["expected_attribute"].lower() for s in sc["stages"]} - {exp}
        want = {r["source_version"] for r in stage["current_source_refs"]}
        cid = "before_answer_grounded_in_v1" if stage["id"] == "before" else "after_answer_grounded_in_v2"
        ok = (current <= selected and cited_versions == want and re.search(rf"\b{exp}\b", text) is not None
              and not any(re.search(rf"\b{o}\b", text) for o in others))
        out.append(_check(sc, stage, cid, ok, {
            "selected_passage_ids": sorted(selected), "cited_source_versions": sorted(cited_versions),
            "expected_attribute_mentioned": re.search(rf"\b{exp}\b", text) is not None,
            "other_attribute_mentioned": sorted(o for o in others if re.search(rf"\b{o}\b", text))}, refs))
        if stage["id"] == "after":
            old = {_ref_key(r) for r in stage["supersedes_source_refs"]}
            nodes = read_jsonl(hdir / "graph" / "nodes.jsonl")
            old_assertions = [n for n in nodes if n["type"] == "Assertion" and any(_ref_key(r) in old for r in n["source_refs"])]
            leaked = [n["id"] for n in old_assertions if n["is_current"]]
            ok2 = not (old & selected) and not leaked and all(
                o not in tr["evidence_text"] for o in [f"attribute {x}" for x in others])
            out.append(_check(sc, stage, "superseded_assertion_not_current", ok2, {
                "superseded_passages_in_evidence": sorted(old & selected),
                "superseded_assertions": [n["id"] for n in old_assertions],
                "superseded_assertions_still_current": leaked,
                "superseded_version_lifecycle": sorted({r["_store_state"]["version_lifecycle"] for r in records
                                                        if r["source_version"] in {x["source_version"] for x in stage["supersedes_source_refs"]}}),
            }, refs + [rel_results(hdir / "graph" / "nodes.jsonl")]))
        ok3, detail = _citations_resolve(ans, records)
        out.append(_check(sc, stage, "versioned_citations_resolve_exactly", ok3, {"citations": detail}, refs))
    else:
        want_ids = {r["passage_id"] for r in stage["current_source_refs"]}
        cited = {c["passage_id"] for c in ans["citations"]}
        nodes = read_jsonl(hdir / "graph" / "nodes.jsonl")
        edges = read_jsonl(hdir / "graph" / "edges.jsonl")
        claim_nodes = [n for n in nodes if n["type"] == "Assertion" and n["is_current"]
                       and {r["passage_id"] for r in n["source_refs"]} & want_ids]
        claim_sources = {r["passage_id"] for n in claim_nodes for r in n["source_refs"]}
        contradicts = [e for e in edges if e["type"] == "contradicts" and e["is_current"]]
        out.append(_check(sc, stage, "both_claims_preserved", want_ids <= claim_sources and bool(contradicts), {
            "current_assertions_by_passage": sorted(claim_sources), "contradicts_edges": [e["id"] for e in contradicts]},
            [rel_results(hdir / "graph" / "nodes.jsonl"), rel_results(hdir / "graph" / "edges.jsonl")]))
        in_evidence = {pid for pid in want_ids if pid in tr["evidence_text"]}
        out.append(_check(sc, stage, "both_sources_in_generation_evidence", in_evidence == want_ids,
                          {"passages_in_evidence_text": sorted(in_evidence)}, refs))
        ok3, detail = _citations_resolve(ans, records)
        out.append(_check(sc, stage, "both_sources_cited_exactly", ok3 and want_ids <= cited,
                          {"cited_passages": sorted(cited), "citations": detail}, refs))
        conflicts = ans.get("conflicts") or []
        unresolved = any(c.get("resolution") == "unresolved" for c in conflicts) or "unresolved" in text
        out.append(_check(sc, stage, "answer_reports_unresolved_conflict", unresolved,
                          {"conflicts": conflicts, "answer_mentions_unresolved": "unresolved" in text}, refs))
        override = OVERRIDE_RE.search(text)
        resolved = any(c.get("resolution") == "resolved_by_evidence" for c in conflicts)
        out.append(_check(sc, stage, "newer_issue_date_not_used_as_automatic_override", not override and not resolved,
                          {"override_phrase": override.group(0) if override else None,
                           "any_conflict_marked_resolved": resolved}, refs))
    return out


def _correction_history_checks(sc, out_dir) -> list[dict]:
    before = out_dir / "history" / sc["id"] / "before"
    files = {k: before / k for k in ("source_records.jsonl", "graph/nodes.jsonl", "graph/edges.jsonl",
                                     "answers.jsonl", "traces.jsonl")}
    exists = {k: v.exists() and v.stat().st_size > 0 for k, v in files.items()}
    v1 = sc["stages"][0]["current_source_refs"][0]
    recs = read_jsonl(files["source_records.jsonl"])
    nodes = read_jsonl(files["graph/nodes.jsonl"])
    has_v1_record = any(r["source_version"] == v1["source_version"] for r in recs)
    has_v1_assertion = any(n["type"] == "Assertion" and any(r["source_version"] == v1["source_version"]
                                                            for r in n["source_refs"]) for n in nodes)
    exact = all(find_exact_span(r["text"], r["text"]) for r in recs)
    return [_check(sc, "before", "old_sources_assertions_answers_traces_inspectable",
                   all(exists.values()) and has_v1_record and has_v1_assertion and exact,
                   {"files_present": exists, "v1_record_preserved": has_v1_record,
                    "v1_assertion_preserved": has_v1_assertion},
                   [rel_results(v) for v in files.values()])]
