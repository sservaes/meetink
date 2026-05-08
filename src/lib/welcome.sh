#!/bin/zsh
# Welcome screen for meetink
# Sourced by bin/meetink.
# Required: src/lib/ui.sh already sourced

VERSION="${MEETINK_VERSION:-0.1.0}"
MK_HOME="${MEETINK_HOME:-$HOME/.meetink}"

# Inner content width — overridden per-render in welcome_screen() based on
# terminal columns. Default keeps boxline() working if other code calls it.
W=66

# State checks
_has_binary()  { [[ -x "$MK_HOME/bin/meetink-capture" ]] || [[ -x "$MK_ROOT/src/capture/meetink-capture" ]] }
_has_model()   { [[ -f "$MK_HOME/models/ggml-small.en.bin" ]] }
_has_whisper() { command -v whisper-server >/dev/null 2>&1 }
_has_diarize() { [[ -x "$MK_HOME/diarize-venv/bin/python" ]] && [[ -f "$MK_HOME/models/speaker-embedding.onnx" ]] }
_titling_backend() {
    # Mirror title_backend_active in titling.sh: env > config > default `local`.
    # Inlined here so welcome.sh stays sourceable independently of titling.sh.
    if [[ -n "$MEETINK_TITLE_BACKEND" ]]; then
        print -n -- "$MEETINK_TITLE_BACKEND"
        return
    fi
    if [[ -f "$MK_HOME/config" ]]; then
        local v=$(grep '^title_backend=' "$MK_HOME/config" 2>/dev/null | head -1 | cut -d= -f2-)
        if [[ "$v" == "claude" || "$v" == "local" ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- "local"
}

# Short, human-friendly label for the active titling model. "Sonnet", "Haiku",
# "Opus" for Claude variants, "Qwen3" for the local model.
_titling_label() {
    if [[ "$(_titling_backend)" == "claude" ]]; then
        local model="${MEETINK_CLAUDE_MODEL:-}"
        if [[ -z "$model" && -f "$MK_HOME/config" ]]; then
            model=$(grep '^claude_model=' "$MK_HOME/config" 2>/dev/null | head -1 | cut -d= -f2-)
        fi
        [[ -z "$model" ]] && model="claude-sonnet-4-6"
        case "$model" in
            *sonnet*) print -n -- "Sonnet" ;;
            *haiku*)  print -n -- "Haiku"  ;;
            *opus*)   print -n -- "Opus"   ;;
            *)        print -n -- "$model" ;;
        esac
    else
        # Read the active local model name from config; matches the registry
        # in titling.sh. Falls back to a sensible default label if config
        # isn't readable so the welcome screen never breaks.
        local active=""
        [[ -f "$MK_HOME/config" ]] && \
            active=$(grep '^local_llm_model=' "$MK_HOME/config" 2>/dev/null | head -1 | cut -d= -f2-)
        [[ -z "$active" ]] && active="qwen3.5-2b"
        # Surface the size segment ("0.8B", "9B", …) since that's the bit
        # that matters at a glance, not the whole "qwen3.5-X" prefix.
        case "$active" in
            *0.8b*) print -n -- "Qwen3.5-0.8B" ;;
            *2b*)   print -n -- "Qwen3.5-2B"   ;;
            *4b*)   print -n -- "Qwen3.5-4B"   ;;
            *9b*)   print -n -- "Qwen3.5-9B"   ;;
            *)      print -n -- "$active"      ;;
        esac
    fi
}

# Resolve the on-disk MLX snapshot directory for the active local model.
# Mirrors the registry in titling.sh — kept here so welcome.sh can decide
# whether titling is actually usable without sourcing all of titling.sh.
_active_local_llm_path() {
    local active=""
    [[ -f "$MK_HOME/config" ]] && \
        active=$(grep '^local_llm_model=' "$MK_HOME/config" 2>/dev/null | head -1 | cut -d= -f2-)
    [[ -z "$active" ]] && active="qwen3.5-2b"
    local dir
    case "$active" in
        *0.8b*) dir="Qwen3.5-0.8B-4bit" ;;
        *2b*)   dir="Qwen3.5-2B-4bit"   ;;
        *4b*)   dir="Qwen3.5-4B-4bit"   ;;
        *9b*)   dir="Qwen3.5-9B-4bit"   ;;
        *)      dir="Qwen3.5-2B-4bit"   ;;
    esac
    print -n -- "$MK_HOME/models/mlx/$dir"
}

# Titling is "available" when the active backend's deps are present. For
# local: the REPL venv has mlx_lm + the snapshot dir has config.json.
_has_titling() {
    if [[ "$(_titling_backend)" == "claude" ]]; then
        command -v claude >/dev/null 2>&1
    else
        # Cheap presence check via the venv's site-packages (avoid spawning
        # python just to print "yes"). Glob for any python* version dir.
        [[ -n "$(/bin/ls -d "$MK_PY_VENV"/lib/python*/site-packages/mlx_lm 2>/dev/null | head -1)" ]] \
            && [[ -f "$(_active_local_llm_path)/config.json" ]]
    fi
}

# Read the user's name (set via /me <name>). Empty string if unset.
_me_name() {
    [[ -f "$MK_HOME/config" ]] || { print -n -- ""; return; }
    local v=$(grep '^me_name=' "$MK_HOME/config" 2>/dev/null | head -1 | cut -d= -f2-)
    print -n -- "$v"
}
_is_running()  { [[ -f /tmp/meetink-capture.pid ]] && kill -0 "$(cat /tmp/meetink-capture.pid 2>/dev/null)" 2>/dev/null }

# Print one line of the box: |  <visible content padded to W chars>  |
# Args: visible-content-text  ansi-decorated-content
# We pass the visible-only string so we can compute padding correctly.
boxline() {
    local visible="$1"
    local decorated="${2:-$1}"
    local pad=$((W - ${#visible}))
    (( pad < 0 )) && pad=0
    printf "${C[gray]}│${C[reset]}%s%*s${C[gray]}│${C[reset]}\n" "$decorated" $pad ""
}

# Empty line
boxblank() { boxline "" ""; }

# Two-column box line. Args: left_width  left_visible  left_decorated  right_visible  right_decorated
# Use empty strings ("" "") for blank halves.
boxline2() {
    local left_w=$1
    local lv="$2"
    local ld="${3:-$2}"
    local rv="$4"
    local rd="${5:-$4}"
    local lpad=$(( left_w - ${#lv} ))
    (( lpad < 0 )) && lpad=0
    local rpad=$(( (W - left_w) - ${#rv} ))
    (( rpad < 0 )) && rpad=0
    printf "${C[gray]}│${C[reset]}%s%*s%s%*s${C[gray]}│${C[reset]}\n" \
        "$ld" $lpad "" \
        "$rd" $rpad ""
}

# Title bar at top: ╭─ meetink vX.Y.Z [• recording] ─...─╮
# Inner visible width = W. Layout: "─ {title} {dashes}"
# So: 1 (─) + 1 (sp) + len(title) + 1 (sp) + N (dashes) = W
welcome_header() {
    local title="meetink v${VERSION}"
    if _is_running; then
        title="$title  ● recording"
        local pad=$((W - ${#title} - 3))
        (( pad < 0 )) && pad=0
        printf "${C[gray]}╭─ ${C[bold]}meetink v${VERSION}${C[reset]}  ${C[green]}● recording${C[reset]}${C[gray]} %s╮${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $pad))"
    else
        local pad=$((W - ${#title} - 3))
        (( pad < 0 )) && pad=0
        printf "${C[gray]}╭─ ${C[bold]}meetink v${VERSION}${C[reset]}${C[gray]} %s╮${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $pad))"
    fi
}

welcome_footer() {
    printf "${C[gray]}╰%s╯${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $W))"
}

welcome_screen() {
    # Responsive width: fit terminal but cap at 110 for readability,
    # never go below 70 (single-column fallback works at any width).
    local cols=$(tput cols 2>/dev/null || echo 100)
    W=$(( cols - 4 ))
    (( W > 110 )) && W=110
    (( W < 70 ))  && W=70
    # 2-column tips/status layout requires room; otherwise stack vertically.
    local two_col=0
    (( W >= 100 )) && two_col=1
    local half=$(( W * 45 / 100 ))   # left ~45% / right ~55%

    print ""
    welcome_header

    boxblank
    # Block-letter MEETINK wordmark (ANSI Shadow font, hand-assembled).
    # Width = 57 visible chars + 3-char indent = 60 — fits W>=70.
    boxline "   ███╗   ███╗███████╗███████╗████████╗██╗███╗   ██╗██╗  ██╗" \
            "   ${C[bright_cyan]}███╗   ███╗███████╗███████╗████████╗██╗███╗   ██╗██╗  ██╗${C[reset]}"
    boxline "   ████╗ ████║██╔════╝██╔════╝╚══██╔══╝██║████╗  ██║██║ ██╔╝" \
            "   ${C[bright_cyan]}████╗ ████║██╔════╝██╔════╝╚══██╔══╝██║████╗  ██║██║ ██╔╝${C[reset]}"
    boxline "   ██╔████╔██║█████╗  █████╗     ██║   ██║██╔██╗ ██║█████╔╝ " \
            "   ${C[bright_cyan]}██╔████╔██║█████╗  █████╗     ██║   ██║██╔██╗ ██║█████╔╝ ${C[reset]}"
    boxline "   ██║╚██╔╝██║██╔══╝  ██╔══╝     ██║   ██║██║╚██╗██║██╔═██╗ " \
            "   ${C[bright_cyan]}██║╚██╔╝██║██╔══╝  ██╔══╝     ██║   ██║██║╚██╗██║██╔═██╗ ${C[reset]}"
    boxline "   ██║ ╚═╝ ██║███████╗███████╗   ██║   ██║██║ ╚████║██║  ██╗" \
            "   ${C[bright_cyan]}██║ ╚═╝ ██║███████╗███████╗   ██║   ██║██║ ╚████║██║  ██╗${C[reset]}"
    boxline "   ╚═╝     ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝" \
            "   ${C[bright_cyan]}╚═╝     ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝${C[reset]}"
    boxblank
    local me=$(_me_name)
    if [[ -n "$me" ]]; then
        # Personalised greeting when /me has been set.
        boxline "   Hi $me — every meeting, inked locally." \
                "   ${C[bold]}Hi ${C[bright_cyan]}$me${C[reset]}${C[bold]}${C[reset]} ${C[dim]}— every meeting, inked locally.${C[reset]}"
    else
        boxline "   Local meeting transcription · Every meeting, inked locally." \
                "   ${C[bold]}Local meeting transcription${C[reset]} ${C[dim]}· Every meeting, inked locally.${C[reset]}"
    fi
    boxblank

    # Build tip rows
    local tip1_v tip1_d tip2_v tip2_d tip3_v tip3_d
    if ! _has_binary || ! _has_whisper || ! _has_model; then
        tip1_v="   • Type \`/setup\`  to install dependencies"
        tip1_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/setup\`${C[reset]}  to install dependencies"
    else
        tip1_v="   • All set. Dependencies installed."
        tip1_d="   ${C[dim]}•${C[reset]} ${C[green]}All set.${C[reset]} Dependencies installed."
    fi
    if _is_running; then
        tip2_v="   • Type \`/tail\`   to follow the live transcript"
        tip2_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/tail\`${C[reset]}   to follow the live transcript"
        tip3_v="   • Type \`/stop\`   to stop recording"
        tip3_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/stop\`${C[reset]}   to stop recording"
    else
        tip2_v="   • Type \`/start\`  to begin transcribing"
        tip2_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/start\`${C[reset]}  to begin transcribing"
        # If the user hasn't set their name yet, surface /me as the third
        # tip — gentler than just leaving "/help" there forever and helps
        # downstream features (titling, /ask) tag transcripts with a real
        # identity rather than ME.
        if [[ -z "$me" ]]; then
            tip3_v="   • Type \`/me <name>\`  to introduce yourself"
            tip3_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/me <name>\`${C[reset]}  to introduce yourself"
        else
            tip3_v="   • Type \`/help\`   for all commands"
            tip3_d="   ${C[dim]}•${C[reset]} Type ${C[bright_cyan]}\`/help\`${C[reset]}   for all commands"
        fi
    fi

    # Build status indicators
    local b_dot m_dot w_dot d_dot t_dot
    if _has_binary;  then b_dot="${C[green]}●${C[reset]}"; else b_dot="${C[gray]}○${C[reset]}"; fi
    if _has_model;   then m_dot="${C[green]}●${C[reset]}"; else m_dot="${C[gray]}○${C[reset]}"; fi
    if _has_whisper; then w_dot="${C[green]}●${C[reset]}"; else w_dot="${C[gray]}○${C[reset]}"; fi
    if _has_diarize; then d_dot="${C[green]}●${C[reset]}"; else d_dot="${C[gray]}○${C[reset]}"; fi
    if _has_titling; then t_dot="${C[green]}●${C[reset]}"; else t_dot="${C[gray]}○${C[reset]}"; fi

    local b_label m_label w_label d_label t_label t_label_v t_suffix_v t_suffix_d
    if _has_binary;  then b_label="capture binary";  else b_label="${C[dim]}capture binary${C[reset]}";  fi
    if _has_model;   then m_label="whisper model";   else m_label="${C[dim]}whisper model${C[reset]}";   fi
    if _has_whisper; then w_label="whisper-cpp";     else w_label="${C[dim]}whisper-cpp${C[reset]}";     fi
    if _has_diarize; then d_label="speaker ID";      else d_label="${C[dim]}speaker ID${C[reset]}";      fi
    # When titling is available, replace the generic "AI titling (optional)"
    # with the active model identity ("Sonnet titling", "Qwen3 titling", …).
    # Both the visible string `t_label_v` and the colored `t_label` must have
    # the same visible width so boxline2 pads correctly.
    if _has_titling; then
        local lbl=$(_titling_label)
        t_label_v="${lbl} titling"
        t_label="${lbl} titling"
        t_suffix_v=""
        t_suffix_d=""
    else
        t_label_v="AI titling"
        t_label="${C[dim]}AI titling${C[reset]}"
        t_suffix_v="   (optional)"
        t_suffix_d="   ${C[dim]}(optional)${C[reset]}"
    fi

    local req_v="   ● capture binary    ● whisper model    ● whisper-cpp"
    local req_d="   ${b_dot} ${b_label}    ${m_dot} ${m_label}    ${w_dot} ${w_label}"
    local opt_v="   ○ speaker ID        ○ ${t_label_v}${t_suffix_v}"
    local opt_d="   ${d_dot} ${d_label}        ${t_dot} ${t_label}${t_suffix_d}"

    if (( two_col )); then
        boxline2 $half \
            "   Tips for getting started" \
            "   ${C[bright_yellow]}Tips for getting started${C[reset]}" \
            "   Status" \
            "   ${C[bright_yellow]}Status${C[reset]}"
        boxline2 $half "$tip1_v" "$tip1_d" "$req_v" "$req_d"
        boxline2 $half "$tip2_v" "$tip2_d" ""        ""
        boxline2 $half "$tip3_v" "$tip3_d" "$opt_v" "$opt_d"
    else
        boxline "   Tips for getting started" \
                "   ${C[bright_yellow]}Tips for getting started${C[reset]}"
        boxline "$tip1_v" "$tip1_d"
        boxline "$tip2_v" "$tip2_d"
        boxline "$tip3_v" "$tip3_d"
        boxblank
        boxline "   Status" "   ${C[bright_yellow]}Status${C[reset]}"
        boxline "$req_v" "$req_d"
        boxline "$opt_v" "$opt_d"
    fi
    boxblank

    welcome_footer
    print ""
    print -P "  ${C[dim]}Made for macOS · Apple Silicon optimized · ${MK_HOME/$HOME/~}${C[reset]}"
    print ""
}
