"""LLM-based project routing for /watch.

Given a calendar event and the list of existing projects, ask the
active backend whether the event clearly belongs to one of them.
Returns the project name (must match a known project verbatim) or
None when the LLM is unsure or no projects match.

We deliberately do NOT auto-create new projects from event titles —
the user explicitly asked for that to stay manual. A 1:1 prompt + a
strict 'must echo a name from the list' contract keeps the LLM from
hallucinating new project names.

Caching: a per-process dict keyed by event id, so a recurring meeting
doesn't pay the LLM tax every time it fires its 1-min notification.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

# Match repl.py's project lookup
MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))
MK_TRANSCRIPTS_DIR = Path(os.environ.get(
    "MEETINK_TRANSCRIPTS_DIR",
    os.path.expanduser("~/Documents/meetink"),
))


_cache: dict[str, str | None] = {}
_cache_lock = threading.Lock()


_SYSTEM = (
    "You match a meeting title to one of the user's existing projects.\n"
    "\n"
    "Output exactly ONE LINE — either the project name verbatim from "
    "the provided list, or the literal token NONE.\n"
    "\n"
    "Rules:\n"
    "- Output the project name only if the title obviously refers to "
    "that project (the project's name or one of its distinctive words "
    "appears in the title).\n"
    "- If the title is generic ('1:1', 'standup', 'chat with X') and "
    "doesn't reference any project, output NONE.\n"
    "- When in doubt, output NONE. False matches are worse than misses.\n"
    "- Do not invent new project names. Do not explain. No punctuation "
    "beyond what's in the project name itself."
)


def _post_llm_sanity_check(project: str, title: str) -> bool:
    """Defence-in-depth: even if the LLM says 'project X', require that
    at least one ≥3-char token from project X actually appears in the
    title. Catches small models picking arbitrarily from the list."""
    title_lower = title.lower()
    tokens = [t for t in re.split(r"[-_\s]+", project.lower()) if len(t) >= 3]
    if not tokens:
        return True   # short project names — trust the LLM
    return any(t in title_lower for t in tokens)


def _list_projects() -> list[str]:
    """Names of subdirectories under the transcripts base. Skips
    hidden / underscore-prefixed dirs (these are conventions for
    sidecar storage like _context, .idx)."""
    if not MK_TRANSCRIPTS_DIR.is_dir():
        return []
    out: list[str] = []
    try:
        for p in sorted(MK_TRANSCRIPTS_DIR.iterdir()):
            if not p.is_dir():
                continue
            if p.name.startswith(".") or p.name.startswith("_"):
                continue
            out.append(p.name)
    except OSError:
        pass
    return out


def _normalize_response(s: str, allowed: list[str]) -> str | None:
    """Trim, lowercase, accept only when the LLM's output exactly
    matches an allowed project name (case-insensitive). Anything else
    is treated as NONE — including 'NONE' itself."""
    if not s:
        return None
    line = s.strip().splitlines()[0].strip()
    # Strip wrapping quotes / markdown the model sometimes adds despite
    # the system instruction.
    line = line.strip("'\"`*_")
    if not line:
        return None
    upper = line.upper()
    if upper.startswith("NONE"):
        return None
    lower = line.lower()
    for p in allowed:
        if p.lower() == lower:
            return p
    return None


def _read_config(key: str, default: str = "") -> str:
    cfg = MK_HOME / "config"
    if not cfg.is_file():
        return default
    try:
        for line in cfg.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def _backend() -> str:
    env = os.environ.get("MEETINK_TITLE_BACKEND", "")
    if env in ("local", "claude"):
        return env
    val = _read_config("title_backend", "local")
    return val if val in ("local", "claude") else "local"


def _local_model_path() -> Path | None:
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


def _claude_model() -> str:
    env = os.environ.get("MEETINK_CLAUDE_MODEL", "")
    if env:
        return env
    return _read_config("claude_model", "claude-sonnet-4-6")


def _generate(system: str, user: str, max_tokens: int = 30, temp: float = 0.0) -> str:
    """Single shot through whichever backend is active. Output is the
    first line only; we keep generation tight to avoid drift."""
    backend = _backend()
    if backend == "local":
        model_path = _local_model_path()
        if model_path is None:
            return ""
        try:
            from llm.mlx_runtime import get_runtime
            return get_runtime().generate(
                model_path=model_path,
                prompt=user, system=system,
                max_tokens=max_tokens, temp=temp,
            )
        except Exception:
            return ""
    # claude path
    prompt = f"{system}\n\n{user}" if system else user
    try:
        proc = subprocess.run(
            ["claude", "-p",
             "--model", _claude_model(),
             "--tools", "",
             "--strict-mcp-config",
             prompt],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout
    except Exception:
        return ""


def resolve_project(event) -> str | None:
    """Given a WatchedEvent (or any object with .id, .title, .notes,
    .attendees, .calendar_title), return an existing project name
    or None.

    Cached by event id within the process; a recurring meeting only
    pays the LLM cost on first fire.
    """
    eid = getattr(event, "id", "") or ""
    with _cache_lock:
        if eid in _cache:
            return _cache[eid]

    projects = _list_projects()
    if not projects:
        with _cache_lock:
            _cache[eid] = None
        return None

    # Quick exact-match shortcut: case-insensitive substring of the
    # event title against a project name. Saves an LLM call when the
    # match is obvious (e.g., event "ACME — weekly sync" → project
    # "acme-corp").
    title = (getattr(event, "title", "") or "").lower()
    for p in projects:
        # Tokenise to avoid substring false positives like 'ai' matching
        # 'aim'. Words from the project name (split on -/_/ ) all need
        # to appear in the title.
        tokens = [t for t in re.split(r"[-_\s]+", p.lower()) if len(t) >= 3]
        if tokens and all(t in title for t in tokens):
            with _cache_lock:
                _cache[eid] = p
            return p

    # LLM round-trip for less-obvious matches.
    user_prompt = (
        "Existing projects (one per line):\n"
        + "\n".join(f"- {p}" for p in projects)
        + "\n\n"
        + "Meeting title: "
        + (getattr(event, "title", "") or "(untitled)")
    )
    cal = getattr(event, "calendar_title", "") or ""
    if cal:
        user_prompt += f"\nCalendar: {cal}"
    out = _generate(_SYSTEM, user_prompt, max_tokens=30, temp=0.0)
    resolved = _normalize_response(out, projects)
    if resolved is not None:
        if not _post_llm_sanity_check(resolved, getattr(event, "title", "") or ""):
            # LLM picked a project that doesn't clearly relate to the
            # title. Reject — phase-2 wins are about not annoying the
            # user with wrong-project routing more than catching every
            # match.
            resolved = None
    with _cache_lock:
        _cache[eid] = resolved
    return resolved
