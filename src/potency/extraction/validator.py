"""Validation of model-proposed assertions. A quote that is not in the passage is rejected, never repaired."""

import re

from pydantic import ValidationError

from potency.extraction.schema import ASSAY_ROLES, Assertion
from potency.textutil import find_exact_span

_REDACTION = re.compile(r"\(b\)\s*\(\d\)")
_STN = re.compile(r"(\d{6})")


def normalize_application_id(value: str) -> str | None:
    m = _STN.search(value)
    return f"STN {m.group(1)}" if m else None


def validate_assertions(passage_text: str, raw_assertions: list, max_assertions: int) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for i, raw in enumerate(raw_assertions or []):
        if len(accepted) >= max_assertions:
            rejected.append({"index": i, "reason": "over_max_assertions_per_passage", "raw": raw})
            continue
        try:
            a = Assertion.model_validate(raw)
        except ValidationError as exc:
            rejected.append({"index": i, "reason": f"schema: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}",
                             "raw": raw})
            continue
        exact = find_exact_span(passage_text, a.quote)
        if exact is None:
            rejected.append({"index": i, "reason": "quote_not_found_in_passage", "raw": raw})
            continue
        keys = {e.key for e in a.entities}
        if len(keys) != len(a.entities):
            rejected.append({"index": i, "reason": "duplicate_entity_keys", "raw": raw})
            continue
        dropped = []
        relations = []
        for r in a.relations:
            if r.subject not in keys:
                dropped.append({"relation": r.model_dump(), "reason": "unknown_subject_key"})
            elif r.type == "used_as":
                role = r.role or (r.object if r.object in ASSAY_ROLES else None)
                if role is None:
                    dropped.append({"relation": r.model_dump(), "reason": "used_as_without_role"})
                else:
                    relations.append(r.model_copy(update={"role": role, "object": role}))
            elif r.object not in keys:
                dropped.append({"relation": r.model_dump(), "reason": "unknown_object_key"})
            else:
                relations.append(r)
        entities = [
            e.model_copy(update={"redacted": True}) if _REDACTION.search(e.name) else e for e in a.entities
        ]
        applies = a.applies_to.model_copy(
            update={
                "application_ids": sorted(
                    {x for x in (normalize_application_id(v) for v in a.applies_to.application_ids) if x}
                )
            }
        )
        out = a.model_copy(update={"relations": relations, "entities": entities, "applies_to": applies}).model_dump()
        out["exact_quote"] = exact
        out["passage_has_redaction"] = bool(_REDACTION.search(exact))
        out["dropped_relations"] = dropped
        accepted.append(out)
    return accepted, rejected
