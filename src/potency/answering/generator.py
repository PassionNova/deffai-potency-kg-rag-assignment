"""Answer generation over packed evidence. `LLMGenerator` is the real path; `TestDoubleGenerator` is offline only."""

import re
from dataclasses import dataclass

from potency.settings import REPO_ROOT

PROMPT_VERSION = "answer_v1"

ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Submit the grounded answer with claim-level citations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}},
                                "required": ["evidence_id", "quote"],
                            },
                        },
                    },
                    "required": ["text", "evidence"],
                },
            },
            "not_found": {"type": "array", "items": {"type": "string"}},
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "resolution": {"type": "string", "enum": ["unresolved", "resolved_by_evidence"]},
                    },
                    "required": ["description", "evidence_ids", "resolution"],
                },
            },
            "uncertainty": {"type": "string"},
        },
        "required": ["answer", "claims", "not_found", "conflicts", "uncertainty"],
    },
}


@dataclass
class Generation:
    output: dict
    usage: dict
    model: str
    elapsed_ms: int
    is_test_double: bool
    request_id: str | None = None


def system_prompt() -> str:
    return (REPO_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def user_prompt(question: str, case_context: str, evidence_text: str) -> str:
    return (
        f"Case configuration (instruction context, not evidence): {case_context}\n\n"
        f"Question: {question}\n\n"
        f"<evidence>\n{evidence_text}\n</evidence>"
    )


class LLMGenerator:
    is_test_double = False
    prompt_version = PROMPT_VERSION

    def __init__(self, llm):
        self._llm = llm
        self.model_id = llm.model_id

    def generate(self, question: str, case_context: str, evidence_text: str) -> Generation:
        res = self._llm.call_tool(system=system_prompt(), user=user_prompt(question, case_context, evidence_text),
                                  tool=ANSWER_TOOL, max_tokens=8000)
        return Generation(res.output, res.usage, res.model, res.elapsed_ms, False, res.request_id)


class TestDoubleGenerator:
    """Deterministic offline stand-in: quotes up to three sentences from each of the first three blocks.
    It does not reason, detect conflicts or report absence; never a reported run."""

    is_test_double = True
    prompt_version = PROMPT_VERSION
    model_id = "test-double-generator-v1"

    def generate(self, question: str, case_context: str, evidence_text: str) -> Generation:
        blocks = re.findall(r"^\[(E\d+)\][^\n]*\n(.*?)(?=\n\n\[E\d+\]|\Z)", evidence_text, flags=re.S | re.M)
        claims = []
        for eid, body in blocks[:3]:
            body = next((s.strip() for s in body.split("[...]") if s.strip()), "")
            for sentence in re.split(r"(?<=\.)\s+", body)[:3]:
                sentence = sentence[:300].strip()
                if sentence:
                    claims.append({"text": f"Evidence {eid} states: {sentence}",
                                   "evidence": [{"evidence_id": eid, "quote": sentence}]})
        answer = "TEST DOUBLE ANSWER. " + " ".join(f"{c['text']} [{c['evidence'][0]['evidence_id']}]" for c in claims)
        return Generation(
            {"answer": answer, "claims": claims, "not_found": [] if claims else ["no evidence supplied"],
             "conflicts": [], "uncertainty": "test double output"},
            {}, self.model_id, 0, True,
        )


def get_generator():
    from potency.settings import get_settings

    if get_settings().potency_llm == "fake":
        return TestDoubleGenerator()
    from potency.adapters.anthropic_llm import get_llm

    return LLMGenerator(get_llm())
