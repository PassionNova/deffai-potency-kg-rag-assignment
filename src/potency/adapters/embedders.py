"""Embedding adapters. Production would add a Bedrock adapter behind the same interface."""

import hashlib
import re
from functools import lru_cache
from typing import Protocol

import numpy as np

from potency.settings import REPO_ROOT, get_settings

DIMS = 768
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    model_id: str
    dims: int
    is_test_double: bool
    texts_embedded: int

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedEmbedder:
    is_test_double = False

    def __init__(self, model_id: str, cache_dir: str, threads: int):
        from fastembed import TextEmbedding

        self.model_id = model_id
        self.dims = DIMS
        self.texts_embedded = 0
        self._model = TextEmbedding(model_name=model_id, cache_dir=cache_dir, threads=threads)

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        self.texts_embedded += len(texts)
        return [np.asarray(v, dtype=np.float32) for v in self._model.embed(texts, batch_size=32)]

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(next(iter(self._model.embed([BGE_QUERY_PREFIX + text]))), dtype=np.float32)


class FakeHashEmbedder:
    """Deterministic bag-of-words hashing embedder. Offline test double only; never used for reported runs."""

    is_test_double = True

    def __init__(self) -> None:
        self.model_id = "fake-hash-embedder-v1"
        self.dims = DIMS
        self.texts_embedded = 0

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIMS, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % DIMS] += 1.0 if (h >> 12) % 2 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v + 1e-6

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        self.texts_embedded += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.potency_embedder == "fake":
        return FakeHashEmbedder()
    cache = s.fastembed_cache_path
    cache_dir = str((REPO_ROOT / cache).resolve()) if not cache.startswith(("/", "\\")) and ":" not in cache else cache
    return FastEmbedEmbedder(s.potency_embedding_model, cache_dir, s.potency_embed_threads)
