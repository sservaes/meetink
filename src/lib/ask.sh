#!/bin/zsh
# /ask: ask Claude a question with the meeting transcript + project context
# loaded as background. Uses the same `claude -p` path as titling so it bills
# against the user's Claude Pro/Max subscription, not the API.
#
# Context bundled into every prompt:
#   - The user's name (from /me) so Claude knows who "ME"/STIJN refers to.
#   - The active project (from /project use) so Claude has high-level context.
#   - Files in <transcripts>/_context/*.{txt,md,markdown} — the user's
#     curated background docs (pre-reads, prior decisions, jargon glossary).
#     PDFs aren't auto-converted yet; convert them externally for now.
#   - The current/most-recent transcript (live.txt while recording, latest
#     by mtime otherwise).
#
# Sourced by bin/meetink AFTER titling.sh (uses claude_model_active),
# identity.sh (me_name_get), projects.sh (project_active_get), and the
# config-dir resolution that adjusts $MK_TRANSCRIPTS_DIR / $MK_TRANSCRIPT.

# Resolve the transcript file we should feed Claude.
# Prefers the live recording, falls back to the most-recently-modified .txt
# in the active project's transcripts directory. Empty string = nothing.
_ask_transcript_path() {
    if [[ -L "$MK_TRANSCRIPT" ]]; then
        local actual=$(readlink "$MK_TRANSCRIPT" 2>/dev/null)
        [[ -f "$actual" ]] && { print -- "$actual"; return; }
    fi
    [[ -f "$MK_TRANSCRIPT" ]] && { print -- "$MK_TRANSCRIPT"; return; }
    # Latest by mtime in the project's transcripts dir (excluding the symlink).
    setopt local_options null_glob
    local -a files=("$MK_TRANSCRIPTS_DIR"/*.txt(N.om))
    local f
    for f in "${files[@]}"; do
        [[ -L "$f" ]] && continue
        print -- "$f"
        return
    done
}

# Concatenate any user-supplied background docs in <project>/_context/.
# Each file is preceded by a "--- filename ---" header so Claude can
# distinguish them. Skips binaries silently.
_ask_context_files() {
    local ctx_dir="$MK_TRANSCRIPTS_DIR/_context"
    [[ -d "$ctx_dir" ]] || return 0
    setopt local_options null_glob
    local f
    for f in "$ctx_dir"/*.txt(N) "$ctx_dir"/*.md(N) "$ctx_dir"/*.markdown(N); do
        print -- "--- ${f:t} ---"
        cat "$f" 2>/dev/null
        print ""
    done
}

cmd_ask() {
    local question="$*"
    if [[ -z "$question" ]]; then
        print -P "${C[red]}usage:${C[reset]} /ask <question>"
        print -P "  ${C[dim]}Examples:${C[reset]}"
        print -P "    ${C[dim]}/ask what action items did we agree on?${C[reset]}"
        print -P "    ${C[dim]}/ask did anyone mention pricing?${C[reset]}"
        print -P "    ${C[dim]}/ask summarise the last 5 minutes${C[reset]}"
        return 1
    fi
    if ! command -v claude >/dev/null 2>&1; then
        print -P "${C[red]}error:${C[reset]} ${C[bold]}claude${C[reset]} CLI not found"
        print -P "  Install Claude Code from ${C[dim]}https://claude.com/code${C[reset]}"
        return 1
    fi

    local tx_path=$(_ask_transcript_path)
    if [[ -z "$tx_path" ]]; then
        print -P "${C[red]}error:${C[reset]} no transcript to ask about"
        print -P "  ${C[dim]}Run /start first, or switch to a project that has past transcripts.${C[reset]}"
        return 1
    fi

    local me=$(me_name_get 2>/dev/null)
    local project=$(project_active_get 2>/dev/null)
    local context_text=$(_ask_context_files)
    local transcript_text=$(<"$tx_path")

    # Past-meetings digest from this project's rolling meetings.md. Claude
    # has 200K context so we send the full file rather than tier-slicing —
    # the cost of tiering on the shell side isn't worth the savings. The
    # in-process MLX path (repl.py) does tier because Qwen3.5-2B has only 8K.
    local meetings_text=""
    local meetings_md="$MK_TRANSCRIPTS_DIR/meetings.md"
    if [[ -f "$meetings_md" ]]; then
        meetings_text=$(<"$meetings_md")
    fi

    # Build the prompt incrementally so we only include sections we have.
    local prompt="You are an assistant helping a user reason about a meeting transcript. Answer their question concisely and directly. If the transcript doesn't contain enough information to answer, say so plainly rather than speculating. When past meetings from the same project are provided, you may reference them — they appear newest first."
    [[ -n "$me" ]] && prompt+="

The user's name is ${me} (their lines appear as ${(U)me}: in the transcript)."
    [[ -n "$project" ]] && prompt+="

This meeting is part of project: ${project}."
    if [[ -n "$context_text" ]]; then
        prompt+="

Background documents (the user has curated these as relevant context):
${context_text}"
    fi
    if [[ -n "$meetings_text" ]]; then
        prompt+="

Past meetings in this project (newest first):
${meetings_text}"
    fi
    prompt+="

Current meeting transcript (file: ${tx_path:t}):
${transcript_text}

Question from the user: ${question}"

    # Dispatch by the same backend setting that titling uses, so /llm backend
    # local makes /ask local too. Honours MEETINK_TITLE_BACKEND env override.
    local backend="claude"
    if typeset -f title_backend_active >/dev/null; then
        backend=$(title_backend_active)
    fi

    if [[ "$backend" == "local" ]]; then
        _ask_local "$prompt"
    else
        _ask_claude "$prompt"
    fi
}

_ask_claude() {
    local prompt="$1"
    local model
    if typeset -f claude_model_active >/dev/null; then
        model=$(claude_model_active)
    else
        model="claude-sonnet-4-6"
    fi
    print -P "${C[dim]}Asking ${model}...${C[reset]}"
    print -P ""
    # Same slimming flags as titling: no built-in tools, no MCP — keeps the
    # call fast (~5-10s on Sonnet) and avoids "Prompt is too long" on Haiku
    # when the user has many plugins loaded.
    claude -p \
        --model "$model" \
        --tools "" \
        --strict-mcp-config \
        "$prompt" </dev/null
    print -P ""
}

_ask_local() {
    local user_prompt="$1"
    if [[ ! -x "$MK_PY_VENV/bin/python" ]]; then
        print -P "${C[red]}error:${C[reset]} REPL Python venv missing"
        print -P "  Run ${C[bright_cyan]}meetink setup${C[reset]} to install it."
        return 1
    fi
    if ! "$MK_PY_VENV/bin/python" -c "import mlx_lm" 2>/dev/null; then
        print -P "${C[red]}error:${C[reset]} ${C[bold]}mlx-lm${C[reset]} not installed in REPL venv"
        print -P "  Run ${C[bright_cyan]}/llm install${C[reset]} or ${C[bright_cyan]}meetink setup${C[reset]}, or switch to claude with ${C[bright_cyan]}/llm backend claude${C[reset]}"
        return 1
    fi
    local model_path="$MK_LLM_MODEL"
    if [[ ! -f "$model_path/config.json" ]]; then
        local active=$(local_llm_active_get 2>/dev/null)
        print -P "${C[red]}error:${C[reset]} active local model ${C[bold]}${active}${C[reset]} not downloaded"
        print -P "  Run ${C[bright_cyan]}/llm download ${active}${C[reset]} first."
        return 1
    fi
    # System message + user prompt — mlx_helper.py wraps both in the model's
    # native chat template (Qwen3.5 in our case). /no_think suppresses Qwen's
    # reasoning preamble, but it can still emit an empty <think></think> stub
    # that we filter out below.
    local system_prompt="You answer questions about a meeting transcript. Be concise and grounded only in the transcript and provided context. If the transcript doesn't contain enough information, say so plainly rather than guessing."
    local active=$(local_llm_active_get 2>/dev/null)
    print -P "${C[dim]}Asking ${active}...${C[reset]}"
    print -P ""
    # Why mlx-lm vs llama.cpp: 30-60% faster on Apple Silicon (native Metal +
    # ANE integration). max-tokens 512 leaves room for a multi-paragraph
    # answer; temp 0.4 a touch higher than titles for natural prose while
    # still staying grounded.
    # Post-filter: strip empty <think></think> stubs the model may emit even
    # with /no_think; collapse leading blank lines for clean output.
    # enable_thinking=False is set in mlx_helper.py at the apply_chat_template
    # call, so we don't need a /no_think marker on the user prompt anymore.
    "$MK_PY_VENV/bin/python" "$MK_ROOT/src/llm/mlx_helper.py" \
        --model "$model_path" \
        --system "$system_prompt" \
        --prompt "$user_prompt" \
        --max-tokens 512 \
        --temp 0.4 \
        2>/dev/null \
      | awk '
            /./ { collected[++n] = $0; last = n }
            !/./ { if (n > 0) collected[++n] = $0 }
            END {
                start = 1
                while (start <= n && collected[start] == "") start++
                for (i = start; i <= last; i++) print collected[i]
            }
        '
    print -P ""
}
