#!/bin/zsh
# Projects: per-meeting context bundles.
#
# A project is just a subdirectory of $MK_TRANSCRIPTS_BASE. When a project
# is "active" (persisted as `active_project=` in $MK_CONFIG_FILE), /start
# writes recordings into <base>/<project>/ and the live.txt symlink lives
# in there too. Without an active project, recordings go to <base>/ as
# before — top-level transcripts are the implicit "default project".
#
# The structure is intentionally minimal:
#
#   $MK_TRANSCRIPTS_BASE/
#   ├─ 2026-05-07_*.txt          ← default project (no folder)
#   ├─ live.txt                  ← symlink to current session
#   └─ <project>/                ← named project
#      ├─ 2026-05-07_*.txt       ← this project's transcripts
#      ├─ live.txt               ← active when this project is the active one
#      └─ _context/              ← (future) PDFs/.md to load for /ask
#
# Sourced by bin/meetink AFTER models.sh (which defines $MK_CONFIG_FILE)
# and BEFORE the `case "$1"` dispatch (so project_resolve_dirs can adjust
# $MK_TRANSCRIPTS_DIR / $MK_TRANSCRIPT for the rest of the run).

# Read the active project name. Empty string = default (top-level) project.
project_active_get() {
    if [[ -f "$MK_CONFIG_FILE" ]]; then
        local v=$(grep '^active_project=' "$MK_CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
        # Reject anything containing slashes or dots — these would break
        # the path construction. Treat as no project.
        if [[ -n "$v" && "$v" != *.* && "$v" != */* ]]; then
            print -n -- "$v"
            return
        fi
    fi
    print -n -- ""
}

project_active_set() {
    local name="$1"   # empty = clear
    mkdir -p "${MK_CONFIG_FILE:h}"
    if [[ -f "$MK_CONFIG_FILE" ]] && grep -q '^active_project=' "$MK_CONFIG_FILE"; then
        if [[ -z "$name" ]]; then
            # Clear: drop the line entirely so `git diff config` is clean.
            sed -i '' '/^active_project=/d' "$MK_CONFIG_FILE"
        else
            sed -i '' "s|^active_project=.*|active_project=$name|" "$MK_CONFIG_FILE"
        fi
    elif [[ -n "$name" ]]; then
        echo "active_project=$name" >> "$MK_CONFIG_FILE"
    fi
}

# Override $MK_TRANSCRIPTS_DIR / $MK_TRANSCRIPT to point at the active
# project's folder. No-op when no project is active or when the user
# pinned $MEETINK_TRANSCRIPT explicitly. Idempotent — call once at startup.
project_resolve_dirs() {
    [[ -n "$MEETINK_TRANSCRIPT" ]] && return 0  # user pin wins
    local active=$(project_active_get)
    [[ -z "$active" ]] && return 0
    MK_TRANSCRIPTS_DIR="$MK_TRANSCRIPTS_BASE/$active"
    MK_TRANSCRIPT="$MK_TRANSCRIPTS_DIR/live.txt"
}

# List existing projects (subdirs of $MK_TRANSCRIPTS_BASE), one name per line.
# Hidden dirs and "_context"-style internals are filtered out.
project_list_names() {
    [[ -d "$MK_TRANSCRIPTS_BASE" ]] || return 0
    setopt local_options null_glob
    local d
    for d in "$MK_TRANSCRIPTS_BASE"/*(/N); do
        local name="${d:t}"
        [[ "$name" == .* || "$name" == _* ]] && continue
        print -- "$name"
    done
}

project_list() {
    local active=$(project_active_get)
    print -P ""
    print -P "${C[bright_yellow]}PROJECTS${C[reset]} ${C[dim]}(${MK_TRANSCRIPTS_BASE/$HOME/~})${C[reset]}"
    local any=0
    local name
    while IFS= read -r name; do
        any=1
        if [[ "$name" == "$active" ]]; then
            print -P "  ${C[green]}●${C[reset]} ${C[bold]}${name}${C[reset]}  ${C[dim]}← active${C[reset]}"
        else
            print -P "  ${C[gray]}○${C[reset]} ${name}"
        fi
    done < <(project_list_names)
    if (( ! any )); then
        print -P "  ${C[dim]}(none — create one with /project use <name>)${C[reset]}"
    fi
    if [[ -z "$active" ]]; then
        print -P ""
        print -P "  ${C[dim]}No active project — recordings go to ${MK_TRANSCRIPTS_BASE/$HOME/~}/${C[reset]}"
    fi
    print -P ""
    print -P "  ${C[dim]}/project use <name>${C[reset]}    activate (creates folder if new)"
    print -P "  ${C[dim]}/project clear${C[reset]}         go back to the default project"
    print -P "  ${C[dim]}/project rm <name>${C[reset]}     delete a project folder"
    print -P ""
}

project_use() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /project use <name>"
        return 1
    fi
    if [[ "$name" == *.* || "$name" == */* ]]; then
        print -P "${C[red]}error:${C[reset]} no slashes or dots in project names"
        return 1
    fi
    if _is_running; then
        print -P "${C[red]}error:${C[reset]} can't switch projects while recording. Run ${C[bright_cyan]}/stop${C[reset]} first."
        return 1
    fi
    local dir="$MK_TRANSCRIPTS_BASE/$name"
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir" || return 1
        print -P "${C[green]}✓${C[reset]} Created project ${C[bold]}$name${C[reset]} ${C[dim]}(${dir/$HOME/~})${C[reset]}"
    fi
    project_active_set "$name"
    print -P "${C[green]}✓${C[reset]} Active project: ${C[bold]}$name${C[reset]}"
    print -P "  ${C[dim]}/start${C[reset]} will record into ${C[dim]}${dir/$HOME/~}/${C[reset]}"
}

project_clear() {
    if _is_running; then
        print -P "${C[red]}error:${C[reset]} can't change projects while recording. Run ${C[bright_cyan]}/stop${C[reset]} first."
        return 1
    fi
    project_active_set ""
    print -P "${C[green]}✓${C[reset]} Cleared active project — recordings go to ${C[dim]}${MK_TRANSCRIPTS_BASE/$HOME/~}/${C[reset]}"
}

project_rm() {
    local name="$1"
    if [[ -z "$name" ]]; then
        print -P "${C[red]}usage:${C[reset]} /project rm <name>"
        return 1
    fi
    if [[ "$name" == *.* || "$name" == */* ]]; then
        print -P "${C[red]}error:${C[reset]} no slashes or dots in project names"
        return 1
    fi
    local dir="$MK_TRANSCRIPTS_BASE/$name"
    if [[ ! -d "$dir" ]]; then
        print -P "${C[red]}error:${C[reset]} no project named ${C[bold]}$name${C[reset]}"
        return 1
    fi
    if [[ "$(project_active_get)" == "$name" ]]; then
        print -P "${C[red]}error:${C[reset]} can't delete the active project. Run ${C[bright_cyan]}/project clear${C[reset]} first."
        return 1
    fi
    # Count what we're about to delete so the user can sanity-check.
    setopt local_options null_glob
    local txts=("$dir"/*.txt(N))
    local n=${#txts}
    print -P "${C[bright_yellow]}⚠${C[reset]}  This will delete ${C[bold]}${dir/$HOME/~}${C[reset]} (${n} transcript$([[ $n != 1 ]] && print -n s)) — type the project name to confirm:"
    print -nP "  > "
    local confirm
    read -r confirm
    if [[ "$confirm" != "$name" ]]; then
        print -P "  ${C[dim]}cancelled${C[reset]}"
        return 1
    fi
    rm -rf "$dir"
    print -P "${C[green]}✓${C[reset]} Removed ${C[bold]}$name${C[reset]}"
}

# /project dispatch. Direct $2 indexing — bin/meetink runs under set -e
# so we can't `shift` without args.
cmd_project() {
    local sub="$1"
    case "$sub" in
        ""|list|ls)            project_list      ;;
        use|switch|activate)   project_use   "$2" ;;
        clear|none|default)    project_clear     ;;
        rm|remove|delete)      project_rm    "$2" ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/project $sub${C[reset]}"
            print -P "  ${C[dim]}/project${C[reset]}                     list projects"
            print -P "  ${C[dim]}/project use <name>${C[reset]}          activate (creates folder if new)"
            print -P "  ${C[dim]}/project clear${C[reset]}               go back to default"
            print -P "  ${C[dim]}/project rm <name>${C[reset]}           delete a project folder"
            ;;
    esac
}
