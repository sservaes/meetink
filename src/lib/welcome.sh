#!/bin/zsh
# Welcome screen for local-speech
# Sourced by bin/local-speech.
# Required: src/lib/ui.sh already sourced

VERSION="${LOCAL_SPEECH_VERSION:-0.1.0}"
LS_HOME="${LOCAL_SPEECH_HOME:-$HOME/.local-speech}"

# Inner content width: 66 chars between the side bars
W=66

# State checks
_has_binary()  { [[ -x "$LS_HOME/bin/local-speech-capture" ]] || [[ -x "$LS_ROOT/src/capture/local-speech-capture" ]] }
_has_model()   { [[ -f "$LS_HOME/models/ggml-small.en.bin" ]] }
_has_whisper() { command -v whisper-server >/dev/null 2>&1 }
_is_running()  { [[ -f /tmp/local-speech-capture.pid ]] && kill -0 "$(cat /tmp/local-speech-capture.pid 2>/dev/null)" 2>/dev/null }

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

# Title bar at top: ╭─ local·speech vX.Y.Z [• recording] ─...─╮
# Inner visible width = W. Layout: "─ {title} {dashes}"
# So: 1 (─) + 1 (sp) + len(title) + 1 (sp) + N (dashes) = W
welcome_header() {
    local title="local·speech v${VERSION}"
    if _is_running; then
        title="$title  ● recording"
        local pad=$((W - ${#title} - 3))
        (( pad < 0 )) && pad=0
        printf "${C[gray]}╭─ ${C[bold]}local·speech v${VERSION}${C[reset]}  ${C[green]}● recording${C[reset]}${C[gray]} %s╮${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $pad))"
    else
        local pad=$((W - ${#title} - 3))
        (( pad < 0 )) && pad=0
        printf "${C[gray]}╭─ ${C[bold]}local·speech v${VERSION}${C[reset]}${C[gray]} %s╮${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $pad))"
    fi
}

welcome_footer() {
    printf "${C[gray]}╰%s╯${C[reset]}\n" "$(printf '─%.0s' $(seq 1 $W))"
}

welcome_screen() {
    print ""
    welcome_header

    boxblank
    boxline "      ██╗     ███████╗" \
            "      ${C[bright_cyan]}██╗     ███████╗${C[reset]}"
    boxline "      ██║     ██╔════╝     Local-first speech-to-text" \
            "      ${C[bright_cyan]}██║     ██╔════╝${C[reset]}     ${C[bold]}Local-first speech-to-text${C[reset]}"
    boxline "      ██║     ███████╗     for the macOS terminal" \
            "      ${C[bright_cyan]}██║     ███████╗${C[reset]}     ${C[dim]}for the macOS terminal${C[reset]}"
    boxline "      ██║     ╚════██║" \
            "      ${C[bright_cyan]}██║     ╚════██║${C[reset]}"
    boxline "      ███████╗███████║" \
            "      ${C[bright_cyan]}███████╗███████║${C[reset]}"
    boxline "      ╚══════╝╚══════╝" \
            "      ${C[bright_cyan]}╚══════╝╚══════╝${C[reset]}"
    boxblank

    boxline "   Tips for getting started" \
            "   ${C[bright_yellow]}Tips for getting started${C[reset]}"
    if ! _has_binary || ! _has_whisper || ! _has_model; then
        boxline "   • Run \`local-speech setup\`  to install dependencies" \
                "   ${C[dim]}•${C[reset]} Run ${C[bright_cyan]}\`local-speech setup\`${C[reset]}  to install dependencies"
    else
        boxline "   • All set. Dependencies installed." \
                "   ${C[dim]}•${C[reset]} ${C[green]}All set.${C[reset]} Dependencies installed."
    fi
    if _is_running; then
        boxline "   • Run \`local-speech tail\`   to follow the live transcript" \
                "   ${C[dim]}•${C[reset]} Run ${C[bright_cyan]}\`local-speech tail\`${C[reset]}   to follow the live transcript"
        boxline "   • Run \`local-speech stop\`   to stop recording" \
                "   ${C[dim]}•${C[reset]} Run ${C[bright_cyan]}\`local-speech stop\`${C[reset]}   to stop recording"
    else
        boxline "   • Run \`local-speech start\`  to begin transcribing" \
                "   ${C[dim]}•${C[reset]} Run ${C[bright_cyan]}\`local-speech start\`${C[reset]}  to begin transcribing"
        boxline "   • Run \`local-speech help\`   for all commands" \
                "   ${C[dim]}•${C[reset]} Run ${C[bright_cyan]}\`local-speech help\`${C[reset]}   for all commands"
    fi
    boxblank

    boxline "   Status" "   ${C[bright_yellow]}Status${C[reset]}"

    # Status line: 3 indicators
    local b_dot m_dot w_dot
    if _has_binary;  then b_dot="${C[green]}●${C[reset]}"; else b_dot="${C[gray]}○${C[reset]}"; fi
    if _has_model;   then m_dot="${C[green]}●${C[reset]}"; else m_dot="${C[gray]}○${C[reset]}"; fi
    if _has_whisper; then w_dot="${C[green]}●${C[reset]}"; else w_dot="${C[gray]}○${C[reset]}"; fi

    local b_label m_label w_label
    if _has_binary;  then b_label="capture binary";  else b_label="${C[dim]}capture binary${C[reset]}"; fi
    if _has_model;   then m_label="whisper model";   else m_label="${C[dim]}whisper model${C[reset]}"; fi
    if _has_whisper; then w_label="whisper-cpp";     else w_label="${C[dim]}whisper-cpp${C[reset]}"; fi

    boxline "   ● capture binary    ● whisper model    ● whisper-cpp" \
            "   ${b_dot} ${b_label}    ${m_dot} ${m_label}    ${w_dot} ${w_label}"
    boxblank

    welcome_footer
    print ""
    print -P "  ${C[dim]}Made for macOS · Apple Silicon optimized · ${LS_HOME/$HOME/~}${C[reset]}"
    print ""
}
