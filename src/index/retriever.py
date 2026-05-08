"""Cosine-similarity retrieval over a transcript's chunk embeddings.

Reads the index sidecar produced by builder.py:
  <transcript>.idx/chunks.npy   (N, 384) float32, L2-normalised
  <transcript>.idx/chunks.jsonl  N lines, one {ts, sp, tx} per line

retrieve() returns top-k chunks with ±expand neighbour expansion. A
chunk that mentions "pricing" is more useful with the line before/
after for who said it and what reply followed. Adjacent expansions
are deduped — back-to-back top hits don't double up.

Returned chunks are sorted by chronological index, not relevance —
the prompt then reads naturally as a sliced excerpt rather than as
a relevance-ranked list. Models reason better on chronological
context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Chunk:
    """A single transcript line plus its position. timestamp is the
    `[HH:MM:SS]` from the source file."""
    index: int
    timestamp: str
    speaker: str
    text: str

    def render(self) -> str:
        return f"[{self.timestamp}] {self.speaker}: {self.text}"


def index_dir_for(transcript_path: Path) -> Path:
    """Sidecar dir for a transcript file. Replaces the .txt suffix with
    .idx so the pair is visually obvious in `ls`."""
    return transcript_path.with_suffix(".idx")


def has_index(transcript_path: Path) -> bool:
    idx = index_dir_for(transcript_path)
    return (idx / "chunks.npy").is_file() and (idx / "chunks.jsonl").is_file()


def chunk_count(transcript_path: Path) -> int:
    """Number of chunks in the index, or 0 if no index."""
    idx = index_dir_for(transcript_path)
    npy = idx / "chunks.npy"
    if not npy.is_file():
        return 0
    try:
        # mmap so we don't pay the load cost just to read the shape.
        embeddings = np.load(str(npy), mmap_mode="r")
        return int(embeddings.shape[0])
    except Exception:
        return 0


def retrieve(
    transcript_path: Path,
    query_embedding: np.ndarray,
    k: int = 8,
    expand: int = 1,
) -> list[Chunk]:
    """Top-k chunks with ±expand neighbour expansion. Returns chunks
    sorted by chronological index (not similarity score)."""
    idx = index_dir_for(transcript_path)
    npy = idx / "chunks.npy"
    meta = idx / "chunks.jsonl"
    if not npy.is_file() or not meta.is_file():
        return []

    embeddings = np.load(str(npy))
    if embeddings.shape[0] == 0:
        return []

    # Cosine similarity = dot product since vectors are L2-normalised.
    sims = embeddings @ query_embedding
    # argsort returns ascending; negate for descending. Take the top k.
    top = np.argsort(-sims)[: max(k, 1)]

    selected: set[int] = set()
    for i in top:
        for offset in range(-expand, expand + 1):
            j = int(i) + offset
            if 0 <= j < embeddings.shape[0]:
                selected.add(j)

    # Load metadata only for the selected indices.
    meta_lines = meta.read_text().splitlines()
    chunks: list[Chunk] = []
    for j in sorted(selected):
        if j >= len(meta_lines):
            continue
        try:
            d = json.loads(meta_lines[j])
        except json.JSONDecodeError:
            continue
        chunks.append(Chunk(
            index=j,
            timestamp=d.get("ts", ""),
            speaker=d.get("sp", ""),
            text=d.get("tx", ""),
        ))
    return chunks


def render_chunks(chunks: list[Chunk]) -> str:
    """Render a list of chunks as a contiguous text block. Inserts an
    ellipsis line between non-adjacent groups so the model can tell that
    the chunks aren't sequential."""
    if not chunks:
        return ""
    lines: list[str] = []
    prev_idx: int | None = None
    for c in chunks:
        if prev_idx is not None and c.index > prev_idx + 1:
            lines.append("...")
        lines.append(c.render())
        prev_idx = c.index
    return "\n".join(lines)
