"""Embedding-only chunking. bge models read at most 512 tokens, so long passages are split into overlapping
windows over whitespace-collapsed text. Chunks always map back to their parent passage for citation."""

from dataclasses import dataclass

from potency.textutil import collapse_ws

CHUNKER_VERSION = "ws-window-v1:1500/200"
WINDOW = 1500
OVERLAP = 200


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str


def chunk_passage(text: str) -> list[Chunk]:
    norm = collapse_ws(text)
    if len(norm) <= WINDOW:
        return [Chunk(0, 0, len(norm), norm)]
    chunks: list[Chunk] = []
    start = 0
    while start < len(norm):
        end = min(start + WINDOW, len(norm))
        if end < len(norm):
            cut = norm.rfind(" ", start + WINDOW // 2, end)
            end = cut if cut > start else end
        chunks.append(Chunk(len(chunks), start, end, norm[start:end].strip()))
        if end >= len(norm):
            break
        nxt = end - OVERLAP
        space = norm.find(" ", nxt, end)
        start = space + 1 if space != -1 else nxt
    return chunks


def embed_text(title: str | None, chunk_text: str) -> str:
    return f"{title}\n{chunk_text}" if title else chunk_text
