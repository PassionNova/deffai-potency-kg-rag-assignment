# P3: vector vs graph retrieval

Final run: `answers-20261014T143900Z-654868` (`results/answers.jsonl`, `results/traces.jsonl`), produced from commit
`c7a84df` with a clean tree. Retrieval and packing are deterministic and match the previous run
(`answers-20261014T072340Z-d689a9`) exactly; the generated wording differs because sampling is not fixed. Both modes use the same
corpus snapshot and index (`idx-9643b8f06092`), the same generation model (`claude-sonnet-5-5`), the same prompt
version, and the same packer. Each mode has an evidence cap of **6,000 Unicode code points** (Python `len`), and the cap
counts every source-derived character sent to generation. Graph mode sends no graph summaries or node descriptions; the
graph only decides *which* passages are packed. Vector used 5,989 code points and graph used 5,991.

## Declared retrieval policies

| | vector (`vector-v1`) | graph (`graph-v2`) |
|---|---|---|
| Seeds | top chunks by cosine similarity (bge-base-en-v1.5, exact pgvector scan over all 1,203 chunks in scope) | entity labels with `word_similarity >= 0.6` (top 8), plus the top 6 vector passages mapped to their SourcePassage and Assertion nodes |
| Traversal | none | undirected (edges followed both ways), at most 2 hops from any seed, at most 8 neighbours per node per hop, at most 60 visited nodes |
| Passage score | vector similarity | 0.5 x normalised graph score + 0.5 x vector similarity |
| Graph score | n/a | sum over distinct seed origins of the best decayed path score (edge weight x 0.7 per hop) reaching the passage |
| Packing | by score until the 6,000 cap; verbatim excerpt of at most 1,100 characters per block, centred on the best chunk or quote, at most 3 blocks per source | same packer |

## What each mode retrieved

**Vector** packed the statistical review page 6 (the TDT clinical-package exchange), SCD approval letter pages 5, 7 and 9,
and SCD SBRA page 20. None of these discusses potency. The model correctly said the potency sequence was not found, and
it explicitly declined to treat the clinical-package exchange as potency evidence.

**Graph** packed draft-guidance pages 7, 13 and 25, the TDT SBRA release specification (page 9), and CMC review
page 61. The answer reports the end state for STN 125785: potency is measured by on-target editing frequency (TIDE),
with the acceptance criterion redacted. It also reports the reviewer's rationale for the one-sided criterion and for no
upper limit on the redacted potency attributes. It states that the initial proposal, FDA's challenge and the revision
were not found in the packed evidence, and that nothing in that evidence covers STN 125787. It does not infer any
redacted value.

## Observed contribution and remaining failure

- **Contribution.** The question's wording ("initially proposed", "request or challenge", "what changed") is not close
  in embedding space to the CMC memo text. The graph reached that text through entities (`potency`, `CASGEVY`,
  `potency assay`) and the assertions and `used_as` edges extracted from those pages. All five graph passages are on topic;
  none of the five vector passages is.
- **Remaining failure.** The passages that hold the actual sequence are CMC review pages 62-c02 and 64 (single
  redacted measure proposed at BLA submission, correlation data, reviewer accepting a combination), plus the earlier
  request for biologically relevant assays. They were candidates ranked 6th and 9th, and the 6,000-point cap cut them
  (`packing_skipped: evidence_budget_exhausted`). Their graph support (2 origins, 0.87) ties with page 61, and their
  vector similarity is lower. The three draft-guidance pages take about half the budget with general recommendations. A
  12,000-point budget (used for P1, P2 and P4 to P6) does surface these pages: P2 and P5 cite 62-c02 and 64. So the
  graph-mode answer is correct but incomplete, and it is not a full proposal-to-conclusion narrative.
- **Citation check.** Graph P3: 7 of 7 claims verified, 8 verified citations, 0 failures (1 citation flagged for
  skipped PDF line numbers). Vector P3: 3 of 3 claims verified, 3 citations, 0 failures. In the previous run, graph P3
  had 1 rejected quote because the model dropped a comma ("verifications, were found"). Punctuation is part of the
  exact-match check, so the verifier rejected the quote rather than repairing it.

## Disclosure: retriever changes made after seeing visible P3 results

1. **graph-v1 (first run, `answers-20261014T071023Z`, archived in `results/answer-runs/...-graph-v1/`).** Graph mode
   packed *exactly the same five passages as vector*. Vector-seeded passages got a graph score equal to their vector
   similarity at hop 0, so the blend counted that similarity twice. Passages reached by traversal (CMC pages 61, 62, 64
   and 96, graph score 0.63) could never outrank them under the cap. In practice the graph contributed nothing.
2. **graph-v2 (the final runs).** A passage's graph score is now the *sum over distinct seed origins* rather than the
   maximum. All seeds from one vector hit share a single origin, so a vector-only passage counts once, and a passage
   connected to several matched concepts accumulates support. The edge weights, hop decay, limits, blend weights and
   packer are unchanged. This is a generic change with no question-specific terms, but it was motivated by a visible
   question, so treat P3 graph results as tuned.
3. **Verifier tolerance for PDF line numbers.** In the draft guidance's extracted text, line numbers are mixed into the
   prose (for example `soon 811 as feasible`), and correct quotes failed the exact-match check. The answer verifier now
   allows standalone 1 to 4 digit tokens *in the source* between quoted words. The stored citation is still the exact
   source substring, numbers included, and is flagged `match: skipped_source_number_tokens`. Digits inside the quote must
   still match. The extraction validator is unchanged.
4. **Answer `max_tokens` raised from 4,000 to 8,000** after one long answer was truncated.
5. The dev/holdout evaluation holdout was frozen (`results/evaluation/holdout-freeze.json`, 2026-10-14T07:14:11Z)
   *before* changes 2 to 4, and none of those changes used holdout results.
