# Data contracts

Every record carries `scope = {tenant_id, project_id}`. Identity is scope-prefixed; the canonical citation target is
`(scope, source_id, source_version, passage_id)`. Prototype column names are used; production adds S3 copies for
analytics (Athena/Glue), so analytical consumers never query the application database.

| Contract | Canonical store / format (production) | Prototype location | Key / identity | Required fields | Producer → consumers | Retention / deletion |
|---|---|---|---|---|---|---|
| Raw source file | S3 `raw/{tenant}/{project}/{source_id}/{sha256}` original bytes, versioned, SSE-KMS | `corpus/raw/` | sha256 of bytes | filename, source_url, retrieved_at, issuer | upload → text extraction | life of contract; deleted on source deletion request |
| Normalized passage record | S3 `normalized/` JSONL (+ Parquet); DB `passage` + `source_version` | `corpus/records.jsonl`; tables `source_document`, `source_version`, `passage`, `passage_text_revision` | `{tenant}/{project}/{source_id}@{source_version}#{passage_id}` | text, text_sha256, locator (page, section), authority, status, issued, subject_scope, corpus_snapshot | importer → index, extraction, graph, answers | current + superseded versions kept; text revisions archived |
| Import ledger | DB `import_run`, `import_event`; S3 JSON report | `results/import-report.json` | run_id, record_key | outcome ∈ imported/unchanged/updated/failed/omitted, reason, reconciled totals | importer → ops, reports | 2 years |
| Chunk + embedding | DB `passage_chunk`, `embedding_cache` (vector(768); an HNSW index is defined, but the prototype query is an exact scan), `index_version` | same | chunk id = hash(passage, chunker_version, index); cache key = (scope, model_id, sha256(embedded text)) | parent passage, char_start/char_end, chunker_version, embedding model, dims | indexer → vector + graph retrieval | rebuilt on model change (new index_version); deleted with passage |
| Extraction result | DB `extraction_run`, `extraction_result`, `extraction_coverage` | same | (passage, text_sha256, extractor_version, model) | assertions JSON, rejected items + reasons, usage, request_id | extractor → graph builder | cached; replaced on extractor version change |
| Graph node | S3 `graph/{tenant}/{project}/{build_id}/nodes.parquet` (+ JSONL); DB `kg_node` | `results/graph/nodes.jsonl` | scope-prefixed id (deterministic hash) | id, type, label, scope, attributes, source_refs[], actor, version, created_at, review_state, is_current, graph_build_id | graph builder → retrieval, analytics, reviewers | every build exported; DB holds current build |
| Graph edge | as above, `edges.parquet`; DB `kg_edge`, `evidence_ref` | `results/graph/edges.jsonl` | scope-prefixed id | id, type, source, target, scope, status, source_refs[] (empty only for system edges), actor, version, created_at, review_state, evidence_bearing | graph builder → traversal, analytics | as nodes |
| Answer | S3 `answers/{tenant}/{project}/dt=YYYY-MM-DD/*.jsonl`; DB `answer` | `results/answers.jsonl` | answer id ↔ trace_id | question_id, mode, answer, claims (verified flag), citations (scope, source_id, source_version, passage_id, page, section, quote), not_found, conflicts, uncertainty, corpus_snapshot | answer service → UI/API, eval, feedback | DB 90 days hot; S3 2 years |
| Retrieval/generation trace | S3 `traces/…` JSONL (partitioned like answers); DB `trace` | `results/traces.jsonl` | trace_id | seed_node_ids, traversed_edge_ids (+ hop, from, to), selected_passage_ids, evidence_text, evidence_chars (code points), traversal_limits, retriever/prompt/model versions, elapsed_ms, usage, index_version, graph_build_id | answer service → debugging, eval, analytics | as answers; evidence_text redacted when its source is deleted |
| Usage ledger | DB `usage_ledger` (scoped) + `model_spend` (global numeric); S3 daily export | `results/usage.json` | (run_id, purpose, call) | model, input/output/cache tokens, estimated_usd | model adapter → budget guard, billing | 2 years |
| Reviewer feedback / correction (design) | S3 `feedback/` append-only JSONL; DB `review_event` | not implemented | event id | actor (human user id), action ∈ approve/reject/edit/flag, target (assertion/edge/answer id), before/after JSON, reason, graph_build_id, source_version, timestamp | reviewer UI → graph builder (approved edits), eval case curation | permanent audit trail |
| Evaluation case | S3 `eval/cases.jsonl` + git | `results/evaluation/cases.jsonl` | (case_id, case_version) | split (dev/holdout), origin, risk, question, expected_evidence_refs (any-of groups), answer_checks | evaluator authors → eval runner | versioned; holdout hash frozen in `holdout-freeze.json` |
| Evaluation result | S3 `eval/results/{run_id}.jsonl`, report JSON | `results/evaluation/results.jsonl`, `report.json` | (case_id, mode, run_id) | status ∈ completed/failed/unscorable, retrieved_passage_ids, expected_evidence_refs, scores (retrieval and answer separately), errors, trace_id, scorer_version | eval runner → CI gate, dashboards | permanent (small) |

Notes

- Character budgets count Unicode code points (`len` in Python) over the full evidence text sent to generation,
  including every header line.
- `review_state` (unreviewed/approved/rejected) is independent of the assertion's regulatory `assertion_state`
  (proposed, requested, revised, accepted, rejected, unresolved, redacted) and of the document `status`.
- Structural edges (`contains_passage`, `next_passage`, `document_subject`, `contains_redaction`, `supersedes`) use
  `actor=system:*` (`system:structure-v1`, `system:document-metadata-v1`, `system:source-management-v1`) and empty
  `source_refs`; they do not establish scientific claims. Redaction placeholders and `contradicts` edges use
  `rule:*` actors; extracted assertions/relations use `llm:<model>`.
