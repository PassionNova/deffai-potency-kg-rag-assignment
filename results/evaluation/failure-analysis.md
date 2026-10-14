# Evaluation: case design, results, and failure analysis

Run: `eval-20261014T073053Z-ea4124`. Real retrieval and generation (`claude-sonnet-5-5`, `BAAI/bge-base-en-v1.5`), no
test doubles. Each case ran in both modes with a 6,000 code-point evidence cap, the same cap as P3. Scores in
`report.json` can be recomputed without model calls via `potency eval score`.

## Case design

There are six candidate-authored cases (`origin: candidate_authored`): three dev and three holdout. Each one targets a
failure mode that matters for regulatory evidence work:

| Case | Split | Risk | What a correct answer must do |
|---|---|---|---|
| EV-D01 redacted criterion | dev | redaction | say the acceptance criterion is `(b) (4)`, and not produce a percentage |
| EV-D02 draft guidance authority | dev | document authority | report draft / nonbinding status from the 2023 document itself |
| EV-D03 absent EC50 | dev | insufficient evidence | report absence *in the inspected scope*, with no invented value (no expected passage, so retrieval is not applicable) |
| EV-H01 false correlation premise | holdout | misleading premise | reject the premise: the CMC reviewer says clinical-outcome correlation analyses were not performed |
| EV-H02 shelf-life potency gap | holdout | proposal vs conclusion | find the reviewer's reason (a later stability point lacked potency data), not only the SBRA summary |
| EV-H03 SCD PMC scope | holdout | scope (SCD vs TDT) | cite the SCD SBRA for the shipping-validation commitment that left potency-related attributes untested |

Expected evidence is a list of any-of groups: a run hits a group if *any* listed passage is packed. Answer checks are
regex patterns (required and forbidden), a "not found" check, a minimum number of verified citations, a zero
citation-failures check, and an optional set of required cited sources.

**Holdout freeze.** `potency eval freeze` recorded a SHA-256 of the holdout cases (`holdout-freeze.json`,
2026-10-14T07:14:11Z) before any evaluation run. `eval run` and `eval score` refuse to run if the holdout lines change.
After the freeze, three changes were made because of *visible P3* behaviour, not holdout results: graph-v2 scoring, the
verifier's line-number tolerance, and the raised answer `max_tokens` (see `../p3-comparison.md`). Holdout results were
seen only once, in this final run. Nothing has been tuned since.

**Visible supplied fixtures** (`fixtures/report.json`) are reported separately as `visible_supplied_regression`: 11 of
11 checks passed, with no test doubles. They are never mixed into the dev or holdout numbers.

## Results (denominators shown)

| Split / mode | All expected groups packed | Expected in candidate pool | All answer checks passed | Answer checks passed |
|---|---|---|---|---|
| dev / graph | 1 / 2 | 1 / 2 | 2 / 3 | 13 / 15 |
| dev / vector | 1 / 2 | 2 / 2 | 3 / 3 | 15 / 15 |
| holdout / graph | 1 / 3 | 2 / 3 | 1 / 3 | 11 / 13 |
| holdout / vector | 0 / 3 | 2 / 3 | 0 / 3 | 10 / 13 |
| **overall** | **3 / 10** | **7 / 10** | **6 / 12** | **49 / 56** |

In the retrieval columns, the absence case (EV-D03) is excluded from the denominators because it is not applicable.
Across all 12 answers, 55 of 56 claims have at least one verified citation, and 1 of 12 answers has a citation
failure. No answer produced a redacted value or a forbidden pattern. All 12 runs completed: 0 failed and 0
unscorable.

## Failures

1. **The budget cut drops evidence that was retrieved (the main failure: 3 of the 6 runs that failed answer
   checks).** Expected passages were in the candidate pool in 7 of 10 runs but were packed in only 3. For EV-H02 (both
   modes), CMC page 96 was a candidate but was cut at 6,000 code points. Vector mode still packed the 18-month
   shelf-life statement and correctly said the potency reason was not found. EV-H03 vector shows the same pattern. EV-D02
   vector also had the expected pages in its pool but not packed; its answer passed anyway, using page 4. The cause is ranking
   among candidates. Each block is an excerpt of up to 1,100 characters plus a provenance header, about 1,270 code
   points in all, so only about 5 blocks fit. Blocks are packed in score order with up to 3 per source, so general
   passages (often draft guidance pages) can take most of the slots.
2. **Vector retrieval misses premise-rejection evidence (EV-H01 vector).** The question's wording ("strongest
   correlation with clinical outcome") retrieved clinical-efficacy and method-validation pages, not CMC page 64. The
   answer still did not name an outcome. It said none of the excerpts reports such a correlation, so the premise was
   not accepted. However, it could not quote the reviewer's explicit statement, so the required-pattern check fails.
   Graph mode reached page 64 through `potency` assay entities and passed every check. This is the clearest
   graph-over-vector contribution in the set.
3. **Scope question not routed to the SCD SBRA (EV-H03, both modes).** Neither mode packed SCD SBRA page 8. That page
   *was* packed for EV-H01 vector, where the answer described the shipping-validation deficiency correctly. Retrieval
   for H03 favoured the DMPQ memo and the SCD SBRA front matter. Both answers correctly said no potency-related PMC was
   shown in the evidence. Neither cited a TDT document as if it covered SCD.
4. **Status wording without the nonbinding phrase (EV-D02 graph).** The answer said "draft" and "recommendations only"
   but not the document's "nonbinding" wording. The cover and disclaimer pages never entered the graph candidate pool
   (candidate group recall 0). I did not investigate the traversal path further, so as not to tune on this case. It also
   had one citation failure. Vector mode packed page 4, which carries the "Nonbinding Recommendations" header, and
   passed every check, but its retrieval is still scored as a miss because page 4 was not in the expected group (see
   the limitations below).

What held up: redaction (EV-D01) and absence (EV-D03) passed in both modes, with no invented values. Every failure is
a failure to find evidence that exists. None is a hallucinated answer.

## Limitations of these labels

- The labels are provisional and candidate-authored. I wrote the expected passages and regexes after reading the
  corpus. They are not expert-confirmed, and they are incomplete: EV-D02's vector run hit equivalent evidence on page 4,
  which the label does not list.
- Regex checks detect required or forbidden statements, not overall correctness. An answer can fail a required
  pattern by paraphrasing (EV-H01 vector says "none ... reports a correlation analysis") or pass while missing nuance.
- Six cases in two modes is a regression smoke set, not an accuracy estimate. The holdout is small and was written by
  the same person who built the system.

## Next fixes (not applied, to keep the holdout clean)

1. Use smaller quote-centred excerpts and source-diversity weighting, so 6,000 code points covers more distinct
   sources. Excerpts are already chunk- or quote-centred windows; the limit is their size and the per-source cap of 3.
2. Add a cross-encoder reranker over the candidate pool. It would be evaluated on new dev cases, and the holdout would
   be re-frozen with fresh cases.
3. Add structural seeds from question metadata (an STN or indication in the question seeds the matching
   `Application` node), so scope questions like EV-H03 reach the right SBRA.
