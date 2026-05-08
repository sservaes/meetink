"""Decision + action-item extractor for the meetink index.

After each ~5-min segment, two focused LLM calls run on the segment's
raw transcript text:
  decisions.md  — concrete decisions made ("we'll ship Q3", "Alice owns auth")
  actions.md    — assigned action items ("Bob to draft proposal by Fri")

Both files are append-only with a fixed `# Decisions` / `# Action items`
heading. We dedupe via exact-line matching — if a new extraction's bullet
already appears verbatim in the file, we skip it. Catches the model
repeating itself across overlapping segments without expensive semantic
comparison. Near-duplicates can leak through; that's acceptable for v1.

The extraction prompt is intentionally narrow: each segment is short, the
model has bounded work, and false positives are filtered by asking for
"concrete" items only and accepting `NONE` as a valid output.
"""

from __future__ import annotations

from pathlib import Path


# Strict on purpose: we'd rather miss soft commitments than drown the
# rollup files in speculation.
_DECISION_SYSTEM = (
    "You extract concrete decisions from a meeting transcript segment. "
    "A decision is a statement of resolution: a path chosen, a constraint "
    "agreed, an outcome accepted. Discussion, options, and questions are "
    "NOT decisions. Output one decision per line, prefixed with '- '. "
    "If the segment contains no decisions, output exactly the single line "
    "NONE. Be concise: each decision in 10-15 words max."
)

_ACTION_SYSTEM = (
    "You extract concrete action items from a meeting transcript segment. "
    "An action item is an assigned task: someone owns it and there's a "
    "deliverable. Suggestions, ideas, and general next-steps are NOT "
    "action items unless explicitly assigned. Output one action per line, "
    "prefixed with '- ', with the owner's name in brackets. Format: "
    "'- [Owner] action by deadline (if mentioned)'. If the segment "
    "contains no action items, output exactly the single line NONE."
)


def _parse(out: str) -> list[str]:
    """Parse the model output into a list of `- ` bullet lines. Returns
    [] when the output is `NONE` or has no recognisable bullets."""
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    if not lines or lines[0].upper().startswith("NONE"):
        return []
    return [l for l in lines if l.startswith("- ")]


def _existing_items(path: Path) -> set[str]:
    """Set of bullet lines already in the file (for dedup)."""
    if not path.is_file():
        return set()
    try:
        return {l.strip() for l in path.read_text().splitlines()
                if l.startswith("- ")}
    except OSError:
        return set()


def update_rollups(index_dir: Path, segment_text: str, backend) -> None:
    """Run decision + action extraction on segment_text and append new
    items to decisions.md / actions.md (deduplicated). Failures are
    silent — rollups are best-effort, the segment summary is the
    primary artefact for the segment."""
    decisions_path = index_dir / "decisions.md"
    actions_path = index_dir / "actions.md"

    for system_prompt, target_path, heading in (
        (_DECISION_SYSTEM, decisions_path, "# Decisions"),
        (_ACTION_SYSTEM, actions_path, "# Action items"),
    ):
        try:
            out = backend.generate(
                system_prompt, segment_text, max_tokens=200, temp=0.2,
            )
        except Exception:
            continue
        existing = _existing_items(target_path)
        new = [l for l in _parse(out) if l not in existing]
        if not new:
            continue
        try:
            with target_path.open("a", encoding="utf-8") as f:
                if not target_path.stat().st_size or not existing:
                    f.write(f"{heading}\n\n")
                for line in new:
                    f.write(line + "\n")
        except OSError:
            pass


def read_rollups(index_dir: Path) -> tuple[str, str]:
    """Return (decisions_text, actions_text). Empty strings when the
    file is missing. /ask drops these into the prompt as part of the
    assembled context."""
    d = index_dir / "decisions.md"
    a = index_dir / "actions.md"
    decisions = d.read_text() if d.is_file() else ""
    actions = a.read_text() if a.is_file() else ""
    return decisions, actions
