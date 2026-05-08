#!/bin/zsh
# Speaker-identification sidecar (the "diarize-server").
#
# Architecture: a small Python HTTP server on :8179 backed by sherpa-onnx
# (a WeSpeaker ResNet34 ONNX model, ~25 MB, Apple-Silicon accelerated via
# CoreML). The Swift capture binary in main.swift POSTs ~10s WAV windows
# to /identify and uses the returned name in transcripts.
#
# Profiles are persisted to $MK_HOME/profiles/<name>.npz. Each profile is
# the L2-normalised centroid of N enrollment samples; /profile add records
# 3 samples per person for stability.
#
# Sourced by bin/meetink.

MK_DIARIZE_VENV="$MK_HOME/diarize-venv"
MK_DIARIZE_MODEL="$MK_HOME/models/speaker-embedding.onnx"
MK_DIARIZE_PROFILES="$MK_HOME/profiles"
MK_DIARIZE_PORT=8179
MK_DIARIZE_PIDFILE="/tmp/meetink-diarize.pid"
MK_DIARIZE_LOG="/tmp/meetink-diarize.log"
# WeSpeaker English ResNet34 (VoxCeleb-trained), ~25 MB. The release tag has
# a typo upstream ("recongition") that we preserve verbatim.
MK_DIARIZE_MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"

diarize_available() {
    [[ -x "$MK_DIARIZE_VENV/bin/python" ]] && [[ -f "$MK_DIARIZE_MODEL" ]]
}

diarize_running() {
    [[ -f "$MK_DIARIZE_PIDFILE" ]] && kill -0 "$(cat "$MK_DIARIZE_PIDFILE")" 2>/dev/null
}

# Persistent on/off toggle (lives in $MK_HOME/config alongside active_model).
# Defaults to "on" so an installed sidecar auto-starts.
diarize_enabled_get() {
    local v=""
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        v=$(grep '^diarize_enabled=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    fi
    [[ "$v" == "false" || "$v" == "off" || "$v" == "0" ]] && { print -n -- "false"; return; }
    print -n -- "true"
}

diarize_enabled_set() {
    local val="$1"   # "true" or "false"
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^diarize_enabled=' "$MK_CONFIG_FILE"; then
        sed -i '' "s|^diarize_enabled=.*|diarize_enabled=$val|" "$MK_CONFIG_FILE"
    else
        echo "diarize_enabled=$val" >> "$MK_CONFIG_FILE"
    fi
}

# Fast on-disk profile count (no server roundtrip — used in the footer).
profile_count() {
    local c=0
    setopt local_options null_glob
    local f
    for f in "$MK_DIARIZE_PROFILES"/*.npz "$MK_DIARIZE_PROFILES"/*.npy; do
        c=$((c + 1))
    done
    print -n -- "$c"
}

# Start the sidecar in the background. No-op if:
#   - not installed
#   - user disabled it via /diarize off
#   - already running
diarize_start() {
    diarize_available || return 0
    [[ "$(diarize_enabled_get)" == "true" ]] || return 0
    diarize_running && return 0

    local server="$MK_ROOT/src/diarize/server.py"
    [[ -f "$server" ]] || return 0

    print -P "${C[dim]}Starting diarize-server (port $MK_DIARIZE_PORT)...${C[reset]}"
    MEETINK_HOME="$MK_HOME" \
    MEETINK_DIARIZE_MODEL="$MK_DIARIZE_MODEL" \
    MEETINK_PROFILES_DIR="$MK_DIARIZE_PROFILES" \
    MEETINK_DIARIZE_PORT="$MK_DIARIZE_PORT" \
        "$MK_DIARIZE_VENV/bin/python" "$server" \
        > "$MK_DIARIZE_LOG" 2>&1 &
    echo $! > "$MK_DIARIZE_PIDFILE"
    disown 2>/dev/null || true

    local i
    for i in {1..30}; do
        if curl -s -o /dev/null "http://127.0.0.1:$MK_DIARIZE_PORT/" 2>/dev/null; then
            print -P "${C[green]}✓${C[reset]} diarize-server ready"
            return 0
        fi
        sleep 0.3
    done
    print -P "${C[yellow]}⚠${C[reset]}  diarize-server didn't respond — see ${C[dim]}$MK_DIARIZE_LOG${C[reset]}"
    return 1
}

diarize_stop() {
    if [[ -f "$MK_DIARIZE_PIDFILE" ]]; then
        local pid=$(cat "$MK_DIARIZE_PIDFILE")
        kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null
        rm -f "$MK_DIARIZE_PIDFILE"
    fi
}

# Install: uv → venv → sherpa-onnx → ONNX model.
diarize_install() {
    if ! command -v uv >/dev/null 2>&1; then
        if ! command -v brew >/dev/null 2>&1; then
            print -P "${C[red]}error:${C[reset]} brew not found"
            return 1
        fi
        print -P "${C[bright_yellow]}▸${C[reset]} Installing uv (fast Python package manager)..."
        brew install uv || return 1
    fi

    if [[ ! -x "$MK_DIARIZE_VENV/bin/python" ]]; then
        print -P "${C[bright_yellow]}▸${C[reset]} Creating Python venv at ${C[dim]}${MK_DIARIZE_VENV/$HOME/~}${C[reset]}..."
        if ! uv venv "$MK_DIARIZE_VENV" --python 3.11 2>/dev/null && \
           ! uv venv "$MK_DIARIZE_VENV" 2>/dev/null; then
            print -P "${C[red]}error:${C[reset]} venv creation failed"
            return 1
        fi
    fi

    print -P "${C[bright_yellow]}▸${C[reset]} Installing sherpa-onnx + numpy (~50 MB)..."
    if ! uv pip install --python "$MK_DIARIZE_VENV/bin/python" --quiet sherpa-onnx numpy; then
        print -P "${C[red]}error:${C[reset]} pip install failed"
        return 1
    fi

    if [[ ! -f "$MK_DIARIZE_MODEL" ]]; then
        print -P "${C[bright_yellow]}▸${C[reset]} Downloading speaker-embedding model (~25 MB)..."
        mkdir -p "${MK_DIARIZE_MODEL:h}"
        if ! curl -L --fail --progress-bar -o "$MK_DIARIZE_MODEL" "$MK_DIARIZE_MODEL_URL"; then
            print -P "${C[red]}error:${C[reset]} model download failed"
            print -P "  Manually download to ${C[dim]}$MK_DIARIZE_MODEL${C[reset]}:"
            print -P "  ${C[bright_cyan]}$MK_DIARIZE_MODEL_URL${C[reset]}"
            rm -f "$MK_DIARIZE_MODEL"
            return 1
        fi
    else
        print -P "${C[green]}✓${C[reset]} model already present"
    fi

    mkdir -p "$MK_DIARIZE_PROFILES"

    # Mark enabled and boot the server so the footer flips to "live" right
    # away — otherwise the user is left thinking it's still half-installed.
    diarize_enabled_set true
    diarize_start

    print -P "${C[green]}✓${C[reset]} Speaker identification ready"
    print -P "  ${C[dim]}Add a profile:${C[reset]} ${C[bright_cyan]}/profile add <name>${C[reset]}"
}

diarize_remove() {
    diarize_stop
    [[ -d "$MK_DIARIZE_VENV" ]] && rm -rf "$MK_DIARIZE_VENV"
    [[ -f "$MK_DIARIZE_MODEL" ]] && rm -f "$MK_DIARIZE_MODEL"
    print -P "${C[green]}✓${C[reset]} Removed venv + model"
    print -P "  ${C[dim]}Profiles preserved at ${MK_DIARIZE_PROFILES/$HOME/~}/.${C[reset]}"
}

diarize_status() {
    print -P ""
    print -P "${C[bright_yellow]}SPEAKER IDENTIFICATION${C[reset]}"
    local enabled="$(diarize_enabled_get)"
    if [[ "$enabled" == "true" ]]; then
        print -P "  ${C[green]}●${C[reset]} Enabled ${C[dim]}(/diarize off to disable)${C[reset]}"
    else
        print -P "  ${C[yellow]}○${C[reset]} Disabled ${C[dim]}(/diarize on to enable)${C[reset]}"
    fi
    if [[ -x "$MK_DIARIZE_VENV/bin/python" ]]; then
        print -P "  ${C[green]}●${C[reset]} Python venv"
    else
        print -P "  ${C[gray]}○${C[reset]} Python venv ${C[dim]}(not installed)${C[reset]}"
    fi
    if [[ -f "$MK_DIARIZE_MODEL" ]]; then
        local size=$(du -h "$MK_DIARIZE_MODEL" 2>/dev/null | cut -f1)
        print -P "  ${C[green]}●${C[reset]} Embedding model ${C[dim]}(${size})${C[reset]}"
    else
        print -P "  ${C[gray]}○${C[reset]} Embedding model ${C[dim]}(not downloaded)${C[reset]}"
    fi
    if diarize_running; then
        print -P "  ${C[green]}●${C[reset]} Server running ${C[dim]}(PID $(cat "$MK_DIARIZE_PIDFILE"))${C[reset]}"
    else
        print -P "  ${C[gray]}○${C[reset]} Server not running"
    fi

    print ""
    print -P "  ${C[bold]}Profiles${C[reset]} ${C[dim]}(${MK_DIARIZE_PROFILES/$HOME/~})${C[reset]}"
    if diarize_running; then
        # Authoritative listing from the server (includes sample counts)
        local body
        body=$(curl -s "http://127.0.0.1:$MK_DIARIZE_PORT/profiles" 2>/dev/null)
        local compact=$(print -- "$body" | tr -d ' \t\n')
        if [[ "$compact" == *'"profiles":[]'* || -z "$body" ]]; then
            print -P "    ${C[dim]}(none — add one with /profile add <name>)${C[reset]}"
        else
            # Have Python emit `name<TAB>count` and let zsh do the colouring
            # via the shared C[] table — earlier we tried embedding ANSI in
            # the Python f-string but f-strings need `\x1b`, not `\\033`,
            # so the codes leaked through as literal `[96m` text.
            print -- "$body" | python3 -c '
import json, sys
try:
    for p in json.load(sys.stdin).get("profiles", []):
        print("{}\t{}".format(p["name"], p["samples"]))
except Exception:
    sys.exit(0)
' | while IFS=$'\t' read -r name count; do
                print -P "    ${C[bright_cyan]}●${C[reset]} ${C[bold]}$name${C[reset]}  ${C[dim]}($count samples)${C[reset]}"
            done
        fi
    else
        # Server's down: just list filenames
        local f any=0
        for f in "$MK_DIARIZE_PROFILES"/*.npz(N) "$MK_DIARIZE_PROFILES"/*.npy(N); do
            print -P "    ${C[bright_cyan]}●${C[reset]} ${C[bold]}${f:t:r}${C[reset]}"
            any=1
        done
        (( any == 0 )) && print -P "    ${C[dim]}(none)${C[reset]}"
    fi
    print ""
    print -P "  ${C[dim]}/diarize install${C[reset]} | ${C[dim]}/diarize rm${C[reset]} | ${C[dim]}/profile add <name>${C[reset]}"
    print ""
}

# /diarize sensitivity — view or set the matching aggressiveness preset.
# Hot-applies via POST /session/sensitivity so a switch mid-meeting takes
# effect on the very next ~10 s identification window. No restart needed.
diarize_sensitivity() {
    local mode="$1"
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi
    if [[ -z "$mode" ]]; then
        # Show current
        local resp=$(curl -sf "http://127.0.0.1:$MK_DIARIZE_PORT/session/sensitivity")
        if [[ -z "$resp" ]]; then
            print -P "${C[red]}error:${C[reset]} no response from diarize-server"
            return 1
        fi
        local cur=$(print -- "$resp" | sed -nE 's/.*"preset":[[:space:]]*"([^"]+)".*/\1/p')
        local thr=$(print -- "$resp" | sed -nE 's/.*"threshold":[[:space:]]*([0-9.]+).*/\1/p')
        local mar=$(print -- "$resp" | sed -nE 's/.*"margin":[[:space:]]*([0-9.]+).*/\1/p')
        local clt=$(print -- "$resp" | sed -nE 's/.*"cluster_threshold":[[:space:]]*([0-9.]+).*/\1/p')
        print -P ""
        print -P "${C[bright_yellow]}SENSITIVITY${C[reset]}"
        print -P "  ${C[dim]}preset:${C[reset]}             ${C[bold]}${cur}${C[reset]}"
        print -P "  ${C[dim]}threshold:${C[reset]}          ${thr}   ${C[dim]}cosine ≥ this to claim a profile match${C[reset]}"
        print -P "  ${C[dim]}margin:${C[reset]}             ${mar}   ${C[dim]}top profile must beat runner-up by this${C[reset]}"
        print -P "  ${C[dim]}cluster_threshold:${C[reset]}  ${clt}   ${C[dim]}cosine ≥ this to merge into existing cluster${C[reset]}"
        print -P ""
        print -P "  ${C[dim]}/diarize sensitivity focused${C[reset]}   ${C[dim]}— 1:1s & small known-speaker meetings${C[reset]}"
        print -P "  ${C[dim]}/diarize sensitivity default${C[reset]}   ${C[dim]}— general purpose (current ship default)${C[reset]}"
        print -P "  ${C[dim]}/diarize sensitivity strict${C[reset]}    ${C[dim]}— large meetings, lots of unknown voices${C[reset]}"
        print -P ""
        return 0
    fi

    case "$mode" in
        focused|default|strict) ;;
        *)
            print -P "${C[red]}error:${C[reset]} unknown mode '$mode'"
            print -P "  ${C[dim]}available:${C[reset]} focused | default | strict"
            return 1
            ;;
    esac
    local resp=$(curl -s -X POST \
        "http://127.0.0.1:$MK_DIARIZE_PORT/session/sensitivity?mode=$mode")
    if ! _resp_ok "$resp"; then
        print -P "${C[red]}error:${C[reset]} $resp"
        return 1
    fi
    local thr=$(print -- "$resp" | sed -nE 's/.*"threshold":[[:space:]]*([0-9.]+).*/\1/p')
    local mar=$(print -- "$resp" | sed -nE 's/.*"margin":[[:space:]]*([0-9.]+).*/\1/p')
    local clt=$(print -- "$resp" | sed -nE 's/.*"cluster_threshold":[[:space:]]*([0-9.]+).*/\1/p')
    print -P "${C[green]}✓${C[reset]} Sensitivity → ${C[bold]}${mode}${C[reset]} ${C[dim]}(threshold=${thr}, margin=${mar}, cluster_threshold=${clt})${C[reset]}"
}

# /diarize auto-train — show or tweak the continuous self-improvement knob.
# When on (default), high-confidence /identify matches fold back into the
# profile so it sharpens with real conversational audio over time. The
# guardrails (floor / margin multiplier / min-samples) are deliberately
# strict to avoid the FLAVIO-pollution failure mode.
diarize_auto_train() {
    local sub="$1" val="$2"
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi

    case "$sub" in
        ""|status)
            local resp=$(curl -sf "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train")
            if [[ -z "$resp" ]]; then
                print -P "${C[red]}error:${C[reset]} no response from diarize-server"
                return 1
            fi
            local en=$(print -- "$resp" | sed -nE 's/.*"enabled":[[:space:]]*(true|false).*/\1/p')
            local fl=$(print -- "$resp" | sed -nE 's/.*"floor":[[:space:]]*([0-9.]+).*/\1/p')
            local mm=$(print -- "$resp" | sed -nE 's/.*"margin_multiplier":[[:space:]]*([0-9.]+).*/\1/p')
            local ms=$(print -- "$resp" | sed -nE 's/.*"min_samples":[[:space:]]*([0-9]+).*/\1/p')
            local en_dot
            if [[ "$en" == "true" ]]; then
                en_dot="${C[green]}● enabled${C[reset]}"
            else
                en_dot="${C[gray]}○ disabled${C[reset]}"
            fi
            print -P ""
            print -P "${C[bright_yellow]}AUTO-TRAIN${C[reset]}"
            print -P "  ${C[dim]}status:${C[reset]}              ${en_dot}"
            print -P "  ${C[dim]}confidence floor:${C[reset]}    ${fl}   ${C[dim]}match must score ≥ this to qualify${C[reset]}"
            print -P "  ${C[dim]}margin multiplier:${C[reset]}   ${mm}×   ${C[dim]}must beat runner-up by ≥ N × MARGIN${C[reset]}"
            print -P "  ${C[dim]}min profile samples:${C[reset]} ${ms}    ${C[dim]}skip auto-train if profile has fewer${C[reset]}"
            print -P ""
            print -P "  ${C[dim]}/diarize auto-train on${C[reset]}              ${C[dim]}— enable${C[reset]}"
            print -P "  ${C[dim]}/diarize auto-train off${C[reset]}             ${C[dim]}— disable${C[reset]}"
            print -P "  ${C[dim]}/diarize auto-train floor 0.92${C[reset]}      ${C[dim]}— stricter confidence floor${C[reset]}"
            print -P "  ${C[dim]}/diarize auto-train margin 3.0${C[reset]}      ${C[dim]}— stricter margin multiplier${C[reset]}"
            print -P ""
            print -P "  ${C[dim]}A bad auto-add can be peeled off with${C[reset]} ${C[bright_cyan]}/profile undo <name>${C[reset]}${C[dim]}.${C[reset]}"
            print -P ""
            ;;
        on|enable)
            curl -s -X POST "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train?enabled=true" >/dev/null
            print -P "${C[green]}✓${C[reset]} Auto-train enabled"
            ;;
        off|disable)
            curl -s -X POST "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train?enabled=false" >/dev/null
            print -P "${C[green]}✓${C[reset]} Auto-train disabled"
            ;;
        floor)
            if [[ -z "$val" ]]; then
                print -P "${C[red]}usage:${C[reset]} /diarize auto-train floor <0.0-1.0>"
                return 1
            fi
            local resp=$(curl -s -X POST "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train?floor=$val")
            if ! _resp_ok "$resp"; then
                print -P "${C[red]}error:${C[reset]} $resp"
                return 1
            fi
            print -P "${C[green]}✓${C[reset]} Auto-train confidence floor → ${C[bold]}${val}${C[reset]}"
            ;;
        margin)
            if [[ -z "$val" ]]; then
                print -P "${C[red]}usage:${C[reset]} /diarize auto-train margin <multiplier>"
                return 1
            fi
            local resp=$(curl -s -X POST "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train?margin_multiplier=$val")
            if ! _resp_ok "$resp"; then
                print -P "${C[red]}error:${C[reset]} $resp"
                return 1
            fi
            print -P "${C[green]}✓${C[reset]} Auto-train margin multiplier → ${C[bold]}${val}×${C[reset]}"
            ;;
        min|min-samples|min_samples)
            if [[ -z "$val" ]]; then
                print -P "${C[red]}usage:${C[reset]} /diarize auto-train min <count>"
                return 1
            fi
            local resp=$(curl -s -X POST "http://127.0.0.1:$MK_DIARIZE_PORT/session/auto-train?min_samples=$val")
            if ! _resp_ok "$resp"; then
                print -P "${C[red]}error:${C[reset]} $resp"
                return 1
            fi
            print -P "${C[green]}✓${C[reset]} Auto-train min samples → ${C[bold]}${val}${C[reset]}"
            ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/diarize auto-train $sub${C[reset]}"
            print -P "  ${C[dim]}/diarize auto-train${C[reset]} ${C[dim]}for status, then on/off/floor/margin/min${C[reset]}"
            return 1
            ;;
    esac
}

cmd_diarize() {
    local sub="$1"
    case "$sub" in
        ""|status)         diarize_status ;;
        install|setup)     diarize_install ;;
        rm|remove|delete|uninstall)
                           diarize_remove ;;
        on|enable)
            diarize_enabled_set true
            print -P "${C[green]}✓${C[reset]} Speaker identification enabled"
            if diarize_available; then
                diarize_start
            else
                print -P "  ${C[dim]}(not installed yet — run${C[reset]} ${C[bright_cyan]}/diarize install${C[reset]}${C[dim]})${C[reset]}"
            fi
            ;;
        off|disable)
            diarize_enabled_set false
            diarize_stop
            print -P "${C[green]}✓${C[reset]} Speaker identification disabled ${C[dim]}(install preserved)${C[reset]}"
            ;;
        start)             diarize_start ;;
        stop)              diarize_stop && print -P "${C[green]}✓${C[reset]} diarize-server stopped" ;;
        sensitivity|sens)  diarize_sensitivity "$2" ;;
        auto-train|autotrain|auto_train)
                           diarize_auto_train "$2" "$3" ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/diarize $sub${C[reset]}"
            print -P "  ${C[dim]}/diarize${C[reset]} | ${C[dim]}/diarize on${C[reset]} | ${C[dim]}/diarize off${C[reset]} | ${C[dim]}/diarize install${C[reset]} | ${C[dim]}/diarize rm${C[reset]} | ${C[dim]}/diarize sensitivity [mode]${C[reset]}"
            ;;
    esac
}


# ---------------------------------------------------------------------------
# /profile commands — voiceprint enrollment + management
# ---------------------------------------------------------------------------

# Record N seconds of mic audio to $1 using the Swift capture binary's
# --record-sample mode. No new dependencies.
_profile_record_sample() {
    local out="$1" seconds="${2:-5}"
    local binary
    if ! binary=$(find_capture_binary); then
        print -P "${C[red]}error:${C[reset]} capture binary not found. Run /setup."
        return 1
    fi
    "$binary" --record-sample "$out" "$seconds" 2>/dev/null
}

# Enroll a person via 3 short samples. We average them server-side into a
# centroid; multiple samples markedly reduce false matches across people.
profile_add() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile add <name>"
        return 1
    fi
    if [[ "$name" == *.* || "$name" == */* ]]; then
        print -P "${C[red]}error:${C[reset]} no slashes or dots in names"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running. Run ${C[bright_cyan]}/diarize install${C[reset]} first."
        return 1
    fi

    print -P ""
    print -P "${C[bright_yellow]}Enrolling voice profile: ${C[bold]}$name${C[reset]}"
    print -P "${C[dim]}We'll record 3 samples (~5s each). Vary your sentences and intonation.${C[reset]}"
    print -P ""

    local sample
    sample="/tmp/meetink-sample-$$.wav"
    local i prompts=(
        "Sample 1/3 — read or say anything for 5 seconds"
        "Sample 2/3 — different sentence, same voice"
        "Sample 3/3 — last one, vary the pace"
    )

    for i in 1 2 3; do
        print -P "  ${C[bright_cyan]}${prompts[$i]}${C[reset]}"
        print -nP "  ${C[dim]}Press Enter to start recording...${C[reset]}"
        read -r _

        print -P "  ${C[bright_yellow]}● recording...${C[reset]}"
        if ! _profile_record_sample "$sample" 5; then
            print -P "  ${C[red]}recording failed${C[reset]}"
            rm -f "$sample"
            return 1
        fi

        local resp
        resp=$(curl -s -X POST \
            -H "Content-Type: audio/wav" \
            --data-binary "@$sample" \
            "http://127.0.0.1:$MK_DIARIZE_PORT/enroll?name=$name")
        rm -f "$sample"

        if ! _resp_ok "$resp"; then
            print -P "  ${C[red]}server error:${C[reset]} $resp"
            return 1
        fi
        local total=$(print -- "$resp" | sed -nE 's/.*"samples":[[:space:]]*([0-9]+).*/\1/p')
        print -P "  ${C[green]}✓${C[reset]} sample $i stored ${C[dim]}(profile total: $total)${C[reset]}"
        print ""
    done

    print -P "${C[green]}${C[bold]}✓ Profile saved: $name${C[reset]}"
    print -P "  ${C[dim]}Voice will be recognised in future recordings (when sufficiently similar).${C[reset]}"
    print ""
}

profile_train() {
    # One-sample append, for sharpening an existing profile after a misfire.
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile train <name>"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi

    print -P ""
    print -P "${C[bright_yellow]}Adding sample to ${C[bold]}$name${C[reset]}"
    print -nP "  ${C[dim]}Press Enter to record 5s...${C[reset]}"
    read -r _

    local sample="/tmp/meetink-sample-$$.wav"
    print -P "  ${C[bright_yellow]}● recording...${C[reset]}"
    if ! _profile_record_sample "$sample" 5; then
        print -P "  ${C[red]}recording failed${C[reset]}"
        rm -f "$sample"
        return 1
    fi
    local resp
    resp=$(curl -s -X POST \
        -H "Content-Type: audio/wav" \
        --data-binary "@$sample" \
        "http://127.0.0.1:$MK_DIARIZE_PORT/enroll?name=$name")
    rm -f "$sample"

    if _resp_ok "$resp"; then
        local total=$(print -- "$resp" | sed -nE 's/.*"samples":[[:space:]]*([0-9]+).*/\1/p')
        print -P "  ${C[green]}✓${C[reset]} added (total: $total samples)"
    else
        print -P "  ${C[red]}server error:${C[reset]} $resp"
    fi
}

profile_list() {
    if ! diarize_running; then
        print -P "${C[dim]}diarize-server not running${C[reset]} — run ${C[bright_cyan]}/diarize install${C[reset]} or ${C[bright_cyan]}/diarize start${C[reset]}"
        return 0
    fi
    diarize_status
}

profile_remove() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile rm <name>"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi
    local resp
    resp=$(curl -s -X DELETE "http://127.0.0.1:$MK_DIARIZE_PORT/profiles/$name")
    if _resp_ok "$resp"; then
        print -P "${C[green]}✓${C[reset]} Removed profile: ${C[bold]}$name${C[reset]}"
    else
        print -P "${C[red]}error:${C[reset]} $resp"
    fi
}

# Tolerant check that a JSON response contains `"ok":true` regardless of
# whitespace (the server uses python json.dumps default, which inserts
# spaces). Older callers used a literal glob and broke on space-after-colon.
_resp_ok() {
    [[ "$(print -- "$1" | tr -d ' \t\n')" == *'"ok":true'* ]]
}

# Rewrite a transcript label in-place: `[HH:MM:SS] OLD:` → `[HH:MM:SS] NEW:`.
# Used by profile_assign / profile_merge after the diarize-server has confirmed
# the change. Anchors on `] OLD:` so transcript text mentioning the label
# (e.g. "THEM-A is…" inside someone's quoted speech) doesn't get clobbered.
_rewrite_transcript_label() {
    local file="$1" old="$2" new="$3"
    local actual="$file"
    [[ -L "$file" ]] && actual=$(readlink "$file" 2>/dev/null)
    [[ -f "$actual" ]] || return 1
    # Truncate-and-rewrite to preserve the file's inode. The obvious
    # `sed -i ''` does an atomic rename, which orphans any process that
    # was tailing or watching the original inode (live-tail window,
    # editors with the file open, fswatch, etc.) and breaks them silently.
    # Truncate-and-write keeps the same inode so tail/editors keep
    # streaming the new content.
    local tmp=$(mktemp -t meetink-rewrite) || return 1
    if sed -E "s|] ${old}:|] ${new}:|g" "$actual" > "$tmp"; then
        cat "$tmp" > "$actual"
    fi
    rm -f "$tmp"
}

# Show currently active clusters (unmatched voices grouped by /identify).
profile_clusters() {
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running. Run ${C[bright_cyan]}/diarize install${C[reset]} first."
        return 1
    fi
    local resp=$(curl -sf "http://127.0.0.1:$MK_DIARIZE_PORT/session/clusters")
    if [[ -z "$resp" ]]; then
        print -P "${C[red]}error:${C[reset]} no response from diarize-server"
        return 1
    fi
    # Use Python to parse — robust to JSON whitespace and quoting. Outputs
    # one "letter samples" line per cluster, or nothing when empty.
    local lines=$(print -- "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    for c in d.get("clusters", []):
        print(c["letter"], c["samples"])
except Exception:
    sys.exit(0)
')
    if [[ -z "$lines" ]]; then
        print -P ""
        print -P "${C[dim]}No active clusters.${C[reset]}"
        print -P "${C[dim]}Clusters appear after ~10s of unidentified speech during a recording.${C[reset]}"
        print -P ""
        return 0
    fi
    print -P ""
    print -P "${C[bright_yellow]}ACTIVE CLUSTERS${C[reset]}"
    print -- "$lines" | while read -r letter count; do
        print -P "  ${C[green]}●${C[reset]} ${C[bold]}THEM-${letter}${C[reset]}  ${C[dim]}(${count} samples)${C[reset]}"
    done
    print -P ""
    print -P "  ${C[dim]}/profile assign <letter> <name>${C[reset]}   promote cluster → real profile"
    print -P "  ${C[dim]}/profile merge <from> <into>${C[reset]}      fold one cluster into another"
    print -P "  ${C[dim]}/profile rename <old> <new>${C[reset]}       rename a profile (or fold into existing)"
    print -P ""
}

# Promote a cluster to a real profile and rewrite the live transcript.
profile_assign() {
    local letter="$1" name="$2"
    if [[ -z "$letter" || -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile assign <cluster-letter> <name>"
        return 1
    fi
    if [[ "$name" == *.* || "$name" == */* ]]; then
        print -P "${C[red]}error:${C[reset]} no slashes or dots in names"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi

    local up_letter=$(print -n -- "$letter" | tr '[:lower:]' '[:upper:]')
    local resp=$(curl -s -X POST \
        "http://127.0.0.1:$MK_DIARIZE_PORT/session/assign?cluster=$up_letter&name=$name")
    if ! _resp_ok "$resp"; then
        print -P "${C[red]}error:${C[reset]} $resp"
        return 1
    fi
    local samples=$(print -- "$resp" | sed -nE 's/.*"samples":[[:space:]]*([0-9]+).*/\1/p')
    print -P "${C[green]}✓${C[reset]} Saved profile ${C[bold]}$name${C[reset]} ${C[dim]}(from cluster $up_letter, $samples samples)${C[reset]}"

    local up_name=$(print -n -- "$name" | tr '[:lower:]' '[:upper:]')
    if [[ -L "$MK_TRANSCRIPT" ]] && _rewrite_transcript_label "$MK_TRANSCRIPT" "THEM-${up_letter}" "$up_name"; then
        local actual=$(readlink "$MK_TRANSCRIPT" 2>/dev/null)
        print -P "${C[green]}✓${C[reset]} Renamed ${C[dim]}THEM-${up_letter}${C[reset]} → ${C[bold]}${up_name}${C[reset]} in ${C[bright_cyan]}${actual:t}${C[reset]}"
    fi
}

# Pop the last N samples off a profile and recompute its centroid.
# Useful when /profile train picked up a stray voice — undo last sample,
# don't trash the whole profile and re-enroll.
profile_undo() {
    local name="$1" count="${2:-1}"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile undo <name> [count]"
        return 1
    fi
    if ! [[ "$count" =~ ^[0-9]+$ ]] || (( count < 1 )); then
        print -P "${C[red]}error:${C[reset]} count must be a positive integer"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi
    local resp=$(curl -s -X POST \
        "http://127.0.0.1:$MK_DIARIZE_PORT/profiles/$name/pop?count=$count")
    if ! _resp_ok "$resp"; then
        print -P "${C[red]}error:${C[reset]} $resp"
        return 1
    fi
    local removed=$(print -- "$resp" | sed -nE 's/.*"removed":[[:space:]]*([0-9]+).*/\1/p')
    local remaining=$(print -- "$resp" | sed -nE 's/.*"remaining":[[:space:]]*([0-9]+).*/\1/p')
    print -P "${C[green]}✓${C[reset]} Dropped last ${C[bold]}${removed}${C[reset]} sample(s) from ${C[bold]}${name}${C[reset]} ${C[dim]}(${remaining} remaining)${C[reset]}"
}

# Rename a profile, OR fold one profile into an existing other (when the
# same speaker got enrolled under two names — e.g. earlier session called
# them BOB, this one calls them FLAVIO). Server is the source of truth;
# we mirror the change to the live transcript.
profile_rename() {
    local from="$1" to="$2"
    if [[ -z "$from" || -z "$to" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile rename <old> <new>"
        return 1
    fi
    if [[ "$to" == *.* || "$to" == */* ]]; then
        print -P "${C[red]}error:${C[reset]} no slashes or dots in names"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi

    local resp=$(curl -s -X POST \
        "http://127.0.0.1:$MK_DIARIZE_PORT/session/rename?from=$from&to=$to")
    if ! _resp_ok "$resp"; then
        print -P "${C[red]}error:${C[reset]} $resp"
        return 1
    fi
    local samples=$(print -- "$resp" | sed -nE 's/.*"samples":[[:space:]]*([0-9]+).*/\1/p')
    local merged=$(print -- "$resp" | sed -nE 's/.*"merged":[[:space:]]*(true|false).*/\1/p')
    if [[ "$merged" == "true" ]]; then
        print -P "${C[green]}✓${C[reset]} Folded ${C[bold]}$from${C[reset]} into ${C[bold]}$to${C[reset]} ${C[dim]}($samples samples total)${C[reset]}"
    else
        print -P "${C[green]}✓${C[reset]} Renamed ${C[bold]}$from${C[reset]} → ${C[bold]}$to${C[reset]} ${C[dim]}($samples samples)${C[reset]}"
    fi

    # Live transcript labels are uppercase (main.swift uppercases names),
    # so we rewrite BOB → FLAVIO not bob → flavio.
    local up_from=$(print -n -- "$from" | tr '[:lower:]' '[:upper:]')
    local up_to=$(print -n -- "$to" | tr '[:lower:]' '[:upper:]')
    if [[ -L "$MK_TRANSCRIPT" ]] && _rewrite_transcript_label "$MK_TRANSCRIPT" "$up_from" "$up_to"; then
        local actual=$(readlink "$MK_TRANSCRIPT" 2>/dev/null)
        print -P "${C[green]}✓${C[reset]} Renamed ${C[dim]}${up_from}${C[reset]} → ${C[bold]}${up_to}${C[reset]} in ${C[bright_cyan]}${actual:t}${C[reset]}"
    fi
}

# Fold one cluster into another (e.g. when one speaker got split across two).
profile_merge() {
    local from="$1" into="$2"
    if [[ -z "$from" || -z "$into" ]]; then
        print -P "${C[red]}usage:${C[reset]} /profile merge <from-letter> <into-letter>"
        return 1
    fi
    if ! diarize_running; then
        print -P "${C[red]}error:${C[reset]} diarize-server not running"
        return 1
    fi

    local up_from=$(print -n -- "$from" | tr '[:lower:]' '[:upper:]')
    local up_into=$(print -n -- "$into" | tr '[:lower:]' '[:upper:]')
    local resp=$(curl -s -X POST \
        "http://127.0.0.1:$MK_DIARIZE_PORT/session/merge?from=$up_from&into=$up_into")
    if ! _resp_ok "$resp"; then
        print -P "${C[red]}error:${C[reset]} $resp"
        return 1
    fi
    print -P "${C[green]}✓${C[reset]} Merged cluster ${C[bold]}$up_from${C[reset]} into ${C[bold]}$up_into${C[reset]}"

    if [[ -L "$MK_TRANSCRIPT" ]] && _rewrite_transcript_label "$MK_TRANSCRIPT" "THEM-${up_from}" "THEM-${up_into}"; then
        local actual=$(readlink "$MK_TRANSCRIPT" 2>/dev/null)
        print -P "${C[green]}✓${C[reset]} Renamed ${C[dim]}THEM-${up_from}${C[reset]} → ${C[dim]}THEM-${up_into}${C[reset]} in ${C[bright_cyan]}${actual:t}${C[reset]}"
    fi
}

# /profile dispatch. Direct $2/$3 indexing to avoid shift-with-no-args
# crashing under bin/meetink's set -e.
cmd_profile() {
    local sub="$1"
    case "$sub" in
        add|enroll|new)        profile_add     "$2"      ;;
        train|append|more)     profile_train   "$2"      ;;
        list|ls|"")            profile_list              ;;
        rm|remove|delete)      profile_remove  "$2"      ;;
        clusters|cluster)      profile_clusters          ;;
        assign)                profile_assign  "$2" "$3" ;;
        merge)                 profile_merge   "$2" "$3" ;;
        rename|mv)             profile_rename  "$2" "$3" ;;
        undo|pop)              profile_undo    "$2" "$3" ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/profile $sub${C[reset]}"
            print -P "  ${C[dim]}/profile add <name>${C[reset]}              enroll a new voice (3 samples)"
            print -P "  ${C[dim]}/profile train <name>${C[reset]}            add another sample"
            print -P "  ${C[dim]}/profile list${C[reset]}                    show enrolled profiles"
            print -P "  ${C[dim]}/profile rm <name>${C[reset]}               delete a profile"
            print -P "  ${C[dim]}/profile clusters${C[reset]}                show active speaker clusters"
            print -P "  ${C[dim]}/profile assign <letter> <name>${C[reset]}  cluster → profile + rewrite transcript"
            print -P "  ${C[dim]}/profile merge <from> <into>${C[reset]}     fold one cluster into another"
            ;;
    esac
}
