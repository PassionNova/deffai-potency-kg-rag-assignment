import json

import pytest

from potency.db import scoped_session
from potency.graph.export import export_graph
from potency.tenancy import Scope
from potency.workflows import isolation
from potency.workflows.io import read_jsonl


@pytest.fixture(scope="module")
def isolated(tmp_path_factory):
    imported = isolation.phase_import()
    out = tmp_path_factory.mktemp("iso")
    result = isolation.phase_query(out_dir=out)
    return imported, result, out


def test_isolation_fixture_containment_both_modes(isolated):
    _, result, _ = isolated
    modes = {c["mode"] for c in result["checks"]}
    assert {"vector", "graph"} <= modes
    assert result["failures"] == [], result["failures"]


def test_same_identifiers_resolve_to_scope_local_rows(isolated):
    cases = json.loads(isolation.CASES_PATH.read_text(encoding="utf-8"))
    for case in cases["assertions"]:
        scope = Scope.from_dict(case["query_scope"])
        with scoped_session(scope) as conn:
            texts = [r["text"] for r in conn.execute("SELECT text FROM passage").fetchall()]
        assert texts and all(case["must_not_contain"] not in t for t in texts)
        assert any(case["must_contain"] in t for t in texts)


def test_graph_endpoints_and_traversals_are_valid(isolated, tmp_path):
    _, result, out = isolated
    traces = read_jsonl(out / "traces.jsonl")
    cases = json.loads(isolation.CASES_PATH.read_text(encoding="utf-8"))
    for case in cases["assertions"]:
        scope = Scope.from_dict(case["query_scope"])
        exp = export_graph(scope, tmp_path / scope.project_id)
        nodes = {n["id"]: n for n in read_jsonl(tmp_path / scope.project_id / "nodes.jsonl")}
        edges = {e["id"]: e for e in read_jsonl(tmp_path / scope.project_id / "edges.jsonl")}
        assert exp["nodes"] == len(nodes) and exp["edges"] == len(edges)
        assert any(n["type"] == "SourceDocument" for n in nodes.values())
        for e in edges.values():
            assert e["source"] in nodes and e["target"] in nodes, e["id"]
            assert e["scope"] == scope.as_dict()
            if e["evidence_bearing"]:
                assert e["source_refs"], e["id"]
                assert e["review_state"] == "unreviewed"
                assert e["actor"] and e["version"] and e["created_at"]
        for n in nodes.values():
            if n["type"] == "Assertion":
                assert n["source_refs"] and n["review_state"] == "unreviewed"
        for t in traces:
            if t["mode"] != "graph" or t["scope"] != scope.as_dict():
                continue
            assert t["seed_node_ids"], "graph mode must start from seeds"
            assert set(t["seed_node_ids"]) <= set(nodes)
            reached = set(t["seed_node_ids"])
            for step in t["traversed_edges"]:
                assert step["edge_id"] in edges
                e = edges[step["edge_id"]]
                assert {step["from"], step["to"]} == {e["source"], e["target"]}
                assert step["from"] in reached, "traversal must expand from an already reached node"
                reached.add(step["to"])
            assert t["traversal_limits"]["max_hops"] >= 1
            assert t["evidence_chars"] == len(t["evidence_text"]) <= 6000
