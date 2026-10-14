# Production architecture: potency evidence KG / GraphRAG on AWS

Region: **us-east-1 (N. Virginia)**. Pricing retrieved **2026-10-13**, on-demand list prices unless stated (links in §8).
Diagram: [architecture-diagram.mmd](architecture-diagram.mmd). Data contracts: [data-contracts.md](data-contracts.md).

## 1. Summary

The prototype already has the production shape: one Postgres database (pgvector + pg_trgm) holds versioned evidence,
chunks/embeddings, the evidence graph and traces, with row-level security (RLS) on `(tenant_id, project_id)` and an
application role that cannot bypass it. The production design keeps that core and adds managed edges around it:
S3 as the system of record for files and analytical exports, Step Functions + SQS + Fargate workers for ingestion,
Textract for the 5% scanned pages, Bedrock (in-region) for generation/extraction, and Athena over S3 for analytics.

Estimated recurring cost at pilot load, all on-demand and using **measured** prototype answer tokens: **≈ $1,090/month**
(baseline ≈ $477 + usage ≈ $613). That is **≈ $90 (9%) over** the $1,000 target. The overrun comes from answer
generation: measured answers average about 2,000 output tokens. The smallest change that meets the target (§6.4) is a
1-year RDS reservation plus a concise answer format that holds output to about 1,400 tokens: **≈ $905/month**, about 9%
headroom. The output reduction is a design target and must pass the regression evaluation before it is relied on.
Backfill of 500,000 pages: **≈ $524** on the current tiering, **$24 over** the $500 limit. Limiting Sonnet to the top
25,000 pages gives **≈ $397** (§6.3).

An earlier version of this document claimed ≈ $822 and ≈ $365. It assumed 900 output tokens per answer, and it priced
Haiku 5.5 at one fifth of its list price. Both errors are corrected here.

## 2. Operational path

**Ingestion (async).** Uploads land in `s3://…/raw/{tenant}/{project}/{source_id}/{sha256}` (versioned, immutable).
An S3 event starts a per-document Step Functions execution:

1. *Register* source + version (content hash = version identity, as in the prototype `source_version`).
2. *Extract text*: born-digital PDF text extraction in a worker; pages with no text layer go to Textract
   `StartDocumentTextDetection` (async). Output: normalized passage records (the prototype record schema) to
   `normalized/` as JSONL (and Parquet for analytics).
3. *Import* through the prototype importer: hash-verified, idempotent upsert; unchanged text is a no-op; changed text
   archives the prior revision; every record gets an import outcome (imported/unchanged/updated/failed/omitted).
4. *Chunk + embed* (bge-base-en-v1.5, 768-d, self-hosted on Fargate Spot ARM). The embedding cache is keyed by
   `(scope, model, sha256(embedded text))`, so retries and re-imports never re-embed.
   **Searchability point:** passages are retrievable in `vector` mode and through structural graph nodes here.
5. *Relevance pass + semantic extraction*: corpus-wide rule scoring (prototype `extraction-rules.yaml`), then Claude
   extraction with forced JSON schema and the quote validator (quotes must be exact passage substrings, otherwise
   rejected). Results are cached by `(passage hash, extractor version, model)`.
6. *Graph build* for the affected scope; export nodes/edges to `graph/{tenant}/{project}/{build_id}/` (Parquet + JSONL).

Each stage reads from its own SQS queue with a dead-letter queue; failures are visible as `failed` import/extraction
events (never silently dropped) and in the per-source coverage table.

**Online answers.** `POST /v1/tenants/{t}/projects/{p}/answers` (the prototype FastAPI endpoint) → Cognito JWT →
scope grant check → `scoped_session` (sets `app.tenant_id/app.project_id`; RLS enforces) → vector or graph retrieval
→ code-point-budgeted packer → Bedrock Claude Sonnet with forced `submit_answer` tool → citation verifier (every quote
must be an exact substring of the cited in-scope passage) → trace persisted (DB hot copy 90 days + S3 JSONL).
If Bedrock is unavailable the API returns the retrieved, verified evidence passages with `answer_status=generation_unavailable`
instead of failing closed.

## 3. Service selection and what changes from the prototype

| Concern | Prototype (retain) | Production | Decision |
|---|---|---|---|
| Evidence/graph/vector store | Postgres 16 + pgvector + pg_trgm, RLS | RDS PostgreSQL 16 Multi-AZ, db.m7g.large, gp3 | **Retain** design; managed hosting |
| Graph traversal | SQL one-hop-per-query, bounded | same | **Retain**; no graph DB needed at this size |
| Embeddings | local bge-base via fastembed | same model on Fargate Spot ARM workers; query embedding in API task | **Retain** (no vendor re-embed, measured cheap) |
| Generation | Anthropic API, Claude Sonnet | Bedrock in-region Claude Sonnet 5.5 (answers); Haiku 5.5 bulk extraction + Sonnet 5.5 for high-relevance pages | **Replace** endpoint; gate on regression eval |
| Orchestration | CLI steps | Step Functions + SQS + ECS Fargate workers | **Replace** |
| Files / exports | repo files, `results/` | S3 (versioned, SSE-KMS), Parquet/JSONL | **Replace** |
| Auth | dev bearer-token → scope map | Cognito (OIDC/SAML federation), JWT claims → scope grants | **Replace**; DB RLS stays as defense in depth |
| OCR | out of scope | Textract DetectDocumentText (async) | **Add** |
| Review / correction | not implemented (design only) | reviewer UI + `review_event` table + S3 feedback log | **Add** (§5) |
| Analytics | JSONL files | Glue Catalog + Athena + Lake Formation row filters | **Add** |
| Cross-scope public evidence | single scope per query | query = own project scope ∪ read-only `public` scope | **Change** (needs a second RLS policy; validate) |

## 4. Isolation and security

- **Data plane:** single shared database; every scoped table has `ENABLE + FORCE ROW LEVEL SECURITY` and policies on
  `current_setting('app.tenant_id')/('app.project_id')`, set per transaction. The app role is `NOBYPASSRLS`; migrations
  use a separate owner role. Identifiers are scope-prefixed so identical source/passage IDs in two projects never
  collide (proved by the prototype isolation fixture in both retrieval modes, after a process restart).
- **Shared public corpus:** stored once under `tenant=public-fda`; customer queries add a read-only union with that
  scope. Customer documents are never written to the public scope; public extraction is paid once, not per tenant.
- **Caches:** embedding cache, extraction cache and prompt caches are keyed by scope (embedding/extraction) or contain
  only public instructions (prompt cache). No cross-tenant cache key exists.
- **Traces, analytics:** S3 prefixes `…/{tenant}/{project}/…` encrypted with a per-tenant KMS key; Athena access
  through Lake Formation with tenant row filters; cross-tenant dashboards use aggregate metrics without content.
- **Network:** private subnets; Bedrock via an interface VPC endpoint; S3 gateway endpoint; one NAT gateway for the rest.
- **Model data use:** Bedrock does not use prompts for training; in-region inference keeps data in the US region.
- **Audit:** CloudTrail + an application audit log of who asked/reviewed what (tenant-scoped).

## 5. Review, correction, deletion, versioning

- **Review state is independent of regulatory state.** Extractions are `unreviewed` (prototype default). A project
  `reviewer` (customer SME; DeffAI curator for the public corpus) can approve, reject, or edit an assertion/edge in the UI.
  Each action is an append-only `review_event` (actor, time, before/after JSON, reason, assertion id, graph build,
  passage and source version). An approved edit creates a new assertion version with `actor=human:<user>`,
  `review_state=approved`, `supersedes` → the extracted one. The next graph build for that scope carries it; evidence
  headers show review state, so answers can prefer approved assertions and say when evidence is unreviewed.
- **Source correction:** an explicit replacement creates a new `source_version`, marks the old one `superseded`, sets
  its passages/assertions `is_current=false`, rebuilds the scope graph, and preserves prior exports, answers and traces
  (implemented and checked in the prototype `source-correction` fixture).
- **Deletion:** a deletion request tombstones the source, then deletes passages, chunks, scoped embeddings, extraction
  results, graph nodes/edges and evidence refs (prototype `delete_scope` shows the ordering), and S3 object versions.
  Traces that quoted deleted text are redacted (evidence_text replaced by passage IDs). Tenant off-boarding uses
  crypto-shredding of the tenant KMS key. RDS backups expire within the 35-day retention window, which is documented
  to customers as the deletion completion bound.
- **Versioning:** every artifact records its versions: chunker, embedding model, extractor + prompt version, graph
  build id, index version, retriever/packer version, generation model and git commit (all present in prototype traces).
  A new embedding model builds a new `index_version` side by side; the active pointer flips only after the
  regression eval passes. Schema changes use Alembic migrations (expand → backfill → contract).

## 6. Capacity and cost

### 6.1 Assumptions (measured where possible)

| Input | Value | Basis |
|---|---|---|
| Characters per page | 2,313 | measured: 1,327,580 chars / 574 pages in the supplied corpus |
| Tokens per page | ≈ 580 | 4 chars/token for English regulatory text |
| Chunks per page | 2.10 | measured: 1,203 chunks / 574 pages (1,500-char window, 200 overlap) |
| Embedding throughput | 0.84 chunks/s per 4 CPU threads (prototype, contended laptop); 4 chunks/s assumed on a dedicated 4-vCPU Fargate task | measured 1,194 embeddings in 1,418 s; Fargate rate to validate |
| New pages / month | 80,000 | 2,000/day × 30 + one 20,000-page burst per month (conservative) |
| OCR share | 5% | given |
| Semantic extraction share | 30% of pages | prototype relevance policy selects ≤ 240 of 791 passages (≈ 30%) |
| Extraction tokens per selected page | 580 in + ~600 cached instructions, ~800 out | prototype prompt; output = JSON assertions |
| Extraction tiering | Haiku 5.5 on every selected page; Sonnet 5.5 on top-third by relevance | design choice; validate with eval |
| Query tokens | 4,395 input + 1,391 cached instructions, 1,999 output | **measured**: mean of the 5 prototype answers at the 12,000-code-point default budget (max output 2,316); at 6,000 the 14 measured answers average 2,417 in / 1,266 out |
| Answer latency | p50 ≈ 14 s at 12,000 code points, ≈ 10 s at 6,000 | measured, prototype traces |
| Questions / month | 15,000 | given; burst 5 req/s |
| Retention | raw files: life of contract; DB traces hot 90 days; S3 traces 2 years (then Glacier); logs 30 days | design |

Outside the estimate: data transfer to customers beyond the free tier, support plans, human review labour, a reviewer
web front end's CDN (small), taxes, and any temporary credits.

### 6.2 Recurring monthly cost (on-demand)

| Item | Calculation | $/month |
|---|---|---:|
| RDS PostgreSQL Multi-AZ db.m7g.large | $0.336/h × 730 | 245.3 |
| RDS gp3 storage 100 GB (×2 Multi-AZ) + backup | 200 GB × $0.115 + ~$5 | 28.0 |
| ECS Fargate API, 2 × (1 vCPU, 3 GB) ARM | 2 × ($0.03238 + 3 × $0.00356) × 730 | 62.9 |
| ECS Fargate Spot workers (routine + bursts) | ≈ 400 task-h × ~$0.024 | 10.0 |
| Step Functions + SQS | ~120k transitions × $0.025/1k + SQS | 4.0 |
| ALB | $0.0225 × 730 + ~2 LCU × $0.008 × 730 | 28.1 |
| WAF | $5 ACL + 5 rules + requests | 10.0 |
| NAT gateway (1) + Bedrock interface endpoint (2 AZ) | $0.045 × 730 + data; 2 × $0.01 × 730 | 49.7 |
| S3 storage + requests | ~110 GB × $0.023 + requests | 6.0 |
| CloudWatch logs/metrics/alarms | 15 GB × $0.50 + metrics + alarms | 16.0 |
| KMS (11 CMKs) + Secrets Manager | 11 × $1 + requests + 4 × $0.40 | 14.6 |
| Athena/Glue, ECR | small scans, catalog free tier | 2.2 |
| **Baseline subtotal** | | **≈ 477** |
| OCR (Textract) | 4,000 pages × $0.0015 | 6.0 |
| Embedding (bge-base on Fargate Spot) | 166k chunks; ≤ 46 task-h | 2.0 |
| Extraction, routine (on-demand, in-region) | 18k pages Haiku × $0.00255 + 6k pages Sonnet × $0.0102 | 107.1 |
| Extraction, burst (Bedrock batch, −50%) | 6k Haiku × $0.00128 + 2k Sonnet × $0.0051 | 17.9 |
| Answers (Sonnet 5.5 in-region) | 15,000 × (4,395 × $2.20 + 1,391 × $0.22 + 1,999 × $11.00)/1M = 15,000 × $0.0320 | 479.5 |
| **Usage subtotal** | | **≈ 613** |
| **Total (on-demand)** | | **≈ $1,090 — $90 over target** |

Per-page extraction cost is 580 input + 600 cached + 800 output tokens. That is $0.00255 on Haiku 5.5 in-region
($0.55 / $0.055 / $2.75 per MTok) and $0.0102 on Sonnet 5.5 in-region ($2.20 / $0.22 / $11.00).

### 6.3 Backfill (one-time, 500,000 pages)

| Item | Calculation | $ |
|---|---|---:|
| OCR | 25,000 pages × $0.0015 | 37.5 |
| Embedding | 1.04M chunks at 4 chunks/s/task → 72 task-h on-demand (Spot ≈ $3.5) | 11.4 |
| Extraction, Haiku 5.5 batch | 150k selected pages × $0.00128 | 191.4 |
| Extraction, Sonnet 5.5 batch (top third) | 50k pages × $0.0051 | 255.0 |
| Temporary DB scale-up (3 days) + S3 requests | | 29.0 |
| **Total** | | **≈ $524 — $24 over the $500 limit** |

**Smallest fix:** run Sonnet only on the top 25,000 pages by relevance, about the top 17% of selected pages, instead of
the top third. Sonnet drops to $127.5 and the **total is ≈ $397**. The remaining pages keep Haiku extraction and can be
upgraded to Sonnet later from the routine monthly budget as they become relevant to questions.

The public regulatory corpus is backfilled once and shared read-only, so this does not multiply by tenant. If the
embedding rate on Fargate is as slow as the prototype laptop (≈ 1 chunk/s under contention), embedding rises to ≈ $46
on-demand and the fixed backfill total is ≈ $432, still below $500.

### 6.4 Shortfall, smallest fixes and sensitivity

On-demand with measured tokens: **≈ $1,090/month, $90 over**. Answers are 44% of the total, and output tokens are
69% of each answer's cost. The fixes below are in order of how little they change the service:

| Change | Effect | Total |
|---|---|---:|
| None (on-demand, measured tokens) | | ≈ $1,090 |
| 1. 1-year no-upfront RDS reservation | ≈ −35% on $245 ≈ −$86; no service change, but a 1-year commitment | ≈ $1,004 (still ≈ $4 over) |
| 2. Plus a concise answer format: shorter claims, quotes cited once, `max_tokens` about 1,600, truncation reported as a failure | output 1,999 → ≈ 1,400 tokens: −$99 | **≈ $905 (9% headroom)** |

Fix 2 is not yet measured. It ships only if the regression evaluation shows no loss in verified claims or "not found"
reporting. If it fails, the fallback is a **service-level change**: use a 6,000-code-point default evidence budget for
routine questions and 12,000 on request. With measured 6,000-budget tokens (2,417 in / 1,266 out), answers cost
≈ $293 and the total is ≈ $903 *without* the reservation. The cost is lower recall: in the evaluation, expected
evidence was packed in only 3 of 10 runs at 6,000.

Sensitivity:

- **Query tokens ×2** (24k-code-point evidence, about 4,000-token answers): answers ≈ $959, so the on-demand total is
  ≈ $1,570. This load needs per-tenant pass-through of model usage; the prototype ledger is already scoped per tenant.
- **All-Sonnet extraction** (no Haiku tier): extraction ≈ $286 instead of ≈ $125, so the total is ≈ $1,250 on-demand.
- **Single-AZ RDS** would save ≈ $134, but its 99.5% SLA leaves no margin for the rest of the stack, so it is not
  recommended.

### 6.4a Throughput and index capacity

- **Bedrock throughput at the 5 req/s burst.** With measured p50 latency of about 14 s, about 70 answers are in flight.
  Tokens per minute are about 300 × (4,395 + 1,391 + 1,999) ≈ **2.3M TPM**, of which ≈ 600k is output. This must be
  confirmed against the account's Bedrock quota before launch. Without a raised quota, requests queue behind SQS with
  429 backoff, and overflow goes to the US cross-region inference profile.
- **Vector index memory.** The backfill gives about 1.04M chunks × 768 dims × 4 B ≈ 3.2 GB of vectors in the table,
  plus about 3.5 GB for an HNSW index. That cannot stay cached on a db.m7g.large (8 GB RAM) alongside the working set.
  Plan: index `halfvec(768)` (pgvector ≥ 0.7), which brings the index to about 1.8 GB, and validate recall on the
  regression set. The fallback is db.r7g.large (roughly +$100/month Multi-AZ; price not confirmed for this document).
- **Filtered ANN under RLS.** The prototype does an exact scan (1,203 chunks), and its `DISTINCT ON` query cannot use
  the HNSW index. In production, HNSW candidates are filtered by RLS after the search, so recall drops for small
  tenants. Plan: pgvector 0.8 iterative index scans (`hnsw.iterative_scan`). At the 10× scale, partition embeddings by
  tenant.

### 6.5 Freshness and backlog

- **Routine 2,000 pages/day** (~83 pages/h, ~175 chunks/h): one worker keeps up; a document is vector-searchable and in
  the structural graph within ~5–10 minutes of upload, and semantically extracted (on-demand) within ~15–30 minutes.
- **20,000-page burst:** 41,600 chunks. Embedding with 8 Spot tasks at an assumed 4 chunks/s/task = 32 chunks/s →
  ≈ 22 min; at the prototype's measured ≈ 1 chunk/s/task, 16 tasks → ≈ 45 min. OCR of ~1,000 pages via async
  Textract runs in parallel (~15–30 min, depends on account quota). **Burst documents are searchable in ≈ 30–60 min**,
  not inside the routine 30-minute target. Semantic extraction for the burst goes to Bedrock batch (−50%); jobs can
  take up to 24 h, so graph enrichment for burst documents is a ≤ 24 h target. Until then the answer trace and
  coverage table state that semantic extraction is pending for those sources.

### 6.6 Availability (99.5% ≈ 3.65 h/month)

RDS Multi-AZ (automatic failover), ≥ 2 API tasks across two AZs behind the ALB, SQS buffering for ingestion so worker
outages delay rather than lose work, and degraded answers (evidence without generation) during model outages. Bedrock
throttling falls back to the US geo cross-region inference profile (keeps US residency).

### 6.7 10× growth path (100 customers, 5M pages, 150k questions/month)

No ground-up replacement: scale RDS vertically (r7g.xlarge/2xlarge) and add a read replica for retrieval; partition
chunk/embedding tables by tenant; move the largest tenants to their own database (the scope abstraction already
isolates connection handling). If vector search outgrows pgvector (~tens of millions of chunks), swap the `vector_search`
adapter to OpenSearch Serverless while keeping Postgres as the system of record. Model cost scales linearly
(≈ $4.8k/month for answers at 10× on measured tokens, ≈ $3.3k with the concise format), so pricing must pass model usage through per tenant; the prototype usage ledger is
already scoped for that.

## 7. Data for research, evaluation, monitoring and improvement

- **Canonical formats** (details in data-contracts.md): raw originals in S3; normalized passages JSONL/Parquet;
  graph builds as nodes/edges Parquet + JSONL per build id; answers and traces JSONL partitioned by tenant/project/date;
  review events append-only JSONL; eval cases/results JSONL and reports JSON. Analytics read S3 via Athena, never the
  production database.
- **Tracing/monitoring:** every answer stores seeds, traversed edges, selected passages, exact evidence text, versions,
  latency and token usage. Metrics: p50/p95 latency, empty-retrieval rate, citation verification failure rate,
  `not_found` rate, extraction coverage and failures per source, spend per tenant vs budget. Alarms on DLQ depth,
  verification failures, spend.
- **Offline evaluation:** the prototype regression suite (dev + frozen holdout, deterministic scoring from saved
  traces) runs in CI on any change to model, prompt, ontology, extractor or index; promotion requires no regression.
- **Online evaluation and feedback:** deterministic checks run on 100% of answers (citation verification, budget,
  empty evidence); reviewer feedback (thumbs, corrections, approved assertions) is joined to traces by trace id and
  becomes new labelled regression cases after review. Reviewer-approved assertions are the training/evaluation signal
  for future agents; no automated judge is treated as expert confirmation.

## 8. Pricing sources (retrieved 2026-10-13, us-east-1)

- RDS for PostgreSQL: https://aws.amazon.com/rds/postgresql/pricing/ (db.m7g.large $0.168/h single-AZ; Multi-AZ doubles)
- Fargate: https://aws.amazon.com/fargate/pricing/ (Linux/ARM $0.03238 per vCPU-h, $0.00356 per GB-h; Spot ≈ −70%)
- Bedrock: https://aws.amazon.com/bedrock/pricing/ and Anthropic list prices 2026-10-07
  https://assets.claude.com/1ef7a4a8e373004bc89d710b0a6f3f219323344f.pdf (Bedrock in-region: Sonnet 5.5 $2.20/$11.00 per
  MTok, cache read $0.22; batch −50%)
- Anthropic model list prices, re-checked 2026-10-14: https://www.anthropic.com/pricing (Sonnet 5.5 $2 / $10 per MTok;
  Haiku 5.5 $0.50 / $2.50 for prompts ≤ 100K tokens; US-only inference 1.1×). In-region figures above apply 1.1× to
  these: Haiku 5.5 $0.55 / $2.75. Cache reads are taken as 0.1× input.
- Textract: https://aws.amazon.com/textract/pricing/ (DetectDocumentText $1.50 per 1,000 pages, first 1M pages/month)
- VPC / NAT / PrivateLink: https://aws.amazon.com/vpc/pricing/, https://aws.amazon.com/privatelink/pricing/
- S3: https://aws.amazon.com/s3/pricing/ ($0.023/GB-month Standard)
- CloudWatch: https://aws.amazon.com/cloudwatch/pricing/ ($0.50/GB logs ingested)
- Elastic Load Balancing: https://aws.amazon.com/elasticloadbalancing/pricing/ (ALB $0.0225/h + $0.008/LCU-h)

## 9. Retain / replace / defer / validate

- **Retain:** Postgres-centric evidence graph, RLS scope model, scoped embedding/extraction caches, quote validator,
  citation verifier, trace schema, regression eval harness.
- **Replace:** CLI orchestration → Step Functions/SQS; dev tokens → Cognito; Anthropic API → Bedrock in-region; files → S3.
- **Defer:** graph database, as-of historical querying, fine-tuning, multi-region active-active.
- **Validate before launch:** Haiku 5.5 extraction quality vs Sonnet on the regression set; bge-base throughput on
  Fargate ARM; the public ∪ project RLS policy with an isolation test; Textract and Bedrock account quotas for bursts;
  real query token distribution from pilot traffic (drives §6.4); the concise answer format against the regression
  set; Bedrock TPM quota for the 5 req/s burst (§6.4a); `halfvec` HNSW recall and iterative scans under RLS.
