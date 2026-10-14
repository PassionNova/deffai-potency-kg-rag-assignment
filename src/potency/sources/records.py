from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from potency import ids
from potency.tenancy import Scope


class RecordScope(BaseModel):
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)


class SourceRecord(BaseModel):
    """One normalized passage record as supplied in records.jsonl or fixture inputs."""

    model_config = ConfigDict(extra="allow")

    scope: RecordScope
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)
    corpus_snapshot: str = Field(min_length=1)
    text: str
    text_sha256: str = Field(min_length=64, max_length=64)
    locator: dict[str, Any]
    authority: str
    status: str
    source_type: str
    corpus_layer: str | None = None
    filename: str | None = None
    issued: str | None = None
    issuer: str | None = None
    retrieved_at: str | None = None
    source_url: str | None = None
    subject_scope: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None

    @property
    def scope_obj(self) -> Scope:
        return Scope(self.scope.tenant_id, self.scope.project_id)

    @property
    def pk(self) -> str:
        return ids.passage_pk(
            self.scope.tenant_id, self.scope.project_id, self.source_id, self.source_version, self.passage_id
        )

    @property
    def page(self) -> int | None:
        page = self.locator.get("page")
        return int(page) if isinstance(page, int | float | str) and str(page).isdigit() else None

    @property
    def section(self) -> str | None:
        section = self.locator.get("section")
        return str(section) if section is not None else None
