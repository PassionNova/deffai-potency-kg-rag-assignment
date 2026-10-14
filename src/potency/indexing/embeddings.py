"""Embedding index build. The cache is keyed by (scope, model, embedded-text hash), so an unchanged re-import
after restart performs zero embedding work."""

import sys
import time

from potency import ids
from potency.adapters.embedders import Embedder, get_embedder
from potency.db import scoped_session
from potency.passages.chunking import CHUNKER_VERSION, chunk_passage, embed_text
from potency.tenancy import Scope

BATCH = 64


def build_index(scope: Scope, embedder: Embedder | None = None, corpus_snapshot: str | None = None,
                log_progress: bool = False) -> dict:
    emb = embedder or get_embedder()
    t0 = time.perf_counter()
    with scoped_session(scope) as conn:
        passages = conn.execute(
            """
            SELECT p.id, p.text, p.text_sha256, d.title
            FROM passage p
            JOIN source_version v ON v.id = p.version_id
            JOIN source_document d ON d.id = v.document_id
            ORDER BY p.id
            """
        ).fetchall()
        existing = {
            r["text_sha256"]
            for r in conn.execute(
                "SELECT text_sha256 FROM embedding_cache WHERE model_id = %s", (emb.model_id,)
            ).fetchall()
        }

    chunk_rows = []
    to_embed: dict[str, str] = {}
    for p in passages:
        for c in chunk_passage(p["text"]):
            text = embed_text(p["title"], c.text)
            sha = ids.sha256_text(text)
            chunk_id = f"{scope.prefix}/chunk/{ids.short_hash(p['id'], CHUNKER_VERSION, c.index)}"
            chunk_rows.append(
                (chunk_id, scope.tenant_id, scope.project_id, p["id"], CHUNKER_VERSION, c.index, c.start, c.end, sha)
            )
            if sha not in existing:
                to_embed[sha] = text

    missing = list(to_embed.items())
    for i in range(0, len(missing), BATCH):
        batch = missing[i : i + BATCH]
        vectors = emb.embed_documents([t for _, t in batch])
        with scoped_session(scope) as conn:
            conn.cursor().executemany(
                "INSERT INTO embedding_cache (tenant_id, project_id, model_id, text_sha256, embedding) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(scope.tenant_id, scope.project_id, emb.model_id, sha, vec) for (sha, _), vec in zip(batch, vectors, strict=True)],
            )
        if log_progress:
            print(f"embedded {min(i + BATCH, len(missing))}/{len(missing)}", file=sys.stderr, flush=True)

    index_id = "idx-" + ids.short_hash(
        scope.prefix, emb.model_id, CHUNKER_VERSION, *sorted(f"{p['id']}={p['text_sha256']}" for p in passages), n=12
    )
    chunks_per_passage: dict[str, int] = {}
    for r in chunk_rows:
        chunks_per_passage[r[3]] = chunks_per_passage.get(r[3], 0) + 1
    stats = {
        "passages_indexed": len(passages),
        "chunks": len(chunk_rows),
        "passages_with_multiple_chunks": sum(1 for n in chunks_per_passage.values() if n > 1),
        "embeddings_computed": len(missing),
        "embedding_cache_hits": sum(1 for r in chunk_rows if r[8] not in to_embed),
        "embedding_model": emb.model_id,
        "embedder_is_test_double": emb.is_test_double,
        "dims": emb.dims,
        "chunker_version": CHUNKER_VERSION,
        "elapsed_s": None,
    }
    with scoped_session(scope) as conn:
        conn.cursor().executemany(
            """
            INSERT INTO passage_chunk (id, tenant_id, project_id, passage_fk, chunker_version, chunk_index,
                char_start, char_end, embed_text_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
            """,
            chunk_rows,
        )
        stats["elapsed_s"] = round(time.perf_counter() - t0, 2)
        conn.execute("UPDATE index_version SET status='inactive' WHERE id <> %s AND status='active'", (index_id,))
        conn.execute(
            """
            INSERT INTO index_version (id, tenant_id, project_id, embedding_model, dims, chunker_version,
                corpus_snapshot, status, stats)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s)
            ON CONFLICT (id) DO UPDATE SET status='active'
            """,
            (index_id, scope.tenant_id, scope.project_id, emb.model_id, emb.dims, CHUNKER_VERSION, corpus_snapshot,
             _jsonb(stats)),
        )
    stats["index_version"] = index_id
    return stats


def active_index_version(conn) -> str | None:
    row = conn.execute(
        "SELECT id FROM index_version WHERE status='active' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _jsonb(v):
    from psycopg.types.json import Jsonb

    return Jsonb(v)
