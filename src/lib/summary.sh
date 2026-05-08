#!/bin/zsh
# Per-meeting summary generation + project-level rolling meetings.md.
#
# Two artefacts per finished meeting:
#   1. <meeting>.summary.md   — sits next to the .txt transcript. Contains
#                                YAML frontmatter (generated_by + timestamp)
#                                and four structured sections: Topics,
#                                Decisions, Action items, Open questions.
#                                Generated once on /stop; never auto-rewritten.
#   2. <project>/meetings.md  — rolling project-level digest. Newest first,
#                                truncated to MEETINK_MEETINGS_LOG_KEEP entries.
#                                Rebuilt from .summary.md files on each /stop
#                                so it's always in sync without bookkeeping.
#
# /ask consumes meetings.md with recency-tiered truncation: most recent
# entries get full content, mid-range get condensed, very old ones get only
# their heading. The slicing happens at READ time (in repl.py / ask.sh) —
# this file just produces the canonical full-content store.
#
# Sourced by bin/meetink AFTER titling.sh + ask.sh. Depends on:
#   - $MK_HOME, $MK_ROOT, $MK_PY_VENV (set in bin/meetink)
#   - title_backend_active, claude_model_active, llm_path,
#     local_llm_active_get  (defined in titling.sh)
#   - C[] color table from ui.sh

# Number of summary entries to keep in the rolling meetings.md. Older
# .summary.md files are NOT deleted — they stay next to their transcript so
# the user can browse them — but they age out of the project digest after
# this many newer meetings exist.
MEETINK_MEETINGS_LOG_KEEP="${MEETINK_MEETINGS_LOG_KEEP:-50}"


# Strip the meeting header (# Meeting Transcript / Started: / Ended: / ---)
# and cap at ~150 lines to keep the prompt bounded — long meetings are
# diminishing-returns for summary quality past a point and we'd rather
# spend the model's context on accuracy over completeness.
_summary_transcript_body() {
    grep -vE '^(# |Started: |Ended: |---|---$)' "$1" 2>/dev/null | head -150
}


# Summary prompt — same wording for both backends so summaries stay
# stylistically consistent if the user mixes backends across meetings.
_summary_system_prompt() {
    cat <<'EOF'
You summarize meeting transcripts into a structured digest. Output ONLY the four sections below in this exact order, using markdown bullet lists. Do not add commentary, framing, or a heading above the sections.

## Topics
- 2 to 5 bullets covering what was actually discussed. Concrete subjects, not "we had a meeting about X".

## Decisions
- Concrete decisions reached. If none, write: - (none)

## Action items
- Format: "[OWNER] specific action". Use OWNER names exactly as they appear in the transcript (e.g. STIJN, HASSAN, ALICE). If the owner is unclear, use [unclear]. If none, write: - (none)

## Open questions
- Unresolved questions raised. If none, write: - (none)
EOF
}


# Generate the summary body via the local MLX path. Returns the raw model
# output on stdout. Uses higher max_tokens than titling because we want
# four bullet sections, not 5 words.
_summary_generate_local() {
    local transcript_text="$1"
    local model_path=$(llm_path "$(local_llm_active_get)")
    if [[ ! -f "$model_path/config.json" ]]; then
        return 1
    fi
    "$MK_PY_VENV/bin/python" "$MK_ROOT/src/llm/mlx_helper.py" \
        --model "$model_path" \
        --system "$(_summary_system_prompt)" \
        --prompt "$transcript_text" \
        --max-tokens 600 \
        --temp 0.3 \
        2>/dev/null
}


# Same via Claude. Same flags as titling/ask: no tools, no MCP — fast and
# costs less of the user's subscription quota.
_summary_generate_claude() {
    local transcript_text="$1"
    local model=$(claude_model_active)
    local prompt="$(_summary_system_prompt)

Transcript:
${transcript_text}"
    claude -p \
        --model "$model" \
        --tools "" \
        --strict-mcp-config \
        "$prompt" </dev/null 2>/dev/null
}


# Public entry point: generate + save the summary file for a transcript.
# Args: $1 = absolute path to the renamed .txt transcript.
# Side effect: writes <transcript>.summary.md (replaces .txt with .summary.md
# in the basename). Returns 0 on success, non-zero on backend failure.
summary_save() {
    local tx_path="$1"
    [[ -f "$tx_path" ]] || return 1

    local body=$(_summary_transcript_body "$tx_path")
    [[ -z "$body" ]] && return 1   # empty/header-only transcript — nothing to summarise

    local backend=$(title_backend_active)
    local model_label
    local raw

    case "$backend" in
        claude)
            if ! command -v claude >/dev/null 2>&1; then
                return 1
            fi
            model_label=$(claude_model_active)
            raw=$(_summary_generate_claude "$body") || return 1
            ;;
        *)
            model_label=$(local_llm_active_get)
            raw=$(_summary_generate_local "$body") || return 1
            ;;
    esac

    [[ -z "$raw" ]] && return 1

    # Strip any <think>…</think> stub the model might emit (defensive — the
    # enable_thinking=False template flag should prevent this, but ASCII-only
    # post-filter is cheap insurance).
    raw=$(print -- "$raw" | awk '
        /<think>/  { in_think=1; next }
        /<\/think>/{ in_think=0; next }
        !in_think  { print }
    ')

    # The summary file lives next to the transcript: foo.txt → foo.summary.md
    local summary_path="${tx_path%.txt}.summary.md"
    local now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local meeting_basename="${tx_path:t}"

    {
        print -- "---"
        print -- "generated_by: $model_label"
        print -- "generated_at: $now"
        print -- "meeting: $meeting_basename"
        print -- "---"
        print -- ""
        print -- "$raw"
    } > "$summary_path"

    print -P "${C[green]}✓${C[reset]} Summary written: ${C[dim]}${summary_path:t}${C[reset]}" >&2
}


# Rebuild meetings.md from all .summary.md files in the project's
# transcripts dir. Newest first (by transcript filename's leading
# YYYY-MM-DD_HH-MM date, which sorts lexicographically), capped at
# MEETINK_MEETINGS_LOG_KEEP entries. The remaining .summary.md files stay
# on disk; they're just not in the digest.
#
# Args: $1 = absolute path to the project's transcripts dir.
meetings_log_rebuild() {
    local proj_dir="$1"
    [[ -d "$proj_dir" ]] || return 1

    local out="$proj_dir/meetings.md"
    setopt local_options null_glob

    # Sort summaries newest-first by lexical filename order. The transcript
    # filenames start with YYYY-MM-DD_HH-MM, which sorts correctly as text
    # (no need to stat-mtime each one). `(On)` reverses the default order.
    local -a all=("$proj_dir"/*.summary.md(.On))
    if (( ${#all[@]} == 0 )); then
        # No summaries exist yet — remove a stale meetings.md if any.
        rm -f "$out"
        return 0
    fi

    # Truncate to KEEP entries.
    local -a kept=("${(@)all[1,$MEETINK_MEETINGS_LOG_KEEP]}")

    {
        print -- "# Meeting log — ${proj_dir:t}"
        print -- ""
        local f base body header
        for f in "${kept[@]}"; do
            base="${f:t:r}"   # strip dir + .md
            base="${base%.summary}"   # strip .summary
            # Pull `generated_by` out of the frontmatter for the per-meeting
            # attribution line. We cap the frontmatter scan at 10 lines.
            local gen_by=$(awk '
                /^---/{c++; if (c>=2) exit; next}
                c==1 && /^generated_by:/ {sub(/^generated_by:[[:space:]]*/, ""); print; exit}
            ' "$f")
            # Skip the frontmatter when including the body content.
            body=$(awk '
                BEGIN{c=0; printing=0}
                /^---/{c++; if (c>=2) {printing=1; next}; next}
                printing{print}
            ' "$f")
            print -- "## ${base}"
            [[ -n "$gen_by" ]] && print -- "*generated by ${gen_by}*"
            print -- ""
            print -- "$body"
            print -- ""
        done
    } > "$out"
}
