#!/bin/zsh
# AI-generated titles for finished transcripts.
#
# After /stop, we summarise the transcript into a 3-5 word slug and rename
# the session file from `2026-05-07_14-32-08.txt` to
# `2026-05-07_14-32_<slug>.txt`.
#
# Two backends, selectable via `/llm backend <name>` (persists in $MK_CONFIG_FILE)
# or the MEETINK_TITLE_BACKEND env var:
#   - local  (default): on-device Qwen3.5 via Apple's MLX framework
#                       (mlx-community/Qwen3.5-*-4bit). Native Metal + ANE
#                       acceleration, 30-60% faster than llama.cpp on Apple
#                       Silicon. Invoked via src/llm/mlx_helper.py.
#   - claude:           Claude via `claude -p` (the Claude Code CLI in headless
#                       mode). Bills against the user's Pro/Max subscription —
#                       no API key needed, same auth as `claude` interactive.
#                       Network required, ~15-20s, slightly better titles.
#
# Optional: gracefully no-ops if the active backend's deps aren't present.
# /setup installs the local backend by default.
#
# Sourced by bin/meetink AFTER models.sh (which defines $MK_CONFIG_FILE).
# Depends on:
#   - $MK_HOME (set in bin/meetink)
#   - $MK_CONFIG_FILE (set in models.sh)
#   - $MK_TRANSCRIPTS_DIR, $MK_TRANSCRIPT (set in bin/meetink)
#   - $MK_PY_VENV (set in bin/meetink) — Python venv with mlx-lm installed
#   - $MK_ROOT (set in bin/meetink) — for src/llm/mlx_helper.py path
#   - C[] color table from src/lib/ui.sh

# Local LLM registry. Each entry: "humanSize|description|hf_repo|runtimeMB".
# `runtimeMB` is a rough estimate of resident memory at inference time —
# model weights + KV cache for a small context. Used by llm_list to colour
# entries based on how comfortably they fit on the host machine.
# All entries point at mlx-community/* — pre-converted Qwen3.5 4-bit MLX
# snapshots. Stay within the Qwen3.5 family so the chat template applied
# in mlx_helper.py is consistent (Qwen format with /no_think marker).
typeset -gA MK_LLM_REGISTRY=(
    [qwen3.5-0.8b]="508M|Tiny — titles only. Default. Too small for /ask.|mlx-community/Qwen3.5-0.8B-4bit|900"
    [qwen3.5-2b]="1.2G|Small — better titles. Basic /ask on short meetings.|mlx-community/Qwen3.5-2B-4bit|1900"
    [qwen3.5-4b]="2.6G|Medium — viable for /ask on hour-long meetings.|mlx-community/Qwen3.5-4B-4bit|4000"
    [qwen3.5-9b]="5.4G|Large — best local /ask quality.|mlx-community/Qwen3.5-9B-4bit|7500"
)
typeset -ga MK_LLM_ORDER=(qwen3.5-0.8b qwen3.5-2b qwen3.5-4b qwen3.5-9b)
MK_LLM_DEFAULT="qwen3.5-2b"

# Extract the HF repo id from a registry entry. With MLX we use the same
# repo for both download and the local-snapshot directory name (we just
# replace `/` with `_` for safety on disk).
_llm_repo() {
    local entry="${MK_LLM_REGISTRY[$1]}"
    [[ -z "$entry" ]] && return 1
    local without_rt="${entry%|*}"
    print -n -- "${without_rt##*|}"
}

# On-disk snapshot directory for a registered model. mlx_lm.load() accepts
# either an HF id (auto-downloads to ~/.cache) or a local path; we always
# pass the local path so users can audit ~/.meetink/models/ and `/llm rm`
# is just a directory delete.
llm_path() {
    local repo=$(_llm_repo "$1") || { print -n -- ""; return 1; }
    # mlx-community/Qwen3.5-2B-4bit → Qwen3.5-2B-4bit
    print -n -- "$MK_HOME/models/mlx/${repo##*/}"
}

# HF repo id for download. Empty if unknown.
llm_url() {
    _llm_repo "$1"
}

# Considered "present" when the snapshot directory contains both a
# config.json (mlx_lm.load entry point) and a chat_template.jinja (HF moved
# chat templates into a sibling .jinja file in 2025; without it,
# tokenizer.apply_chat_template raises). A partial / pre-fix download is
# treated as not-present, prompting a re-fetch that picks up the .jinja.
llm_present() {
    local p=$(llm_path "$1")
    [[ -n "$p" && -f "$p/config.json" && -f "$p/chat_template.jinja" ]]
}

# Read/persist the active local model (config key: local_llm_model).
local_llm_active_get() {
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^local_llm_model=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        [[ -n "$v" && -n "${MK_LLM_REGISTRY[$v]}" ]] && { print -n -- "$v"; return; }
    fi
    print -n -- "$MK_LLM_DEFAULT"
}

local_llm_active_set() {
    local name="$1"
    [[ -n "${MK_LLM_REGISTRY[$name]}" ]] || return 1
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^local_llm_model=' "$MK_CONFIG_FILE"; then
        sed -i '' "s|^local_llm_model=.*|local_llm_model=$name|" "$MK_CONFIG_FILE"
    else
        echo "local_llm_model=$name" >> "$MK_CONFIG_FILE"
    fi
}

# Resolve MK_LLM_MODEL — the local snapshot directory passed to mlx_lm.load.
# MEETINK_LLM_MODEL env-var override beats the registry (lets power users
# point at any locally-available MLX-format model).
MK_LLM_MODEL="${MEETINK_LLM_MODEL:-$(llm_path "$(local_llm_active_get)")}"
MK_LLM_URL="$(llm_url "$(local_llm_active_get)")"

# Which Claude model to use when backend=claude. Default Sonnet for best
# title quality; users can switch to Haiku (faster/cheaper) or Opus (deepest)
# via `/llm model <name>` or the MEETINK_CLAUDE_MODEL env var.
MK_CLAUDE_MODEL_DEFAULT="claude-sonnet-4-6"

claude_model_active() {
    if [[ -n "$MEETINK_CLAUDE_MODEL" ]]; then
        print -n -- "$MEETINK_CLAUDE_MODEL"
        return
    fi
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^claude_model=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        if [[ -n "$v" ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- "$MK_CLAUDE_MODEL_DEFAULT"
}

claude_model_set() {
    local name="$1"
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^claude_model=' "$MK_CONFIG_FILE"; then
        sed -i '' "s|^claude_model=.*|claude_model=$name|" "$MK_CONFIG_FILE"
    else
        echo "claude_model=$name" >> "$MK_CONFIG_FILE"
    fi
}

# Read the active backend. MEETINK_TITLE_BACKEND wins, then $MK_CONFIG_FILE,
# then default `local`.
title_backend_active() {
    if [[ -n "$MEETINK_TITLE_BACKEND" ]]; then
        print -n -- "$MEETINK_TITLE_BACKEND"
        return
    fi
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^title_backend=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        if [[ "$v" == "local" || "$v" == "claude" ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- "local"
}

# Persist the active backend.
title_backend_set() {
    local name="$1"
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^title_backend=' "$MK_CONFIG_FILE"; then
        sed -i '' "s|^title_backend=.*|title_backend=$name|" "$MK_CONFIG_FILE"
    else
        echo "title_backend=$name" >> "$MK_CONFIG_FILE"
    fi
}

# Per-backend availability checks.
#
# Local: we use `llama-completion` specifically (not `llama-cli`): recent
# llama.cpp builds reject `--no-conversation` in `llama-cli` ("not supported,
# please use llama-completion instead") and force interactive mode, which
# polluted titles with the rejection message.
_local_available() {
    # Local backend means: REPL venv has mlx-lm AND the active model is
    # snapshotted on disk. We don't try to import mlx_lm here (subshell
    # cost on every status check); presence of the venv's mlx_lm directory
    # is enough.
    [[ -d "$MK_PY_VENV/lib" ]] || return 1
    [[ -n "$(/bin/ls -d "$MK_PY_VENV"/lib/python*/site-packages/mlx_lm 2>/dev/null | head -1)" ]] || return 1
    llm_present "$(local_llm_active_get)"
}

_claude_available() {
    command -v claude >/dev/null 2>&1
}

# True when the active backend's deps are present.
llm_available() {
    case "$(title_backend_active)" in
        claude) _claude_available ;;
        *)      _local_available  ;;
    esac
}

# Ensure mlx-lm is installed in the REPL venv. We share the REPL's venv
# because it already has prompt_toolkit; bundling mlx-lm in the same
# environment avoids a second venv to manage. Returns 0 if available
# (or successfully installed), 1 on failure.
#
# Uses `uv pip install --python <venv>/bin/python` rather than the venv's
# own pip — `uv venv` creates minimal venvs without pip by default, so the
# venv-local pip may not exist. uv handles the targeting itself.
_ensure_mlx_lm() {
    if [[ ! -x "$MK_PY_VENV/bin/python" ]]; then
        print -P "${C[red]}error:${C[reset]} REPL Python venv missing — run ${C[bright_cyan]}meetink setup${C[reset]} first"
        return 1
    fi
    if "$MK_PY_VENV/bin/python" -c "import mlx_lm" 2>/dev/null; then
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        print -P "${C[red]}error:${C[reset]} uv not found — run ${C[bright_cyan]}meetink setup${C[reset]} first"
        return 1
    fi
    # mlx-lm pulls in mlx, transformers, sentencepiece, protobuf — combined
    # ~200 MB. The progress is on uv's stderr; we don't redirect it so the
    # user sees the wheel downloads.
    print -P "${C[bright_yellow]}▸${C[reset]} Installing mlx-lm into REPL venv ${C[dim]}(~200 MB with deps)...${C[reset]}"
    if ! uv pip install --python "$MK_PY_VENV/bin/python" --quiet mlx-lm; then
        print -P "${C[red]}error:${C[reset]} mlx-lm install failed"
        return 1
    fi
    print -P "${C[green]}✓${C[reset]} mlx-lm installed"
}

# Download a model snapshot from HuggingFace into ~/.meetink/models/mlx/.
# Uses our Python helper which calls huggingface_hub.snapshot_download,
# giving an accurate progress bar across the multi-shard .safetensors files.
llm_download() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /llm download <name>"
        print -P "  Available: ${(j:, :)MK_LLM_ORDER}"
        return 1
    fi
    if [[ -z "${MK_LLM_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model ${C[bold]}$name${C[reset]}"
        print -P "  Available: ${(j:, :)MK_LLM_ORDER}"
        return 1
    fi
    if llm_present "$name"; then
        local p=$(llm_path "$name")
        print -P "${C[green]}✓${C[reset]} ${C[bold]}$name${C[reset]} already downloaded ${C[dim]}(${p/$HOME/~})${C[reset]}"
        return 0
    fi
    _ensure_mlx_lm || return 1

    local size="${MK_LLM_REGISTRY[$name]%%|*}"
    local repo=$(_llm_repo "$name")
    local target=$(llm_path "$name")
    print -P "${C[bright_yellow]}▸${C[reset]} Downloading ${C[bold]}$name${C[reset]} ${C[dim]}(~$size, from ${repo})...${C[reset]}"
    mkdir -p "${target:h}"
    # huggingface_hub's tqdm goes to stderr; we want users to see it.
    if ! "$MK_PY_VENV/bin/python" "$MK_ROOT/src/llm/mlx_download.py" \
            --repo "$repo" --target "$target"; then
        print -P "${C[red]}error:${C[reset]} download failed"
        rm -rf "$target"
        return 1
    fi
    print -P "${C[green]}✓${C[reset]} Downloaded ${C[bold]}$name${C[reset]}"
}

# Switch the active local model. The launcher re-resolves MK_LLM_MODEL on
# its next invocation, so this takes effect immediately for /start, /ask,
# titling, etc. (every command runs the launcher fresh).
llm_use() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /llm use <name>"
        print -P "  Available: ${(j:, :)MK_LLM_ORDER}"
        return 1
    fi
    if [[ -z "${MK_LLM_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model ${C[bold]}$name${C[reset]}"
        return 1
    fi
    local_llm_active_set "$name"
    print -P "${C[green]}✓${C[reset]} Active local model: ${C[bold]}$name${C[reset]}"
    if ! llm_present "$name"; then
        print -P "  ${C[dim]}(not downloaded yet — run${C[reset]} ${C[bright_cyan]}/llm download $name${C[reset]}${C[dim]})${C[reset]}"
    fi
}

# List the registry: each known model's size, description, and download/active
# state. Sizes are colour-coded by how comfortably the model's runtime memory
# (weights + KV cache) fits on this machine: green = fits, yellow = tight,
# red = won't fit comfortably (would page heavily).
llm_list() {
    local active=$(local_llm_active_get)
    local total_mb=$(mk_total_ram_mb)
    local free_mb=$(mk_free_ram_mb)
    print -P ""
    print -P "${C[bright_yellow]}LOCAL LLMs${C[reset]} ${C[dim]}(in ${MK_HOME/$HOME/~}/models/)${C[reset]}"
    if (( total_mb > 0 )); then
        local total_gb=$((total_mb / 1024))
        local free_gb_int=$((free_mb / 1024))
        local free_gb_dec=$(( (free_mb * 10 / 1024) % 10 ))
        print -P "${C[dim]}  This Mac: ${total_gb} GB RAM, ~${free_gb_int}.${free_gb_dec} GB free now — colours = ${C[reset]}${C[green]}fits${C[reset]}${C[dim]} / ${C[reset]}${C[bright_yellow]}tight${C[reset]}${C[dim]} / ${C[reset]}${C[red]}won't fit${C[reset]}"
    fi
    local name
    for name in "${MK_LLM_ORDER[@]}"; do
        local entry="${MK_LLM_REGISTRY[$name]}"
        # entry = "humanSize|description|filename|runtimeMB"
        local size="${entry%%|*}"
        local rest="${entry#*|}"
        local desc="${rest%%|*}"
        local rt_mb="${entry##*|}"
        local size_render=$(mk_fit_render "$size" "$rt_mb")
        local present="${C[gray]}○ not downloaded${C[reset]}"
        llm_present "$name" && present="${C[green]}● downloaded${C[reset]}"
        local marker="  "
        [[ "$name" == "$active" ]] && marker="${C[bright_cyan]}▸ ${C[reset]}"
        print -P "${marker}${C[bold]}$name${C[reset]}  ${size_render}  $present"
        print -P "    ${C[dim]}$desc${C[reset]}"
    done
    print -P ""
    print -P "  ${C[dim]}/llm download <name>${C[reset]}   fetch a model from HuggingFace"
    print -P "  ${C[dim]}/llm use <name>${C[reset]}        set active local model (▸)"
    print -P "  ${C[dim]}/llm rm <name>${C[reset]}         delete a downloaded model"
    print -P ""
}

# Install whatever the active backend needs.
#   local  → ensure mlx-lm is in the REPL venv + download the active model
#   claude → verify the `claude` CLI is on PATH.
llm_install() {
    case "$(title_backend_active)" in
        claude)
            if _claude_available; then
                print -P "${C[green]}✓${C[reset]} ${C[bold]}claude${C[reset]} CLI present (using $(claude_model_active))"
                print -P "  ${C[dim]}Bills against your Claude Pro/Max subscription.${C[reset]}"
                return 0
            fi
            print -P "${C[red]}error:${C[reset]} ${C[bold]}claude${C[reset]} CLI not found"
            print -P "  Install Claude Code from ${C[dim]}https://claude.com/code${C[reset]}"
            print -P "  …or switch to the local backend: ${C[dim]}/llm backend local${C[reset]}"
            return 1
            ;;
    esac

    # local backend: ensure mlx-lm + the active model are present.
    _ensure_mlx_lm || return 1
    llm_download "$(local_llm_active_get)" || return 1
    print -P "${C[green]}✓${C[reset]} AI titling ready"
}

# Remove a specific local model. Refuses to delete the active model
# without confirmation (would silently break titling on next /stop).
# MLX models are directories, not single files — rm -rf the snapshot.
llm_remove() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /llm rm <name>"
        print -P "  Run ${C[bright_cyan]}/llm list${C[reset]} to see what's downloaded."
        return 1
    fi
    if [[ -z "${MK_LLM_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model ${C[bold]}$name${C[reset]}"
        return 1
    fi
    local target=$(llm_path "$name")
    if [[ ! -d "$target" ]]; then
        print -P "${C[dim]}${name} not downloaded.${C[reset]}"
        return 0
    fi
    if [[ "$name" == "$(local_llm_active_get)" ]]; then
        print -P "${C[bright_yellow]}⚠${C[reset]}  ${C[bold]}$name${C[reset]} is the active model — type the name to confirm deletion:"
        print -nP "  > "
        local confirm
        read -r confirm
        if [[ "$confirm" != "$name" ]]; then
            print -P "  ${C[dim]}cancelled${C[reset]}"
            return 1
        fi
    fi
    rm -rf "$target"
    print -P "${C[green]}✓${C[reset]} Removed ${C[bold]}$name${C[reset]}"
}

llm_status() {
    local backend=$(title_backend_active)
    print -P ""
    print -P "${C[bright_yellow]}AI TITLING${C[reset]}"
    print -P "  Backend: ${C[bold]}$backend${C[reset]}"
    if [[ "$backend" == "claude" ]]; then
        if _claude_available; then
            print -P "  ${C[green]}●${C[reset]} claude CLI installed ${C[dim]}(model: $(claude_model_active))${C[reset]}"
        else
            print -P "  ${C[gray]}○${C[reset]} claude CLI not installed ${C[dim]}(install Claude Code)${C[reset]}"
        fi
    else
        if command -v llama-completion >/dev/null 2>&1; then
            print -P "  ${C[green]}●${C[reset]} llama.cpp installed"
        else
            print -P "  ${C[gray]}○${C[reset]} llama.cpp not installed"
        fi
        local active=$(local_llm_active_get)
        local active_path=$(llm_path "$active")
        if [[ -f "$active_path" ]]; then
            local size=$(du -h "$active_path" 2>/dev/null | cut -f1)
            print -P "  ${C[green]}●${C[reset]} ${C[bold]}$active${C[reset]} ${C[dim]}(${size}) at ${active_path/$HOME/~}${C[reset]}"
        else
            print -P "  ${C[gray]}○${C[reset]} ${C[bold]}$active${C[reset]} not downloaded ${C[dim]}(/llm download $active)${C[reset]}"
        fi
    fi
    print -P ""
    print -P "  ${C[dim]}/llm list${C[reset]}                      list available local models"
    print -P "  ${C[dim]}/llm download <name>${C[reset]}           fetch a local model"
    print -P "  ${C[dim]}/llm use <name>${C[reset]}                set active local model"
    print -P "  ${C[dim]}/llm rm <name>${C[reset]}                 delete a downloaded model"
    print -P "  ${C[dim]}/llm install${C[reset]}                   ensure llama.cpp + active model"
    print -P "  ${C[dim]}/llm backend <local|claude>${C[reset]}"
    print -P "  ${C[dim]}/llm model <name>${C[reset]}              set Claude model ${C[dim]}(claude backend only)${C[reset]}"
    print -P ""
}

# Lowercase, alphanum-only, hyphen-separated, max 50 chars.
slugify() {
    print -n -- "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -cs 'a-z0-9' '-' \
        | sed 's/^-//;s/-$//' \
        | cut -c1-50
}

# Drop the "# Meeting Transcript" / "Started: …" header — without it the
# tiny local model loves to echo them back as the title ("meeting transcript").
# Then truncate to ~80 lines (more than enough for a 3-5 word task).
_transcript_body() {
    grep -vE '^(# |Started: |---|Ended: )' "$1" 2>/dev/null | head -80
}

# Strip leading "title:"/"topic:" labels and trailing junk markers some
# backends add. Take the first non-empty, non-think-tag line.
_clean_title() {
    print -- "$1" \
        | grep -v '^$' \
        | grep -vi '<think>\|</think>' \
        | head -1 \
        | sed -E 's/[[:space:]]*\[end of text\][[:space:]]*$//' \
        | sed -E 's/^[[:space:]]*(title|topic)[[:space:]]*:[[:space:]]*//i' \
        | tr -d '\r'
}

_generate_title_local() {
    local content="$1"
    # System prompt sets the rules; mlx_helper.py wraps the user content in
    # the model's own chat template (Qwen3.5 in our case), so we don't have
    # to construct <|im_start|>...<|im_end|> markers by hand.
    local system_prompt="You name the SUBJECT of a meeting transcript in 3 to 5 lowercase words.
Output rules:
- ONLY the 3-5 word topic, nothing else
- lowercase, no punctuation, no quotes, no \"title:\" prefix
- describe what was discussed; do NOT say \"meeting\", \"transcript\", or \"conversation\"
- if there's not enough content to tell, output: untitled session"

    local user_prompt="${content}

Topic (3-5 words):"

    # Resolve the snapshot path for the active model. mlx_lm.load() accepts
    # a local directory, which we always pass — so titling works offline
    # once the snapshot is downloaded (no HF reachability required).
    local model_path=$(llm_path "$(local_llm_active_get)")

    # Why mlx-lm not llama-completion: native MLX is 30-60% faster on Apple
    # Silicon for the same Q4 model. -n 30 caps output (titles are short),
    # temp 0.3 keeps it deterministic-ish.
    "$MK_PY_VENV/bin/python" "$MK_ROOT/src/llm/mlx_helper.py" \
        --model "$model_path" \
        --system "$system_prompt" \
        --prompt "$user_prompt" \
        --max-tokens 30 \
        --temp 0.3 \
        2>/dev/null
}

_generate_title_claude() {
    local content="$1"
    local model=$(claude_model_active)
    # claude -p (print mode) is non-interactive: prints the assistant response
    # to stdout and exits. Auth uses the user's existing Claude Code session,
    # so it bills against their Pro/Max subscription, not the API.
    #
    # `--tools ""` disables all built-in tools (Bash/Read/etc.) and
    # `--strict-mcp-config` (with no --mcp-config) ignores every configured
    # MCP server. We just want a one-shot chat completion — no agentic
    # loop, no tool calls. These flags do two things:
    #   1. ~3× speedup (no MCP boot, ~7s instead of ~20s on M2 Pro).
    #   2. Fixes "Prompt is too long" on Haiku for users with many plugins
    #      or MCPs loaded — Claude Code's tool/MCP definitions otherwise
    #      blow Haiku's effective input budget even on tiny prompts.
    # `</dev/null` guards against the CLI ever probing stdin.
    claude -p \
        --model "$model" \
        --tools "" \
        --strict-mcp-config \
        "Pick a 3-5 word lowercase topic for this meeting transcript. Output ONLY the topic — no punctuation, no quotes, no 'title:' prefix, no explanation. Don't say 'meeting', 'transcript', or 'conversation'. If unclear, output: untitled session

Transcript:
$content" </dev/null 2>/dev/null
}

# Run the active backend on the first ~80 lines of the transcript and emit a
# slug. Returns empty string on any failure — caller treats that as "skip rename".
generate_title() {
    local transcript_file="$1"
    [[ -f "$transcript_file" ]] || return 1
    llm_available || return 1

    local content=$(_transcript_body "$transcript_file")
    [[ -z "$content" ]] && return 1

    local raw
    case "$(title_backend_active)" in
        claude) raw=$(_generate_title_claude "$content") ;;
        *)      raw=$(_generate_title_local  "$content") ;;
    esac

    local title=$(_clean_title "$raw")
    [[ -z "$title" ]] && return 1
    slugify "$title"
}

# Rename a session file with an AI slug. Best-effort — leaves the file alone
# if anything fails. Updates the live.txt symlink if it pointed at the old name.
# Args: $1 = absolute path to the session file (e.g. /…/2026-05-07_14-32-08.txt)
title_session_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    llm_available || return 0

    print -P "${C[dim]}Generating title...${C[reset]}"
    local slug=$(generate_title "$file" 2>/dev/null)
    if [[ -z "$slug" ]]; then
        print -P "${C[dim]}  (titling skipped)${C[reset]}"
        return 0
    fi

    # Strip the trailing -SS off the basename, append the slug.
    # 2026-05-07_14-32-08.txt → 2026-05-07_14-32_<slug>.txt
    local dir="${file:h}"
    local base="${file:t:r}"
    local trimmed="${base%-*}"
    local new="$dir/${trimmed}_${slug}.txt"

    if [[ -e "$new" ]]; then
        print -P "${C[dim]}  (skipping rename — ${new:t} already exists)${C[reset]}"
        return 0
    fi

    if ! mv "$file" "$new" 2>/dev/null; then
        print -P "${C[dim]}  (rename failed)${C[reset]}"
        return 0
    fi

    # Update live.txt symlink target if it pointed at the old file
    local live="$MK_TRANSCRIPTS_DIR/live.txt"
    if [[ -L "$live" ]]; then
        local target=$(readlink "$live" 2>/dev/null)
        if [[ "$target" == "$file" ]]; then
            ln -sfn "$new" "$live"
        fi
    fi

    print -P "${C[green]}✓${C[reset]} Renamed: ${C[bright_cyan]}${new:t}${C[reset]}"

    # After titling succeeds, generate the per-meeting summary and rebuild
    # the project's rolling meetings.md. Best-effort — a summary failure
    # shouldn't block /stop's main success path. Both functions are defined
    # in summary.sh; if the file failed to load we silently skip.
    if typeset -f summary_save >/dev/null 2>&1; then
        if summary_save "$new" 2>/dev/null; then
            if typeset -f meetings_log_rebuild >/dev/null 2>&1; then
                meetings_log_rebuild "${new:h}" 2>/dev/null
            fi
        else
            print -P "${C[dim]}  (summary skipped)${C[reset]}"
        fi
    fi
}

# /llm dispatch. Note: bin/meetink runs under `set -e`, so we can't `shift`
# without args (that would abort the script). Use $2 / $3 directly instead.
cmd_llm() {
    local sub="$1"
    case "$sub" in
        ""|status)
            llm_status
            ;;
        install|setup)
            llm_install
            ;;
        list|ls|models)
            llm_list
            ;;
        download|dl|get)
            llm_download "$2"
            ;;
        use|switch|set)
            llm_use "$2"
            ;;
        rm|remove|delete|uninstall)
            llm_remove "$2"
            ;;
        backend)
            local choice="$2"
            case "$choice" in
                "")
                    print -P "  Backend: ${C[bold]}$(title_backend_active)${C[reset]}"
                    print -P "  ${C[dim]}/llm backend local${C[reset]}   on-device (Qwen3-0.6B, fast, offline)"
                    print -P "  ${C[dim]}/llm backend claude${C[reset]}  Claude via subscription (best quality, network)"
                    ;;
                local)
                    title_backend_set "local"
                    print -P "${C[green]}✓${C[reset]} Backend set to ${C[bold]}local${C[reset]}"
                    _local_available || print -P "  ${C[dim]}Run /llm install to download the local model.${C[reset]}"
                    ;;
                claude)
                    title_backend_set "claude"
                    print -P "${C[green]}✓${C[reset]} Backend set to ${C[bold]}claude${C[reset]} ${C[dim]}(model: $(claude_model_active))${C[reset]}"
                    if ! _claude_available; then
                        print -P "  ${C[red]}!${C[reset]} ${C[dim]}claude CLI not found — install Claude Code from https://claude.com/code${C[reset]}"
                    fi
                    ;;
                *)
                    print -P "${C[red]}unknown backend:${C[reset]} ${C[dim]}$choice${C[reset]} ${C[dim]}(use: local | claude)${C[reset]}"
                    ;;
            esac
            ;;
        model)
            local name="$2"
            if [[ -z "$name" ]]; then
                print -P "  Claude model: ${C[bold]}$(claude_model_active)${C[reset]}"
                print -P "  ${C[dim]}/llm model sonnet${C[reset]}   balanced default ${C[dim]}(latest Sonnet)${C[reset]}"
                print -P "  ${C[dim]}/llm model haiku${C[reset]}    fastest, cheapest ${C[dim]}(latest Haiku)${C[reset]}"
                print -P "  ${C[dim]}/llm model opus${C[reset]}     deepest reasoning ${C[dim]}(latest Opus)${C[reset]}"
                print -P "  ${C[dim]}…or pin a full model id, e.g. claude-sonnet-4-6${C[reset]}"
            else
                claude_model_set "$name"
                print -P "${C[green]}✓${C[reset]} Claude model set to ${C[bold]}$name${C[reset]}"
                # `|| true` so the failed test doesn't trip `set -e` in bin/meetink.
                [[ "$(title_backend_active)" != "claude" ]] && \
                    print -P "  ${C[dim]}(only used when backend=claude — run /llm backend claude to switch)${C[reset]}" \
                    || true
            fi
            ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/llm $sub${C[reset]}"
            print -P "  ${C[dim]}/llm${C[reset]} | ${C[dim]}/llm list${C[reset]} | ${C[dim]}/llm download <name>${C[reset]} | ${C[dim]}/llm use <name>${C[reset]} | ${C[dim]}/llm rm <name>${C[reset]} | ${C[dim]}/llm backend${C[reset]} | ${C[dim]}/llm model${C[reset]}"
            ;;
    esac
}
