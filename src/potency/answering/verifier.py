"""Citation verification: every quote must appear in the cited evidence block AND resolve to an exact substring of
the current, in-scope source passage. Failures are reported, never repaired."""

from potency.retrieval.packer import EvidenceBlock
from potency.tenancy import Scope
from potency.textutil import match_form, match_span


def verify(output: dict, blocks: list[EvidenceBlock], passage_texts: dict[str, str], scope: Scope) -> dict:
    by_id = {b.evidence_id: b for b in blocks}
    citations: list[dict] = []
    failures: list[dict] = []
    claims_out: list[dict] = []
    for ci, claim in enumerate(output.get("claims") or []):
        verified_any = False
        claim_cites = []
        for ev in claim.get("evidence") or []:
            eid, quote = ev.get("evidence_id"), (ev.get("quote") or "").strip()
            block = by_id.get(eid)
            if block is None:
                failures.append({"claim_index": ci, "evidence_id": eid, "reason": "unknown_evidence_id"})
                continue
            if not quote or (match_form(quote) not in match_form(block.excerpt)
                             and match_span(block.excerpt, quote)[0] is None):
                failures.append({"claim_index": ci, "evidence_id": eid, "reason": "quote_not_in_evidence_block",
                                 "quote": quote})
                continue
            exact, skipped_numbers = match_span(passage_texts[block.passage_pk], quote)
            if exact is None:
                failures.append({"claim_index": ci, "evidence_id": eid, "reason": "quote_not_in_source_passage",
                                 "quote": quote})
                continue
            cite = {
                "scope": scope.as_dict(),
                "source_id": block.source_id,
                "source_version": block.source_version,
                "passage_id": block.passage_id,
                "page": block.page,
                "section": block.section,
                "quote": exact,
                "evidence_id": eid,
                "claim_index": ci,
                "document_authority": block.authority,
                "document_status": block.status,
                "match": "skipped_source_number_tokens" if skipped_numbers else "exact",
                "skipped_source_number_tokens": skipped_numbers,
            }
            citations.append(cite)
            claim_cites.append(eid)
            verified_any = True
        claims_out.append({"text": claim.get("text", ""), "evidence_ids": claim_cites, "verified": verified_any})
    referenced = {c["evidence_id"] for c in citations}
    conflicts = output.get("conflicts") or []
    for cf in conflicts:
        for eid in cf.get("evidence_ids") or []:
            if eid not in by_id:
                failures.append({"conflict": cf.get("description"), "evidence_id": eid, "reason": "unknown_evidence_id"})
    n_claims = len(claims_out)
    return {
        "citations": citations,
        "claims": claims_out,
        "failures": failures,
        "summary": {
            "claims": n_claims,
            "verified_claims": sum(1 for c in claims_out if c["verified"]),
            "citations_verified": len(citations),
            "citations_with_skipped_number_tokens": sum(1 for c in citations if c["skipped_source_number_tokens"]),
            "citation_failures": len(failures),
            "evidence_blocks_cited": len(referenced),
            "evidence_blocks_supplied": len(blocks),
        },
    }
