#!/bin/zsh
# User identity. Lets meetink know who's behind the mic.
#
# Persisted in $MK_CONFIG_FILE as `me_name=Stijn`. Two effects:
#   1. main.swift reads MEETINK_ME_NAME and labels mic-stream lines
#      `STIJN:` instead of `ME:` (still uppercased — same convention as
#      diarize-server speaker labels).
#   2. The transcript header embeds `# user: Stijn` so downstream tooling
#      (titling, future /ask, post-hoc rewrites) can resolve who the
#      mic-stream speaker actually is.
#
# Sourced by bin/meetink AFTER models.sh (for $MK_CONFIG_FILE).

# Read the configured name. Empty string = none set, fall back to "ME".
me_name_get() {
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^me_name=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        if [[ -n "$v" ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- ""
}

me_name_set() {
    local name="$1"   # empty = clear
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^me_name=' "$MK_CONFIG_FILE"; then
        if [[ -z "$name" ]]; then
            sed -i '' '/^me_name=/d' "$MK_CONFIG_FILE"
        else
            sed -i '' "s|^me_name=.*|me_name=$name|" "$MK_CONFIG_FILE"
        fi
    elif [[ -n "$name" ]]; then
        echo "me_name=$name" >> "$MK_CONFIG_FILE"
    fi
}

# /me dispatch.
#   /me                 → show current
#   /me <name>          → set
#   /me clear|none      → unset (back to "ME")
cmd_me() {
    local arg="$1"
    case "$arg" in
        "")
            local cur=$(me_name_get)
            if [[ -n "$cur" ]]; then
                print -P "  You are: ${C[bold]}$cur${C[reset]} ${C[dim]}(mic stream labelled ${cur:u}: in transcripts)${C[reset]}"
            else
                print -P "  ${C[dim]}No name set — mic stream labelled${C[reset]} ${C[bold]}ME:${C[reset]}"
            fi
            print -P "  ${C[dim]}/me <name>${C[reset]}     set your name"
            print -P "  ${C[dim]}/me clear${C[reset]}      unset (back to ME)"
            ;;
        clear|none|unset)
            me_name_set ""
            print -P "${C[green]}✓${C[reset]} Cleared — mic stream labelled ${C[bold]}ME:${C[reset]} again"
            ;;
        *)
            if [[ "$arg" == *.* || "$arg" == */* || "$arg" == *" "* ]]; then
                print -P "${C[red]}error:${C[reset]} no spaces, slashes, or dots in names"
                return 1
            fi
            me_name_set "$arg"
            print -P "${C[green]}✓${C[reset]} Hi ${C[bold]}$arg${C[reset]} — mic stream will be labelled ${C[bold]}${arg:u}:${C[reset]} from the next /start"
            if _is_running; then
                print -P "  ${C[dim]}(takes effect on the next recording — current session keeps its labels)${C[reset]}"
            fi
            ;;
    esac
}
