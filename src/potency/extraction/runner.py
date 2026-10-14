"""Corpus-wide relevance pass plus batched semantic extraction with per-passage coverage reporting.
Results are cached by (passage, text hash, extractor version, model) so re-runs repeat no paid calls."""

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from psycopg.types.json import Jsonb

from potency import ids
from potency.db import scoped_session
from potency.extraction.extractors import Extractor, PassageInput, get_extractor
from potency.extraction.validator import validate_assertions
from potency.observability.usage import BudgetExceeded, record_usage
from potency.settings import REPO_ROOT, get_settings
from potency.tenancy import Scope
from potency.textutil import collapse_ws

RULES_PATH = REPO_ROOT / "configs" / "extraction-rules.yaml"


def load_rules(path: Path = RULES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def relevance_score(text: str, authority: str | None, rules: dict) -> float:
    t = " " + collapse_ws(text).lower() + " "
    raw = sum(w * min(t.count(term.lower()), 3) for term, w in rules["terms"].items())
    length_norm = math.sqrt(1 + len(t) / 1500)
    weights = rules["authority_weights"]
    return round(raw / length_norm * weights.get(authority or "", weights["default"]), 3)


def select_passages(rows: list[dict], rules: dict) -> dict[str, tuple[bool, str | None]]:
    """Returns passage_pk -> (selected, skip_reason). Deterministic: ordered by score, then id."""
    decision: dict[str, tuple[bool, str | None]] = {}
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if len(collapse_ws(r["text"])) < rules["min_chars"]:
            decision[r["id"]] = (False, "too_short_for_semantic_content")
        else:
            by_source[r["source_id"]].append(r)
    candidates = []
    for items in by_source.values():
        items.sort(key=lambda r: (-r["score"], r["id"]))
        for rank, r in enumerate(items):
            forced = rank < rules["min_selected_per_source"] and r["score"] > 0
            if rank >= rules["max_selected_per_source"]:
                decision[r["id"]] = (False, "per_source_cap")
            elif r["score"] < rules["min_score"] and not forced:
                decision[r["id"]] = (False, "below_relevance_threshold")
            else:
                candidates.append((forced, r))
    candidates.sort(key=lambda fr: (not fr[0], -fr[1]["score"], fr[1]["id"]))
    for i, (_forced, r) in enumerate(candidates):
        decision[r["id"]] = (True, None) if i < rules["max_selected_total"] else (False, "total_budget_cap")
    for r in rows:
        decision.setdefault(r["id"], (False, "zero_relevance"))
    return decision


def run_extraction(scope: Scope, extractor: Extractor | None = None, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    ex = extractor or get_extractor()
    run_id = ids.new_run_id("extract")
    started = ids.utcnow()
    with scoped_session(scope) as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.source_id, p.text, p.text_sha256, p.authority, p.status, p.page, p.section,
                   p.subject_scope, d.title, d.source_type
            FROM passage p JOIN source_document d ON d.id = (SELECT document_id FROM source_version WHERE id = p.version_id)
            WHERE p.is_current ORDER BY p.id
            """
        ).fetchall()
        cached = {
            r["passage_fk"]: r
            for r in conn.execute(
                "SELECT passage_fk, text_sha256, status FROM extraction_result WHERE extractor_version=%s AND model=%s",
                (ex.version, ex.model_id),
            ).fetchall()
        }
        conn.execute(
            "INSERT INTO extraction_run (id, tenant_id, project_id, extractor_version, model, prompt_version, policy, "
            "started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, scope.tenant_id, scope.project_id, ex.version, ex.model_id, ex.prompt_version, Jsonb(rules),
             started),
        )
    for r in rows:
        r["score"] = relevance_score(r["text"], r["authority"], rules)
    decision = select_passages(rows, rules)

    todo = [r for r in rows if decision[r["id"]][0] and not (
        r["id"] in cached and cached[r["id"]]["text_sha256"] == r["text_sha256"] and cached[r["id"]]["status"] == "ok")]
    batches = [todo[i : i + rules["batch_size"]] for i in range(0, len(todo), rules["batch_size"])]
    outcomes: dict[str, tuple[str, str | None]] = {}
    workers = 1 if ex.is_test_double else max(1, get_settings().potency_extraction_concurrency)

    def work(batch: list[dict]):
        inputs = [
            PassageInput(f"P{i + 1}", r["id"], r["text"], r["title"], r["authority"], r["status"], r["source_type"],
                         r["page"], r["section"], r["subject_scope"])
            for i, r in enumerate(batch)
        ]
        return batch, inputs, ex.extract(inputs, rules["max_assertions_per_passage"])

    budget_stop = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, b): b for b in batches}
        for fut in as_completed(futures):
            try:
                batch, inputs, res = fut.result()
            except BudgetExceeded as exc:
                budget_stop = str(exc)
                continue
            except Exception as exc:  # recorded per passage, never hidden
                err = f"{type(exc).__name__}: {exc}"[:500]
                batch = futures[fut]
                _store_error(scope, run_id, ex, batch, err)
                for r in batch:
                    outcomes[r["id"]] = ("error", err)
                continue
            if res.usage:
                record_usage(scope, run_id, "extraction", res.model, res.usage)
            with scoped_session(scope) as conn:
                for inp, r in zip(inputs, batch, strict=True):
                    accepted, rejected = validate_assertions(r["text"], res.per_passage.get(inp.evidence_id, []),
                                                             rules["max_assertions_per_passage"])
                    conn.execute(
                        """
                        INSERT INTO extraction_result (id, tenant_id, project_id, passage_fk, text_sha256,
                            extractor_version, model, run_id, status, raw, accepted, rejected, usage)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ok',%s,%s,%s,%s)
                        ON CONFLICT (passage_fk, text_sha256, extractor_version, model) DO UPDATE SET
                            run_id=EXCLUDED.run_id, status='ok', raw=EXCLUDED.raw, accepted=EXCLUDED.accepted,
                            rejected=EXCLUDED.rejected, usage=EXCLUDED.usage, error=NULL, created_at=now()
                        """,
                        (f"{scope.prefix}/extraction/{ids.short_hash(r['id'], r['text_sha256'], ex.version, ex.model_id)}",
                         scope.tenant_id, scope.project_id, r["id"], r["text_sha256"], ex.version, ex.model_id,
                         run_id, Jsonb(res.per_passage.get(inp.evidence_id, [])), Jsonb(accepted), Jsonb(rejected),
                         Jsonb({**res.usage, "batch_size": len(batch), "request_id": res.request_id})),
                    )
                    outcomes[r["id"]] = ("extracted", None)

    with scoped_session(scope) as conn:
        results = {
            r["passage_fk"]: r
            for r in conn.execute(
                "SELECT passage_fk, status, jsonb_array_length(accepted) AS n_acc, "
                "jsonb_array_length(rejected) AS n_rej FROM extraction_result "
                "WHERE extractor_version=%s AND model=%s",
                (ex.version, ex.model_id),
            ).fetchall()
        }
        per_source: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        cov_rows = []
        for r in rows:
            sel, skip = decision[r["id"]]
            if not sel:
                outcome, reason = "skipped", skip
            elif r["id"] in outcomes:
                outcome, reason = outcomes[r["id"]]
            elif r["id"] in results and results[r["id"]]["status"] == "ok":
                outcome, reason = "cached", "reused prior extraction for identical text/extractor/model"
            else:
                outcome, reason = "not_processed", budget_stop or "unknown"
            res_row = results.get(r["id"])
            s = per_source[r["source_id"]]
            s["passages"] += 1
            s["selected"] += int(sel)
            s[f"outcome_{outcome}"] += 1
            if not sel:
                s[f"skip_{skip}"] += 1
            if sel and res_row and res_row["status"] == "ok":
                s["assertions_accepted"] += res_row["n_acc"]
                s["assertions_rejected"] += res_row["n_rej"]
            cov_rows.append((run_id, scope.tenant_id, scope.project_id, r["id"], r["source_id"], r["score"], sel,
                             outcome, reason))
        conn.cursor().executemany(
            "INSERT INTO extraction_coverage (run_id, tenant_id, project_id, passage_fk, source_id, relevance_score, "
            "selected, outcome, reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            cov_rows,
        )
        totals = defaultdict(int)
        for s in per_source.values():
            for k, v in s.items():
                totals[k] += v
        stats = {
            "run_id": run_id,
            "extractor": {"actor": ex.actor, "version": ex.version, "model": ex.model_id,
                          "prompt_version": ex.prompt_version, "is_test_double": ex.is_test_double},
            "policy_version": rules["version"],
            "totals": dict(totals),
            "per_source": {k: dict(v) for k, v in sorted(per_source.items())},
            "budget_stop": budget_stop,
            "complete": budget_stop is None and not any(o[0] == "error" for o in outcomes.values()),
        }
        conn.execute("UPDATE extraction_run SET finished_at=now(), stats=%s WHERE id=%s", (Jsonb(stats), run_id))
    return stats


def _store_error(scope: Scope, run_id: str, ex: Extractor, batch: list[dict], err: str) -> None:
    with scoped_session(scope) as conn:
        for r in batch:
            conn.execute(
                """
                INSERT INTO extraction_result (id, tenant_id, project_id, passage_fk, text_sha256, extractor_version,
                    model, run_id, status, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'error',%s)
                ON CONFLICT (passage_fk, text_sha256, extractor_version, model) DO UPDATE SET
                    run_id=EXCLUDED.run_id, status='error', error=EXCLUDED.error
                """,
                (f"{scope.prefix}/extraction/{ids.short_hash(r['id'], r['text_sha256'], ex.version, ex.model_id)}",
                 scope.tenant_id, scope.project_id, r["id"], r["text_sha256"], ex.version, ex.model_id, run_id, err),
            )
