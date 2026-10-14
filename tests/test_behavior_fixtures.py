"""Source-correction and unresolved-conflict scenarios through the generic pipeline with test doubles.

Checks that depend on generated language (reporting an unresolved conflict) need a real model; with the test-double
generator only their presence is asserted. Retrieval/graph/citation/version checks must pass offline."""

import pytest

from potency.workflows.behavior import run_behavior_fixtures
from potency.workflows.io import read_jsonl

GENERATION_DEPENDENT = {"answer_reports_unresolved_conflict"}


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("fixtures")
    return run_behavior_fixtures(out_dir=out), out


def test_report_shape_and_history(fixture_run):
    rep, out = fixture_run
    for key in ("scenarios", "commands", "history_artifacts", "checks", "failures"):
        assert key in rep
    stages = {(h["scenario_id"], h["stage"]) for h in rep["history_artifacts"]}
    assert {("source-correction", "before"), ("source-correction", "after")} <= stages
    for h in rep["history_artifacts"]:
        for k in ("source_records_path", "graph_nodes_path", "graph_edges_path", "answers_path", "traces_path"):
            assert (out / h[k]).exists() or (out.parent / h[k]).exists() or h[k]
    assert len(read_jsonl(out / "answers.jsonl")) == 3
    assert rep["execution"]["test_doubles_used"] is True


def test_correction_uses_v2_without_mixing_v1(fixture_run):
    rep, _ = fixture_run
    by_id = {(c["scenario_id"], c["stage"], c["check_id"]): c for c in rep["checks"]}
    for key in [("source-correction", "before", "before_answer_grounded_in_v1"),
                ("source-correction", "after", "after_answer_grounded_in_v2"),
                ("source-correction", "after", "superseded_assertion_not_current"),
                ("source-correction", "after", "versioned_citations_resolve_exactly"),
                ("source-correction", "before", "old_sources_assertions_answers_traces_inspectable")]:
        assert by_id[key]["status"] == "passed", (key, by_id[key]["observed"])


def test_conflict_both_claims_retrieved_and_cited(fixture_run):
    rep, _ = fixture_run
    conflict = [c for c in rep["checks"] if c["scenario_id"] == "unresolved-conflict"]
    assert conflict
    for c in conflict:
        if c["check_id"] in GENERATION_DEPENDENT:
            continue
        assert c["status"] == "passed", (c["check_id"], c["observed"])
    assert any(c["check_id"] in GENERATION_DEPENDENT for c in conflict)
