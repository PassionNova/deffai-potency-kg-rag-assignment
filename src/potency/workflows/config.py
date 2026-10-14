import json
from dataclasses import dataclass, field
from pathlib import Path

from potency.settings import REPO_ROOT
from potency.tenancy import Scope


@dataclass
class CaseConfig:
    path: Path
    scope: Scope
    corpus_snapshot: str
    manifest_path: Path
    records_path: Path
    questions_path: Path
    behavior_cases_path: Path | None
    case: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @property
    def corrections(self) -> dict | None:
        p = self.manifest_path.parent / "corrections.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    @property
    def questions(self) -> list[dict]:
        return json.loads(self.questions_path.read_text(encoding="utf-8"))


def load_case_config(path: str | Path = "configs/casgevy-potency.json") -> CaseConfig:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    raw = json.loads(p.read_text(encoding="utf-8"))
    base = p.parent

    def res(key: str) -> Path:
        return (base / raw[key]).resolve()

    return CaseConfig(
        path=p,
        scope=Scope.from_dict(raw["scope"]),
        corpus_snapshot=raw["corpus_snapshot"],
        manifest_path=res("corpus_manifest"),
        records_path=res("source_records"),
        questions_path=res("questions"),
        behavior_cases_path=res("behavior_cases") if raw.get("behavior_cases") else None,
        case=raw.get("case", {}),
        raw=raw,
    )


def case_context_text(case: dict) -> str:
    """Instruction-side case context from configuration (not source evidence)."""
    if not case:
        return "No case configuration was supplied for this scope."
    parts = []
    if case.get("product_name"):
        parts.append(f"Configured product: {case['product_name']} ({case.get('proper_name', 'proper name not configured')}).")
    if case.get("configured_application_id"):
        parts.append(
            f"Configured application: {case['configured_application_id']} ({case.get('configured_indication', '')})."
        )
    if case.get("related_application_id"):
        parts.append(
            f"Related application in scope: {case['related_application_id']} ({case.get('related_indication', '')})."
        )
    if case.get("scope_note"):
        parts.append(case["scope_note"])
    return " ".join(parts)
