from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    project_id: str

    @classmethod
    def from_dict(cls, d: dict) -> "Scope":
        return cls(tenant_id=str(d["tenant_id"]), project_id=str(d["project_id"]))

    def as_dict(self) -> dict:
        return {"tenant_id": self.tenant_id, "project_id": self.project_id}

    @property
    def prefix(self) -> str:
        return f"{self.tenant_id}/{self.project_id}"

    def __str__(self) -> str:
        return self.prefix
