"""Tip rotation for the welcome line.

Single-line nudges that surface lesser-known commands and workflows.
One is picked at random when the REPL launches. Constraints:
  - one line, ≤ 90 chars after the "Tip:" prefix is added at render
  - reference an actual command or behaviour (no fortune-cookie filler)
  - assume the user is already past `/start` / `/stop` / `/help`

Curate by adding to TIPS. Order doesn't matter — random.choice picks one.
"""

from __future__ import annotations

import random


TIPS: tuple[str, ...] = (
    # --- Identity, projects, context ---
    "Set your name with /me Stijn so transcripts label you STIJN: instead of ME:.",
    "Group meetings with /project use acme — recordings, summaries, /ask all scope to it.",
    "Drop a PDF into /context add ~/Downloads/spec.pdf and /ask can read it.",
    "/context list shows attached docs, token totals, and how full each backend's window is.",
    "/context show <name> dumps the converted markdown if you want to re-read it.",
    "/context add accepts spaces in paths — escape with \\ or wrap in quotes.",
    "Drag a file from Finder onto the terminal — paths get escaped automatically.",

    # --- /ask ---
    "/ask works without a transcript — attach context docs first, then ask away.",
    "Follow-up /asks see the prior 5 Q&A pairs as a thread. /clear resets the thread.",
    "On long meetings, /ask retrieves only the relevant chunks via the RAG index.",
    "/index install (~50 MB) flips on RAG-backed /ask — fast even on 4-hour meetings.",
    "/ask streams the answer token-by-token; the prompt stays usable while it does.",
    "Past transcripts get indexed lazily on first /ask — one-time cost, then cached.",

    # --- LLM backends ---
    "/llm backend claude for premium /ask quality; /llm backend local to stay offline.",
    "Index on backend=local, switch to claude for the final /ask — cheap + premium synthesis.",
    "/llm use qwen3.5-9b on a 32 GB machine for the highest local /ask quality.",
    "/llm model claude-haiku-4-5 makes titling cheap; sonnet is the default.",
    "/llm download <name> snapshots a new local model into ~/.meetink/models/mlx/.",

    # --- Whisper / capture ---
    "Switch the recognizer with /model use medium.en for tougher accents.",
    "/prompt opens ~/.meetink/prompts/default.txt — feed whisper your jargon to bias decoding.",
    "small.en-tdrz emits THEM-A / THEM-B speaker turn markers when /diarize is off.",
    "Long meetings? Pre-edit the prompt with the names of the people on the call.",

    # --- Speaker ID ---
    "/diarize install + /profile add Alice — three 5-second samples, then names appear live.",
    "/profile assign A Alice converts a live cluster into a profile AND rewrites past lines.",
    "/profile merge A B folds one cluster into another when noise split a voice.",
    "/profile train <name> sharpens an existing voice profile with another sample.",
    "/profile clusters lists the live unknown voices the current session has accumulated.",

    # --- Recording ---
    "/tail opens or raises a separate Terminal window tailing the live transcript.",
    "/start auto-opens the tail window; /stop closes it.",
    "/transcripts (or /ls) shows past sessions in the active project's directory.",
    "Recording continues if you /quit — only /stop ends capture and triggers titling.",
    "/status reports recording state and current line count without opening tail.",

    # --- Index / RAG ---
    "/index status shows per-transcript chunk + segment counts for the active project.",
    "/index rm wipes sidecar dirs in this project — /ask will rebuild them lazily.",
    "/index rm deps frees ~50 MB by uninstalling fastembed if you don't use RAG.",
    "Each /ask reports its strategy: indexed N L means RAG fired, vs full-transcript fallback.",

    # --- Footer chips ---
    "The 📚 chip on the bottom row reflects indexer state in real time during recording.",
    "The context bar shows how full the next /ask prompt would be against the active model.",
    "🧠 chip turns red when free RAM dips below 4 GB — switch to a smaller LLM if needed.",
    "🎙 chip shows the active whisper model; /model use to switch.",

    # --- Power user / discovery ---
    "Most slash commands have aliases: /q for /quit, /ls for /transcripts, /h for /help.",
    "Cmd+F searches the scrollback natively — meetink runs inline, terminal owns the buffer.",
    "Native text selection works: click-and-drag, Cmd+C, paste anywhere.",
    "Wheel scroll the terminal as usual — meetink's footer scrolls with content, not pinned.",
    "Recording without an active /project lands in the top-level default project.",
    "/context add a meeting transcript itself if you want past meetings as fixed context.",

    # --- Privacy / config ---
    "Audio never leaves the machine on backend=local — /llm backend to confirm.",
    "Voice profiles are embedding centroids in ~/.meetink/profiles/ — not recoverable audio.",
    "Set MEETINK_TRANSCRIPTS_DIR to redirect transcripts away from ~/Documents/meetink.",
    "~/.meetink/config is human-editable — keys: me_name, active_project, title_backend, etc.",
)


def random_tip() -> str:
    """Return a single tip line, ready to embed in the welcome message.
    Caller wraps with their own colour codes / 'Tip:' prefix."""
    return random.choice(TIPS)
