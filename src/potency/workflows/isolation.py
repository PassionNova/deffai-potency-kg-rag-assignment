"""Project-isolation demonstration: two scopes that reuse identical source/version/passage identifiers, imported
into the same store. The query phase runs in a fresh process (restart) and checks marker containment across
selected evidence, traversed graph content, citations and answers, in both retrieval modes."""

import json
import subprocess
import sys

from potency import ids
from potency.answering.service import answer_question
from potency.db import scoped_session
from potency.extraction.runner import run_extraction
from potency.graph.builder import build_graph
from potency.indexing.embeddings import build_index
from potency.settings import REPO_ROOT
from potency.sources.importer import import_records
from potency.tenancy import Scope
from potency.workflows.io import RESULTS, write_json, write_jsonl

CASES_PATH = REPO_ROOT / "fixtures" / "isolation-cases.json"
CONFIGS = [REPO_ROOT / "fixtures" / "configs" / "project-one.json", REPO_ROOT / "fixtures" / "configs" / "project-two.json"]
PROBE_QUESTION = "Which attribute does the fictional assay measure, according to the project evidence?"
OUT = RESULTS / "isolation"


def _load(cfg_path):
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return Scope.from_dict(cfg["scope"]), (cfg_path.parent / cfg["source_records"]).resolve(), cfg["corpus_snapshot"]


def phase_import() -> dict:
    out = {}
    for cfg_path in CONFIGS:
        scope, records, snapshot = _load(cfg_path)
        rep = import_records([records], expected_scope=scope, expected_snapshot=snapshot)
        idx = build_index(scope, corpus_snapshot=snapshot)
        ext = run_extraction(scope)
        gb = build_graph(scope)
        out[scope.prefix] = {"import": rep["counts"], "index_version": idx["index_version"],
                             "embeddings_computed": idx["embeddings_computed"],
                             "extraction": ext["totals"], "graph": {k: gb[k] for k in ("graph_build_id", "nodes", "edges")}}
    return out


def _traversed_content(scope: Scope, trace: dict) -> str:
    node_ids = set(trace["seed_node_ids"]) | {t["to"] for t in trace["traversed_edges"]} | {
        t["from"] for t in trace["traversed_edges"]}
    with scoped_session(scope) as conn:
        rows = conn.execute("SELECT label, attributes, source_refs FROM kg_node WHERE id = ANY(%s)",
                            (list(node_ids),)).fetchall()
        quotes = conn.execute(
            "SELECT p.text FROM evidence_ref er JOIN passage p ON p.id = er.passage_fk WHERE er.owner_id = ANY(%s)",
            (list(node_ids) + trace["traversed_edge_ids"],),
        ).fetchall()
    return json.dumps([dict(r) for r in rows], default=str) + json.dumps([q["text"] for q in quotes])


def phase_query(out_dir=OUT) -> dict:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    run_id = ids.new_run_id("isolation")
    answers, traces, checks = [], [], []
    for case in cases["assertions"]:
        scope = Scope.from_dict(case["query_scope"])
        with scoped_session(scope) as conn:
            leaked_rows = conn.execute(
                "SELECT count(*) AS n FROM passage WHERE text LIKE %s", (f"%{case['must_not_contain']}%",)
            ).fetchone()["n"]
            visible = conn.execute("SELECT count(*) AS n FROM passage").fetchone()["n"]
        checks.append({"scope": scope.as_dict(), "mode": None, "check_id": "store_rows_visible_in_scope",
                       "status": "passed" if leaked_rows == 0 else "failed",
                       "observed": {"visible_passages": visible, "rows_with_other_marker": leaked_rows}})
        for mode in ("vector", "graph"):
            a, t = answer_question(scope, question_id="ISO-probe", question=PROBE_QUESTION, mode=mode, run_id=run_id,
                                   corpus_snapshot="isolation-fixture-v1",
                                   case_context="Synthetic isolation fixture scope; no case configuration.",
                                   budget=6000, extra={"fixture": "project-isolation"})
            answers.append(a)
            traces.append(t)
            surfaces = {
                "selected_evidence": t["evidence_text"],
                "traversed_graph_content": _traversed_content(scope, t) if mode == "graph" else "",
                "citations": json.dumps(a["citations"]),
                "answer": a["answer"] + json.dumps(a["claims"]),
                "trace": json.dumps(t, default=str),
            }
            for name, text in surfaces.items():
                leaked = case["must_not_contain"] in text
                checks.append({"scope": scope.as_dict(), "mode": mode, "check_id": f"no_foreign_marker_in_{name}",
                               "status": "failed" if leaked else "passed",
                               "observed": {"must_not_contain": case["must_not_contain"], "present": leaked}})
            present = case["must_contain"] in t["evidence_text"]
            checks.append({"scope": scope.as_dict(), "mode": mode, "check_id": "own_marker_in_selected_evidence",
                           "status": "passed" if present else "failed",
                           "observed": {"must_contain": case["must_contain"], "present": present}})
    write_jsonl(out_dir / "answers.jsonl", answers)
    write_jsonl(out_dir / "traces.jsonl", traces)
    return {"run_id": run_id, "checks": checks, "failures": [c for c in checks if c["status"] != "passed"]}


def run_isolation() -> dict:
    imported = phase_import()
    proc = subprocess.run([sys.executable, "-m", "potency.entrypoints.cli", "isolation-query"], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"isolation query subprocess failed: {proc.stderr[-2000:]}")
    query = json.loads((OUT / "_query-result.json").read_text(encoding="utf-8"))
    report = {
        "fixture": "isolation-fixture-v1",
        "collision": json.loads(CASES_PATH.read_text(encoding="utf-8"))["local_identity_collision"],
        "store": "single shared Postgres database; isolation enforced by row-level security on (tenant_id, project_id)",
        "restart": "query phase executed in a separate OS process after import (fresh connection pool and caches)",
        "import_phase": imported,
        "query_run_id": query["run_id"],
        "checks": query["checks"],
        "failures": query["failures"],
        "passed": not query["failures"],
        "commands": ["uv run potency isolation"],
        "artifacts": {"answers": "isolation/answers.jsonl", "traces": "isolation/traces.jsonl"},
    }
    (OUT / "_query-result.json").unlink(missing_ok=True)
    write_json(OUT / "report.json", report)
    return report


def query_entrypoint() -> None:
    res = phase_query()
    write_json(OUT / "_query-result.json", res)
