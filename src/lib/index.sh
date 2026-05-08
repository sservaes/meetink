#!/bin/zsh
# /index — install / status / rm for the RAG sidecar that backs /ask
# on long meetings.
#
# The index is a per-transcript sidecar directory built next to the
# .txt file:
#
#   <project>/2026-05-07_14-32_some-slug.idx/
#   ├── chunks.npy        # (N, 384) bge-small embeddings, L2-normalised
#   ├── chunks.jsonl      # one {ts, sp, tx} per chunk
#   ├── segments/NNNN.md  # ~5-min segment summaries
#   ├── decisions.md      # rolling decisions list
#   ├── actions.md        # rolling action items
#   └── meta.json         # {byte_offset, segment_count}
#
# It's built live in the REPL process during /start (background thread,
# minimal CPU during recording — only embeddings are continuous; segment
# summaries fire every ~5 min). Past transcripts are indexed lazily on
# first /ask. The Python side handles all of this; this file just wraps
# the install / status / rm dance.
#
# Sourced by bin/meetink AFTER repl.sh, context.sh.

# fastembed is an ONNX-backed embedder (~50 MB install, no torch). We
# previously used sentence-transformers, but its torch + transformers
# stack hit a NameError regression in transformers/integrations/
# accelerate.py at module import that crashed the REPL on Apple Silicon.
# fastembed bundles bge-small as ONNX, runs via onnxruntime, and avoids
# the entire torch import path.
INDEX_INSTALL_PYDEPS="fastembed"


index_available() {
    # Use a tolerant check: any exception during import counts as
    # "not available", not just ModuleNotFoundError. A broken stack
    # (e.g. legacy sentence-transformers leftovers) shouldn't be
    # reported as installed.
    [[ -x "$MK_PY_VENV/bin/python" ]] && \
        "$MK_PY_VENV/bin/python" -c \
        "
try:
    import fastembed
except Exception:
    raise SystemExit(1)
" 2>/dev/null
}


# Detect a legacy sentence-transformers install so we can clean it up
# during /index install. Two reasons to remove it: free ~700 MB of disk
# (torch + transformers), and avoid future confusion if the import
# regression resurfaces.
_index_has_legacy_st() {
    [[ -x "$MK_PY_VENV/bin/python" ]] && \
        "$MK_PY_VENV/bin/python" -c "import sentence_transformers" 2>/dev/null
    # Also catches a half-broken install where the package directory
    # exists but the import errors out — uv's metadata is the source
    # of truth, so check that.
    if [[ $? -ne 0 ]]; then
        # Import failed; check the metadata directly.
        "$MK_PY_VENV/bin/python" -c \
            "import importlib.metadata as m; m.version('sentence-transformers')" \
            2>/dev/null
    fi
}


# Install fastembed into the REPL venv. ~50 MB; bge-small.onnx (~80 MB)
# downloads on first encode() call into ~/.cache/fastembed.
index_install() {
    if [[ ! -x "$MK_PY_VENV/bin/python" ]]; then
        print -P "${C[red]}error:${C[reset]} REPL Python venv missing — run ${C[bright_cyan]}meetink setup${C[reset]} first"
        return 1
    fi
    if index_available; then
        print -P "${C[green]}✓${C[reset]} Index dependencies already installed"
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        print -P "${C[red]}error:${C[reset]} uv not found — run ${C[bright_cyan]}meetink setup${C[reset]} first"
        return 1
    fi

    # Migrate away from the legacy sentence-transformers stack if
    # present. This frees ~700 MB and removes the broken transformers
    # import path that crashed the REPL on previous installs.
    if _index_has_legacy_st >/dev/null 2>&1; then
        print -P "${C[bright_yellow]}▸${C[reset]} Removing legacy sentence-transformers install ${C[dim]}(~700 MB freed)...${C[reset]}"
        uv pip uninstall --python "$MK_PY_VENV/bin/python" \
            sentence-transformers torch transformers tokenizers safetensors \
            2>/dev/null || true
    fi

    print -P "${C[bright_yellow]}▸${C[reset]} Installing index dependencies ${C[dim]}(~50 MB, ONNX-based)...${C[reset]}"
    if ! uv pip install --python "$MK_PY_VENV/bin/python" --quiet \
            $INDEX_INSTALL_PYDEPS; then
        print -P "${C[red]}error:${C[reset]} install failed"
        return 1
    fi
    print -P "${C[green]}✓${C[reset]} fastembed installed"
    print -P "  ${C[dim]}bge-small.onnx (~80 MB) will download on first /start.${C[reset]}"
}


# Show install state + per-project index summary.
index_status() {
    if index_available; then
        print -P "${C[green]}✓${C[reset]} Index ${C[bold]}available${C[reset]} ${C[dim]}(fastembed in $MK_PY_VENV/bin)${C[reset]}"
    else
        print -P "${C[dim]}Index dependencies not installed.${C[reset]}"
        print -P "  Run ${C[bright_cyan]}/index install${C[reset]} ${C[dim]}(~50 MB; enables RAG-backed /ask on long meetings)${C[reset]}"
        return 0
    fi

    # Per-transcript index report for the active project.
    setopt local_options null_glob
    local -a idx_dirs=("$MK_TRANSCRIPTS_DIR"/*.idx(N/))
    if (( ${#idx_dirs[@]} == 0 )); then
        print -P "  ${C[dim]}No indexed transcripts in this project yet.${C[reset]}"
        return 0
    fi
    print -P ""
    print -P "${C[bright_yellow]}INDEXED TRANSCRIPTS${C[reset]} ${C[dim]}(in ${MK_TRANSCRIPTS_DIR/$HOME/~})${C[reset]}"
    print -P ""
    local d
    for d in "${idx_dirs[@]}"; do
        local name="${d:t:r}"
        local chunks=0 segs=0
        if [[ -f "$d/chunks.jsonl" ]]; then
            chunks=$(wc -l < "$d/chunks.jsonl" 2>/dev/null | tr -d ' ')
        fi
        if [[ -d "$d/segments" ]]; then
            setopt local_options null_glob
            local -a sf=("$d/segments"/*.md(N))
            segs=${#sf[@]}
        fi
        local has_dec="·" has_act="·"
        [[ -f "$d/decisions.md" ]] && has_dec="✓"
        [[ -f "$d/actions.md" ]]   && has_act="✓"
        printf "  ${C[bold]}%-32s${C[reset]} ${C[dim]}%6s lines · %2s segs · decisions %s · actions %s${C[reset]}\n" \
            "$name" "$chunks" "$segs" "$has_dec" "$has_act"
    done
    print -P ""
}


# Drop sentence-transformers + all sidecar .idx dirs in the current project.
# Doesn't touch other projects — call /project use first.
index_remove() {
    local arg="$1"
    if [[ "$arg" == "deps" || "$arg" == "uninstall" ]]; then
        # Best-effort cleanup: remove fastembed + the legacy ST stack
        # in case the user is migrating from a half-installed state.
        print -nP "Uninstall index dependencies (fastembed + any legacy stack)? ${C[dim]}(y/N)${C[reset]}: "
        local confirm; read confirm
        [[ "$confirm" != "y" && "$confirm" != "Y" ]] && {
            print -P "${C[dim]}cancelled${C[reset]}"; return 0
        }
        uv pip uninstall --python "$MK_PY_VENV/bin/python" \
            fastembed onnxruntime tokenizers \
            sentence-transformers torch transformers safetensors \
            2>/dev/null
        print -P "${C[green]}✓${C[reset]} Uninstalled"
        return 0
    fi
    # Default: remove sidecar dirs in the active project.
    setopt local_options null_glob
    local -a idx_dirs=("$MK_TRANSCRIPTS_DIR"/*.idx(N/))
    if (( ${#idx_dirs[@]} == 0 )); then
        print -P "${C[dim]}No .idx sidecars to remove.${C[reset]}"
        return 0
    fi
    print -P "Will remove ${#idx_dirs[@]} sidecar(s) in ${C[bright_cyan]}${MK_TRANSCRIPTS_DIR/$HOME/~}/${C[reset]}:"
    local d
    for d in "${idx_dirs[@]}"; do
        print -P "  ${C[dim]}${d:t}${C[reset]}"
    done
    print -nP "Proceed? ${C[dim]}(y/N)${C[reset]}: "
    local confirm; read confirm
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && {
        print -P "${C[dim]}cancelled${C[reset]}"; return 0
    }
    for d in "${idx_dirs[@]}"; do
        rm -rf "$d"
    done
    print -P "${C[green]}✓${C[reset]} Removed ${#idx_dirs[@]} sidecar(s)"
    print -P "  ${C[dim]}/ask will rebuild the index lazily on next use.${C[reset]}"
}


# /index dispatcher
cmd_index() {
    local sub="$1"
    case "$sub" in
        ""|status|list|ls)        index_status ;;
        install|setup)            index_install ;;
        rm|remove|delete)         index_remove "$2" ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/index $sub${C[reset]}"
            print -P "  ${C[dim]}/index${C[reset]}             show install state + per-transcript index"
            print -P "  ${C[dim]}/index install${C[reset]}     install sentence-transformers (~700 MB)"
            print -P "  ${C[dim]}/index rm${C[reset]}          delete sidecar dirs in active project"
            print -P "  ${C[dim]}/index rm deps${C[reset]}     uninstall sentence-transformers"
            ;;
    esac
}
