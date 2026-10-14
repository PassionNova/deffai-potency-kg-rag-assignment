"""Passage-batch extractors. `LLMExtractor` is the real path; `RuleTestDoubleExtractor` is an offline test double."""

import re
from dataclasses import dataclass, field
from typing import Protocol

from potency.extraction.schema import ExtractorVersion, tool_schema
from potency.settings import REPO_ROOT
from potency.textutil import collapse_ws

PROMPT_VERSION = "extraction_v1"


@dataclass
class PassageInput:
    evidence_id: str
    passage_pk: str
    text: str
    title: str | None
    authority: str | None
    status: str | None
    source_type: str | None
    page: int | None
    section: str | None
    subject_scope: dict


@dataclass
class BatchResult:
    per_passage: dict[str, list]
    usage: dict = field(default_factory=dict)
    model: str = ""
    elapsed_ms: int = 0
    request_id: str | None = None


class Extractor(Protocol):
    actor: str
    model_id: str
    version: str
    prompt_version: str
    is_test_double: bool

    def extract(self, batch: list[PassageInput], max_assertions: int) -> BatchResult: ...


def _system_prompt(max_assertions: int) -> str:
    text = (REPO_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    return text.replace("{max_assertions}", str(max_assertions))


def _user_prompt(batch: list[PassageInput]) -> str:
    parts = []
    for p in batch:
        meta = (
            f"evidence_id: {p.evidence_id}\n"
            f"document: {p.title}\n"
            f"document_type: {p.source_type}; document_authority: {p.authority}; document_status: {p.status}\n"
            f"document_level_subject_metadata (not proof the passage applies to it): {p.subject_scope or 'none'}\n"
            f"page: {p.page}; section: {p.section}"
        )
        parts.append(f"<passage>\n{meta}\n<text>\n{collapse_ws(p.text)}\n</text>\n</passage>")
    return "\n\n".join(parts)


class LLMExtractor:
    is_test_double = False
    version = ExtractorVersion
    prompt_version = PROMPT_VERSION

    def __init__(self, llm):
        self._llm = llm
        self.model_id = llm.model_id
        self.actor = f"llm:{llm.model_id}"

    def extract(self, batch: list[PassageInput], max_assertions: int) -> BatchResult:
        res = self._llm.call_tool(
            system=_system_prompt(max_assertions),
            user=_user_prompt(batch),
            tool=tool_schema(),
            max_tokens=12000,
        )
        per = {p.evidence_id: [] for p in batch}
        for item in res.output.get("passages", []) or []:
            if isinstance(item, dict) and item.get("evidence_id") in per:
                per[item["evidence_id"]] = item.get("assertions") or []
        return BatchResult(per, res.usage, res.model, res.elapsed_ms, res.request_id)


_SENT = re.compile(r"[^.]*\.")


class RuleTestDoubleExtractor:
    """Deterministic pattern extractor for offline tests. Not used for reported runs."""

    is_test_double = True
    version = "rule-test-double-v1"
    prompt_version = "none"
    model_id = "rule-test-double-v1"
    actor = "rule:test-double-v1"

    _measures = re.compile(r"assay (\w+) measures attribute (\w+)", re.I)
    _measures_anon = re.compile(r"fictional assay measures attribute (\w+)", re.I)
    _used_as = re.compile(r"assay (\w+) is (not )?used as an? (\w+) assay", re.I)

    def extract(self, batch: list[PassageInput], max_assertions: int) -> BatchResult:
        per = {}
        for p in batch:
            text = collapse_ws(p.text)
            out = []
            for sent in _SENT.findall(text):
                sent = sent.strip()
                if m := self._measures.search(sent):
                    out.append(self._a(sent, "measured_attribute", [("e1", "Assay", m.group(1)),
                               ("e2", "MeasuredAttribute", m.group(2))], [("measures", "e1", "e2", None, False)]))
                elif m := self._measures_anon.search(sent):
                    out.append(self._a(sent, "measured_attribute", [("e1", "Assay", "fictional assay"),
                               ("e2", "MeasuredAttribute", m.group(1))], [("measures", "e1", "e2", None, False)]))
                elif m := self._used_as.search(sent):
                    role = m.group(3).lower()
                    out.append(self._a(sent, "assay_role", [("e1", "Assay", m.group(1))],
                                       [("used_as", "e1", role, role, bool(m.group(2)))]))
            per[p.evidence_id] = out[:max_assertions]
        return BatchResult(per, {}, self.model_id, 0)

    @staticmethod
    def _a(sent, atype, ents, rels) -> dict:
        return {
            "quote": sent,
            "summary": sent,
            "assertion_type": atype,
            "assertion_state": "reported",
            "speaker": "unknown",
            "applies_to": {"products": [], "application_ids": [], "indications": [], "basis": "unknown"},
            "event_date": None,
            "entities": [{"key": k, "type": t, "name": n, "redacted": False} for k, t, n in ents],
            "relations": [{"type": t, "subject": s, "object": o, "role": r, "negated": neg} for t, s, o, r, neg in rels],
        }


def get_extractor() -> Extractor:
    from potency.settings import get_settings

    if get_settings().potency_llm == "fake":
        return RuleTestDoubleExtractor()
    from potency.adapters.anthropic_llm import get_llm

    return LLMExtractor(get_llm())
