"""Background indexer + unified backend interface.

Lifecycle (one IndexBuilder per active recording):
  start() →
    1. load existing index state (chunks.npy + chunks.jsonl + meta.json)
    2. spawn a daemon thread
    3. thread polls the transcript file every POLL_INTERVAL_S seconds
    4. new transcript lines → embed → append to chunks.npy / .jsonl
    5. when SEGMENT_LINES new chunks accumulated since the last segment OR
       SEGMENT_INTERVAL_S elapsed, run a segment summary + rollup updates
    6. stop() does a final flush and segment summary

Concurrency:
  - Embedding runs on its own (sentence-transformers internals; no lock
    contention with /ask).
  - Segment summary + rollup calls go through MLXRuntime (local) or
    `claude -p` (claude). Local serializes against /ask via the runtime
    lock — indexer waits, /ask wins. That's fine because /ask never
    blocks on the indexer (its own data — recent_tail, retrieved_chunks,
    rollups read from disk — is always ready).
  - chunks.npy write is atomic-replace (write to .tmp, rename).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .embedder import Embedder, get_embedder
from .retriever import index_dir_for
from .rollups import update_rollups


POLL_INTERVAL_S = 2.0       # how often to look for new transcript content
SEGMENT_LINES = 30           # min new chunks before a segment summary
SEGMENT_INTERVAL_S = 300.0   # max wall-clock between segment summaries


_SEGMENT_SYSTEM = (
    "You summarise a 5-minute segment of a meeting transcript into a "
    "concise paragraph (3-5 sentences). Capture the topics discussed, "
    "any decisions or action items, and notable quotes verbatim. Do NOT "
    "include speculation or interpretation. Skip pleasantries and small "
    "talk."
)


# ---------------------------------------------------------------------------
# Unified backend interface (local MLX or claude -p)
# ---------------------------------------------------------------------------

MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))


def _read_config(key: str, default: str = "") -> str:
    cfg = MK_HOME / "config"
    if cfg.is_file():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return default


def _active_backend() -> str:
    env = os.environ.get("MEETINK_TITLE_BACKEND", "")
    if env in ("local", "claude"):
        return env
    val = _read_config("title_backend", "local")
    return val if val in ("local", "claude") else "local"


def _active_local_model_path() -> Path | None:
    """Mirror the lookup logic in repl.py without a circular import."""
    name = _read_config("local_llm_model", "qwen3.5-2b")
    repo_map = {
        "qwen3.5-0.8b": "Qwen3.5-0.8B-4bit",
        "qwen3.5-2b":   "Qwen3.5-2B-4bit",
        "qwen3.5-4b":   "Qwen3.5-4B-4bit",
        "qwen3.5-9b":   "Qwen3.5-9B-4bit",
    }
    dirname = repo_map.get(name)
    if not dirname:
        return None
    p = MK_HOME / "models" / "mlx" / dirname
    if not (p / "config.json").exists():
        return None
    return p


def _active_claude_model() -> str:
    env = os.environ.get("MEETINK_CLAUDE_MODEL", "")
    if env:
        return env
    return _read_config("claude_model", "claude-sonnet-4-6")


class IndexBackend:
    """Backend selector for indexing-related LLM calls. `local` uses the
    resident MLXRuntime (same model the user picked via /llm use); `claude`
    shells out to `claude -p` with the same minimum-tools flags titling /
    ask use, so it stays fast and bills against the user's subscription."""

    def __init__(self, backend: str | None = None):
        self.backend = backend or _active_backend()

    def generate(self, system: str, user: str,
                 max_tokens: int = 256, temp: float = 0.3) -> str:
        if self.backend == "local":
            return self._gen_local(system, user, max_tokens, temp)
        if self.backend == "claude":
            return self._gen_claude(system, user, max_tokens, temp)
        raise RuntimeError(f"unknown backend: {self.backend}")

    def _gen_local(self, system: str, user: str,
                   max_tokens: int, temp: float) -> str:
        model_path = _active_local_model_path()
        if model_path is None:
            raise RuntimeError("local backend selected but no model snapshot present")
        try:
            from llm.mlx_runtime import get_runtime
        except ImportError as e:
            raise RuntimeError("mlx_runtime module missing") from e
        runtime = get_runtime()
        return runtime.generate(model_path, prompt=user, system=system,
                                max_tokens=max_tokens, temp=temp)

    def _gen_claude(self, system: str, user: str,
                    max_tokens: int, temp: float) -> str:
        # Fold system into the user message — `claude -p` doesn't accept
        # a separate system prompt argument and we want to stay portable
        # across CLI versions.
        prompt = f"{system}\n\n{user}" if system else user
        result = subprocess.run(
            ["claude", "-p",
             "--model", _active_claude_model(),
             "--tools", "",
             "--strict-mcp-config",
             prompt],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude -p failed: {result.stderr.strip()}")
        return result.stdout.strip()


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

def _parse_line(line: str) -> tuple[str, str, str] | None:
    """Parse '[HH:MM:SS] SPEAKER: text' → (timestamp, speaker, text).
    Returns None for malformed lines (header, blank, comment)."""
    if not line.startswith("["):
        return None
    rb = line.find("]")
    if rb < 1:
        return None
    ts = line[1:rb]
    rest = line[rb + 1:].lstrip()
    colon = rest.find(":")
    if colon < 1:
        return None
    speaker = rest[:colon].strip()
    text = rest[colon + 1:].strip()
    if not speaker or not text:
        return None
    return ts, speaker, text


def _read_meta(idx_dir: Path) -> dict:
    p = idx_dir / "meta.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"byte_offset": 0, "segment_count": 0}


def _write_meta(idx_dir: Path, meta: dict) -> None:
    p = idx_dir / "meta.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(p)


def _append_chunks(idx_dir: Path, new_embeds: np.ndarray,
                   new_meta: list[dict]) -> None:
    """Append rows to chunks.npy (atomic-replace) and chunks.jsonl
    (append-only). Caller guarantees row counts match."""
    if new_embeds.shape[0] == 0:
        return
    npy_path = idx_dir / "chunks.npy"
    if npy_path.is_file():
        existing = np.load(str(npy_path))
        combined = np.vstack([existing, new_embeds])
    else:
        combined = new_embeds
    # Important: pass an open file handle to np.save, not a string path.
    # When given a string that doesn't end in `.npy`, numpy silently
    # appends `.npy` — so np.save("chunks.npy.tmp", arr) actually
    # writes to "chunks.npy.tmp.npy" and the subsequent .replace()
    # fails with FileNotFoundError. The handle form bypasses that.
    tmp = npy_path.with_suffix(".npy.tmp")
    with tmp.open("wb") as f:
        np.save(f, combined)
    tmp.replace(npy_path)

    jl_path = idx_dir / "chunks.jsonl"
    with jl_path.open("a", encoding="utf-8") as f:
        for m in new_meta:
            f.write(json.dumps(m) + "\n")


class IndexBuilder:
    """Per-transcript live indexer. Created by IndexManager."""

    def __init__(self, transcript_path: Path):
        self.transcript_path = Path(transcript_path)
        self.idx_dir = index_dir_for(self.transcript_path)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._byte_offset = 0
        self._segment_count = 0
        self._last_segment_at = 0.0
        self._pending_segment_lines: list[str] = []

    def start(self) -> None:
        """Start the indexer thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self.idx_dir.mkdir(parents=True, exist_ok=True)
        meta = _read_meta(self.idx_dir)
        self._byte_offset = meta.get("byte_offset", 0)
        self._segment_count = meta.get("segment_count", 0)
        self._last_segment_at = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="meetink-indexer", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal stop and wait for the thread. Triggers a final segment
        summary on whatever is in the pending buffer."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        # Final pending flush — runs in caller's thread, so the lock is
        # uncontested by our own loop at this point.
        if self._pending_segment_lines:
            self._segment(self._pending_segment_lines)
            self._pending_segment_lines = []

    def _loop(self) -> None:
        embedder = get_embedder()
        # Pre-warm the embedder. Loads sentence-transformers + bge-small
        # in this thread instead of the first /ask thread; users see the
        # initial recording start as the load point, not their first
        # /ask, which is the more responsive UX.
        try:
            with embedder._lock:
                embedder._ensure_loaded()
        except Exception as e:
            print(f"[indexer] embedder load failed: {e}", file=sys.stderr)
            return
        while not self._stop_event.is_set():
            try:
                self._tick(embedder)
            except Exception as e:
                print(f"[indexer] tick failed: {e}", file=sys.stderr)
            self._stop_event.wait(POLL_INTERVAL_S)
        # One last tick to drain any tail content the file got after the
        # last poll — if we're racing /stop we want everything indexed.
        try:
            self._tick(embedder)
        except Exception:
            pass

    def _tick(self, embedder: Embedder) -> None:
        """One poll iteration: read new bytes, embed, maybe segment."""
        try:
            with self.transcript_path.open("rb") as f:
                f.seek(self._byte_offset)
                new_bytes = f.read()
                new_offset = f.tell()
        except OSError:
            return

        pending_chunks: list[tuple[str, str, str]] = []
        if new_bytes:
            new_text = new_bytes.decode("utf-8", errors="replace")
            for raw_line in new_text.splitlines():
                parsed = _parse_line(raw_line.strip())
                if parsed is None:
                    continue
                pending_chunks.append(parsed)
                self._pending_segment_lines.append(raw_line.strip())
            self._byte_offset = new_offset

        if pending_chunks:
            texts = [c[2] for c in pending_chunks]
            embeds = embedder.encode(texts)
            metas = [{"ts": ts, "sp": sp, "tx": tx}
                     for (ts, sp, tx) in pending_chunks]
            _append_chunks(self.idx_dir, embeds, metas)

        _write_meta(self.idx_dir, {
            "byte_offset": self._byte_offset,
            "segment_count": self._segment_count,
        })

        # Maybe summarise a segment — either accumulated enough chunks or
        # too long since the last summary.
        now = time.time()
        if (len(self._pending_segment_lines) >= SEGMENT_LINES or
                (self._pending_segment_lines and
                 now - self._last_segment_at > SEGMENT_INTERVAL_S)):
            self._segment(self._pending_segment_lines)
            self._pending_segment_lines = []
            self._last_segment_at = now

    def _segment(self, lines: list[str]) -> None:
        """Run a segment summary + rollup updates. Best-effort — failure
        is logged to stderr but doesn't kill the indexer."""
        if not lines:
            return
        segment_text = "\n".join(lines)
        try:
            backend = IndexBackend()
            summary = backend.generate(
                _SEGMENT_SYSTEM, segment_text, max_tokens=200, temp=0.3,
            )
            self._segment_count += 1
            seg_dir = self.idx_dir / "segments"
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_file = seg_dir / f"{self._segment_count:04d}.md"
            seg_file.write_text(summary.strip() + "\n")
            update_rollups(self.idx_dir, segment_text, backend)
            _write_meta(self.idx_dir, {
                "byte_offset": self._byte_offset,
                "segment_count": self._segment_count,
            })
        except Exception as e:
            print(f"[indexer] segment failed: {e}", file=sys.stderr)


def build_synchronous(transcript_path: Path) -> bool:
    """Build the index for a past transcript synchronously, no thread.
    Drives _tick directly until the file is fully consumed. Used at
    /ask time when the user hits a pre-existing transcript that doesn't
    have an index yet (lazy build)."""
    builder = IndexBuilder(transcript_path)
    builder.idx_dir.mkdir(parents=True, exist_ok=True)
    meta = _read_meta(builder.idx_dir)
    builder._byte_offset = meta.get("byte_offset", 0)
    builder._segment_count = meta.get("segment_count", 0)
    builder._last_segment_at = time.time()
    embedder = get_embedder()
    try:
        with embedder._lock:
            embedder._ensure_loaded()
    except Exception as e:
        print(f"[indexer] embedder load failed: {e}", file=sys.stderr)
        return False
    while True:
        try:
            size = transcript_path.stat().st_size
        except OSError:
            return False
        before = builder._byte_offset
        builder._tick(embedder)
        if (builder._byte_offset >= size and
                not builder._pending_segment_lines):
            break
        # Safety against pathological loops.
        if (builder._byte_offset == before and
                not builder._pending_segment_lines):
            break
    if builder._pending_segment_lines:
        builder._segment(builder._pending_segment_lines)
        builder._pending_segment_lines = []
    return True
