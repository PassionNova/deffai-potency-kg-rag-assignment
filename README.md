# Potency evidence KG / GraphRAG: solution

This is a scope-isolated evidence graph and GraphRAG system over the supplied FDA potency corpus. It runs on
Postgres 16 with pgvector and pg_trgm, uses local `BAAI/bge-base-en-v1.5` embeddings and `claude-sonnet-5-5` for
generation, has a CLI that produces everything under `results/`, and has a thin FastAPI layer over the same answering
service. AWS and a frontend are design-only (`results/architecture/`).

## Completion status

| Deliverable | Status | Where |
|---|---|---|
| Import, hash verification, re-import ledger | done: 791 imported, 0 failed, 0 omitted; re-import after restart is unchanged | `results/import-report.json` |
| Graph (structural + Claude extraction) | done: 4,679 nodes, 6,470 edges, 584 quote-validated assertions | `results/graph/` |
| P1–P6 answers and traces (P3 in both modes) | done: 7 records, real model | `results/answers.jsonl`, `traces.jsonl`, `p3-comparison.md` |
| Project isolation | done: 26 of 26 checks pass | `results/isolation/` |
| Source-correction and conflict fixtures | done: 0 failures, before/after history preserved | `results/fixtures/` |
| Evaluation (dev + frozen holdout) | done: 6 cases x 2 modes, 0 failed or unscorable runs | `results/evaluation/` |
| Architecture, cost model, data contracts | done (design only) | `results/architecture/` |
| Automated tests | 17 passing (test doubles, labelled) | `tests/` |

**Code version of the results.** The main answers and traces (`answers-20261014T143900Z-654868`) were produced from
commit `c7a84df` with a clean tree, and every trace records that commit. Isolation, fixtures and evaluation ran
earlier, from the uncommitted tree on top of `b9a7f80`, so their traces say `uncommitted_changes: true`. Between that
tree and `c7a84df` the only code changes are lint fixes with no behaviour change (`zip(strict=True)`, an unused
variable), plus a corrected key in the evaluation report's visible-fixture summary. The evaluation was not re-run, so
the frozen holdout has been scored exactly once.

Known gaps:

- Extracted assertions are unreviewed (`review_status: unreviewed`); no human-review workflow exists, which the brief
  does not require.
- 0 `contradicts` edges were found in the main corpus. The conflict behaviour is shown on the fixture.
- The graph-mode P3 answer is correct but incomplete: the proposal-to-conclusion pages were cut by the 6,000-point cap.
- Evaluation answer checks passed 6 of 12 runs. Every failure is a retrieval miss, not a fabrication (see
  `results/evaluation/failure-analysis.md`).

## Setup and run

Prerequisites: Docker, Python 3.12, and [uv](https://docs.astral.sh/uv/) (or plain `venv` + `pip`).

```powershell
docker compose up -d db                     # pgvector/pgvector:pg16 on localhost:55433
uv sync                                     # or: python -m venv .venv; .venv\Scripts\pip install -e . pytest httpx ruff
copy .env.example .env                      # set ANTHROPIC_API_KEY and ANTHROPIC_MODEL=claude-sonnet-5-5
uv run potency migrate                      # owner role: schema, RLS, non-owner potency_app role
uv run potency smoke                        # tiny embedding + generation checks -> results/access-check.json
uv run potency pipeline                     # import -> index -> extract -> build-graph -> report
uv run potency answer                       # P1-P6 (P3 vector + graph) -> results/answers.jsonl, traces.jsonl
uv run potency isolation                    # project-isolation fixture (query runs in a fresh process)
uv run potency fixtures                     # source-correction + unresolved-conflict scenarios
uv run potency eval run                     # dev + holdout, both modes (refuses if holdout changed since freeze)
uv run potency eval score                   # recompute scores/report from saved outputs, no model calls
uv run potency usage                        # results/usage.json
uv run pytest                               # offline: fake embedder + fake LLM, separate potency_test database
uv run uvicorn potency.entrypoints.api:app  # POST /v1/tenants/{t}/projects/{p}/answers, GET /v1/traces/{id}
```

The first `index` downloads bge-base (about 0.4 GB) to `.cache/fastembed` and embeds 1,203 chunks on CPU in about
24 minutes. Embeddings are cached by (scope, model, SHA-256 of text), so re-running computes nothing (re-index:
1,203 cache hits, 0 computed). Every write goes through `scoped_session(tenant, project)` as the RLS-bound
`potency_app` role. `POTENCY_MAX_USD` is a hard spend stop checked before each model call.

Dependencies were installed with `uv sync` (uv 0.13.1). `uv sync --locked --dry-run` confirms that `uv.lock` matches
`pyproject.toml`.

## Implementation overview

```text
src/potency/
  tenancy/      Scope + scoped_session (set_config of tenant/project; RLS ENABLE + FORCE on every table)
  sources/      record parsing, SHA-256 verification, idempotent upsert, correction/supersession, import ledger
  passages/     ws-window-v1 chunker (1,500 / 200 chars) mapped back to parent passages
  indexing/     fastembed adapter, scoped embedding cache, index_version
  extraction/   corpus-wide relevance pass -> batched Claude extraction -> quote validator (exact source substring)
  graph/        structural graph (Source/Passage/Application/Redaction ...) + assertion/role/contradiction layer, JSONL export
  retrieval/    vector-v1, graph-v2 (bounded BFS, per-origin summed scoring), code-point packer
  answering/    prompt, Claude structured output, citation verifier, trace writer
  evaluation/   cases, scorer-v1, holdout freeze, runner
  workflows/    main case, isolation, behaviour fixtures, smoke, usage
  entrypoints/  Typer CLI, FastAPI
```

- **Evidence identity.** Every record is keyed by `(tenant_id, project_id, source_id, source_version, passage_id)`.
  Citations store that full key plus the *exact source substring*. The verifier rejects any quote that is not in both
  the evidence block and the current in-scope passage, and it reports failures instead of repairing them.
- **Extraction policy (corpus-wide, not question-driven).** All 791 passages are scored by
  `configs/extraction-rules.yaml`. 240 were selected for Claude extraction. Skips are recorded per passage: 75 too
  short, 90 below threshold, 106 over the total cap, 280 over the per-source cap. Of the extracted assertions, 584 were
  accepted and 44 rejected (`quote_not_found`). `(b) (4)` spans become `Redaction` nodes and are never filled in.
- **Graph retrieval (graph-v2).** Seeds are entity labels (`word_similarity >= 0.6`, top 8) plus the top 6 vector
  passages. Traversal is undirected, up to 2 hops, 8 neighbours per node per hop, and 60 nodes in total. A passage's
  score is 0.5 x normalised graph score + 0.5 x vector similarity. The graph score is a sum over distinct seed origins.
  The graph only selects passages; generation sees source text only, inside the same code-point cap.
- **Structured output.** `claude-sonnet-5-5` rejects forced `tool_choice`. The adapter tries forced tool use, then
  falls back to `output_config` JSON-schema output (strict schema). The model does not accept `temperature`, so default
  sampling is used. The mechanism actually used is recorded in each trace and in `results/access-check.json`.

## Actual system versions

| Component | Version |
|---|---|
| Corpus snapshot | `potency-assignment-2026-09-21-v3` |
| Index / graph | `idx-9643b8f06092` (`ws-window-v1:1500/200`, bge-base 768-d) / `graph-builder-v1`, retriever `graph-v2`, `vector-v1`, `packer-v1` |
| Generation / embeddings | `claude-sonnet-5-5` (Anthropic API) / `BAAI/bge-base-en-v1.5` via fastembed 0.9.0, onnxruntime 1.31.0, CPU |
| Runtime | Python 3.12.10, Postgres 16 (`pgvector/pgvector:pg16`), Docker 29.4.3 |
| Libraries | anthropic 1.13.0, psycopg 3.3.6, psycopg-pool 3.3.3, pgvector 0.5.1, alembic 1.20.0, SQLAlchemy 2.1.4, FastAPI 0.143.0, Typer 0.27.3, Pydantic 2.14.1, pytest 9.1.1 |
| Scorer / eval cases | `scorer-v1`; holdout SHA-256 `4725bd16…` frozen 2026-10-14T07:14:11Z |

## Elapsed time and usage/cost

Active time is about 5 h 50 min. That is 5 h 10 min to the first handoff, of which about 2 h 50 min was reading and
planning before code, plus about 40 min for the review and the fixes it prompted. The breakdown is in
`results/time-log.md`.

Recorded model usage (`results/usage.json`) is 134 calls and about **$3.55** estimated: extraction $2.45, answers
$1.10 (main-case answers including the clean-tree re-run, evaluation, isolation and fixtures). This is priced at $2 / $10 per MTok in/out, the
`claude-sonnet-5-5` list price as of 2026-10-07 (sources in `architecture.md` §8), with cache reads at 0.1x and cache writes at 1.25x. It is an
estimate from provider-reported tokens, not an invoice. Calls that errored before returning usage are not in the
ledger. That covers schema-rejected extraction attempts, and one answer truncated at `max_tokens`, about 4k output
tokens or $0.04. Embeddings ran locally at $0.

**Production cost estimate** (`results/architecture/architecture.md` §6), re-based on measured answer tokens (12,000
code-point answers average 4,395 in / 1,999 out) and corrected Haiku 5.5 prices:

- **Monthly, on-demand:** ≈ $1,090, which is **$90 over** the $1,000 target.
- **With the smallest fixes:** a 1-year RDS reservation plus a concise answer format (about 1,400 output tokens, to be
  validated on the regression set) gives ≈ $905.
- **Backfill:** ≈ $524, **$24 over** the $500 limit. Limiting Sonnet extraction to the top 25,000 pages gives ≈ $397.

My local `.env` had older 3 / 15 price values. The final runs set `POTENCY_PRICE_INPUT_PER_MTOK=2.0` and
`POTENCY_PRICE_OUTPUT_PER_MTOK=10.0` in the process environment, and `.env.example` and the defaults now use 2 / 10.

## Tradeoffs

- **Postgres for graph, vectors and isolation, not Neo4j or Pinecone.** One transactional store with RLS gives
  scope isolation in the storage layer and exact evidence joins. Two-hop bounded traversal is cheap in SQL at this size.
  The cost is no native graph query language; the 10x path is in `architecture.md`.
- **Local bge-base, not a hosted embedder.** It needs no extra vendor or key and is reproducible. The cost is about
  24 minutes of CPU for the first index, and its quality is below the best hosted models.
- **Selective extraction under a declared corpus-wide policy.** Spend stays bounded (about $2.45), but some relevant
  pages have structural nodes only.
- **Strict verification over fluency.** Exact-substring quotes mean some correct paraphrased quotes are rejected and
  reported (2 in the final P1–P6 run). That is preferred to silently accepting near-matches.
- **Fixed-size excerpts with full provenance headers.** Each block is a verbatim excerpt of at most 1,100 characters
  centred on the matched chunk or quote, with at most 3 blocks per source, plus a header giving title, passage, page,
  authority and status. A block costs about 1,270 code points, so only about 5 fit in 6,000. That keeps context around
  each quote, but it is the main cause of evaluation misses.
- **Exact vector scan, not ANN.** At 1,203 chunks an exact scan is fast and deterministic. The `DISTINCT ON` query
  cannot use the HNSW index, so production needs the ANN design in `architecture.md`.

## How AI-generated work was verified

Most code was written by an AI coding agent from a plan I reviewed. Verification did not rely on the agent's own
claims:

- **Automated checks** (`tests/`, 17 passing, with test doubles labelled `is_test_double`). They cover re-import after
  a restart in a separate process (unchanged counts, 0 new embeddings, same `index_version`), exact citation text and
  source version, isolation containment across colliding IDs, graph endpoint and traversal validity, the correction and
  conflict scenarios, scoring repeatability, API scope enforcement, and line-number tolerance.
- **Mechanical guards at runtime.** The extraction quote validator rejected 44 assertions. The answer verifier records
  every failed quote. Import totals reconcile (imported + unchanged + failed + omitted = records). The holdout hash is
  checked on every evaluation run.
- **Reading traces, not just outputs.** Inspecting the P3 traces showed that graph-v1 packed exactly the same passages
  as vector because the vector similarity was counted twice. That led to graph-v2, which is disclosed. Grepping the
  evidence text for "failed" quotes showed PDF line numbers inside the draft guidance, which led to the flagged
  tolerance. In the opposite direction, checking a remaining failure showed the model really had dropped a comma, and
  the note was corrected to say so.
- **Real runs labelled separately from test doubles.** Every answer, trace and evaluation row records `model`,
  `embedding_model` and `is_test_double`.
- **Disclosed tuning.** All changes made after seeing visible results are listed in `results/p3-comparison.md`. The
  holdout was frozen before them and has not been used for tuning.
- **A separate review pass.** A second AI agent audited the finished repository against the output contract and the
  code. It re-verified all 56 citations against `corpus/records.jsonl` and found four documentation errors that the
  first pass had written. The packing description was wrong, `p3-comparison.md` said HNSW where the code does an
  exact scan, the model-access sheet was stale, and the cost model assumed 900 output tokens per answer where the
  measured average is about 1,700. While re-basing the cost model, re-checking the pricing page showed a fifth error:
  Haiku 5.5 had been priced at one fifth of its list price. All five are corrected; see "Production cost estimate"
  below.

## Next three steps

1. **Ranking and packing within the budget.** Add a cross-encoder reranker and source-diversity weighting over the
   candidate pool, and try smaller quote-centred excerpts. Expected evidence was retrieved in 7 of 10 evaluation runs
   but packed in only 3. The full-text index on `passage.tsv` exists but is unused, so hybrid lexical + vector ranking
   is a cheap addition. Evaluate on new dev cases and re-freeze a fresh holdout.
2. **Entity resolution and scope-aware seeding.** The graph has 24 Product nodes (CASGEVY, `exa-cel`, `CTX001` ... are
   separate) and 21 Indication nodes. 502 of 584 assertions carry no explicit application ID. Merge aliases using the
   manifest's product identity, inherit application scope from the document (marked `document_context`), and map STN
   or indication terms in questions to `Application` seeds, so SCD-vs-TDT questions (EV-H03) reach the right documents.
3. **Review workflow for assertions.** Add a reviewer queue (accept, correct or reject with audit trail) and
   contradiction review, so answers can prefer reviewed assertions and surface unreviewed ones as such.

---

# Supplied assignment materials

Start with [ASSIGNMENT.md](ASSIGNMENT.md), then complete [MODEL_ACCESS.md](MODEL_ACCESS.md) before the clock starts. Package edition: `2026-09-21-potency-rag-v2`; the frozen source files are retained; corpus snapshot v3 includes one audited heading-order correction.

This folder contains the data and contracts for the exercise. It does not prescribe an implementation stack or include starter application code.

## Contents

| Path | Purpose |
|---|---|
| `corpus/records.jsonl` | Mechanical convenience extraction: 791 normalized passages. |
| `corpus/manifest.json` | Frozen snapshot, source inventory, hashes, and versions. |
| `corpus/raw/regulatory/` | Official FDA guidance/policy PDFs and dated eCFR XML. |
| `corpus/raw/precedent/` | Official ZYNTEGLO and LYFGENIA SBRAs. |
| `corpus/raw/casgevy-125785/` | Supplied public TDT approval materials. |
| `corpus/raw/casgevy-125787/` | SCD approval letter and SBRA. |
| `questions.json` | Six required questions; P3 requires vector and graph modes. |
| `schema/graph-core.json` | Adjustable reference schema; document mappings to required behavior. |
| `schema/output-contract.json` | Minimum artifact and field expectations. |
| `configs/casgevy-potency.json` | Corpus scope, case identity, and relative resource paths. |
| `configs/model-access.template.json` | Team-completed access configuration; contains no credentials. |
| `MODEL_ACCESS.md` | Pre-start checklist, two access smoke calls and outage handling. |
| `fixtures/` | Project-isolation collision inputs plus correction/conflict scenarios. |

## Evidence boundary

- Process all records in `corpus/records.jsonl`; do not pre-filter to documents known to answer the questions.
- Treat files under `corpus/raw/` as the source of truth. The JSONL is a mechanical extraction provided for convenience.
- Do not ingest the assignment, questions, schemas, fixture records, or generated outputs as factual FDA evidence.
- Use the raw files to inspect provenance and page context; PDF/XML parsing is not required.
- Use only the supplied corpus for factual answers. URLs are provenance references, not permission to change the frozen snapshot.
- `(b)(4)` denotes public redaction and must not be filled by inference.
- The CASGEVY CMC review memorandum, approval letter, and SBRA are separate documents.
- The configured application is TDT/STN 125785, but the shared exa-cel potency history spans TDT and SCD/STN 125787. Preserve application and indication scope.

The record identity is:

```text
(tenant_id, project_id, source_id, source_version, passage_id)
```

Do not treat `passage_id` alone as globally unique. The fixture inputs intentionally collide on local source and passage identifiers across project scopes.

## Starting the assignment

1. Confirm the archive opens and manifest-listed resources are available. Report any supplied-data mismatch before starting; you are not expected to repair the extraction.
2. Complete the team-provided model access sheet and verify both embedding and generation with tiny test input. A successful local file check is not a successful model-access check.
3. Confirm paid terms, any material clarification, readiness timestamp and submission deadline. The eight-hour active-work clock starts only then; personal model credits are not required.

Implement your own system and generated `results/` alongside these resources. The package contains no starter application. The access template/checklist is onboarding documentation, not a completed implementation. All source records are considered; semantic extraction can be selective under a documented corpus-wide policy. Public source IDs, fixture scope IDs and metadata are inputs, not prewritten answers.

Provide the seven required answers/traces, a compact executed regression evaluation with saved per-case results, and a concise architecture/cost/data-contract design. The evaluator checks decisions and actual mechanisms; graph size and diagram polish do not earn separate credit.

## Confirmed design scope

CASGEVY is the sole main case; P6 still compares both ZYNTEGLO and LYFGENIA. P3 runs vector and graph modes under the 6,000-character evidence cap; each other question needs one mode. The graph schema is adjustable under the documented behavioral contracts.

Run the [source-correction and conflict fixtures](fixtures/BEHAVIOR_CASES.md) in addition to project isolation, and preserve before/after source and graph artifacts with three separate fixture answers/traces. Historical re-query and working human-review operations are not required.
