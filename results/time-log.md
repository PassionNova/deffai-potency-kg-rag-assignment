# Time log

Times are UTC on 2026-10-14, rebuilt from file creation/modification times, run IDs and artifact timestamps. They are
approximate to within about 5 minutes. The first handoff took about **5 h 10 min** from repository init.

| UTC | Duration | Work area | Evidence |
|---|---|---|---|
| 02:48 – 05:37 | ~2 h 50 min | Reading the brief, contracts, schemas, fixtures and corpus sample; stack and design discussion; implementation plan; model-access questions to DeffAI | git init 02:48; first source file 05:37 |
| 05:37 – 06:00 | ~25 min | Scaffold (uv project, docker-compose, Alembic migration with RLS and app role) and all domain modules: importer, chunking, embeddings, extraction, graph, retrieval, answering, workflows, CLI | file creation 05:37 – 06:00 |
| 06:00 – 06:20 | ~20 min | Evaluation cases and scorer, FastAPI layer, pytest suite with test doubles | 06:15 – 06:20 |
| 06:15 – 06:35 | ~20 min (overlapping) | Import and embedding build; the first build lost its final bulk write, so it was changed to per-batch, resumable inserts; Docker Desktop/WSL hang and restart; YAML key and importer fixes | `embeddings.py` 06:17, `importer.py` 06:32 |
| 06:38 – 06:58 | ~20 min | AWS architecture, Mermaid diagram, cost/capacity model, data contracts | 06:38 – 06:58 |
| 06:55 – 06:57 | ~5 min | Model-access smoke checks; structured-output fallback (`output_config` JSON schema) | `access-check.json` 06:56 |
| 06:57 – 07:10 | ~13 min | Claude extraction (strict-schema fixes, thread race fix, re-run) and graph build/export | extraction run 07:08, graph 07:09 |
| 07:10 – 07:27 | ~17 min | P1–P6 answers; P3 diagnosis; holdout freeze; graph-v2 scoring; verifier line-number tolerance; final answer run; P3 notes | answers 07:10 / 07:18 / 07:23; freeze 07:14 |
| 07:28 – 07:30 | ~3 min | Isolation fixture, import report, correction/conflict fixtures (real model) | 07:28 – 07:30 |
| 07:30 – 07:37 | ~7 min | Evaluation run, offline rescore, failure analysis, usage report | 07:33 – 07:36 |
| 07:37 – 07:50 | ~13 min | README handoff, time log, lint, cleanup | this file |
| 07:50 – 13:30 | — | Break (not active work) | |
| 13:30 – ~13:50 | ~20 min | Second AI agent's review of the repository against the contract and the code | review session start 13:30 |
| 14:32 – 14:50 | ~18 min | Fixes from the review: doc corrections, cost model re-based on measured tokens and re-checked Haiku price, `.gitattributes`, commit `c7a84df`, clean-tree re-run of the main answers | answers 14:39 |

Active total including the review and fixes: about **5 h 50 min**.

Coding went fast because an AI coding agent generated most of the modules from the plan. The verification steps used
for that code are listed in the README under "How AI-generated work was verified".
