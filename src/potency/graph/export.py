"""Inspectable JSONL graph export (one JSON object per line, sorted by id)."""

import json
from pathlib import Path

from potency import ids
from potency.db import scoped_session
from potency.tenancy import Scope


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def export_graph(scope: Scope, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with scoped_session(scope) as conn:
        nodes = conn.execute("SELECT * FROM kg_node ORDER BY id").fetchall()
        edges = conn.execute("SELECT * FROM kg_edge ORDER BY id").fetchall()
    nodes_path, edges_path = out_dir / "nodes.jsonl", out_dir / "edges.jsonl"
    with nodes_path.open("w", encoding="utf-8", newline="\n") as fh:
        for n in nodes:
            fh.write(json.dumps({
                "id": n["id"], "type": n["type"], "label": n["label"], "scope": scope.as_dict(),
                "attributes": n["attributes"], "source_refs": n["source_refs"], "aliases": n["aliases"],
                "evidence_bearing": n["evidence_bearing"], "actor": n["actor"], "version": n["version"],
                "created_at": _iso(n["created_at"]), "review_state": n["review_state"],
                "is_current": n["is_current"], "graph_build_id": n["graph_build_id"],
            }, ensure_ascii=False, sort_keys=False) + "\n")
    with edges_path.open("w", encoding="utf-8", newline="\n") as fh:
        for e in edges:
            fh.write(json.dumps({
                "id": e["id"], "type": e["type"], "source": e["src"], "target": e["dst"], "scope": scope.as_dict(),
                "status": e["status"], "source_refs": e["source_refs"], "actor": e["actor"], "version": e["version"],
                "created_at": _iso(e["created_at"]), "review_state": e["review_state"], "attributes": e["attributes"],
                "evidence_bearing": e["evidence_bearing"], "is_current": e["is_current"],
                "graph_build_id": e["graph_build_id"],
            }, ensure_ascii=False) + "\n")
    return {
        "nodes_path": nodes_path.as_posix(), "edges_path": edges_path.as_posix(), "nodes": len(nodes),
        "edges": len(edges), "nodes_sha256": ids.sha256_file(nodes_path), "edges_sha256": ids.sha256_file(edges_path),
    }
