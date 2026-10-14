import json

import pytest

from potency.evaluation import runner
from potency.evaluation.scoring import aggregate, score_case
from potency.workflows.io import read_jsonl, write_jsonl

CASE = {
    "case_id": "T1", "case_version": 1, "split": "dev", "origin": "candidate_authored", "risk": "redaction",
    "question": "q?",
    "expected_evidence_refs": [[{"source_id": "s1", "passage_id": "s1:p1"}, {"source_id": "s2", "passage_id": "s2:p9"}]],
    "answer_checks": {"required_patterns": ["(?i)redact"], "forbidden_patterns": ["\\d+%"],
                      "expect_not_found": True, "min_verified_citations": 1},
}


def _trace(passages):
    return {"trace_id": "t", "run_id": "r", "selected_passages": [{"source_id": s, "passage_id": p} for s, p in passages],
            "candidate_scores": [{"passage_pk": f"x/y/{s}@v#{p}"} for s, p in passages], "model": "m",
            "embedding_model": "e", "is_test_double": {"generator": False, "embedder": False}}


def _answer(text, cites=1, failures=0, not_found=None):
    return {"answer": text, "not_found": not_found or [], "claims": [{"text": text}], "conflicts": [],
            "citations": [{"source_id": "s1"}] * cites,
            "citation_check": {"claims": 1, "verified_claims": 1 if cites else 0, "citations_verified": cites,
                               "citation_failures": failures}}


def test_case_scores_are_deterministic_and_separate():
    t = _trace([("s9", "s9:p1"), ("s2", "s2:p9")])
    a = _answer("The criterion is redacted as (b)(4).", not_found=["numeric value"])
    r1, r2 = score_case(CASE, t, a), score_case(CASE, t, a)
    assert r1 == r2
    status, scores, _ = r1
    assert status == "completed"
    assert scores["retrieval"]["all_groups_hit"] and scores["retrieval"]["first_hit_rank"] == 2
    assert scores["answer"]["all_checks_passed"]


def test_forbidden_and_missing_are_failures_not_passes():
    status, scores, _ = score_case(CASE, _trace([("s9", "s9:p1")]), _answer("It is 85%.", cites=0))
    assert status == "completed"
    assert not scores["retrieval"]["all_groups_hit"]
    assert not scores["answer"]["all_checks_passed"]
    assert score_case(CASE, None, None)[0] == "failed"
    assert score_case(CASE, _trace([]), {"answer": "", "not_found": []})[0] == "unscorable"


def test_absence_case_retrieval_is_not_applicable():
    case = {**CASE, "expected_evidence_refs": []}
    _, scores, _ = score_case(case, _trace([("s1", "s1:p1")]), _answer("Not found: redacted"))
    assert scores["retrieval"]["applicable"] is False and scores["retrieval"]["group_recall"] is None
    agg = aggregate([{"status": "completed", "scores": scores}])
    assert agg["retrieval"]["all_expected_groups_in_packed_evidence"]["denominator"] == 0
    assert agg["retrieval"]["all_expected_groups_in_packed_evidence"]["rate"] is None


@pytest.fixture
def eval_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(runner, "CASES", tmp_path / "cases.jsonl")
    monkeypatch.setattr(runner, "FREEZE", tmp_path / "holdout-freeze.json")
    hold = {**CASE, "case_id": "H1", "split": "holdout"}
    write_jsonl(tmp_path / "cases.jsonl", [CASE, hold])
    answers, traces = [], []
    for c in (CASE, hold):
        for mode in runner.MODES:
            traces.append({**_trace([("s1", "s1:p1")]), "eval_case_id": c["case_id"], "mode": mode})
            answers.append({**_answer("redacted"), "eval_case_id": c["case_id"], "mode": mode})
    write_jsonl(tmp_path / "answers.jsonl", answers)
    write_jsonl(tmp_path / "traces.jsonl", traces)
    return tmp_path


def test_offline_rescore_is_repeatable(eval_dir):
    r1 = runner.score_saved(rescore=True)
    rows1 = read_jsonl(eval_dir / "results.jsonl")
    r2 = runner.score_saved(rescore=True)
    rows2 = read_jsonl(eval_dir / "results.jsonl")
    assert rows1 == rows2
    strip = lambda r: {k: v for k, v in r.items() if k != "visible_supplied_regression"}  # noqa: E731
    assert json.dumps(strip(r1), sort_keys=True) == json.dumps(strip(r2), sort_keys=True)
    assert runner.score_saved(rescore=False)["overall"] == r1["overall"]
    assert len(rows1) == 4 and all(r["status"] == "completed" for r in rows1)
    for field in ("case_id", "case_version", "split", "run_id", "status", "retrieved_passage_ids",
                  "expected_evidence_refs", "scores", "errors", "trace_id"):
        assert field in rows1[0]


def test_holdout_freeze_detects_change(eval_dir):
    assert runner.freeze_holdout()["status"] == "frozen"
    assert runner.freeze_holdout()["status"] == "already_frozen"
    cases = read_jsonl(eval_dir / "cases.jsonl")
    cases[1]["question"] = "tuned after freeze"
    write_jsonl(eval_dir / "cases.jsonl", cases)
    with pytest.raises(runner.HoldoutFreezeError):
        runner.freeze_holdout()
