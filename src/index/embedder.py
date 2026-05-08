"""Text embedder for the meetink index.

Wraps `sentence-transformers` with `BAAI/bge-small-en-v1.5` (~80 MB,
~33 M params). Stays resident in the REPL process so consecutive
encode() calls are cheap (~5 ms / sentence on M-series with MPS).

Why bge-small: it's the sweet spot of size vs retrieval quality for
English meeting transcripts. Outperforms much larger generic
embedders on the MTEB retrieval benchmark while still loading in
~1 s and using ~150 MB resident.

Why sentence-transformers (not raw transformers + manual pooling):
the library handles attention-mask pooling, padding, and batching
correctly, plus picks MPS automatically on Apple Silicon.

Why not embeddings via mlx-lm: Qwen3.5 isn't trained for retrieval;
its last-hidden-state mean-pooled embeddings score 10-15 points
worse than purpose-built embedders on MTEB. Quality matters more
than minimising deps for the index.

BGE-specific detail: retrieval is asymmetric. Queries get prefixed
with "Represent this sentence for searching relevant passages:";
documents (transcript chunks) don't. encode() is for documents;
encode_query() applies the prefix. The model was instruction-tuned
this way; passing both unprefixed degrades retrieval ~5 points.
"""

from __future__ import annotations

import threading
from typing import Iterable

import numpy as np


_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    """Singleton bge-small wrapper. Lazy-loaded on first encode()."""

    _instance: "Embedder | None" = None
    _instance_lock = threading.Lock()
    MODEL_NAME = "BAAI/bge-small-en-v1.5"
    DIMENSION = 384  # bge-small output dim

    @classmethod
    def get(cls) -> "Embedder":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._loaded = False

    def is_available(self) -> bool:
        """Quick check without loading the model: is sentence-transformers
        importable in this venv? Used to decide whether to enable the
        index path at all (graceful degradation when the user hasn't run
        /index install yet)."""
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_loaded(self) -> None:
        """Caller must hold self._lock."""
        if self._loaded:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers not installed. Run /index install."
            ) from e
        # SentenceTransformer auto-picks MPS on Apple Silicon, falls back
        # to CPU otherwise. Both work; MPS is ~5x faster for bge-small.
        self._model = SentenceTransformer(self.MODEL_NAME)
        self._loaded = True

    def encode(self, texts: Iterable[str], batch_size: int = 32) -> np.ndarray:
        """Encode documents (transcript chunks). Returns L2-normalised
        float32 array of shape (N, 384). Cosine similarity becomes a
        plain dot product on these vectors."""
        text_list = list(texts)
        if not text_list:
            return np.zeros((0, self.DIMENSION), dtype=np.float32)
        with self._lock:
            self._ensure_loaded()
            embeds = self._model.encode(
                text_list,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        return embeds.astype(np.float32, copy=False)

    def encode_query(self, query: str) -> np.ndarray:
        """Encode a single query for retrieval. Applies the BGE-specific
        instruction prefix. Returns shape (384,)."""
        return self.encode([_QUERY_PREFIX + query])[0]


def get_embedder() -> Embedder:
    return Embedder.get()
