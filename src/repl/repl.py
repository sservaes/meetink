#!/usr/bin/env python3
"""Inline-mode REPL for meetink (prompt_toolkit PromptSession + patch_stdout).

Layout:
  ...  command output (lives in the terminal's normal scrollback)  ...
  > input line                              ← prompt rendered by PromptSession
  🎙 model │ 📁 dir │ 👤 ID │ ✨ titling   ← 2-line bottom_toolbar redrawn
  ● recording 02:14 │ 24 lines               every refresh tick

Why inline (vs. the previous full_screen Application): the alt-screen mode
gave us a fixed footer at the cost of native scrollback, native text
selection, and native Cmd+F search. The inline mode lets the terminal own
the scrollback (so wheel scroll, click-and-drag select, and copy all just
work), and the footer is pinned-but-scrolls-with-content — same as the
status line in `man`, `claude`, etc. Trade-off: the footer scrolls away
with the rest when you scroll up; scroll back to the prompt to re-check
status.

Slash commands shell out to bin/meetink for everything stateful; the UI
lives here.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Add src/ to sys.path so we can `from llm.mlx_runtime import get_runtime`
# without a package-relative import (repl.py is run as a script, not module).
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.completion import (
        Completer, Completion, NestedCompleter, WordCompleter,
    )
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
except ImportError:
    print("error: prompt_toolkit not installed in this venv", file=sys.stderr)
    print("       Run: bin/meetink setup", file=sys.stderr)
    sys.exit(1)


# Force the zsh launcher (and any other child processes) to emit ANSI colors
# even when stdout is piped (capture_output=True paths). When subprocess.run
# inherits the TTY directly, this is redundant — the launcher detects the
# TTY — but harmless.
os.environ["MK_FORCE_COLOR"] = "1"


# ---------------------------------------------------------------------------
# Paths + state lookup (mirrors bin/meetink defaults)
# ---------------------------------------------------------------------------

MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))
MK_TRANSCRIPTS_BASE = Path(os.environ.get(
    "MEETINK_TRANSCRIPTS_DIR",
    os.path.expanduser("~/Documents/meetink"),
))
MK_CONFIG_FILE = MK_HOME / "config"


def active_project() -> str:
    """Mirror project_active_get in projects.sh — empty string = no project."""
    try:
        if MK_CONFIG_FILE.exists():
            for line in MK_CONFIG_FILE.read_text().splitlines():
                if line.startswith("active_project="):
                    val = line.split("=", 1)[1].strip()
                    if val and "/" not in val and "." not in val:
                        return val
    except OSError:
        pass
    return ""


def _resolve_transcripts_dir() -> Path:
    proj = active_project()
    return MK_TRANSCRIPTS_BASE / proj if proj else MK_TRANSCRIPTS_BASE


MK_TRANSCRIPTS_DIR = _resolve_transcripts_dir()
MK_TRANSCRIPT = Path(os.environ.get(
    "MEETINK_TRANSCRIPT",
    str(MK_TRANSCRIPTS_DIR / "live.txt"),
))
PID_FILE = Path("/tmp/meetink-capture.pid")
DIARIZE_PIDFILE = Path("/tmp/meetink-diarize.pid")
LAUNCHER = Path(os.environ.get("MK_LAUNCHER", "")) if os.environ.get("MK_LAUNCHER") else None
if LAUNCHER is None:
    LAUNCHER = Path(__file__).resolve().parent.parent.parent / "bin" / "meetink"


def _config_get(key: str, default: str = "") -> str:
    if not MK_CONFIG_FILE.exists():
        return default
    try:
        for line in MK_CONFIG_FILE.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        os.kill(int(PID_FILE.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def recording_start() -> float | None:
    try:
        return PID_FILE.stat().st_mtime
    except (OSError, FileNotFoundError):
        return None


def line_count(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def active_model() -> str:
    return _config_get("active_model", "small.en")


def diarize_enabled() -> bool:
    return _config_get("diarize_enabled", "true") not in ("false", "off", "0")


def diarize_available() -> bool:
    return (
        (MK_HOME / "diarize-venv" / "bin" / "python").exists()
        and (MK_HOME / "models" / "speaker-embedding.onnx").exists()
    )


def diarize_running() -> bool:
    if not DIARIZE_PIDFILE.exists():
        return False
    try:
        os.kill(int(DIARIZE_PIDFILE.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def profile_count() -> int:
    d = MK_HOME / "profiles"
    if not d.exists():
        return 0
    return len(list(d.glob("*.npz"))) + len(list(d.glob("*.npy")))


def _title_backend() -> str:
    """Mirror title_backend_active() in titling.sh: env > config > default."""
    env = os.environ.get("MEETINK_TITLE_BACKEND")
    if env in ("local", "claude"):
        return env
    cfg = MK_HOME / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("title_backend="):
                    val = line.split("=", 1)[1].strip()
                    if val in ("local", "claude"):
                        return val
        except OSError:
            pass
    return "local"


# Maps active local model name (config: local_llm_model=) to the on-disk
# snapshot directory under ~/.meetink/models/mlx/. Mirrors MK_LLM_REGISTRY
# in titling.sh — keep in sync.
_LOCAL_LLM_DIRS = {
    "qwen3.5-0.8b": "Qwen3.5-0.8B-4bit",
    "qwen3.5-2b":   "Qwen3.5-2B-4bit",
    "qwen3.5-4b":   "Qwen3.5-4B-4bit",
    "qwen3.5-9b":   "Qwen3.5-9B-4bit",
}


def _active_local_model_path() -> Path:
    """Resolve the MLX snapshot directory for the active local model,
    matching titling.sh's llm_path. The directory contains config.json,
    tokenizer*, and the .safetensors shards."""
    env = os.environ.get("MEETINK_LLM_MODEL")
    if env:
        return Path(env)
    active = "qwen3.5-2b"
    cfg = MK_HOME / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("local_llm_model="):
                    val = line.split("=", 1)[1].strip()
                    if val in _LOCAL_LLM_DIRS:
                        active = val
                    break
        except OSError:
            pass
    return MK_HOME / "models" / "mlx" / _LOCAL_LLM_DIRS.get(active, _LOCAL_LLM_DIRS["qwen3.5-2b"])


def _mlx_lm_installed() -> bool:
    """Mirror titling.sh's _local_available: checks the REPL venv has
    mlx_lm. We're already running inside that venv (this is repl.py), so
    a direct import attempt is the cheapest possible check."""
    try:
        import mlx_lm  # noqa: F401
        return True
    except ImportError:
        return False


def llm_available() -> bool:
    """Match titling.sh llm_available: backend-aware. Local backend needs
    mlx_lm in the venv AND the active model snapshot on disk (both the
    weights/config and the chat_template.jinja — see llm_present in
    titling.sh for the rationale)."""
    if _title_backend() == "claude":
        return shutil.which("claude") is not None
    snapshot = _active_local_model_path()
    return (
        _mlx_lm_installed()
        and (snapshot / "config.json").exists()
        and (snapshot / "chat_template.jinja").exists()
    )


def _claude_model() -> str:
    """Mirror claude_model_active() in titling.sh: env > config > default."""
    env = os.environ.get("MEETINK_CLAUDE_MODEL")
    if env:
        return env
    cfg = MK_HOME / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("claude_model="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
        except OSError:
            pass
    return "claude-sonnet-4-6"


def _titling_label() -> str:
    """Short label for the active titling backend/model — used in the footer."""
    if _title_backend() == "claude":
        m = _claude_model().lower()
        if "sonnet" in m: return "Sonnet"
        if "haiku" in m:  return "Haiku"
        if "opus" in m:   return "Opus"
        return _claude_model()
    active = ""
    cfg = MK_HOME / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("local_llm_model="):
                    active = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    if not active:
        active = "qwen3.5-2b"
    a = active.lower()
    if "0.8b" in a: return "Qwen3.5-0.8B"
    if "2b"   in a: return "Qwen3.5-2B"
    if "4b"   in a: return "Qwen3.5-4B"
    if "9b"   in a: return "Qwen3.5-9B"
    return active


# ---------------------------------------------------------------------------
# RAM info for the footer chip — helps users decide which local LLM fits.
# Total is read once (sysctl, ~ms). Free is re-read every 5s via vm_stat
# parsing — cheap with the cache, would be wasteful on every keystroke.
# ---------------------------------------------------------------------------

_ram_total_gb_cached: float | None = None
_ram_free_cache: dict = {"gb": None, "ts": 0.0}


def _ram_total_gb() -> float:
    global _ram_total_gb_cached
    if _ram_total_gb_cached is not None:
        return _ram_total_gb_cached
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        _ram_total_gb_cached = int(out) / (1024 ** 3)
    except Exception:
        _ram_total_gb_cached = 0.0
    return _ram_total_gb_cached


def _ram_free_gb() -> float:
    """macOS-specific: parse vm_stat. "Free" here = Pages free + inactive +
    purgeable, the same definition Activity Monitor's "App Memory" bar uses."""
    now = time.time()
    if _ram_free_cache["gb"] is not None and (now - _ram_free_cache["ts"]) < 5.0:
        return _ram_free_cache["gb"]
    try:
        out = subprocess.check_output(
            ["vm_stat"], text=True, stderr=subprocess.DEVNULL
        )
        page_size = 4096
        ms = re.search(r"page size of (\d+)", out)
        if ms:
            page_size = int(ms.group(1))
        pages: dict[str, int] = {}
        for line in out.splitlines():
            if ":" in line and line.lstrip().startswith("Pages"):
                k, v = line.split(":", 1)
                v = v.strip().rstrip(".")
                if v.isdigit():
                    pages[k.strip()] = int(v)
        free_pages = (
            pages.get("Pages free", 0)
            + pages.get("Pages inactive", 0)
            + pages.get("Pages purgeable", 0)
        )
        _ram_free_cache["gb"] = (free_pages * page_size) / (1024 ** 3)
    except Exception:
        _ram_free_cache["gb"] = 0.0
    _ram_free_cache["ts"] = now
    return _ram_free_cache["gb"]


# ---------------------------------------------------------------------------
# Footer (bottom_toolbar). prompt_toolkit re-invokes this every refresh tick
# so the recording timer ticks live.
# ---------------------------------------------------------------------------

FOOTER_SEP = "\033[90m │ \033[0m"


# Cached chip for the RAG indexer. File-IO-light, but the footer ticks
# every 1s; cache the rendered string for 2s so we don't stat the .idx
# dir every refresh.
_index_chip_cache: dict = {"text": None, "ts": 0.0}


def _index_chip() -> str:
    """Status chip for the RAG sidecar indexer. Always renders so the
    user gets positive confirmation that the install worked.

    States:
      📚 off             — fastembed not installed
      📚 ready           — installed, no recording active
      📚 starting        — recording active, embedder still loading
      📚 NN L · M segs   — recording active, indexer keeping up
    """
    now = time.time()
    if (_index_chip_cache["text"] is not None and
            now - _index_chip_cache["ts"] < 2.0):
        return _index_chip_cache["text"]

    text = ""
    try:
        from index import IndexManager  # type: ignore[import-not-found]
        from index.retriever import chunk_count  # type: ignore[import-not-found]
    except ImportError:
        _index_chip_cache["text"] = ""
        _index_chip_cache["ts"] = now
        return ""

    mgr = IndexManager.get()
    if not mgr.is_available():
        text = "\033[90m📚 off\033[0m"
    elif is_running():
        tx = _ask_transcript_path()
        if tx is not None:
            n = chunk_count(tx)
            seg_dir = tx.with_suffix(".idx") / "segments"
            seg_count = 0
            if seg_dir.is_dir():
                try:
                    seg_count = sum(1 for _ in seg_dir.glob("*.md"))
                except OSError:
                    pass
            if n > 0:
                text = f"\033[36m📚 {n}L · {seg_count} segs\033[0m"
            else:
                text = "\033[33m📚 starting\033[0m"
        else:
            text = "\033[36m📚 ready\033[0m"
    else:
        text = "\033[36m📚 ready\033[0m"

    _index_chip_cache["text"] = text
    _index_chip_cache["ts"] = now
    return text


def _footer_top_raw() -> str:
    """Static-ish config: model, transcripts folder, speaker-ID setup state."""
    parts: list[str] = []
    parts.append(f"\033[36m🎙 {active_model()}\033[0m")
    proj = active_project()
    transcripts_dir = _resolve_transcripts_dir()
    short_dir = str(transcripts_dir).replace(str(Path.home()), "~")
    parts.append(f"\033[90m📁 {short_dir}\033[0m")
    if proj:
        parts.append(f"\033[35m📦 {proj}\033[0m")

    if not diarize_available():
        parts.append("\033[90m👤 not installed\033[0m")
    elif not diarize_enabled():
        parts.append("\033[90m👤 off\033[0m")
    elif diarize_running():
        n = profile_count()
        label = f"{n} profile" + ("s" if n != 1 else "")
        parts.append(f"\033[36m👤 {label}\033[0m")
    elif is_running():
        parts.append("\033[33m👤 starting\033[0m")
    else:
        n = profile_count()
        if n > 0:
            parts.append(f"\033[36m👤 {n} enrolled\033[0m")
        else:
            parts.append("\033[36m👤 ready\033[0m")

    if llm_available():
        parts.append(f"\033[36m✨ {_titling_label()}\033[0m")
    else:
        parts.append("\033[90m✨ off\033[0m")

    chip = _index_chip()
    if chip:
        parts.append(chip)

    total = _ram_total_gb()
    free = _ram_free_gb()
    if total > 0:
        if free < 4:
            colour = "\033[31m"
        elif free < 8:
            colour = "\033[33m"
        else:
            colour = "\033[32m"
        parts.append(
            f"\033[90m🧠 {total:.0f}GB · {colour}{free:.1f}GB free\033[0m"
        )

    return FOOTER_SEP.join(parts)


# Per-segment gradient for the context bar — green → yellow as fill grows.
# 256-color escapes; degrades gracefully on terminals that don't honour the
# specific colour (they'll fall back to the closest 16-colour match).
_CTX_BAR_GRADIENT = (
    "\033[38;5;46m",   # bright green
    "\033[38;5;82m",
    "\033[38;5;118m",
    "\033[38;5;154m",
    "\033[38;5;190m",
    "\033[38;5;226m",  # yellow
    "\033[38;5;220m",
    "\033[38;5;208m",  # orange
)
_CTX_BAR_SEGMENTS = len(_CTX_BAR_GRADIENT)
_CTX_BAR_DIM = "\033[38;5;238m"

# Cached chip — recomputed at most every 2s so the 1s footer tick doesn't
# stat the transcript file on every keystroke / blink.
_ctx_bar_cache: dict = {"text": "", "ts": 0.0}


def _active_local_llm_key() -> str:
    """Read local_llm_model= from the config, fall back to the default."""
    cfg = MK_HOME / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("local_llm_model="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
        except OSError:
            pass
    return "qwen3.5-2b"


def _claude_budget() -> int:
    """Approximate the active Claude model's context window. Sonnet/Opus/
    Haiku 4.x ship at 200K; the `[1m]` extended-window variants take 1M.
    Tokens, not characters."""
    m = _claude_model().lower()
    if "1m" in m:
        return 1_000_000
    return 200_000


def _active_backend_budget() -> int:
    """Token budget of whichever backend is currently active. For local
    that's the per-quant /ask budget; for claude, the model's window."""
    if _title_backend() == "claude":
        return _claude_budget()
    return _ask_budget_for(_active_local_llm_key())


def _context_bar() -> str:
    """Render a 'context ▰▰▰▱▱▱▱▱ 38% 16K' chip showing how full the active
    backend's token budget is for the next /ask. Always visible — even on
    claude, the chip tells the user how much headroom remains in the 200K
    (or 1M, for [1m] variants) window. Uses a chars/4 estimate so the
    footer doesn't pay tokenizer cost on every tick."""
    now = time.time()
    if _ctx_bar_cache["text"] and now - _ctx_bar_cache["ts"] < 2.0:
        return _ctx_bar_cache["text"]

    budget = _active_backend_budget()
    # Local path reserves ~600 tokens for response + safety (matches
    # _try_handle_ask_local). Claude has plenty of headroom; reserve a
    # nominal 2K so the bar approaches 100% honestly when the user is
    # actually approaching the window's edge.
    reserved = 2000 if _title_backend() == "claude" else 600
    effective = max(1, budget - reserved)

    # Cheap chars/4 estimate of what the next /ask prompt would weigh.
    # Mirror the strategy ladder in _try_handle_ask_local: for backend=local
    # we prefer the per-doc .summary.md when one exists (that's what /ask
    # actually escalates to when the full doc would overflow); for claude
    # we always count the full markdown.
    used = 0
    backend_is_local = _title_backend() == "local"

    tx = _ask_transcript_path()
    has_idx = False
    if tx is not None:
        try:
            from index import IndexManager  # type: ignore[import-not-found]
            has_idx = IndexManager.get().has_index_for(tx)
        except ImportError:
            pass
    if tx is not None:
        if has_idx:
            # Index path: /ask substitutes the raw transcript with the
            # bounded RAG sections. Estimate matches retrieve_for_ask:
            # rollups + per-segment summaries + recent tail + retrieved
            # excerpts.
            idx_dir = tx.with_suffix(".idx")
            for name in ("decisions.md", "actions.md"):
                p = idx_dir / name
                if p.is_file():
                    try:
                        used += p.stat().st_size // 4
                    except OSError:
                        pass
            seg_dir = idx_dir / "segments"
            if seg_dir.is_dir():
                try:
                    for p in seg_dir.glob("*.md"):
                        try:
                            used += p.stat().st_size // 4
                        except OSError:
                            pass
                except OSError:
                    pass
            # Constant-ish: last 20 lines verbatim (~400 tok) and ~24
            # retrieved chunks after ±1 expansion (~480 tok).
            used += 880
        else:
            try:
                used += tx.stat().st_size // 4
            except OSError:
                pass

    try:
        from llm.mlx_runtime import get_runtime  # type: ignore[import-not-found]
        used += len(get_runtime().ask_history_text()) // 4
    except Exception:
        pass

    proj_dir = _resolve_transcripts_dir()

    # Project's rolling past-meetings digest. /ask trims this with the
    # recency tier slicer for backend=local — we just count the file size,
    # which slightly over-estimates compared to the slice. That's fine for
    # a footer chip: erring on the side of "you're closer to full" is the
    # right kind of conservatism.
    meetings_md = proj_dir / "meetings.md"
    if meetings_md.is_file():
        try:
            used += meetings_md.stat().st_size // 4
        except OSError:
            pass

    # Per-project context docs. Prefer .summary.md on local so the bar
    # tracks what /ask actually consumes after it falls back to summaries.
    ctx_dir = proj_dir / "_context"
    if ctx_dir.is_dir():
        try:
            for f in ctx_dir.glob("*.md"):
                if f.name.endswith(".summary.md"):
                    continue  # counted via its parent below if needed
                summary = ctx_dir / f"{f.stem}.summary.md"
                target = summary if backend_is_local and summary.is_file() else f
                try:
                    used += target.stat().st_size // 4
                except OSError:
                    pass
        except OSError:
            pass

    pct = min(100, int(round(100 * used / effective)))
    filled = max(0, min(_CTX_BAR_SEGMENTS, int(round(_CTX_BAR_SEGMENTS * pct / 100))))

    if pct < 60:
        pct_colour = "\033[38;5;82m"
    elif pct < 85:
        pct_colour = "\033[38;5;226m"
    else:
        pct_colour = "\033[38;5;196m"

    bar = ""
    for i in range(_CTX_BAR_SEGMENTS):
        if i < filled:
            bar += f"{_CTX_BAR_GRADIENT[i]}▰\033[0m"
        else:
            bar += f"{_CTX_BAR_DIM}▱\033[0m"

    if budget >= 1_000_000:
        budget_label = f"{budget // 1_000_000}M"
    elif budget >= 1000:
        budget_label = f"{budget // 1000}K"
    else:
        budget_label = str(budget)
    text = (
        f"\033[90mcontext\033[0m {bar} "
        f"{pct_colour}{pct}%\033[0m \033[90m{budget_label}\033[0m"
    )
    _ctx_bar_cache["text"] = text
    _ctx_bar_cache["ts"] = now
    return text


def _footer_bottom_raw() -> str:
    """Runtime state: recording status + transcript progress + context bar,
    with a hint on the right when idle."""
    parts: list[str] = []
    if is_running():
        start = recording_start()
        elapsed = int(time.time() - start) if start else 0
        time_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        parts.append(f"\033[32m● recording {time_str}\033[0m")
        n = line_count(MK_TRANSCRIPT)
        if n > 0:
            parts.append(f"\033[90m{n} lines\033[0m")
    else:
        parts.append("\033[90m○ idle\033[0m")

    bar = _context_bar()
    if bar:
        parts.append(bar)

    if not is_running():
        parts.append("\033[2m/start to record · /help for commands\033[0m")

    return FOOTER_SEP.join(parts)


# Last time the footer-tick checked for idle MLX release. The sweep is cheap
# (lockless when nothing is loaded; non-blocking when something is) but we
# still don't want to import + dispatch every refresh — once per 30s is more
# than fine since IDLE_RELEASE_SECONDS is 5 minutes.
_idle_sweep_ts: float = 0.0


def _sync_indexer_to_recording() -> None:
    """Match the IndexManager's lifecycle to the recording state. Called
    after any command that might have changed it (/start, /stop). Cheap
    when nothing changed: IndexManager.start/stop are idempotent."""
    try:
        from index import IndexManager  # type: ignore[import-not-found]
    except ImportError:
        return
    mgr = IndexManager.get()
    if not mgr.is_available():
        return
    if is_running():
        tx = _ask_transcript_path()
        if tx is not None:
            mgr.start(tx)
    else:
        if mgr.is_running():
            mgr.stop()


def _maybe_idle_sweep() -> None:
    """Periodic idle-release poll. The bottom of handle_command already
    sweeps after non-/ask commands, but if the user just sits at the
    prompt (no commands at all) the model would otherwise stay resident
    forever. This piggybacks on the footer's 1s tick so the sweep keeps
    happening even during quiet stretches."""
    global _idle_sweep_ts
    now = time.time()
    if now - _idle_sweep_ts < 30:
        return
    _idle_sweep_ts = now
    try:
        from llm.mlx_runtime import get_runtime
        get_runtime().maybe_release_idle()
    except ImportError:
        pass


def bottom_toolbar():
    """prompt_toolkit calls this every refresh tick to redraw the bottom
    region under the prompt. Two lines: static config above, runtime below."""
    _maybe_idle_sweep()
    return ANSI(_footer_top_raw() + "\n" + _footer_bottom_raw())


# ---------------------------------------------------------------------------
# Input prompt
# ---------------------------------------------------------------------------

SLASH_COMMANDS = [
    "/start", "/stop", "/status", "/tail", "/prompt", "/transcripts",
    "/model", "/llm", "/diarize", "/profile", "/project", "/me", "/ask",
    "/context", "/index", "/watch", "/setup", "/clear", "/help", "/quit",
]


def prompt_text():
    dot = "\033[32m●\033[0m" if is_running() else "\033[90m○\033[0m"
    return ANSI(f"\033[36mmeetink\033[0m {dot} \033[90m>\033[0m ")


# ---------------------------------------------------------------------------
# Completion: nested suggestions for /command sub <arg>
# ---------------------------------------------------------------------------

class _SlashNestedCompleter(NestedCompleter):
    """NestedCompleter for slash-prefixed commands.

    Stock NestedCompleter strips the leading `/` when matching first-token
    completions, so `/pr` yields nothing even though `/profile`, `/project`,
    `/prompt` are all in the dict. We override the first-token fallback to
    use WORD=True so the leading `/` stays in the partial word.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if " " not in text:
            yield from WordCompleter(
                list(self.options.keys()),
                ignore_case=self.ignore_case,
                WORD=True,
            ).get_completions(document, complete_event)
            return
        yield from super().get_completions(document, complete_event)


class _DynamicWords(Completer):
    """Lazy WordCompleter — `fn()` is called every keystroke so the list
    stays in sync with the filesystem (new project folders, removed
    profiles, …) without a REPL restart."""

    def __init__(self, fn):
        self.fn = fn

    def get_completions(self, document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        try:
            options = self.fn()
        except Exception:
            return
        wl = word.lower()
        for opt in options:
            if opt.lower().startswith(wl):
                yield Completion(opt, start_position=-len(word))


def _project_names() -> list[str]:
    base = MK_TRANSCRIPTS_BASE
    if not base.exists():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )


def _profile_names() -> list[str]:
    d = MK_HOME / "profiles"
    if not d.exists():
        return []
    names = {p.stem for p in d.glob("*.npz")} | {p.stem for p in d.glob("*.npy")}
    return sorted(names)


def _context_names() -> list[str]:
    """Names of /context-managed docs in the active project's _context/ dir.
    Excludes .summary.md siblings — they're addressed via the parent's name."""
    d = _resolve_transcripts_dir() / "_context"
    if not d.exists():
        return []
    names: list[str] = []
    for p in sorted(d.glob("*.md")):
        if p.name.endswith(".summary.md"):
            continue
        names.append(p.stem)
    return names


# Whisper model names match titles in src/lib/models.sh's MK_MODEL_REGISTRY.
_WHISPER_MODELS = [
    "tiny.en", "base.en", "small.en", "small.en-tdrz",
    "medium.en", "medium.en-tdrz", "large-v3-turbo", "large-v3",
]

# Local LLM names — keep in sync with MK_LLM_REGISTRY in src/lib/titling.sh.
_LOCAL_LLMS = ["qwen3.5-0.8b", "qwen3.5-2b", "qwen3.5-4b", "qwen3.5-9b"]


_project_names_completer = _DynamicWords(_project_names)
_profile_names_completer = _DynamicWords(_profile_names)
_context_names_completer = _DynamicWords(_context_names)
_whisper_model_completer = WordCompleter(_WHISPER_MODELS, ignore_case=True, WORD=True)
_local_llm_completer = WordCompleter(_LOCAL_LLMS, ignore_case=True, WORD=True)


_NESTED_COMMANDS = {
    "/start": None,
    "/stop": None,
    "/status": None,
    "/tail": None,
    "/prompt": None,
    "/transcripts": None,
    "/setup": None,
    "/clear": None,
    "/help": None,
    "/quit": None,
    "/model": {
        "list": None,
        "download": _whisper_model_completer,
        "use": _whisper_model_completer,
        "rm": _whisper_model_completer,
    },
    "/llm": {
        "status": None,
        "install": None,
        "list": None,
        "download": _local_llm_completer,
        "use": _local_llm_completer,
        "rm": _local_llm_completer,
        "backend": {"local": None, "claude": None},
        "model": {"sonnet": None, "haiku": None, "opus": None},
    },
    "/diarize": {
        "status": None,
        "install": None,
        "rm": None,
        "on": None,
        "off": None,
        "start": None,
        "stop": None,
    },
    "/profile": {
        "add": None,
        "list": None,
        "train": _profile_names_completer,
        "rm": _profile_names_completer,
        "clusters": None,
        "assign": None,
        "merge": None,
    },
    "/project": {
        "list": None,
        "use": _project_names_completer,
        "clear": None,
        "rm": _project_names_completer,
    },
    "/me": {"clear": None},
    "/ask": None,
    "/context": {
        "list": None,
        "add": None,           # <file> path — too dynamic to autocomplete usefully
        "rm": _context_names_completer,
        "show": _context_names_completer,
    },
    "/index": {
        "status": None,
        "install": None,
        "rm": None,
    },
    "/watch": {
        "events": None,
        "notify": None,
        "detect": None,
        "on": None,
        "off": None,
        "status": None,
        "help": None,
    },
}


# ---------------------------------------------------------------------------
# Slash command dispatch
# ---------------------------------------------------------------------------

HELP_TEXT = """\033[93mCOMMANDS\033[0m
  \033[1m/start\033[0m        \033[2mbegin recording (auto-opens transcript window)\033[0m
  \033[1m/stop\033[0m         \033[2mstop recording (closes transcript window)\033[0m
  \033[1m/status\033[0m       \033[2mshow recording state and line count\033[0m
  \033[1m/tail\033[0m         \033[2mopen or raise the live transcript window\033[0m
  \033[1m/prompt\033[0m       \033[2medit custom whisper vocabulary in TextEdit\033[0m
  \033[1m/transcripts\033[0m  \033[2mlist past transcripts\033[0m
  \033[1m/model\033[0m        \033[2mlist/switch/download whisper models\033[0m
  \033[1m/llm\033[0m          \033[2minstall/remove the AI-titling LLM\033[0m
  \033[1m/diarize\033[0m      \033[2mspeaker-ID sidecar (/diarize on|off|install|rm)\033[0m
  \033[1m/profile\033[0m      \033[2menroll voices: /profile add <name> | list | train | rm\033[0m
  \033[1m/project\033[0m      \033[2mscope recordings to a project: /project use <name> | list | clear\033[0m
  \033[1m/me\033[0m            \033[2mset your name: /me Stijn → mic stream labelled STIJN: in transcripts\033[0m
  \033[1m/ask\033[0m           \033[2mask the AI about the current/latest transcript: /ask what did we decide?\033[0m
  \033[1m/context\033[0m       \033[2mattach docs to a project: /context add report.pdf | list | rm | show\033[0m
  \033[1m/index\033[0m         \033[2mRAG sidecar for /ask on long meetings: /index install | status | rm\033[0m
  \033[1m/watch\033[0m         \033[2mauto-record from calendar (phase 1: /watch events | notify | detect)\033[0m
  \033[1m/setup\033[0m        \033[2minstall dependencies + download whisper model\033[0m
  \033[1m/clear\033[0m        \033[2mclear scrollback\033[0m
  \033[1m/help\033[0m         \033[2mthis list\033[0m
  \033[1m/quit\033[0m         \033[2mexit (recording continues if active)\033[0m

\033[2mTab to autocomplete commands. Trackpad / wheel / Cmd+F all work — the
terminal owns the scrollback now.\033[0m"""


def emit(text: str) -> None:
    """Print to the terminal scrollback. ANSI escapes are honoured by the
    terminal directly; print_formatted_text routes through patch_stdout so
    background-thread emissions interleave cleanly with an active prompt."""
    try:
        print_formatted_text(ANSI(text))
    except Exception:
        # Pre-prompt phase, or prompt_toolkit not yet active.
        print(text)


# ---------------------------------------------------------------------------
# In-process /ask
# ---------------------------------------------------------------------------
#
# When backend=local AND mlx_lm is installed in this venv, we handle /ask
# directly in the REPL process. The model loads on first call (~3s cold
# start) and stays resident in unified memory for subsequent calls (<1s).
# After IDLE_RELEASE_SECONDS of inactivity the runtime drops it so the RAM
# is reclaimable.
#
# Falls through to subprocess dispatch when:
#   - backend=claude (no resident model needed; claude -p is itself fast)
#   - mlx_lm import fails (venv not yet provisioned with mlx-lm)
#   - the active model snapshot isn't on disk
# Subprocess fall-through means meetink keeps working even if the in-process
# path breaks for any reason — degrades gracefully to the existing shell-out.


def _me_name() -> str:
    """Read me_name= from config. Mirrors me_name_get in identity.sh."""
    return _config_get("me_name", "")


def _ask_transcript_path() -> Path | None:
    """Resolve the transcript file to feed the model. Mirrors
    _ask_transcript_path in ask.sh: prefer the live recording (live.txt
    symlink, dereferenced), else the most-recently-modified .txt in the
    active project's transcripts dir, excluding the symlink itself."""
    if MK_TRANSCRIPT.is_symlink():
        target = Path(os.readlink(MK_TRANSCRIPT))
        if target.is_file():
            return target
    if MK_TRANSCRIPT.is_file():
        return MK_TRANSCRIPT
    txd = _resolve_transcripts_dir()
    if not txd.is_dir():
        return None
    candidates = [
        p for p in txd.glob("*.txt")
        if p.is_file() and not p.is_symlink()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block (---…---) if present. Used
    when including converted /context docs in a prompt — the frontmatter
    is metadata for our tooling, not content the model needs."""
    if not text.startswith("---\n"):
        return text
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        return text
    return rest[end + 5:]


def _context_doc_pairs() -> list[tuple[str, Path, Path | None]]:
    """List of (name, full_md_path, summary_md_path_or_None) for /context-
    managed docs in the active project. Sorted alphabetically by name.

    User-curated .md files (without a sibling .summary.md, e.g. a hand-
    written glossary.md) get summary_md_path=None — same path is used as
    "summary" since there's no smaller alternative."""
    ctx_dir = _resolve_transcripts_dir() / "_context"
    if not ctx_dir.is_dir():
        return []
    pairs: list[tuple[str, Path, Path | None]] = []
    for full in sorted(ctx_dir.glob("*.md")):
        if full.name.endswith(".summary.md"):
            continue
        sumf = ctx_dir / f"{full.stem}.summary.md"
        pairs.append((full.stem, full, sumf if sumf.is_file() else None))
    # Also pick up legacy .txt / .markdown drops that pre-date /context.
    for ext in ("*.txt", "*.markdown"):
        for p in sorted(ctx_dir.glob(ext)):
            pairs.append((p.stem, p, None))
    return pairs


def _ask_context_text(prefer_summaries: bool = False) -> str:
    """Concatenate the active project's _context/ docs into a single
    blob with per-doc headers. When `prefer_summaries=True`, use each
    doc's .summary.md if one exists (smaller; needed for local backend's
    8K context)."""
    pairs = _context_doc_pairs()
    if not pairs:
        return ""
    parts: list[str] = []
    for name, full, sumf in pairs:
        chosen = sumf if (prefer_summaries and sumf is not None) else full
        try:
            content = _strip_frontmatter(chosen.read_text())
        except OSError:
            continue
        suffix = " (summary)" if (prefer_summaries and sumf is not None) else ""
        parts.append(f"--- {name}{suffix} ---")
        parts.append(content.strip())
        parts.append("")
    return "\n".join(parts)


# Per-model practical context budget for /ask. Conservative — we leave
# headroom for the generated response (max 512 tokens per _try_handle_ask_local)
# plus a small safety margin. Native context windows are larger but bigger
# prompts cost more KV cache RAM.
_ASK_TOKEN_BUDGETS: dict[str, int] = {
    "qwen3.5-0.8b":  8_000,
    "qwen3.5-2b":   16_000,
    "qwen3.5-4b":   16_000,
    "qwen3.5-9b":   32_000,
}
_ASK_DEFAULT_BUDGET = 8_000


def _ask_budget_for(active_model_key: str) -> int:
    return _ASK_TOKEN_BUDGETS.get(active_model_key, _ASK_DEFAULT_BUDGET)


def _count_tokens(text: str, runtime=None) -> int:
    """Count tokens using the resident MLX tokenizer if loaded, else fall
    back to a chars/4 estimate. Using the actual tokenizer is essentially
    free once the runtime has loaded it (it's just a vocab lookup)."""
    if runtime is not None and runtime.is_loaded():
        try:
            return len(runtime._tokenizer.encode(text))
        except Exception:
            pass
    return len(text) // 4


# Recency tiers for past-meeting context. Indices are 1-based positions in
# meetings.md (newest first). Tuned for Qwen3.5-2B (8K context); Claude has
# headroom to take more, but the same tiering is fine — we just send less
# than the model could handle, which is harmless.
_PAST_MEETINGS_FULL_THROUGH = 3       # entries 1-3: full content (Topics +
                                       # Decisions + Action items + Open Qs)
_PAST_MEETINGS_CONDENSED_THROUGH = 10  # entries 4-10: Topics + Decisions only
_PAST_MEETINGS_HEADING_THROUGH = 30    # entries 11-30: heading line only
                                       # entries 31+: dropped from the prompt


def _slice_meetings_md(text: str) -> str:
    """Read a project's rolling meetings.md and return a recency-tiered
    excerpt to feed the model. Newer meetings get more detail; older ones
    fade to just their heading; very old ones drop entirely. The on-disk
    file always contains the full content — slicing is read-time only."""
    if not text.strip():
        return ""
    # Split into entries by the per-meeting `## ` heading. The first chunk
    # before the first ## is the file's own H1 + blank line — discard it.
    chunks = re.split(r"(?m)^## ", text)
    entries = [c for c in chunks[1:] if c.strip()]
    if not entries:
        return ""

    sliced: list[str] = []
    for idx, raw in enumerate(entries, start=1):
        if idx > _PAST_MEETINGS_HEADING_THROUGH:
            break
        # raw: "<heading>\n*generated by …*\n\n## Topics\n…"
        heading_line, _, body = raw.partition("\n")
        if idx <= _PAST_MEETINGS_FULL_THROUGH:
            sliced.append(f"## {heading_line}\n{body}".rstrip())
        elif idx <= _PAST_MEETINGS_CONDENSED_THROUGH:
            # Keep only the Topics + Decisions sections (drop Action items
            # and Open questions). Sections start with `### Topics` etc. in
            # meetings.md — see meetings_log_rebuild.
            kept_sections: list[str] = []
            for section in re.split(r"(?m)^### ", body):
                if not section.strip():
                    continue
                # First chunk is preamble (the *generated by* line).
                head = section.split("\n", 1)[0].strip().lower()
                if head in ("topics", "decisions"):
                    kept_sections.append("### " + section.rstrip())
            condensed = "\n\n".join(kept_sections) if kept_sections else "*(condensed)*"
            sliced.append(f"## {heading_line}\n{condensed}")
        else:
            sliced.append(f"## {heading_line}")
    return "\n\n".join(sliced)


def _meetings_md_for_project() -> str:
    """Return the recency-sliced past-meeting context for the active project,
    or empty string if no project / no meetings.md."""
    proj_dir = _resolve_transcripts_dir()
    meetings_md = proj_dir / "meetings.md"
    if not meetings_md.is_file():
        return ""
    try:
        return _slice_meetings_md(meetings_md.read_text())
    except OSError:
        return ""


def _build_ask_prompt(
    question: str,
    transcript_path: Path | None,
    prefer_summaries: bool = False,
    include_past_meetings: bool = True,
    include_ask_history: bool = True,
) -> tuple[str, str, dict]:
    """Build (system_prompt, user_prompt, stats) the way ask.sh does, but
    in Python so we can pass straight to mlx_runtime.stream() without a
    subprocess.

    Composes (in order) the user prompt body:
      - user identity (from /me)
      - project name
      - background docs (manual, from <project>/_context/)
      - past meetings digest (auto, recency-tiered slice of meetings.md)
      - current transcript (omitted when transcript_path is None)
      - question
    Sections that don't apply are omitted entirely.

    `transcript_path=None` is allowed — /ask is also useful for project-only
    questions when only context docs or past meetings exist.
    `prefer_summaries=True` uses per-doc .summary.md files instead of the
    full converted markdown — needed for local backend's tighter budget.
    `include_past_meetings=False` suppresses the meetings.md section
    entirely; used as a last-resort drop when even summaries don't fit."""
    me = _me_name()
    project = active_project()
    context = _ask_context_text(prefer_summaries=prefer_summaries)
    past_meetings = _meetings_md_for_project() if include_past_meetings else ""
    ask_history = ""
    if include_ask_history:
        try:
            from llm.mlx_runtime import get_runtime
            ask_history = get_runtime().ask_history_text()
        except ImportError:
            ask_history = ""

    # Try the RAG path: if a sidecar index exists for this transcript, we
    # replace the bulky raw-transcript section with a structured assembly
    # of decisions / actions / retrieved chunks / recent tail / segment
    # summaries. Falls back to today's full-transcript path when there's
    # no index (sentence-transformers not installed, or recording started
    # before the index feature).
    transcript_text = ""
    index_assembly = None
    used_index = False
    if transcript_path is not None:
        try:
            from index import IndexManager  # type: ignore[import-not-found]
            mgr = IndexManager.get()
            if mgr.has_index_for(transcript_path):
                index_assembly = mgr.retrieve_for_ask(transcript_path, question)
                used_index = True
        except ImportError:
            pass
        if not used_index:
            try:
                transcript_text = transcript_path.read_text()
            except OSError:
                transcript_text = ""

    if transcript_path is not None:
        system = (
            "You are an assistant helping a user reason about a meeting "
            "transcript. Answer their question concisely and directly. If "
            "the transcript doesn't contain enough information to answer, "
            "say so plainly rather than speculating. When past meetings from "
            "the same project are provided, you may reference them — they "
            "appear newest first, with older entries condensed; the most "
            "recent are most relevant. When earlier turns in the current "
            "conversation are provided, treat them as the active thread — "
            "the user may be asking a follow-up that depends on what you "
            "said before."
        )
    else:
        system = (
            "You are an assistant helping a user reason about an ongoing "
            "project. Answer their question concisely and directly using "
            "the provided background documents and past meeting summaries. "
            "If the context doesn't contain enough information to answer, "
            "say so plainly rather than speculating. Past meetings appear "
            "newest first; older entries are condensed. When earlier turns "
            "in the current conversation are provided, treat them as the "
            "active thread — the user may be asking a follow-up that "
            "depends on what you said before."
        )

    parts = []
    if me:
        parts.append(
            f"The user's name is {me} (their lines appear as {me.upper()}: "
            "in transcripts)."
        )
    if project:
        parts.append(f"Active project: {project}.")
    if context:
        parts.append(
            "Background documents (the user has curated these as relevant "
            f"context):\n{context}"
        )
    if past_meetings:
        parts.append(
            "Past meetings in this project (newest first; older entries are "
            f"condensed or heading-only by recency):\n\n{past_meetings}"
        )
    if used_index and index_assembly is not None:
        # Structured RAG section. Each subsection is dropped if empty so
        # the model isn't fed empty headings. Order: rollups first (the
        # cheap, dense facts), then the per-segment summaries (the
        # global timeline view), then the retrieved excerpts (the dense
        # match for the question), and finally the recent tail (verbatim
        # so "what did we just say" answers stay grounded).
        sections: list[str] = []
        if index_assembly.decisions.strip():
            sections.append(
                f"### Decisions made so far\n{index_assembly.decisions.strip()}"
            )
        if index_assembly.actions.strip():
            sections.append(
                f"### Action items so far\n{index_assembly.actions.strip()}"
            )
        if index_assembly.segment_summaries.strip():
            sections.append(
                "### Per-segment summaries (chronological, ~5 min each)\n"
                f"{index_assembly.segment_summaries.strip()}"
            )
        if index_assembly.retrieved_chunks.strip():
            sections.append(
                "### Most relevant transcript excerpts to the question\n"
                f"{index_assembly.retrieved_chunks.strip()}"
            )
        if index_assembly.recent_tail.strip():
            sections.append(
                "### Most recent transcript lines (verbatim)\n"
                f"{index_assembly.recent_tail.strip()}"
            )
        body = "\n\n".join(sections) if sections else "(no indexed content yet)"
        parts.append(
            f"Current meeting (file: {transcript_path.name}, "
            f"{index_assembly.chunk_count} indexed lines):\n\n{body}"
        )
    elif transcript_path is not None:
        parts.append(
            f"Current meeting transcript (file: {transcript_path.name}):\n"
            f"{transcript_text}"
        )
    if ask_history:
        parts.append(
            "Earlier in this conversation (most recent last; the user may be "
            f"asking a follow-up):\n\n{ask_history}"
        )
    parts.append(f"Question from the user: {question}")
    user = "\n\n".join(parts)

    stats = {
        "doc_count": len(_context_doc_pairs()),
        "used_summaries": prefer_summaries,
        "had_past_meetings": bool(past_meetings),
        "had_ask_history": bool(ask_history),
        "had_transcript": transcript_path is not None,
        "used_index": used_index,
        "indexed_chunks": index_assembly.chunk_count if index_assembly else 0,
    }
    return system, user, stats


# Synchronization for the background-threaded /ask path. The lock
# serializes back-to-back /ask calls (the underlying MLXRuntime also has its
# own lock, but holding this one in handle_command prevents the prompt loop
# from racing the worker thread on emit ordering).
_ask_running = threading.Event()


def _try_handle_ask_local(question: str) -> bool:
    """In-process /ask path. Returns True if handled (success or graceful
    error printed to terminal); False to fall back to subprocess dispatch.

    Generation runs in a background thread so the prompt loop stays alive
    while the model thinks — that's what keeps the bottom_toolbar (footer)
    rendered the whole time. Streamed tokens are written via
    print_formatted_text, which patch_stdout interleaves above the active
    prompt. Each chunk is followed by an app.invalidate() so the renderer
    paints immediately rather than waiting for the next refresh tick."""
    if _title_backend() != "local":
        return False  # Claude path goes through the launcher
    model_path = _active_local_model_path()
    if not (model_path / "config.json").exists():
        return False  # Snapshot missing — let the shell path show the error
    if not (model_path / "chat_template.jinja").exists():
        emit("\033[31merror:\033[0m model snapshot is missing chat_template.jinja")
        emit(f"  \033[2mRun /llm rm {model_path.name} && /llm download {model_path.name} to re-fetch.\033[0m")
        return True
    try:
        from llm.mlx_runtime import get_runtime
    except ImportError:
        return False  # mlx_runtime module missing somehow

    transcript_path = _ask_transcript_path()
    # /ask is useful even without a transcript — context docs and past-meeting
    # digests are valid grounding too. Only refuse when we have absolutely
    # nothing to anchor the answer in.
    if transcript_path is None:
        has_context = bool(_context_doc_pairs())
        has_past = bool(_meetings_md_for_project())
        if not has_context and not has_past:
            emit("\033[31merror:\033[0m nothing to ask about")
            emit("  \033[2mAttach context with /context add <file>, or /start a recording first.\033[0m")
            return True

    # Lazy index build for past transcripts. If a transcript exists but
    # has no .idx sidecar (recorded before /index install was run, or on
    # an older meetink), build one now. The cost is one-time per
    # transcript: ~5-30 s for a 1h meeting on local. Skipped when
    # sentence-transformers isn't installed (graceful degradation to the
    # full-transcript path).
    if transcript_path is not None and not is_running():
        try:
            from index import IndexManager  # type: ignore[import-not-found]
            mgr = IndexManager.get()
            if mgr.is_available() and not mgr.has_index_for(transcript_path):
                emit(f"\033[2mBuilding index for {transcript_path.name} (one-time, ~30s)...\033[0m")
                mgr.ensure_index_for(transcript_path)
        except ImportError:
            pass

    if _ask_running.is_set():
        # Previous /ask still streaming — refuse rather than silently queue
        # behind the MLXRuntime lock (would look like a hang to the user).
        emit("\033[33m⚠\033[0m  An /ask is already running — wait for it to finish.")
        return True

    runtime = get_runtime()
    loading = not runtime.is_loaded()

    # The model has to be loaded to count tokens accurately. If it isn't
    # loaded yet, ensure_loaded happens lazily inside runtime.stream() —
    # but we want to know the budget BEFORE streaming. So load it here
    # synchronously if needed. This is the same load that would happen on
    # first stream() call; we just bring it forward by a few ms.
    try:
        # ensure_loaded is private but safe — locks internally.
        with runtime._lock:
            runtime._ensure_loaded(model_path)
    except RuntimeError as e:
        emit(f"\033[31merror:\033[0m {e}")
        return True

    # Determine the active model's token budget and try to fit the prompt
    # into it. Strategy in escalating order:
    #   1. full docs + past meetings (most context, best /ask quality)
    #   2. .summary.md docs + past meetings (smaller — drops to summaries)
    #   3. .summary.md docs only, no past meetings (last resort)
    # Each step rebuilds the prompt and re-counts tokens. We reserve 600
    # tokens for the response (max_tokens=512 plus padding).
    active_model_key = ""
    cfg = MK_HOME / "config"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.startswith("local_llm_model="):
                active_model_key = line.split("=", 1)[1].strip()
                break
    budget = _ask_budget_for(active_model_key) - 600

    strategies = [
        ("full",       {"prefer_summaries": False, "include_past_meetings": True}),
        ("summaries",  {"prefer_summaries": True,  "include_past_meetings": True}),
        ("compact",    {"prefer_summaries": True,  "include_past_meetings": False}),
    ]
    chosen_strategy = "full"
    system = user = ""
    stats: dict = {}
    used_tokens = 0
    for label, kwargs in strategies:
        system, user, stats = _build_ask_prompt(question, transcript_path, **kwargs)
        used_tokens = _count_tokens(system + "\n" + user, runtime=runtime)
        if used_tokens <= budget:
            chosen_strategy = label
            break
        chosen_strategy = label  # fall through; if all overflow, last one stays
    stats["chosen_strategy"] = chosen_strategy
    stats["used_tokens"] = used_tokens
    stats["budget"] = budget

    # Pre-flight message — synchronous, lands above the prompt before the
    # background worker spawns. Format it as a compact one-liner with the
    # token budget visible so the user can see what fit.
    budget_kb = budget // 1000
    used_kb = used_tokens / 1000.0
    docs_label = ""
    if stats["doc_count"] > 0:
        if chosen_strategy in ("summaries", "compact"):
            docs_label = f" · {stats['doc_count']} docs (summaries)"
        else:
            docs_label = f" · {stats['doc_count']} docs"
    meetings_label = " · past meetings" if stats["had_past_meetings"] else ""
    index_label = ""
    if stats.get("used_index"):
        n = stats.get("indexed_chunks", 0)
        index_label = f" · indexed {n}L"
    if loading:
        emit(
            f"\033[2mLoading {model_path.name} (~2-3s cold start)... "
            f"({used_kb:.1f}K of {budget_kb}K{meetings_label}{docs_label}{index_label})\033[0m"
        )
    else:
        emit(
            f"\033[2mAsking {model_path.name}... "
            f"({used_kb:.1f}K of {budget_kb}K{meetings_label}{docs_label}{index_label})\033[0m"
        )

    # Warn loudly if even the most-compact strategy still overshoots — the
    # generation will still try, but coherence may suffer past the model's
    # native window.
    if used_tokens > budget:
        emit(
            f"\033[33m⚠\033[0m  Prompt is {used_kb:.1f}K tokens but budget is "
            f"{budget_kb}K — answer quality may degrade. Consider /llm backend claude."
        )

    def worker():
        # Accumulate the visible (non-think) text so we can persist the
        # finished answer into MLXRuntime's ask history. Follow-up /asks
        # in the same session then see the prior Q&A as a thread.
        answer_buf: list[str] = []
        try:
            # Stream tokens via print_formatted_text — patch_stdout-aware,
            # so they render above the still-live prompt + footer. Buffer
            # at line granularity so the <think>…</think> filter can decide
            # before we emit; chunk-level streaming would mean partial
            # `<think>` tokens leak before we'd recognised the open marker.
            in_think = False
            buffer = ""
            for chunk in runtime.stream(
                model_path=model_path,
                prompt=user,
                system=system,
                max_tokens=512,
                temp=0.4,
            ):
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if "<think>" in line:
                        in_think = True
                        line = line.split("<think>")[0]
                    if "</think>" in line:
                        in_think = False
                        line = line.split("</think>", 1)[1]
                    if not in_think:
                        print_formatted_text(ANSI(line))
                        answer_buf.append(line)
                # Force a redraw per chunk so the user sees streaming, not
                # 1-second bursts at refresh_interval. invalidate is a no-op
                # if the app isn't running (race during teardown).
                try:
                    get_app().invalidate()
                except Exception:
                    pass
            # Flush any trailing partial line.
            if buffer and not in_think:
                print_formatted_text(ANSI(buffer))
                answer_buf.append(buffer)
            # Save into the thread for follow-ups. Cancelled / empty
            # streams are skipped by add_ask_pair itself.
            runtime.add_ask_pair(question, "\n".join(answer_buf))
        except RuntimeError as e:
            print_formatted_text(ANSI(f"\033[31merror:\033[0m {e}"))
        except Exception as e:
            print_formatted_text(ANSI(f"\033[31merror:\033[0m {type(e).__name__}: {e}"))
        finally:
            _ask_running.clear()
            try:
                get_app().invalidate()
            except Exception:
                pass

    _ask_running.set()
    threading.Thread(target=worker, daemon=True, name="meetink-ask").start()
    return True


def handle_command(line: str) -> bool:
    """Dispatch a slash command. Returns False to quit, True to continue.

    Unlike the previous full-screen design, command output goes straight to
    the terminal (which has native scrollback). subprocess.run inherits the
    parent stdio so interactive launchers (/setup picker, /profile add
    enrollment, /llm download progress) all "just work" without any
    needs_tty bookkeeping."""
    line = line.strip()
    if not line:
        return True

    # Echo the user's input so it scrolls into history alongside the output.
    emit(f"\033[2m> \033[0m\033[36m{line}\033[0m")

    if not line.startswith("/"):
        emit("\033[2mcommands start with / — try /help\033[0m")
        return True

    # shlex so paths with backslash-escaped or quoted spaces survive intact:
    # `/context add /Users/x/Mobile\ Documents/foo.md` → args=["add", "/Users/.../foo.md"]
    # `/context add "/Users/x/Mobile Documents/foo.md"` → same
    # Falls back to whitespace split on unclosed quotes (e.g. /ask's "what's"
    # apostrophe), which is fine because /ask rejoins with " ".
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        parts = line.split()
    cmd = parts[0]
    args = parts[1:]

    if cmd in ("/quit", "/exit", "/q", ":q"):
        return False
    if cmd == "/clear":
        # Clear the terminal screen + scrollback. \033[2J wipes the visible
        # screen, \033[3J wipes scrollback (xterm/Terminal.app/iTerm2 all
        # support this), \033[H homes the cursor.
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
        # Drop any in-session /ask thread so the next /ask starts a new
        # conversation. Mirrors the visual reset of clearing the screen.
        try:
            from llm.mlx_runtime import get_runtime
            get_runtime().clear_ask_history()
        except ImportError:
            pass
        # Re-render the welcome banner so /clear feels like a fresh start.
        # No capture_output — let the launcher write directly to the terminal
        # so tput cols sees the real width and the banner renders full-size.
        subprocess.run([str(LAUNCHER), "welcome"], check=False)
        return True
    if cmd in ("/help", "/h", "/?"):
        emit(HELP_TEXT)
        return True

    # /ask: try the in-process MLX path first (model stays resident across
    # calls, second + subsequent /ask <1s). Falls through to subprocess
    # dispatch when backend=claude or mlx_lm/snapshot isn't available.
    if cmd == "/ask":
        if not args:
            emit("\033[31musage:\033[0m /ask <question>")
            emit("  \033[2mExamples:\033[0m")
            emit("    \033[2m/ask what action items did we agree on?\033[0m")
            emit("    \033[2m/ask did anyone mention pricing?\033[0m")
            return True
        if _try_handle_ask_local(" ".join(args)):
            # Opportunistic idle-release sweep: if the user runs another
            # command (not /ask) after a long pause, the maybe_release_idle
            # in handle_command's tail will free the model. Cheap to call.
            return True
        # Fall through to launcher subprocess (claude path, or graceful
        # degradation if MLX wasn't usable for any reason).

    # Everything else: shell out synchronously. The child inherits our
    # stdin/stdout/stderr (a TTY), so:
    #   - colors render natively
    #   - interactive prompts in /setup, /profile add, /me work
    #   - long-running commands (/llm download) stream progress as they go
    #   - Ctrl+C is forwarded to the child by default, so the user can cancel
    cmd_name = cmd[1:]
    try:
        subprocess.run([str(LAUNCHER), cmd_name, *args], check=False)
    except KeyboardInterrupt:
        # User cancelled the child. Bubble up as a normal continue.
        emit("\033[2m(cancelled)\033[0m")
    except Exception as e:
        emit(f"\033[31merror:\033[0m {e}")

    # Idle-release the resident MLX model if it's been sitting unused. Only
    # matters when the user mixes /ask with non-/ask commands: a long gap
    # since the last /ask (>5min) means we drop the ~1.5–7 GB of weights
    # so other apps can use the unified memory.
    try:
        from llm.mlx_runtime import get_runtime
        get_runtime().maybe_release_idle()
    except ImportError:
        pass

    # Sync the indexer's lifecycle to the recording state. The launcher
    # owns the actual recording (via a PID file); the REPL just observes
    # via is_running(). Calling this after every command catches the
    # /start → indexer-on and /stop → indexer-off transitions without
    # needing a dedicated event hook.
    _sync_indexer_to_recording()

    return True


# ---------------------------------------------------------------------------
# Key bindings (input-only — scroll bindings are gone, terminal handles it)
# ---------------------------------------------------------------------------

kb = KeyBindings()


@kb.add("enter")
def _(event):
    """Accept the active completion if one is selected; otherwise submit."""
    buf = event.current_buffer
    cs = buf.complete_state
    if cs is not None and cs.current_completion is not None:
        buf.complete_state = None
        return
    buf.validate_and_handle()


@kb.add("tab")
def _(event):
    b = event.current_buffer
    if b.complete_state:
        b.complete_next()
    else:
        b.start_completion(select_first=True)


@kb.add("s-tab")
def _(event):
    b = event.current_buffer
    if b.complete_state:
        b.complete_previous()
    else:
        b.start_completion(select_first=False)


@kb.add("escape", eager=True)
def _(event):
    if event.current_buffer.complete_state:
        event.current_buffer.complete_state = None


# Note: prompt_toolkit's default Ctrl+C raises KeyboardInterrupt out of the
# prompt. The main loop catches it and clears the line — same UX as the
# previous binding ("clear input, don't quit"). Quitting is /quit only.


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

style = Style.from_dict({
    "bottom-toolbar": "bg:default fg:default noreverse",
    # Completion popup — subtle, matches the cyan/gray theme.
    "completion-menu": "bg:#1c1c1c",
    "completion-menu.completion": "bg:#1c1c1c #d0d0d0",
    "completion-menu.completion.current": "bg:#005f87 #ffffff bold",
    "completion-menu.meta.completion": "bg:#1c1c1c #888888",
    "completion-menu.meta.completion.current": "bg:#005f87 #d0d0d0",
})


session: PromptSession = PromptSession(
    message=prompt_text,
    completer=_SlashNestedCompleter.from_nested_dict(_NESTED_COMMANDS),
    history=InMemoryHistory(),
    key_bindings=kb,
    complete_while_typing=True,
    bottom_toolbar=bottom_toolbar,
    refresh_interval=1.0,   # tick the bottom_toolbar every 1s so the
                            # recording timer updates
    style=style,
    mouse_support=False,    # let the terminal own mouse events: trackpad
                            # selection + wheel scroll all work natively
)


def main() -> int:
    # Welcome banner — print directly to the TTY so the launcher's tput cols
    # sees the real terminal width and the banner renders full-size.
    subprocess.run([str(LAUNCHER), "welcome"], check=False)

    # One-time legacy-transcripts migration (no-op once moved).
    subprocess.run(
        [str(LAUNCHER), "_migrate"], check=False, stderr=subprocess.DEVNULL
    )

    if is_running():
        try:
            pid = int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            pid = 0
        n = line_count(MK_TRANSCRIPT)
        emit(
            f"\033[32m●\033[0m \033[1mAttaching to active recording\033[0m "
            f"\033[2m(PID {pid}, {n} lines)\033[0m"
        )
        emit(f"  \033[2mTranscript:\033[0m \033[36m{MK_TRANSCRIPT}\033[0m")

    emit("")
    emit("\033[2mType /help for commands. Tab to autocomplete.\033[0m")
    # Random tip from src/repl/tips.py — fresh nudge each launch toward
    # commands users haven't discovered yet. Sits below the static help
    # line so the basics stay visible. Silently skipped if tips.py
    # fails to import (defensive; no external deps so it shouldn't).
    try:
        from tips import random_tip  # type: ignore[import-not-found]
        emit(f"\033[2mTip:\033[0m \033[2m{random_tip()}\033[0m")
    except Exception:
        pass
    emit("")

    # patch_stdout lets prints from background threads (none in this design,
    # but kept for safety) interleave cleanly with the active prompt.
    with patch_stdout(raw=True):
        while True:
            try:
                line = session.prompt()
            except KeyboardInterrupt:
                # Ctrl+C: clear the line and re-prompt. Same behaviour as the
                # previous `c-c` keybinding — quitting requires /quit so the
                # user doesn't accidentally drop out of a running meeting.
                continue
            except EOFError:
                # Ctrl+D on empty line.
                break
            if not handle_command(line):
                break

    # On exit, if recording, ask whether to stop or detach. Plain input()
    # since we're back in the regular terminal.
    if is_running():
        print()
        print("\033[33mRecording is still active.\033[0m")
        try:
            choice = input("  (s)top recording, or (d)etach? ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            choice = "d"
        if choice in ("s", "stop"):
            subprocess.run([str(LAUNCHER), "stop"], check=False)
        else:
            print("\033[2mDetaching. Recording continues — re-run `meetink` to attach.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
