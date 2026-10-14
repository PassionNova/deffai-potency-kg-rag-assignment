from fastapi.testclient import TestClient

from potency.entrypoints.api import app
from potency.extraction.validator import validate_assertions
from potency.textutil import find_exact_span, match_span


def test_api_rejects_missing_unknown_and_foreign_scope_tokens():
    c = TestClient(app)
    url = "/v1/tenants/public-fda/projects/casgevy-potency/answers"
    assert c.post(url, json={"question": "potency?"}).status_code == 401
    assert c.post(url, json={"question": "potency?"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    other = "/v1/tenants/fixture-tenant/projects/project-one/answers"
    r = c.post(other, json={"question": "potency?"}, headers={"Authorization": "Bearer dev-public"})
    assert r.status_code == 403
    assert c.get("/v1/traces/does-not-exist", headers={"Authorization": "Bearer dev-public"}).status_code == 404


def test_exact_span_returns_source_text_not_model_text():
    src = "Potency  On-Target\nEditing “frequency” – TIDE"
    span = find_exact_span(src, 'potency on-target editing "frequency" - tide')
    assert span == src
    assert find_exact_span(src, "potency on-target editing rate") is None


def test_line_number_tolerance_is_surfaced_and_never_hides_quoted_numbers():
    src = "Potency assays should be qualified as soon 811 as feasible, at 25 mg."
    assert find_exact_span(src, "qualified as soon as feasible") is None
    span, skipped = match_span(src, "qualified as soon as feasible")
    assert span == "qualified as soon 811 as feasible" and skipped == ["811"]
    assert match_span(src, "at 25 mg") == ("at 25 mg", [])
    assert match_span(src, "at 30 mg") == (None, [])


def test_validator_rejects_unquoted_assertions():
    passage = "Vertex proposed (b) (4) as the sole measure of exa-cel potency."
    raw = [
        {"quote": "Vertex proposed (b) (4) as the sole measure of exa-cel potency", "assertion_type": "potency_strategy",
         "assertion_state": "proposed", "speaker": "applicant", "summary": "s", "entities": [], "relations": []},
        {"quote": "FDA accepted the TIDE assay as the sole potency assay", "assertion_type": "review_conclusion",
         "assertion_state": "accepted", "speaker": "fda_reviewer", "summary": "s", "entities": [], "relations": []},
    ]
    accepted, rejected = validate_assertions(passage, raw, 6)
    assert len(accepted) == 1 and accepted[0]["passage_has_redaction"]
    assert len(rejected) == 1
