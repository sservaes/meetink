#!/bin/zsh
# UI helpers — colors, ASCII art, boxes
# Sourced by bin/local-speech

# Colors
typeset -gA C
C[reset]=$'\033[0m'
C[bold]=$'\033[1m'
C[dim]=$'\033[2m'
C[italic]=$'\033[3m'
C[red]=$'\033[31m'
C[green]=$'\033[32m'
C[yellow]=$'\033[33m'
C[blue]=$'\033[34m'
C[magenta]=$'\033[35m'
C[cyan]=$'\033[36m'
C[white]=$'\033[37m'
C[gray]=$'\033[90m'
C[bright_cyan]=$'\033[96m'
C[bright_magenta]=$'\033[95m'
C[bright_yellow]=$'\033[93m'

# Disable colors if not a TTY or NO_COLOR is set
if [[ ! -t 1 ]] || [[ -n "$NO_COLOR" ]]; then
    for k in ${(k)C}; do C[$k]=""; done
fi

# Block-letter logo for "local-speech"
ls_logo() {
    print -P "${C[bright_cyan]}    █     ███   ███ █████ █     █████ ████ █████ █████ ███ █   █${C[reset]}"
    print -P "${C[bright_cyan]}    █    █   █ █    █   █ █     █     █  █ █     █     █   █   █${C[reset]}"
    print -P "${C[bright_cyan]}    █    █   █ █    █████ █     █████ ████ █████ █████ █   █████${C[reset]}"
    print -P "${C[bright_cyan]}    █    █   █ █    █   █ █     ${C[dim]}── ${C[bright_cyan]}█     █     █     █   █${C[reset]}"
    print -P "${C[bright_cyan]}    █████ ███   ███ █   █ █████ █     ████ █████ █████ ███ █   █${C[reset]}"
}

# Smaller mark: "Ls~"
ls_mark() {
    print -P "${C[bright_cyan]}╭─────╮${C[reset]}"
    print -P "${C[bright_cyan]}│${C[reset]} ${C[bold]}Ls${C[reset]}${C[dim]}~${C[reset]}${C[bright_cyan]} │${C[reset]}"
    print -P "${C[bright_cyan]}╰─────╯${C[reset]}"
}

# Print N spaces
spaces() { printf "%*s" "$1" ""; }

# Print a horizontal line of N width
hline() { printf "%${1}s" "" | tr ' ' "${2:-─}"; }

# Box-drawing helpers
box_top()    { print -P "${C[gray]}╭─${1}─${2}╮${C[reset]}" }
box_bottom() { print -P "${C[gray]}╰${1}${2}╯${C[reset]}" }
box_side()   { print -P "${C[gray]}│${C[reset]}" }

# Header line in a box: "─ TITLE ─────────"
box_header() {
    local title="$1"
    local width="${2:-60}"
    local title_len=${#title}
    local rest=$((width - title_len - 4))
    if (( rest < 1 )); then rest=1; fi
    printf "${C[gray]}╭─ ${C[bold]}%s${C[reset]}${C[gray]} %s╮${C[reset]}\n" "$title" "$(hline $rest)"
}

# Bottom of a box at given width
box_footer() {
    local width="${1:-60}"
    printf "${C[gray]}╰%s╯${C[reset]}\n" "$(hline $((width - 2)))"
}

# Bullet item with colored marker
bullet() {
    local label="$1"
    local desc="$2"
    print -P "  ${C[bright_yellow]}•${C[reset]} ${C[bold]}${label}${C[reset]}  ${C[dim]}${desc}${C[reset]}"
}

# Section heading
heading() {
    print -P "${C[bright_yellow]}${C[bold]}$1${C[reset]}"
}

# Colored status dot
dot_green()  { printf "${C[green]}●${C[reset]}" }
dot_red()    { printf "${C[red]}●${C[reset]}" }
dot_yellow() { printf "${C[yellow]}●${C[reset]}" }
dot_gray()   { printf "${C[gray]}○${C[reset]}" }
