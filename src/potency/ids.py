"""Stable, scope-derived identifiers.

Local source/passage IDs are not globally unique (the isolation fixture reuses them across projects),
so every persistent identifier is prefixed with tenant and project.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def short_hash(*parts: object, n: int = 16) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


def scope_prefix(tenant_id: str, project_id: str) -> str:
    return f"{tenant_id}/{project_id}"


def document_pk(tenant_id: str, project_id: str, source_id: str) -> str:
    return f"{tenant_id}/{project_id}/{source_id}"


def version_pk(tenant_id: str, project_id: str, source_id: str, source_version: str) -> str:
    return f"{tenant_id}/{project_id}/{source_id}@{source_version}"


def passage_pk(tenant_id: str, project_id: str, source_id: str, source_version: str, passage_id: str) -> str:
    return f"{tenant_id}/{project_id}/{source_id}@{source_version}#{passage_id}"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str, max_len: int = 80) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-") or "x") if s else "x"


def new_run_id(kind: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"


def utcnow() -> datetime:
    return datetime.now(UTC)
