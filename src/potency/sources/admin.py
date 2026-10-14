"""Scope-level administration: full deletion of one scope's rows (RLS-limited) and record export."""

import json
from pathlib import Path

from potency.db import scoped_session
from potency.tenancy import Scope

DELETE_ORDER = [
    "evidence_ref", "kg_edge", "kg_node", "graph_build", "extraction_coverage", "extraction_result", "extraction_run",
    "embedding_cache", "passage_chunk", "index_version", "answer", "trace", "run_record", "usage_ledger",
    "import_event", "import_run", "passage_text_revision", "passage", "source_version", "source_document",
]


def delete_scope(scope: Scope) -> dict:
    """Delete every row of one scope. Row-level security guarantees no other scope is touched."""
    counts = {}
    with scoped_session(scope) as conn:
        conn.execute("UPDATE source_version SET superseded_by = NULL")
        for table in DELETE_ORDER:
            counts[table] = conn.execute(f"DELETE FROM {table}").rowcount
    return counts


def export_source_records(scope: Scope, path: Path) -> list[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with scoped_session(scope) as conn:
        rows = conn.execute(
            "SELECT p.record, p.is_current, v.lifecycle, v.superseded_by FROM passage p "
            "JOIN source_version v ON v.id = p.version_id ORDER BY p.id"
        ).fetchall()
    out = []
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            rec = dict(r["record"])
            rec["_store_state"] = {"is_current": r["is_current"], "version_lifecycle": r["lifecycle"],
                                   "superseded_by": r["superseded_by"]}
            out.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out
