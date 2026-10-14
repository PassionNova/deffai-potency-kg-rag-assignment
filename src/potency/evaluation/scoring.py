"""Deterministic per-case scoring from saved traces/answers. Retrieval and answer scores are computed separately;
a metric that cannot be computed is reported as None with a reason, never as a pass."""

import re

SCORER_VERSION = "scorer-v1"


def _ref_key(ref: dict) -> tuple[str, str]:
    return ref["source_id"], ref["passage_id"]


def retrieved_refs(trace: dict) -> list[tuple[str, str]]:
    return [(p["source_id"], p["passage_id"]) for p in trace.get("selected_passages") or []]


def candidate_refs(trace: dict) -> list[str]:
    return [c["passage_pk"].split("#", 1)[1] for c in trace.get("candidate_scores") or [] if "#" in c["passage_pk"]]


def score_retrieval(case: dict, trace: dict) -> dict:
    groups = case.get("expected_evidence_refs") or []
    if not groups:
        return {"applicable": False, "reason": "case has no expected evidence passage (absence case)",
                "group_recall": None, "all_groups_hit": None, "first_hit_rank": None,
                "candidate_group_recall": None}
    packed = retrieved_refs(trace)
    packed_set = set(packed)
    cands = candidate_refs(trace)
    hits, first_rank, cand_hits = 0, None, 0
    for g in groups:
        keys = {_ref_key(r) for r in g}
        if keys & packed_set:
            hits += 1
            rank = min(i + 1 for i, k in enumerate(packed) if k in keys)
            first_rank = rank if first_rank is None else min(first_rank, rank)
        if {k[1] for k in keys} & set(cands):
            cand_hits += 1
    return {
        "applicable": True,
        "groups": len(groups),
        "groups_hit": hits,
        "group_recall": round(hits / len(groups), 4),
        "all_groups_hit": hits == len(groups),
        "first_hit_rank": first_rank,
        "packed_passages": len(packed),
        "candidate_group_recall": round(cand_hits / len(groups), 4),
    }


def _answer_text(answer: dict) -> str:
    parts = [answer.get("answer") or "", answer.get("uncertainty") or ""]
    parts += [str(x) for x in answer.get("not_found") or []]
    parts += [c.get("description", "") if isinstance(c, dict) else str(c) for c in answer.get("conflicts") or []]
    parts += [c.get("text", "") for c in answer.get("claims") or []]
    return "\n".join(parts)


def score_answer(case: dict, answer: dict) -> dict:
    checks = case.get("answer_checks") or {}
    text = _answer_text(answer)
    results: dict[str, bool] = {}
    for i, pat in enumerate(checks.get("required_patterns") or []):
        results[f"required_{i}"] = re.search(pat, text) is not None
    for i, pat in enumerate(checks.get("forbidden_patterns") or []):
        results[f"forbidden_{i}_absent"] = re.search(pat, answer.get("answer") or "") is None
    if checks.get("expect_not_found"):
        results["not_found_or_absence_reported"] = bool(answer.get("not_found")) or results.get("required_0", False)
    summary = answer.get("citation_check") or {}
    n_cites = summary.get("citations_verified", 0)
    results["min_verified_citations"] = n_cites >= checks.get("min_verified_citations", 0)
    results["no_citation_failures"] = summary.get("citation_failures", 0) == 0
    if checks.get("required_cited_source_ids"):
        cited = {c["source_id"] for c in answer.get("citations") or []}
        results["required_cited_sources"] = set(checks["required_cited_source_ids"]) <= cited
    passed = sum(results.values())
    return {
        "checks": results,
        "checks_passed": passed,
        "checks_total": len(results),
        "all_checks_passed": passed == len(results),
        "claims": summary.get("claims", 0),
        "verified_claims": summary.get("verified_claims", 0),
        "citations_verified": n_cites,
        "citation_failures": summary.get("citation_failures", 0),
    }


def score_case(case: dict, trace: dict | None, answer: dict | None) -> tuple[str, dict, list[str]]:
    """Returns (status, scores, errors) with status in completed | failed | unscorable."""
    errors: list[str] = []
    if trace is None or answer is None:
        return "failed", {}, ["no trace/answer saved for this case"]
    if not (answer.get("answer") or "").strip() and not answer.get("not_found"):
        return "unscorable", {"retrieval": score_retrieval(case, trace)}, ["empty generated answer"]
    return "completed", {"retrieval": score_retrieval(case, trace), "answer": score_answer(case, answer)}, errors


def _rate(num: int, den: int) -> dict:
    return {"numerator": num, "denominator": den, "rate": round(num / den, 4) if den else None}


def aggregate(rows: list[dict]) -> dict:
    out: dict = {"cases": len(rows), "status_counts": {}}
    for r in rows:
        out["status_counts"][r["status"]] = out["status_counts"].get(r["status"], 0) + 1
    ret = [r["scores"]["retrieval"] for r in rows if r["scores"].get("retrieval", {}).get("applicable")]
    ret_na = sum(1 for r in rows if r["scores"].get("retrieval") and not r["scores"]["retrieval"]["applicable"])
    out["retrieval"] = {
        "all_expected_groups_in_packed_evidence": _rate(sum(1 for s in ret if s["all_groups_hit"]), len(ret)),
        "mean_group_recall": round(sum(s["group_recall"] for s in ret) / len(ret), 4) if ret else None,
        "expected_in_candidate_pool": _rate(sum(1 for s in ret if s["candidate_group_recall"] == 1.0), len(ret)),
        "not_applicable_cases": ret_na,
        "failed_or_missing_cases": sum(1 for r in rows if r["status"] == "failed"),
    }
    ans = [r["scores"]["answer"] for r in rows if r["status"] == "completed"]
    out["answer"] = {
        "all_checks_passed": _rate(sum(1 for s in ans if s["all_checks_passed"]), len(rows)),
        "checks_passed": _rate(sum(s["checks_passed"] for s in ans), sum(s["checks_total"] for s in ans)),
        "verified_claims": _rate(sum(s["verified_claims"] for s in ans), sum(s["claims"] for s in ans)),
        "answers_with_citation_failures": _rate(sum(1 for s in ans if s["citation_failures"]), len(ans)),
        "denominator_note": "all_checks_passed uses every case (failed/unscorable count as not passed)",
    }
    return out
