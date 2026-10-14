from typing import Literal

from pydantic import BaseModel, Field

ASSERTION_STATES = ("reported", "proposed", "requested", "revised", "accepted", "rejected", "unresolved", "redacted")
ASSAY_ROLES = ("potency", "identity", "release", "characterization", "stability", "supporting")
SPEAKERS = (
    "regulation",
    "final_guidance",
    "draft_guidance",
    "applicant",
    "fda_reviewer",
    "fda_review_team",
    "agency_action",
    "advisory_or_other",
    "unknown",
)
ASSERTION_TYPES = (
    "potency_strategy",
    "assay_description",
    "assay_role",
    "measured_attribute",
    "specification_or_acceptance_criterion",
    "regulatory_requirement",
    "guidance_recommendation",
    "information_request",
    "applicant_response",
    "review_finding",
    "review_conclusion",
    "agency_action",
    "clinical_correlation",
    "postmarketing_commitment",
    "redaction_notice",
    "other",
)
ENTITY_TYPES = (
    "Product",
    "Application",
    "Indication",
    "PotencyStrategy",
    "Assay",
    "MeasuredAttribute",
    "RegulatoryInteraction",
    "ReviewConclusion",
    "Regulation",
    "Guidance",
)
RELATION_TYPES = (
    "measures",
    "used_as",
    "proposed_in",
    "requested_in",
    "revised_by",
    "concluded_in",
    "applies_to",
    "cites",
    "supports",
    "part_of",
)

ExtractorVersion = "extractor-v1"


class AppliesTo(BaseModel):
    products: list[str] = Field(default_factory=list)
    application_ids: list[str] = Field(default_factory=list)
    indications: list[str] = Field(default_factory=list)
    basis: Literal["explicit_in_passage", "document_context", "unknown"] = "unknown"


class Entity(BaseModel):
    key: str
    type: Literal[ENTITY_TYPES]  # type: ignore[valid-type]
    name: str = Field(min_length=1)
    redacted: bool = False


class Relation(BaseModel):
    type: Literal[RELATION_TYPES]  # type: ignore[valid-type]
    subject: str
    object: str
    role: Literal[ASSAY_ROLES] | None = None  # type: ignore[valid-type]
    negated: bool = False


class Assertion(BaseModel):
    quote: str = Field(min_length=3)
    summary: str
    assertion_type: Literal[ASSERTION_TYPES]  # type: ignore[valid-type]
    assertion_state: Literal[ASSERTION_STATES]  # type: ignore[valid-type]
    speaker: Literal[SPEAKERS]  # type: ignore[valid-type]
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    event_date: str | None = None
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


def tool_schema() -> dict:
    str_list = {"type": "array", "items": {"type": "string"}}
    assertion = {
        "type": "object",
        "properties": {
            "quote": {"type": "string", "description": "Verbatim span copied from the passage (<=400 chars)."},
            "summary": {"type": "string", "description": "One-sentence neutral paraphrase."},
            "assertion_type": {"type": "string", "enum": list(ASSERTION_TYPES)},
            "assertion_state": {"type": "string", "enum": list(ASSERTION_STATES)},
            "speaker": {"type": "string", "enum": list(SPEAKERS)},
            "applies_to": {
                "type": "object",
                "properties": {
                    "products": str_list,
                    "application_ids": str_list,
                    "indications": str_list,
                    "basis": {"type": "string", "enum": ["explicit_in_passage", "document_context", "unknown"]},
                },
                "required": ["products", "application_ids", "indications", "basis"],
            },
            "event_date": {"type": ["string", "null"], "description": "YYYY-MM-DD if the passage dates the event."},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                        "name": {"type": "string"},
                        "redacted": {"type": "boolean"},
                    },
                    "required": ["key", "type", "name", "redacted"],
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(RELATION_TYPES)},
                        "subject": {"type": "string"},
                        "object": {"type": "string"},
                        "role": {"type": ["string", "null"], "enum": [*ASSAY_ROLES, None]},
                        "negated": {"type": "boolean"},
                    },
                    "required": ["type", "subject", "object", "negated"],
                },
            },
        },
        "required": ["quote", "summary", "assertion_type", "assertion_state", "speaker", "applies_to", "entities",
                     "relations"],
    }
    return {
        "name": "record_extractions",
        "description": "Record evidence-grounded assertions for each supplied passage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "passages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "assertions": {"type": "array", "items": assertion},
                        },
                        "required": ["evidence_id", "assertions"],
                    },
                }
            },
            "required": ["passages"],
        },
    }
