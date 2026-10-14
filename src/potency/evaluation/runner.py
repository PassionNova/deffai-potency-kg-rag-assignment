"""Regression evaluation: freeze holdout -> run cases through the normal answering service -> score offline.

`score_saved` recomputes every per-case score and aggregate from saved traces/answers without model calls."""

import hashlib
import json

from potency import ids
from potency.answering.service import answer_question, code_version
from potency.evaluation.scoring import SCORER_VERSION, aggregate, retrieved_refs, score_case
from potency.workflows.config import case_context_text, load_case_config
from potency.workflows.io import RESULTS, read_jsonl, write_json, write_jsonl

EVAL_DIR = RESULTS / "evaluation"
CASES = EVAL_DIR / "cases.jsonl"
FREEZE = EVAL_DIR / "holdout-freeze.json"
MODES = ("graph", "vector")
EVIDENCE_BUDGET = 6000


class HoldoutFreezeError(RuntimeError):
    pass


def load_cases() -> list[dict]:
    return read_jsonl(CASES)


def _canonical_sha(rows: list[dict]) -> str:
    blob = "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in sorted(rows, key=lambda r: r["case_id"]))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def freeze_holdout() -> dict:
    holdout = [c for c in load_cases() if c["split"] == "holdout"]
    sha = _canonical_sha(holdout)
    if FREEZE.exists():
        prior = json.loads(FREEZE.read_text(encoding="utf-8"))
        if prior["sha256"] != sha:
            raise HoldoutFreezeError(
                "holdout cases changed after freeze; delete holdout-freeze.json deliberately and disclose the change"
            )
        return {**prior, "status": "already_frozen"}
    rec = {
        "frozen_at": ids.utcnow().isoformat(),
        "sha256": sha,
        "hash_method": "sha256 over sorted canonical JSON lines (sort_keys) of split=holdout cases",
        "case_ids": [c["case_id"] for c in holdout],
        "code_version": code_version(),
        "note": "Frozen before the final evaluation run. Holdout cases are not used for prompt/retrieval tuning.",
    }
    write_json(FREEZE, rec)
    return {**rec, "status": "frozen"}


def _check_freeze() -> dict:
    if not FREEZE.exists():
        raise HoldoutFreezeError("holdout not frozen; run `potency eval freeze` before running holdout cases")
    prior = json.loads(FREEZE.read_text(encoding="utf-8"))
    sha = _canonical_sha([c for c in load_cases() if c["split"] == "holdout"])
    if prior["sha256"] != sha:
        raise HoldoutFreezeError("holdout cases differ from the frozen hash")
    return prior


def run_eval(split: str = "all", config: str = "configs/casgevy-potency.json") -> dict:
    cases = [c for c in load_cases() if split == "all" or c["split"] == split]
    if any(c["split"] == "holdout" for c in cases):
        _check_freeze()
    cfg = load_case_config(config)
    ctx = case_context_text(cfg.case)
    run_id = ids.new_run_id("eval")
    keep = {c["case_id"] for c in cases}
    answers = [a for a in read_jsonl(EVAL_DIR / "answers.jsonl") if a.get("eval_case_id") not in keep]
    traces = [t for t in read_jsonl(EVAL_DIR / "traces.jsonl") if t.get("eval_case_id") not in keep]
    errors: dict[tuple[str, str], str] = {}
    for c in cases:
        for mode in MODES:
            try:
                a, t = answer_question(
                    cfg.scope, question_id=c["case_id"], question=c["question"], mode=mode, run_id=run_id,
                    corpus_snapshot=cfg.corpus_snapshot, case_context=ctx, budget=EVIDENCE_BUDGET,
                    extra={"eval_case_id": c["case_id"], "eval_split": c["split"], "case_version": c["case_version"]},
                )
                answers.append(a)
                traces.append(t)
            except Exception as e:  # recorded as a failed case, never dropped
                errors[(c["case_id"], mode)] = f"{type(e).__name__}: {e}"
    write_jsonl(EVAL_DIR / "answers.jsonl", answers)
    write_jsonl(EVAL_DIR / "traces.jsonl", traces)
    report = score_saved(rescore=True, run_errors=errors, last_run_id=run_id)
    return {"run_id": run_id, "cases": len(cases), "modes": list(MODES), "errors": len(errors),
            "summary": report["by_split_mode"]}


def _index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r.get("eval_case_id"), r["mode"]): r for r in rows}


def score_saved(rescore: bool = True, run_errors: dict | None = None, last_run_id: str | None = None) -> dict:
    cases = load_cases()
    cases_sha = _canonical_sha(cases)
    if rescore:
        answers = _index(read_jsonl(EVAL_DIR / "answers.jsonl"))
        traces = _index(read_jsonl(EVAL_DIR / "traces.jsonl"))
        prior = {(r["case_id"], r["mode"]): r for r in read_jsonl(EVAL_DIR / "results.jsonl")}
        rows = []
        for c in cases:
            for mode in MODES:
                key = (c["case_id"], mode)
                t, a = traces.get(key), answers.get(key)
                status, scores, errs = score_case(c, t, a)
                if run_errors and key in run_errors:
                    errs = [run_errors[key], *errs]
                elif status == "failed" and key in prior:
                    errs = prior[key].get("errors") or errs
                rows.append({
                    "case_id": c["case_id"],
                    "case_version": c["case_version"],
                    "split": c["split"],
                    "origin": c["origin"],
                    "risk": c["risk"],
                    "mode": mode,
                    "run_id": (t or {}).get("run_id"),
                    "status": status,
                    "retrieved_passage_ids": [p for _, p in retrieved_refs(t)] if t else [],
                    "expected_evidence_refs": c["expected_evidence_refs"],
                    "scores": scores,
                    "errors": errs,
                    "trace_id": (t or {}).get("trace_id"),
                    "model": (t or {}).get("model"),
                    "embedding_model": (t or {}).get("embedding_model"),
                    "is_test_double": (t or {}).get("is_test_double"),
                    "evidence_chars": (t or {}).get("evidence_chars"),
                    "scorer_version": SCORER_VERSION,
                    "cases_sha256": cases_sha,
                })
        write_jsonl(EVAL_DIR / "results.jsonl", rows)
    else:
        rows = read_jsonl(EVAL_DIR / "results.jsonl")

    by_split_mode = {}
    for split in ("dev", "holdout"):
        for mode in MODES:
            sel = [r for r in rows if r["split"] == split and r["mode"] == mode]
            if sel:
                by_split_mode[f"{split}/{mode}"] = aggregate(sel)
    failures = []
    for r in rows:
        if r["status"] != "completed":
            failures.append({"case_id": r["case_id"], "mode": r["mode"], "status": r["status"], "errors": r["errors"]})
            continue
        ret, ans = r["scores"]["retrieval"], r["scores"]["answer"]
        failed_checks = [k for k, v in ans["checks"].items() if not v]
        if failed_checks or (ret["applicable"] and not ret["all_groups_hit"]):
            failures.append({"case_id": r["case_id"], "mode": r["mode"], "failed_answer_checks": failed_checks,
                             "retrieval_all_groups_hit": ret.get("all_groups_hit"),
                             "retrieved_passage_ids": r["retrieved_passage_ids"]})
    report = {
        "scorer_version": SCORER_VERSION,
        "cases_path": "evaluation/cases.jsonl",
        "cases_sha256": cases_sha,
        "holdout_freeze": json.loads(FREEZE.read_text(encoding="utf-8")) if FREEZE.exists() else None,
        "run_ids": sorted({r["run_id"] for r in rows if r.get("run_id")}),
        "last_run_id": last_run_id,
        "models": sorted({r["model"] for r in rows if r.get("model")}),
        "embedding_models": sorted({r["embedding_model"] for r in rows if r.get("embedding_model")}),
        "any_test_double": any((r.get("is_test_double") or {}).get("generator") or
                               (r.get("is_test_double") or {}).get("embedder") for r in rows),
        "evidence_budget_chars": EVIDENCE_BUDGET,
        "by_split_mode": by_split_mode,
        "overall": aggregate(rows) if rows else None,
        "case_failures": failures,
        "visible_supplied_regression": _visible_supplied(),
        "recompute_command": "potency eval score   # recomputes results.jsonl scores + this report from saved "
                             "evaluation/answers.jsonl and traces.jsonl; no model calls",
        "limitations": [
            "Candidate-authored labels: expected passages and regex checks were written by the implementer after "
            "reading the corpus; they are provisional and not expert-confirmed.",
            "Regex answer checks detect required/forbidden statements, not overall answer correctness or completeness.",
            "Six cases x two modes is a regression smoke set, not an accuracy estimate; the holdout is small and "
            "authored by the same person as the system.",
        ],
        "failure_analysis_path": "evaluation/failure-analysis.md",
    }
    write_json(EVAL_DIR / "report.json", report)
    return report


def _visible_supplied() -> dict:
    """Supplied behavior fixtures are visible acceptance cases; reported separately, never mixed into the holdout."""
    path = RESULTS / "fixtures" / "report.json"
    if not path.exists():
        return {"origin": "visible_supplied", "status": "not_run", "source": "fixtures/report.json"}
    rep = json.loads(path.read_text(encoding="utf-8"))
    checks = rep.get("checks") or []
    counts: dict[str, int] = {}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {"origin": "visible_supplied", "source": "fixtures/report.json", "check_status_counts": counts,
            "checks_total": len(checks), "test_doubles_used": (rep.get("execution") or {}).get("test_doubles_used")}
