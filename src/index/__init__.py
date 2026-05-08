"""IndexManager — public API for the meetink RAG index.

Singleton accessor; one IndexBuilder at a time matches the reality
that meetink only has one active recording.

Used by repl.py:
  IndexManager.get().is_available()       # is sentence-transformers installed?
  IndexManager.get().start(path)          # begin live indexing on /start
  IndexManager.get().stop()               # graceful stop on /stop
  IndexManager.get().has_index_for(path)
  IndexManager.get().chunk_count_for(path)
  IndexManager.get().ensure_index_for(path)  # synchronous build for past tx
  IndexManager.get().retrieve_for_ask(path, query) -> AssemblyResult
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .builder import IndexBuilder, build_synchronous
from .embedder import get_embedder
from .retriever import (
    chunk_count,
    has_index,
    index_dir_for,
    render_chunks,
    retrieve,
)
from .rollups import read_rollups


# Last N raw transcript lines that always go into the prompt verbatim,
# regardless of retrieval. Keeps "what did we just decide" answers
# grounded in actual speech, not a possibly-lossy segment summary.
RECENT_TAIL_LINES = 20


@dataclass
class AssemblyResult:
    """Index-derived sections /ask uses in place of the raw transcript."""
    decisions: str
    actions: str
    retrieved_chunks: str
    recent_tail: str          # last N lines verbatim
    segment_summaries: str    # newest last
    chunk_count: int


class IndexManager:
    _instance: "IndexManager | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "IndexManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._builder: IndexBuilder | None = None

    def is_available(self) -> bool:
        """True if sentence-transformers is importable in this venv."""
        return get_embedder().is_available()

    def is_running(self) -> bool:
        """True if a live indexer is currently attached to a recording."""
        return self._builder is not None and \
               self._builder._thread is not None and \
               self._builder._thread.is_alive()

    def start(self, transcript_path: Path) -> bool:
        """Start live indexing on the given transcript. Idempotent.
        Returns False if embeddings aren't available (caller should fall
        back to today's full-transcript /ask path)."""
        if not self.is_available():
            return False
        with self._lock:
            if (self._builder is not None and
                    self._builder.transcript_path == Path(transcript_path) and
                    self._builder._thread is not None and
                    self._builder._thread.is_alive()):
                return True
            if self._builder is not None:
                self._builder.stop(timeout=2.0)
            self._builder = IndexBuilder(transcript_path)
            self._builder.start()
            return True

    def stop(self) -> None:
        """Stop any running indexer. Final segment summary fires before
        return."""
        with self._lock:
            if self._builder is not None:
                self._builder.stop()
                self._builder = None

    def has_index_for(self, transcript_path: Path) -> bool:
        return has_index(Path(transcript_path))

    def chunk_count_for(self, transcript_path: Path) -> int:
        return chunk_count(Path(transcript_path))

    def ensure_index_for(self, transcript_path: Path) -> bool:
        """Build the index synchronously if it doesn't exist. Used at
        /ask time for past transcripts. Returns True if an index is
        available afterwards (existing or freshly built)."""
        path = Path(transcript_path)
        if has_index(path):
            return True
        if not self.is_available():
            return False
        return build_synchronous(path)

    def retrieve_for_ask(
        self,
        transcript_path: Path,
        query: str,
        k: int = 8,
    ) -> AssemblyResult:
        """Assemble all index-derived context for /ask. The caller
        decides how to fit the pieces into the budget; we just produce
        them. Empty fields are normal (no decisions yet, no segments
        yet, etc.) and the caller should drop empty sections from the
        prompt."""
        path = Path(transcript_path)
        idx_dir = index_dir_for(path)

        decisions, actions = read_rollups(idx_dir)

        retrieved = ""
        if has_index(path) and self.is_available():
            try:
                qvec = get_embedder().encode_query(query)
                chunks = retrieve(path, qvec, k=k, expand=1)
                retrieved = render_chunks(chunks)
            except Exception:
                retrieved = ""

        recent_tail = ""
        try:
            lines = path.read_text().splitlines()
            recent_tail = "\n".join(lines[-RECENT_TAIL_LINES:]) if lines else ""
        except OSError:
            pass

        seg_dir = idx_dir / "segments"
        segs: list[str] = []
        if seg_dir.is_dir():
            for f in sorted(seg_dir.glob("*.md")):
                try:
                    segs.append(f.read_text().strip())
                except OSError:
                    pass
        seg_summary = "\n\n".join(segs)

        return AssemblyResult(
            decisions=decisions,
            actions=actions,
            retrieved_chunks=retrieved,
            recent_tail=recent_tail,
            segment_summaries=seg_summary,
            chunk_count=chunk_count(path),
        )


def get_manager() -> IndexManager:
    return IndexManager.get()
