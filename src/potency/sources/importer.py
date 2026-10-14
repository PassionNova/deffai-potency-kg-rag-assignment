"""Idempotent, scope-checked import of normalized passage records with a reconciled ledger."""

import json
from collections import defaultdict
from pathlib import Path

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from potency import ids
from potency.db import scoped_session
from potency.settings import REPO_ROOT
from potency.sources.records import SourceRecord
from potency.tenancy import Scope

OUTCOMES = ("imported", "unchanged", "updated", "failed", "omitted")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read(paths: list[Path]) -> tuple[list[tuple[str, SourceRecord]], list[dict], list[dict]]:
    """Parse inputs. Returns (valid records with key, failures, input descriptors)."""
    valid: list[tuple[str, SourceRecord]] = []
    failures: list[dict] = []
    inputs: list[dict] = []
    for path in map(Path, paths):
        lines = path.read_text(encoding="utf-8").splitlines()
        inputs.append({"path": _rel(path), "sha256": ids.sha256_file(path), "lines": len(lines)})
        for lineno, line in enumerate(lines, start=1):
            loc = f"{_rel(path)}:{lineno}"
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                failures.append({"record_key": loc, "source_id": None, "reason": f"invalid_json: {exc.msg}"})
                continue
            try:
                rec = SourceRecord.model_validate(raw)
            except ValidationError as exc:
                failures.append(
                    {
                        "record_key": loc,
                        "source_id": raw.get("source_id"),
                        "reason": f"schema_invalid: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}",
                    }
                )
                continue
            valid.append((loc, rec))
    return valid, failures, inputs


def import_records(
    paths: list[Path],
    *,
    expected_scope: Scope,
    expected_snapshot: str | None = None,
    manifest: dict | None = None,
    corrections: dict | None = None,
    supersedes: list[dict] | None = None,
    replaces_with: list[dict] | None = None,
    run_label: str = "import",
) -> dict:
    run_id = ids.new_run_id(run_label)
    started = ids.utcnow()
    parsed, failures, inputs = _read(paths)

    events: list[dict] = [dict(f, outcome="failed") for f in failures]
    accepted: list[SourceRecord] = []
    seen: set[str] = set()
    for loc, rec in parsed:
        reason = None
        if rec.scope_obj != expected_scope:
            reason = f"scope_mismatch: record scope {rec.scope_obj} != configured {expected_scope}"
        elif ids.sha256_text(rec.text) != rec.text_sha256:
            reason = "text_sha256_mismatch"
        elif expected_snapshot and rec.corpus_snapshot != expected_snapshot:
            reason = f"snapshot_mismatch: {rec.corpus_snapshot} != {expected_snapshot}"
        if reason:
            events.append({"record_key": loc, "source_id": rec.source_id, "reason": reason, "outcome": "failed"})
            continue
        if rec.pk in seen:
            events.append(
                {"record_key": loc, "source_id": rec.source_id, "reason": "duplicate_identity_in_input", "outcome": "omitted"}
            )
            continue
        seen.add(rec.pk)
        accepted.append(rec)

    ordinals: dict[str, int] = defaultdict(int)
    with scoped_session(expected_scope) as conn:
        conn.execute(
            "INSERT INTO import_run (id, tenant_id, project_id, corpus_snapshot, started_at, inputs) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, expected_scope.tenant_id, expected_scope.project_id, expected_snapshot, started, Jsonb(inputs)),
        )
        for rec in accepted:
            t, p = rec.scope.tenant_id, rec.scope.project_id
            doc_id = ids.document_pk(t, p, rec.source_id)
            ver_id = ids.version_pk(t, p, rec.source_id, rec.source_version)
            ordinal = ordinals[ver_id]
            ordinals[ver_id] += 1
            conn.execute(
                """
                INSERT INTO source_document (id, tenant_id, project_id, source_id, title, source_type, authority,
                    status, issuer, source_url, filename, corpus_layer, subject_scope)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, project_id, source_id) DO UPDATE SET
                    title = EXCLUDED.title, source_type = EXCLUDED.source_type, authority = EXCLUDED.authority,
                    status = EXCLUDED.status, issuer = EXCLUDED.issuer, source_url = EXCLUDED.source_url,
                    filename = EXCLUDED.filename, corpus_layer = EXCLUDED.corpus_layer,
                    subject_scope = EXCLUDED.subject_scope
                WHERE (source_document.title, source_document.authority, source_document.status,
                       source_document.filename, source_document.subject_scope)
                   IS DISTINCT FROM (EXCLUDED.title, EXCLUDED.authority, EXCLUDED.status,
                       EXCLUDED.filename, EXCLUDED.subject_scope)
                """,
                (doc_id, t, p, rec.source_id, rec.title, rec.source_type, rec.authority, rec.status, rec.issuer,
                 rec.source_url, rec.filename, rec.corpus_layer, Jsonb(rec.subject_scope)),
            )
            conn.execute(
                """
                INSERT INTO source_version (id, tenant_id, project_id, source_id, source_version, document_id,
                    corpus_snapshot, issued, retrieved_at, authority, status, import_run_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, project_id, source_id, source_version) DO NOTHING
                """,
                (ver_id, t, p, rec.source_id, rec.source_version, doc_id, rec.corpus_snapshot, rec.issued,
                 rec.retrieved_at, rec.authority, rec.status, run_id),
            )
            row = conn.execute("SELECT text_sha256, text FROM passage WHERE id = %s", (rec.pk,)).fetchone()
            record_json = Jsonb(rec.model_dump(mode="json"))
            if row is None:
                conn.execute(
                    """
                    INSERT INTO passage (id, tenant_id, project_id, source_id, source_version, passage_id, version_id,
                        corpus_snapshot, ordinal, text, text_sha256, locator, page, section, authority, status,
                        subject_scope, record, last_import_run_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (rec.pk, t, p, rec.source_id, rec.source_version, rec.passage_id, ver_id, rec.corpus_snapshot,
                     ordinal, rec.text, rec.text_sha256, Jsonb(rec.locator), rec.page, rec.section, rec.authority,
                     rec.status, Jsonb(rec.subject_scope), record_json, run_id),
                )
                outcome, reason = "imported", None
            elif row["text_sha256"] == rec.text_sha256:
                outcome, reason = "unchanged", None
            else:
                conn.execute(
                    "INSERT INTO passage_text_revision (passage_fk, tenant_id, project_id, text_sha256, text, import_run_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (rec.pk, t, p, row["text_sha256"], row["text"], run_id),
                )
                conn.execute(
                    "UPDATE passage SET text=%s, text_sha256=%s, record=%s, locator=%s, corpus_snapshot=%s, "
                    "last_import_run_id=%s WHERE id=%s",
                    (rec.text, rec.text_sha256, record_json, Jsonb(rec.locator), rec.corpus_snapshot, run_id, rec.pk),
                )
                outcome, reason = "updated", f"text changed from {row['text_sha256'][:12]} (prior text archived)"
            events.append({"record_key": rec.pk, "source_id": rec.source_id, "outcome": outcome, "reason": reason})

        supersession = []
        if supersedes:
            supersession = _apply_supersession(conn, expected_scope, supersedes, replaces_with or [], run_id)

        counts = {k: sum(1 for e in events if e["outcome"] == k) for k in OUTCOMES}
        records_read = sum(i["lines"] for i in inputs)
        counts["records_read"] = records_read
        counts["reconciled"] = sum(counts[k] for k in OUTCOMES) == records_read
        conn.cursor().executemany(
            "INSERT INTO import_event (run_id, tenant_id, project_id, record_key, source_id, outcome, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(run_id, expected_scope.tenant_id, expected_scope.project_id, e["record_key"], e.get("source_id"),
              e["outcome"], e.get("reason")) for e in events],
        )

        per_source: dict[str, dict] = defaultdict(lambda: {k: 0 for k in OUTCOMES})
        for e in events:
            per_source[e.get("source_id") or "<unparsed>"][e["outcome"]] += 1
        per_source = {k: dict(v) for k, v in sorted(per_source.items())}

        manifest_check = _check_manifest(conn, manifest, inputs, per_source) if manifest else None
        corrections_check = _check_corrections(conn, expected_scope, corrections) if corrections else None

        report = {
            "run_id": run_id,
            "scope": expected_scope.as_dict(),
            "corpus_snapshot": expected_snapshot,
            "inputs": inputs,
            "counts": counts,
            "per_source": per_source,
            "errors": [e for e in events if e["outcome"] in ("failed", "omitted")],
            "manifest_check": manifest_check,
            "corrections_check": corrections_check,
            "supersession": supersession,
            "started_at": started.isoformat(),
            "finished_at": ids.utcnow().isoformat(),
        }
        conn.execute(
            "UPDATE import_run SET finished_at = now(), counts = %s, report = %s WHERE id = %s",
            (Jsonb(counts), Jsonb(report), run_id),
        )
    return report


def _apply_supersession(conn, scope: Scope, old_refs: list[dict], new_refs: list[dict], run_id: str) -> list[dict]:
    """Apply an explicitly declared replacement: old source versions stop being current."""
    applied = []
    new_versions = {(r["source_id"], r["source_version"]) for r in new_refs}
    for ref in old_refs:
        if Scope.from_dict(ref["scope"]) != scope:
            raise ValueError(f"supersession ref outside configured scope: {ref}")
        old_ver = ids.version_pk(scope.tenant_id, scope.project_id, ref["source_id"], ref["source_version"])
        replacement = next((v for (s, v) in new_versions if s == ref["source_id"]), None)
        if replacement is None:
            raise ValueError(f"no replacement revision declared for {ref}")
        new_ver = ids.version_pk(scope.tenant_id, scope.project_id, ref["source_id"], replacement)
        exists = conn.execute(
            "SELECT 1 FROM source_version WHERE id = ANY(%s)", ([old_ver, new_ver],)
        ).fetchall()
        if len(exists) != 2:
            raise ValueError(f"supersession endpoints missing in store: {old_ver} -> {new_ver}")
        conn.execute(
            "UPDATE source_version SET lifecycle='superseded', superseded_by=%s, superseded_at=now() WHERE id=%s",
            (new_ver, old_ver),
        )
        conn.execute("UPDATE passage SET is_current=false WHERE version_id=%s", (old_ver,))
        applied.append({"superseded_version": old_ver, "replaced_by": new_ver, "import_run_id": run_id})
    return applied


def _check_manifest(conn, manifest: dict, inputs: list[dict], per_source: dict) -> dict:
    record_file = manifest.get("record_file")
    file_hash_ok = None
    for i in inputs:
        if record_file and i["path"].endswith(record_file):
            file_hash_ok = i["sha256"] == manifest.get("record_sha256")
    stored = {
        r["source_id"]: r
        for r in conn.execute(
            "SELECT source_id, count(*) AS n, min(source_version) AS ver FROM passage GROUP BY source_id"
        ).fetchall()
    }
    mismatches = []
    for src in manifest.get("sources", []):
        got = stored.get(src["source_id"])
        if got is None:
            mismatches.append({"source_id": src["source_id"], "issue": "missing_in_store"})
        elif got["n"] != src["passage_count"] or got["ver"] != src["source_version"]:
            mismatches.append(
                {"source_id": src["source_id"], "issue": "count_or_version_mismatch",
                 "expected": [src["passage_count"], src["source_version"]], "stored": [got["n"], got["ver"]]}
            )
    total = sum(r["n"] for r in stored.values())
    return {
        "record_file_sha256_matches": file_hash_ok,
        "expected_record_count": manifest.get("record_count"),
        "stored_record_count": total,
        "expected_source_count": manifest.get("source_count"),
        "stored_source_count": len(stored),
        "source_mismatches": mismatches,
        "ok": file_hash_ok is not False and not mismatches and total == manifest.get("record_count"),
    }


def _check_corrections(conn, scope: Scope, corrections: dict) -> list[dict]:
    out = []
    for entry in corrections.get("entries", []):
        pk = ids.passage_pk(scope.tenant_id, scope.project_id, entry["source_id"], entry["source_version"],
                            entry["passage_id"])
        row = conn.execute("SELECT text_sha256 FROM passage WHERE id=%s", (pk,)).fetchone()
        stored = row["text_sha256"] if row else None
        out.append(
            {
                "passage_id": entry["passage_id"],
                "kind": entry.get("kind"),
                "expected_corrected_sha256": entry["corrected_text_sha256"],
                "stored_sha256": stored,
                "corrected_text_in_store": stored == entry["corrected_text_sha256"],
                "previous_text_in_store": stored == entry.get("previous_text_sha256"),
            }
        )
    return out
