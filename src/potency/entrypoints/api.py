"""Thin HTTP layer over the same answering service the CLI uses.

Auth is a development stand-in: POTENCY_DEV_TOKENS maps bearer tokens to the scopes they may read
("token=tenant/project[|tenant/project],..."). In production this is replaced by OIDC claims -> scope grants;
the database still enforces row-level security on every query regardless of this check."""

from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from potency import __version__, ids
from potency.db import scoped_session
from potency.settings import get_settings
from potency.tenancy import Scope

app = FastAPI(title="Potency evidence API", version=__version__)
_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _token_map(raw: str) -> dict[str, frozenset[Scope]]:
    out: dict[str, frozenset[Scope]] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        token, _, scopes = entry.partition("=")
        parsed = []
        for s in scopes.split("|"):
            tenant, _, project = s.strip().partition("/")
            if tenant and project:
                parsed.append(Scope(tenant, project))
        out[token.strip()] = frozenset(parsed)
    return out


def granted_scopes(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> frozenset[Scope]:
    if cred is None:
        raise HTTPException(401, "missing bearer token")
    scopes = _token_map(get_settings().potency_dev_tokens).get(cred.credentials)
    if not scopes:
        raise HTTPException(401, "unknown token")
    return scopes


class AnswerRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: str = Field("graph", pattern="^(graph|vector)$")
    evidence_budget_chars: int = Field(6000, ge=1000, le=20000)
    case_config: str | None = Field(None, description="Optional case config path for instruction-side case context")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/v1/tenants/{tenant_id}/projects/{project_id}/answers")
def create_answer(tenant_id: str, project_id: str, req: AnswerRequest,
                  scopes: frozenset[Scope] = Depends(granted_scopes)) -> dict:
    scope = Scope(tenant_id, project_id)
    if scope not in scopes:
        raise HTTPException(403, "token is not granted this tenant/project scope")
    from potency.answering.service import answer_question
    from potency.workflows.config import case_context_text, load_case_config

    snapshot, ctx = None, "No case configuration was supplied for this scope."
    if req.case_config:
        cfg = load_case_config(req.case_config)
        if cfg.scope != scope:
            raise HTTPException(400, "case_config scope does not match the request scope")
        snapshot, ctx = cfg.corpus_snapshot, case_context_text(cfg.case)
    answer, trace = answer_question(scope, question_id="api", question=req.question, mode=req.mode,
                                    run_id=ids.new_run_id("api"), corpus_snapshot=snapshot, case_context=ctx,
                                    budget=req.evidence_budget_chars)
    return {"answer": answer, "trace_url": f"/v1/traces/{trace['trace_id']}"}


@app.get("/v1/traces/{trace_id}")
def get_trace(trace_id: str, scopes: frozenset[Scope] = Depends(granted_scopes)) -> dict:
    for scope in sorted(scopes, key=lambda s: s.prefix):
        with scoped_session(scope) as conn:
            row = conn.execute("SELECT payload FROM trace WHERE id = %s", (trace_id,)).fetchone()
        if row:
            return row["payload"]
    raise HTTPException(404, "trace not found in any scope granted to this token")
