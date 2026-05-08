"""Text embedder for the meetink index.

Wraps `fastembed` with `BAAI/bge-small-en-v1.5` (~80 MB ONNX, ~33 M
params). Stays resident in the REPL process so consecutive encode()
calls are cheap (~5 ms / sentence on M-series via onnxruntime's
CoreMLExecutionProvider).

Why fastembed (vs sentence-transformers): fastembed is an ONNX-backed
embedder maintained by Qdrant. ~50 MB install vs ~700 MB for the ST
stack (torch + transformers), and it bundles bge-small.onnx so there's
no model conversion step. The transformers package has had repeated
regressions where module-level type annotations reference unimported
names; we hit one (`nn.Module` in transformers/integrations/accelerate.py)
that crashed the entire REPL on `import sentence_transformers`. Moving
to a torch-free runtime sidesteps that whole class of fragility.

bge-small is the same model either way — same weights, same retrieval
quality. We just load it through a more reliable runtime.

BGE-specific detail: retrieval is asymmetric. fastembed's `embed()` is
for documents, `query_embed()` is for queries — and `query_embed`
applies the BGE instruction prefix automatically. We don't have to
prepend it ourselves any more.
"""

from __future__ import annotations

import threading
from typing import Iterable

import numpy as np


class Embedder:
    """Singleton bge-small wrapper. Lazy-loaded on first encode()."""

    _instance: "Embedder | None" = None
    _instance_lock = threading.Lock()
    MODEL_NAME = "BAAI/bge-small-en-v1.5"
    DIMENSION = 384

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
        """Quick check: is fastembed importable in this venv? Catches
        *any* exception — not just ImportError — because broken installs
        (e.g. version skew, missing transitive deps) raise other errors
        at import time and we don't want them to kill the REPL. A broken
        environment looks the same as 'not installed' from the caller's
        perspective: /ask falls back to the legacy full-transcript path."""
        try:
            import fastembed  # noqa: F401
            return True
        except Exception:
            return False

    def _ensure_loaded(self) -> None:
        """Caller must hold self._lock."""
        if self._loaded:
            return
        try:
            from fastembed import TextEmbedding
        except Exception as e:
            raise RuntimeError(
                f"fastembed not available: {e}. Run /index install."
            ) from e
        # First call downloads the ONNX model (~80 MB) into ~/.cache/
        # fastembed. Subsequent calls are instant. On Apple Silicon
        # onnxruntime uses CoreMLExecutionProvider when available.
        self._model = TextEmbedding(self.MODEL_NAME)
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
            # fastembed.embed yields per-text np.ndarray; stack into a
            # contiguous (N, D) matrix so callers can work with one
            # slice instead of N separate arrays.
            embeds = np.stack(list(self._model.embed(text_list)), axis=0)
        return embeds.astype(np.float32, copy=False)

    def encode_query(self, query: str) -> np.ndarray:
        """Encode a single query for retrieval. fastembed's query_embed
        applies the BGE-specific instruction prefix automatically.
        Returns shape (384,)."""
        with self._lock:
            self._ensure_loaded()
            emb = next(iter(self._model.query_embed([query])))
        return emb.astype(np.float32, copy=False)


def get_embedder() -> Embedder:
    return Embedder.get()
