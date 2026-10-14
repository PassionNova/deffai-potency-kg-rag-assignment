"""Semantic retrieval over current source passages. Passage score = best chunk cosine similarity.
Exact scan (no ANN) at this corpus size, so results are deterministic; RLS limits every table to the scope."""

from potency.passages.chunking import CHUNKER_VERSION

VECTOR_RETRIEVER_VERSION = "vector-v1"

_BEST_CHUNKS = """
WITH c AS (
  SELECT pc.passage_fk, pc.chunk_index, pc.char_start, pc.char_end,
         1 - (ec.embedding <=> %(q)s) AS sim
  FROM passage_chunk pc
  JOIN embedding_cache ec ON ec.text_sha256 = pc.embed_text_sha256 AND ec.model_id = %(m)s
  JOIN passage p ON p.id = pc.passage_fk AND p.is_current
  WHERE pc.chunker_version = %(cv)s {extra}
), best AS (
  SELECT DISTINCT ON (passage_fk) passage_fk, chunk_index, char_start, char_end, sim
  FROM c ORDER BY passage_fk, sim DESC, chunk_index
)
SELECT * FROM best ORDER BY sim DESC, passage_fk LIMIT %(k)s
"""


def vector_search(conn, qvec, model_id: str, k: int) -> list[dict]:
    return conn.execute(
        _BEST_CHUNKS.format(extra=""), {"q": qvec, "m": model_id, "cv": CHUNKER_VERSION, "k": k}
    ).fetchall()


def passage_similarities(conn, qvec, model_id: str, passage_pks: list[str]) -> dict[str, dict]:
    if not passage_pks:
        return {}
    rows = conn.execute(
        _BEST_CHUNKS.format(extra="AND pc.passage_fk = ANY(%(ids)s)"),
        {"q": qvec, "m": model_id, "cv": CHUNKER_VERSION, "k": len(passage_pks), "ids": passage_pks},
    ).fetchall()
    return {r["passage_fk"]: r for r in rows}
