"""Resident MLX model for the REPL process.

Imported by repl.py's /ask handler. Holds the model in memory across calls
so the 2-3s mlx_lm.load() cold-start only happens once per meetink session
(or after an idle release).

Why singleton: there's only one active local model at a time (the user picks
one via `/llm use`), and the REPL is single-process. A module-level singleton
keeps the API simple — `get_runtime().generate(...)` from anywhere.

Idle release: after IDLE_RELEASE_SECONDS of no use, drop the model so the
~1.5–7 GB of unified memory it holds frees up for whatever else the user
is doing. The next /ask transparently reloads. Trade-off: post-idle latency
goes back to cold-start; alternative (keep forever) costs RAM the user might
need elsewhere. 5 min is a reasonable compromise.
"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path
from typing import Iterator


# Release the model after this many seconds of no activity. Tuned to sit
# above typical "ask 3 follow-ups in a row" but well below "leave meetink
# open all day" — at which point we'd rather give the RAM back.
IDLE_RELEASE_SECONDS = 300


class MLXRuntime:
    """Process-wide singleton holding a loaded MLX model + tokenizer.

    Thread-safe via a lock around (load, release, generate). Generation is
    serialized — concurrent generate() calls would otherwise corrupt the
    KV cache. In practice the REPL only fires one /ask at a time, so the
    lock just defends against future surprises.
    """

    _instance: "MLXRuntime | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "MLXRuntime":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # Cap on retained /ask Q&A pairs. Five is enough for typical follow-up
    # threads without ballooning token cost. Each pair is ~200-1000 tokens
    # so 5 pairs cap the history at ~1-5K tokens — the budget logic in
    # repl.py drops history when it can't fit.
    ASK_HISTORY_MAX = 5

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._tokenizer = None
        self._loaded_path: Path | None = None
        self._last_used: float = 0.0
        # In-session /ask Q&A thread. Cleared on /quit (process exit) and
        # by clear_ask_history() (used by /clear).
        self._ask_history: list[tuple[str, str]] = []

    # -- lifecycle -----------------------------------------------------------

    def maybe_release_idle(self) -> bool:
        """Release the model if it's been idle longer than IDLE_RELEASE_SECONDS.
        Returns True if a release happened."""
        with self._lock:
            if self._model is None:
                return False
            if time.time() - self._last_used > IDLE_RELEASE_SECONDS:
                self._release_locked()
                return True
            return False

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        self._model = None
        self._tokenizer = None
        self._loaded_path = None
        # Force GC so Metal allocations are actually freed (mlx holds large
        # arrays via mx.array which only release when their refcount hits 0).
        gc.collect()

    def is_loaded(self) -> bool:
        return self._model is not None

    def loaded_path(self) -> Path | None:
        return self._loaded_path

    # -- /ask conversation thread --------------------------------------------

    def add_ask_pair(self, question: str, answer: str) -> None:
        """Record a completed (question, answer) so the next /ask can see
        the recent thread. Trims to ASK_HISTORY_MAX. Empty answers (e.g. a
        cancelled stream) are not added."""
        if not answer.strip():
            return
        with self._lock:
            self._ask_history.append((question, answer))
            if len(self._ask_history) > self.ASK_HISTORY_MAX:
                self._ask_history = self._ask_history[-self.ASK_HISTORY_MAX:]

    def clear_ask_history(self) -> None:
        with self._lock:
            self._ask_history = []

    def ask_history_text(self) -> str:
        """Render the Q&A history as a compact dialogue block ready to drop
        into the next prompt. Empty string when no history exists."""
        with self._lock:
            if not self._ask_history:
                return ""
            parts: list[str] = []
            for q, a in self._ask_history:
                parts.append(f"User: {q}\nAssistant: {a.strip()}")
            return "\n\n".join(parts)

    # -- inference ------------------------------------------------------------

    def _ensure_loaded(self, model_path: Path) -> None:
        """Load the model if not already loaded with the same path. Caller
        must hold self._lock."""
        if self._model is not None and self._loaded_path == model_path:
            return
        if self._model is not None:
            self._release_locked()
        try:
            from mlx_lm import load
        except ImportError as e:
            raise RuntimeError(
                "mlx_lm not installed in this venv. Run /llm install or meetink setup."
            ) from e
        self._model, self._tokenizer = load(str(model_path))
        self._loaded_path = model_path

    def _build_prompt(self, system: str, user: str) -> str:
        """Wrap the user message in the model's native chat template.
        Qwen3.5-Instruct's tokenizer applies the right `<|im_start|>...`
        markers; we just hand it a messages list.

        We pass `enable_thinking=False` because Qwen3.5's default chat
        template injects a `<think>...` block that the model fills with
        step-by-step reasoning before the actual answer. For titling and
        /ask we want the answer directly. The template injects an empty
        `<think>\\n\\n</think>` instead, suppressing the reasoning. The
        kwarg is forwarded to the jinja template via apply_chat_template's
        **kwargs — older Qwen3 templates ignore it harmlessly."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return self._tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
            enable_thinking=False,
        )

    def generate(
        self,
        model_path: Path,
        prompt: str,
        system: str = "",
        max_tokens: int = 512,
        temp: float = 0.4,
    ) -> str:
        """Single-shot generation. Loads the model if needed, then returns
        the full output string."""
        with self._lock:
            self._ensure_loaded(model_path)
            from mlx_lm import generate
            from mlx_lm.sample_utils import make_sampler

            prompt_text = self._build_prompt(system, prompt)
            sampler = make_sampler(temp=temp)
            out = generate(
                self._model, self._tokenizer,
                prompt=prompt_text,
                max_tokens=max_tokens,
                sampler=sampler,
                verbose=False,
            )
            self._last_used = time.time()
            return out

    def stream(
        self,
        model_path: Path,
        prompt: str,
        system: str = "",
        max_tokens: int = 512,
        temp: float = 0.4,
    ) -> Iterator[str]:
        """Streaming generation — yields token text as it arrives. Use this
        for /ask so the user sees the answer flow rather than waiting for
        the full completion. The lock is held across the whole stream so
        concurrent calls queue up rather than corrupting the KV cache."""
        with self._lock:
            self._ensure_loaded(model_path)
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler

            prompt_text = self._build_prompt(system, prompt)
            sampler = make_sampler(temp=temp)
            for chunk in stream_generate(
                self._model, self._tokenizer,
                prompt=prompt_text,
                max_tokens=max_tokens,
                sampler=sampler,
            ):
                # stream_generate yields GenerationResponse objects with a
                # .text attribute on each token chunk. Older versions yielded
                # raw strings; handle both for forward-compat resilience.
                yield getattr(chunk, "text", chunk)
            self._last_used = time.time()


def get_runtime() -> MLXRuntime:
    """Convenience accessor — same as MLXRuntime.get()."""
    return MLXRuntime.get()
