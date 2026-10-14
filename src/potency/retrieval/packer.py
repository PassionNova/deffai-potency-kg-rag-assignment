"""Evidence packing under a Unicode code-point budget (Python len over the exact string sent to the model).

The budget covers everything source-derived in the prompt: block headers (document title, passage id, page,
authority/status metadata) and excerpt text. Excerpts are verbatim source text with layout whitespace collapsed;
"[...]" marks a cut. Both retrieval modes use this same packer and policy.
"""

from dataclasses import dataclass, field

from potency.textutil import code_points, collapse_ws

PACKER_VERSION = "packer-v1"
EXCERPT_CHARS = 1100
PER_SOURCE_CAP = 3
MIN_EXCERPT = 300
CUT = "[...]"


@dataclass
class Candidate:
    passage_pk: str
    score: float
    quote_hint: str | None = None
    chunk_span: tuple[int, int] | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class EvidenceBlock:
    evidence_id: str
    passage_pk: str
    source_id: str
    source_version: str
    passage_id: str
    page: int | None
    section: str | None
    title: str | None
    authority: str | None
    status: str | None
    excerpt: str
    header: str
    score: float


@dataclass
class Packed:
    evidence_text: str
    blocks: list[EvidenceBlock]
    budget: int
    chars: int
    skipped: list[dict]


def _window(norm: str, center_start: int, center_len: int, size: int) -> tuple[int, int]:
    if len(norm) <= size:
        return 0, len(norm)
    lead = max(0, (size - center_len) // 3)
    start = max(0, min(center_start - lead, len(norm) - size))
    end = min(len(norm), start + size)
    if start > 0:
        sp = norm.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 40 else start
    if end < len(norm):
        sp = norm.rfind(" ", end - 40, end)
        end = sp if sp > start else end
    return start, end


def excerpt_for(text: str, quote_hint: str | None, chunk_span: tuple[int, int] | None, size: int) -> str:
    norm = collapse_ws(text)
    pos, qlen = 0, 0
    if quote_hint:
        q = collapse_ws(quote_hint)
        pos = norm.lower().find(q.lower())
        qlen = len(q)
        if pos < 0:
            pos, qlen = 0, 0
    elif chunk_span:
        pos, qlen = chunk_span[0], min(chunk_span[1] - chunk_span[0], size)
    start, end = _window(norm, pos, qlen, size)
    body = norm[start:end]
    return (CUT + " " if start > 0 else "") + body + (" " + CUT if end < len(norm) else "")


def pack(conn, candidates: list[Candidate], budget: int, excerpt_chars: int = EXCERPT_CHARS,
         per_source_cap: int = PER_SOURCE_CAP) -> Packed:
    pks = [c.passage_pk for c in candidates]
    rows = {
        r["id"]: r
        for r in conn.execute(
            """
            SELECT p.id, p.source_id, p.source_version, p.passage_id, p.page, p.section, p.text, p.authority,
                   p.status, d.title
            FROM passage p JOIN source_version v ON v.id = p.version_id JOIN source_document d ON d.id = v.document_id
            WHERE p.id = ANY(%s) AND p.is_current
            """,
            (pks,),
        ).fetchall()
    }
    blocks: list[EvidenceBlock] = []
    skipped: list[dict] = []
    per_source: dict[str, int] = {}
    parts: list[str] = []
    used = 0
    for c in candidates:
        r = rows.get(c.passage_pk)
        if r is None:
            skipped.append({"passage_pk": c.passage_pk, "reason": "not_current_or_not_in_scope"})
            continue
        if per_source.get(r["source_id"], 0) >= per_source_cap:
            skipped.append({"passage_pk": c.passage_pk, "reason": "per_source_cap"})
            continue
        eid = f"E{len(blocks) + 1}"
        page = f"p.{r['page']}" if r["page"] is not None else (r["section"] or "")
        header = f"[{eid}] {r['title']} | {r['passage_id']} | {page} | document authority: {r['authority']}; status: {r['status']}"
        sep = "\n\n" if parts else ""
        remaining = budget - used - code_points(sep) - code_points(header) - 1
        if remaining < MIN_EXCERPT:
            skipped.append({"passage_pk": c.passage_pk, "reason": "evidence_budget_exhausted"})
            continue
        size = min(excerpt_chars, remaining - 2 * (len(CUT) + 1))
        excerpt = excerpt_for(r["text"], c.quote_hint, c.chunk_span, size)
        block = f"{header}\n{excerpt}"
        if used + code_points(sep) + code_points(block) > budget:
            skipped.append({"passage_pk": c.passage_pk, "reason": "evidence_budget_exhausted"})
            continue
        parts.append(sep + block)
        used += code_points(sep) + code_points(block)
        per_source[r["source_id"]] = per_source.get(r["source_id"], 0) + 1
        blocks.append(EvidenceBlock(eid, r["id"], r["source_id"], r["source_version"], r["passage_id"], r["page"],
                                    r["section"], r["title"], r["authority"], r["status"], excerpt, header, c.score))
    text = "".join(parts)
    assert code_points(text) <= budget, "evidence budget violated"
    return Packed(text, blocks, budget, code_points(text), skipped)
