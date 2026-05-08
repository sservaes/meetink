#!/bin/zsh
# Whisper model registry, download, switching, and persistence.
#
# Sourced by bin/meetink. Depends on:
#   - $MK_HOME (set in bin/meetink)
#   - C[] color table from src/lib/ui.sh
#   - _is_running, stop_whisper_server, $PID_FILE from bin/meetink
#
# Active-model selection is persisted in $MK_HOME/config (one key=value per
# line). Read fresh on every cmd_start so users can switch by editing the
# file too if they prefer.

MK_CONFIG_FILE="$MK_HOME/config"

# Download a URL with periodic line-printed progress. curl's built-in
# --progress-bar uses \r to overwrite the same line, which works in a real
# terminal but spams the REPL's line-buffered output (every refresh becomes
# a new line). This polls the target file size every few seconds and prints
# one line per tick instead — looks tidy in both contexts.
#
#   $1 = target path
#   $2 = source URL
#   $3 = (optional) human-readable label for the progress line
#
# Returns curl's exit code. Removes the partial file on failure.
mk_download_with_progress() {
    local target="$1" url="$2" label="${3:-download}"
    local total=0
    # Probe the expected size up front so we can show a percentage. HEAD with
    # -I gives Content-Length on the final hop after redirects.
    local cl=$(curl -sIL "$url" 2>/dev/null | grep -i '^content-length:' | tail -1 | awk '{print $2}' | tr -d '\r')
    [[ "$cl" =~ '^[0-9]+$' ]] && total=$cl
    mkdir -p "${target:h}"
    # Run curl in the background, silent.
    curl -sL --fail -o "$target" "$url" &
    local curl_pid=$!
    local last_mb=-1
    # Poll roughly every 2s. stat -f %z returns bytes; convert to MB. Print
    # one tidy status line per tick: "▸ label  348 MB / 1.5 GB (23%)".
    while kill -0 "$curl_pid" 2>/dev/null; do
        sleep 2
        local now=0
        [[ -f "$target" ]] && now=$(stat -f %z "$target" 2>/dev/null || echo 0)
        local mb=$((now / 1024 / 1024))
        # Suppress duplicate ticks (file size unchanged → no progress to show).
        if (( mb != last_mb )); then
            # Use plain `print` here (not -P): the `C[...]` colour codes are
            # already raw ANSI escape sequences, so no prompt expansion is
            # needed. With -P, the literal `%` in `${pct}%)` would be eaten
            # as a prompt-format directive ("16)" instead of "16%)").
            if (( total > 0 )); then
                local pct=$(( now * 100 / total ))
                local total_mb=$((total / 1024 / 1024))
                print -- "  ${C[dim]}${label}: ${mb} MB / ${total_mb} MB (${pct}%)${C[reset]}"
            else
                print -- "  ${C[dim]}${label}: ${mb} MB${C[reset]}"
            fi
            last_mb=$mb
        fi
    done
    wait "$curl_pid"
    local rc=$?
    if (( rc != 0 )); then
        rm -f "$target"
    fi
    return $rc
}

# Total physical RAM in MB. 0 if sysctl is unavailable. Cached after first
# call (machine RAM doesn't change).
typeset -g _mk_total_ram_mb=0
mk_total_ram_mb() {
    if (( _mk_total_ram_mb > 0 )); then
        print -n -- "$_mk_total_ram_mb"
        return
    fi
    if command -v sysctl >/dev/null 2>&1; then
        local bytes=$(sysctl -n hw.memsize 2>/dev/null)
        [[ -n "$bytes" ]] && _mk_total_ram_mb=$((bytes / 1024 / 1024))
    fi
    print -n -- "$_mk_total_ram_mb"
}

# Currently-available RAM in MB (free + inactive + purgeable, the same number
# the footer chip surfaces). This is what actually constrains whether a model
# loads *right now* — not total RAM. Falls back to 0 on parse failure.
mk_free_ram_mb() {
    local out
    out=$(vm_stat 2>/dev/null) || { print -n -- 0; return; }
    local page_size=4096
    if [[ "$out" =~ 'page size of ([0-9]+)' ]]; then
        page_size=$match[1]
    fi
    local free=0 inactive=0 purgeable=0 line key val
    while IFS= read -r line; do
        case "$line" in
            "Pages free:"*)      val="${line##*:}"; free=${val// /};      free=${free%.}      ;;
            "Pages inactive:"*)  val="${line##*:}"; inactive=${val// /};  inactive=${inactive%.} ;;
            "Pages purgeable:"*) val="${line##*:}"; purgeable=${val// /}; purgeable=${purgeable%.} ;;
        esac
    done <<< "$out"
    print -n -- $(( (free + inactive + purgeable) * page_size / 1024 / 1024 ))
}

# Render a "(size, fits/tight/won't fit)" tag coloured by how a model's hot
# runtime memory compares to *currently free* RAM. Used by /model list and
# /llm list so the colour matches what would actually happen if you loaded
# the model right now (vs theoretical capacity on a fresh boot).
#
#   $1 = human size string (e.g. "466M", "5.4G")
#   $2 = runtime MB (integer)
#   $3 = effective free RAM in MB. If empty/zero, computed via mk_free_ram_mb.
#        Callers pass an adjusted value when there's a currently-loaded model
#        whose RAM would be reclaimed by switching (e.g. swapping whisper
#        models stops the active server first, so its weights free up).
mk_fit_render() {
    local size="$1" rt_mb="$2" free_mb="${3:-0}"
    if (( free_mb <= 0 )); then
        free_mb=$(mk_free_ram_mb)
    fi
    if (( free_mb <= 0 )); then
        print -n -- "${C[dim]}${size}${C[reset]}"
        return
    fi
    # red:    model > free RAM (won't load without paging)
    # yellow: model > 60% of free (you'd need to close apps; OS will press
    #         compressed memory hard)
    # green:  comfortable headroom
    if (( rt_mb > free_mb )); then
        print -n -- "${C[red]}${size} won't fit${C[reset]}"
    elif (( rt_mb * 5 > free_mb * 3 )); then
        print -n -- "${C[bright_yellow]}${size} tight${C[reset]}"
    else
        print -n -- "${C[green]}${size} fits${C[reset]}"
    fi
}

# name → "humanSize|one-line description|runtimeMB"
# Models with `-tdrz` are tinydiarize variants — they emit [SPEAKER_TURN]
# markers that we use to label THEM-A / THEM-B / THEM-C as speakers change.
# runtimeMB is roughly weights + KV cache while whisper-server is hot — used
# by model_list to colour-code which models will fit on this machine.
typeset -gA MK_MODEL_REGISTRY=(
    [tiny.en]="75M|EN — fastest, real-time on any Mac. Quick notes, demos. Misses accents.|250"
    [base.en]="142M|EN — realtime+. Solo dictation, clean speech. Struggles with proper nouns.|400"
    [small.en]="466M|EN — balanced default. Handles accents/jargon for typical meetings.|900"
    [small.en-tdrz]="466M|EN + speaker-turn detection. Same as small.en, plus THEM-A/B/C labels.|900"
    [medium.en]="1.4G|EN — better proper nouns + accents. ~2× slower than small.|2500"
    [medium.en-tdrz]="1.4G|EN + speaker-turn detection. Same as medium.en, plus THEM-A/B/C labels.|2500"
    [large-v3-turbo]="1.5G|Multilingual — modern, fast + accurate. Mixed-language calls.|2500"
    [large-v3]="2.9G|Multilingual — highest accuracy, slow on Apple Silicon. Legal / verbatim.|4000"
)

# Display order
typeset -ga MK_MODEL_ORDER=(
    tiny.en
    base.en
    small.en
    small.en-tdrz
    medium.en
    medium.en-tdrz
    large-v3-turbo
    large-v3
)

# Path on disk for a given model name.
model_path() {
    print -n -- "$MK_HOME/models/ggml-${1}.bin"
}

# Path to the CoreML encoder companion. whisper.cpp auto-detects this
# directory sitting next to the .bin file and runs the encoder pass on the
# Apple Neural Engine — 2-3× faster than the Metal-only path. Not all
# variants have CoreML companions (notably -tdrz fine-tunes don't); see
# _model_has_coreml below.
model_coreml_dir() {
    print -n -- "$MK_HOME/models/ggml-${1}-encoder.mlmodelc"
}

# Is this model downloaded?
model_present() {
    [[ -f "$(model_path "$1")" ]]
}

# Does this model variant have a CoreML companion published by whisper.cpp?
# tdrz fine-tunes don't get CoreML conversions, so we skip the fetch for them.
_model_has_coreml() {
    case "$1" in
        *-tdrz) return 1 ;;
        *)      return 0 ;;
    esac
}

# Read the active model name from config, defaulting to small.en.
model_active() {
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^active_model=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        if [[ -n "$v" ]] && [[ -n "${MK_MODEL_REGISTRY[$v]}" ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- "small.en"
}

# Persist active model to config.
model_set_active() {
    local name="$1"
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^active_model=' "$MK_CONFIG_FILE"; then
        # In-place update (BSD sed needs the empty backup arg)
        sed -i '' "s|^active_model=.*|active_model=$name|" "$MK_CONFIG_FILE"
    else
        echo "active_model=$name" >> "$MK_CONFIG_FILE"
    fi
}

# Download a whisper model from HuggingFace, plus its CoreML encoder
# companion when one is published. The companion is a zipped .mlmodelc
# bundle that whisper.cpp auto-detects from disk: it falls back to the
# Metal-only path if absent, so a missing/failed CoreML download is a
# soft failure (we warn but don't roll back the .bin).
model_download() {
    local name="$1"
    if [[ -z "${MK_MODEL_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model '$name'"
        print -P "  Available: ${(j:, :)MK_MODEL_ORDER}"
        return 1
    fi
    local target=$(model_path "$name")
    local already_have_bin=0
    [[ -f "$target" ]] && already_have_bin=1

    if (( ! already_have_bin )); then
        local size="${MK_MODEL_REGISTRY[$name]%%|*}"
        local url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${name}.bin"
        print -P "${C[bright_yellow]}▸${C[reset]} Downloading whisper model ${C[bold]}$name${C[reset]} ${C[dim]}(~$size)...${C[reset]}"
        if ! mk_download_with_progress "$target" "$url" "$name"; then
            print -P "${C[red]}error:${C[reset]} download failed"
            return 1
        fi
        print -P "${C[green]}✓${C[reset]} Downloaded: ${C[dim]}$target${C[reset]}"
    else
        print -P "${C[green]}✓${C[reset]} ${C[bold]}$name${C[reset]} weights already present"
    fi

    # CoreML encoder companion: only for variants that have one published,
    # and only if not already extracted on disk.
    if _model_has_coreml "$name"; then
        local coreml_dir=$(model_coreml_dir "$name")
        if [[ -d "$coreml_dir" ]]; then
            (( already_have_bin )) && print -P "${C[green]}✓${C[reset]} CoreML encoder companion already present"
        else
            local zip_url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${name}-encoder.mlmodelc.zip"
            local zip_target="$MK_HOME/models/ggml-${name}-encoder.mlmodelc.zip"
            print -P "${C[bright_yellow]}▸${C[reset]} Downloading CoreML encoder ${C[dim]}(uses Apple Neural Engine, ~2-3× faster encode)...${C[reset]}"
            if mk_download_with_progress "$zip_target" "$zip_url" "${name}-coreml"; then
                # macOS ships unzip by default; quiet mode to avoid spamming the REPL.
                if unzip -q -o "$zip_target" -d "$MK_HOME/models/" 2>/dev/null; then
                    rm -f "$zip_target"
                    print -P "${C[green]}✓${C[reset]} CoreML encoder ready: ${C[dim]}$coreml_dir${C[reset]}"
                else
                    print -P "${C[yellow]}⚠${C[reset]} CoreML encoder downloaded but unzip failed — continuing without it"
                    rm -f "$zip_target"
                fi
            else
                print -P "${C[yellow]}⚠${C[reset]} CoreML encoder unavailable — falling back to Metal-only encode"
            fi
        fi
    fi
}

# Remove a downloaded model. Refuses to remove the active model. Also
# cleans up the CoreML companion directory if it was extracted.
model_remove() {
    local name="$1"
    if [[ -z "${MK_MODEL_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model '$name'"
        return 1
    fi
    local path=$(model_path "$name")
    local coreml=$(model_coreml_dir "$name")
    if [[ ! -f "$path" ]] && [[ ! -d "$coreml" ]]; then
        print -P "${C[dim]}$name not present${C[reset]}"
        return 0
    fi
    if [[ "$(model_active)" == "$name" ]]; then
        print -P "${C[red]}error:${C[reset]} cannot remove the active model. Switch first with ${C[bright_cyan]}/model use <name>${C[reset]}"
        return 1
    fi
    [[ -f "$path" ]] && rm "$path"
    [[ -d "$coreml" ]] && rm -rf "$coreml"
    print -P "${C[green]}✓${C[reset]} Removed ${C[bold]}$name${C[reset]}"
}

# Pretty list of all models with status indicator. 2-line layout per model:
# row 1 has the indicator + name + size + status; row 2 has the description.
# Sizes are colour-coded by how comfortably the model's hot runtime memory
# fits on this machine (same scheme as /llm list): green = fits, yellow =
# tight, red = won't fit comfortably (would page heavily).
model_list() {
    local active=$(model_active)
    local total_mb=$(mk_total_ram_mb)
    local free_mb=$(mk_free_ram_mb)
    # Effective free for swap candidates = current free + active model's
    # runtime, since switching models stops whisper-server (frees its
    # weights) before loading the new one.
    local active_rt=0
    if [[ -n "${MK_MODEL_REGISTRY[$active]}" ]]; then
        active_rt="${MK_MODEL_REGISTRY[$active]##*|}"
    fi
    local effective_free=$(( free_mb + active_rt ))
    print -P ""
    print -P "${C[bright_yellow]}WHISPER MODELS${C[reset]}"
    if (( total_mb > 0 )); then
        local total_gb=$((total_mb / 1024))
        local free_gb_int=$((free_mb / 1024))
        local free_gb_dec=$(( (free_mb * 10 / 1024) % 10 ))
        print -P "${C[dim]}  This Mac: ${total_gb} GB RAM, ~${free_gb_int}.${free_gb_dec} GB free (+ ${active_rt}MB active = budget for swap)${C[reset]}"
        print -P "${C[dim]}  colours = ${C[reset]}${C[green]}fits${C[reset]}${C[dim]} / ${C[reset]}${C[bright_yellow]}tight${C[reset]}${C[dim]} / ${C[reset]}${C[red]}won't fit${C[reset]}"
    fi
    print ""
    local name=""
    local meta="" size="" rest="" desc="" rt_mb="" indicator="" status_label="" size_render=""
    for name in $MK_MODEL_ORDER; do
        meta="${MK_MODEL_REGISTRY[$name]}"
        size="${meta%%|*}"
        rest="${meta#*|}"
        desc="${rest%%|*}"
        rt_mb="${meta##*|}"
        if [[ "$name" == "$active" ]]; then
            # Already loaded — no fit check; it's running fine right now.
            size_render="${C[green]}${size} loaded${C[reset]}"
            indicator="${C[green]}●${C[reset]}"
            status_label="${C[green]}active${C[reset]}"
        else
            size_render=$(mk_fit_render "$size" "$rt_mb" "$effective_free")
            if model_present "$name"; then
                indicator="${C[gray]}○${C[reset]}"
                status_label="${C[dim]}downloaded${C[reset]}"
            else
                indicator=" "
                status_label="${C[dim]}not installed${C[reset]}"
            fi
        fi
        printf "  %s ${C[bold]}%-16s${C[reset]}  %s  %s\n" \
            "$indicator" "$name" "$size_render" "$status_label"
        printf "       ${C[dim]}%s${C[reset]}\n" "$desc"
    done
    print ""
    print -P "  ${C[dim]}/model download <name>${C[reset]}  ${C[dim]}/model use <name>${C[reset]}  ${C[dim]}/model rm <name>${C[reset]}"
    print ""
}

# Switch the active model. Refuses if a recording is in progress.
model_use() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /model use <name>"
        return 1
    fi
    if [[ -z "${MK_MODEL_REGISTRY[$name]}" ]]; then
        print -P "${C[red]}error:${C[reset]} unknown model '$name'"
        print -P "  Run ${C[bright_cyan]}/model${C[reset]} to see available."
        return 1
    fi
    if ! model_present "$name"; then
        print -P "${C[yellow]}$name not downloaded.${C[reset]} Run ${C[bright_cyan]}/model download $name${C[reset]} first."
        return 1
    fi
    if _is_running 2>/dev/null; then
        print -P "${C[yellow]}Stop the current recording first${C[reset]} ${C[dim]}(/stop)${C[reset]}"
        return 1
    fi
    # whisper-server may still be running from a previous /start; restart it
    # next time around with the new model.
    stop_whisper_server 2>/dev/null
    model_set_active "$name"
    MK_MODEL=$(model_path "$name")
    print -P "${C[green]}✓${C[reset]} Active model: ${C[bold]}$name${C[reset]}"
}

# Interactive picker used by cmd_setup. Reads a numeric choice, downloads
# the selected model, and sets it as active.
model_setup_picker() {
    print -P ""
    print -P "${C[bright_yellow]}Choose a whisper model${C[reset]}"
    print -P "${C[dim]}Smaller = faster + less accurate. small.en is the recommended default.${C[reset]}"
    print -P ""
    local i=1
    local default_idx=3   # small.en
    local name="" meta="" size="" rest="" desc="" marker=""
    for name in $MK_MODEL_ORDER; do
        meta="${MK_MODEL_REGISTRY[$name]}"
        size="${meta%%|*}"
        rest="${meta#*|}"
        desc="${rest%%|*}"
        marker=""
        [[ "$name" == "small.en" ]] && marker=" ${C[green]}(recommended)${C[reset]}"
        printf "  ${C[bright_yellow]}%d${C[reset]}) ${C[bold]}%-16s${C[reset]} ${C[dim]}%5s${C[reset]}%s\n" \
            $i "$name" "$size" "$marker"
        printf "     ${C[dim]}%s${C[reset]}\n" "$desc"
        i=$((i + 1))
    done
    print ""
    print -nP "  Choose [1-${#MK_MODEL_ORDER}, default $default_idx]: "
    local choice=""
    read choice
    [[ -z "$choice" ]] && choice=$default_idx
    if [[ ! "$choice" =~ '^[0-9]+$' ]] || (( choice < 1 || choice > ${#MK_MODEL_ORDER} )); then
        print -P "${C[red]}invalid choice${C[reset]} — using small.en"
        choice=$default_idx
    fi
    local selected="${MK_MODEL_ORDER[$choice]}"
    if model_download "$selected"; then
        model_set_active "$selected"
        MK_MODEL=$(model_path "$selected")
        print -P "${C[green]}✓${C[reset]} Active model: ${C[bold]}$selected${C[reset]}"
    fi
}

# /model dispatch (called from repl.sh).
cmd_model() {
    local sub="$1"
    shift 2>/dev/null
    case "$sub" in
        ""|status|list|ls)
            model_list
            ;;
        download|dl|get)
            model_download "$1"
            ;;
        use|switch|set)
            model_use "$1"
            ;;
        rm|remove|delete)
            model_remove "$1"
            ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/model $sub${C[reset]}"
            print -P "  ${C[dim]}/model${C[reset]} | ${C[dim]}/model download <name>${C[reset]} | ${C[dim]}/model use <name>${C[reset]} | ${C[dim]}/model rm <name>${C[reset]}"
            ;;
    esac
}
