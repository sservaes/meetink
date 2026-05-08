#!/bin/zsh
# Interactive REPL for meetink.
#
# Entered when bin/meetink is launched with no arguments at a TTY. Wraps
# the existing cmd_* functions in subshells so any `exit` they call won't
# kill the loop, and disables errexit locally so a failing command just
# prints and returns to the prompt.
#
# Sourced by bin/meetink. Depends on:
#   - cmd_start, cmd_stop, cmd_status from bin/meetink
#   - _is_running from src/lib/welcome.sh
#   - C[] color table from src/lib/ui.sh
#   - window_open_tail, window_close_tail, window_tail_exists from src/lib/window.sh

# Canonical slash commands offered by Tab-completion. Aliases like /q, /exit,
# /follow, /vocab, /ls, /cls, /h, /? still work in dispatch — they're just
# omitted from the completion menu to keep it tidy.
typeset -ga MK_SLASH_COMMANDS=(
    /start /stop /status /tail /prompt /transcripts
    /model /llm /diarize /profile
    /setup /clear /help /quit
)

# Tab-completion widget. Bound to ^I in repl_loop.
#   - Empty buffer + Tab → list all commands
#   - "/x" + Tab with one match → complete to "/xxx " (trailing space)
#   - "/x" + Tab with multiple matches → extend to longest common prefix,
#     then on second Tab show options below the prompt
#   - No match → bell
_mk_complete() {
    local prefix="$LBUFFER"
    local -a matches

    if [[ -z "$prefix" ]]; then
        matches=("${MK_SLASH_COMMANDS[@]}")
    elif [[ "$prefix" == /* ]]; then
        matches=(${(M)MK_SLASH_COMMANDS:#${prefix}*})
    else
        # User is typing non-slash text — leave it alone
        return
    fi

    if (( ${#matches[@]} == 0 )); then
        zle beep
        return
    elif (( ${#matches[@]} == 1 )); then
        LBUFFER="${matches[1]} "
        return
    fi

    # Find longest common prefix among matches.
    local common="${matches[1]}"
    local m
    for m in "${matches[@]:1}"; do
        while [[ -n "$common" && "$m" != "$common"* ]]; do
            common="${common%?}"
        done
        [[ -z "$common" ]] && break
    done

    if [[ -n "$common" && "$common" != "$prefix" ]]; then
        LBUFFER="$common"
    else
        zle -M "${(j: :)matches}"
    fi
}

# Banner shown on REPL entry when a recording is already in progress.
repl_attach_banner() {
    _is_running || return 0
    local pid=$(< "$PID_FILE")
    local lines=0
    [[ -f "$MK_TRANSCRIPT" ]] && lines=$(wc -l < "$MK_TRANSCRIPT" | tr -d ' ')
    print -P "${C[green]}●${C[reset]} ${C[bold]}Attaching to active recording${C[reset]} ${C[dim]}(PID $pid, $lines lines)${C[reset]}"
    print -P "  ${C[dim]}Transcript:${C[reset]} ${C[bright_cyan]}$MK_TRANSCRIPT${C[reset]}"
    print ""
}

# --- Multi-line prompt with live status footer ---
#
# build_prompt emits a two-line string: line 1 is the status footer, line 2
# is the input prompt. Used as vared's -p argument with single quotes so
# PROMPT_SUBST re-runs the function on every redraw — including the 1-second
# TMOUT/TRAPALRM ticks for the elapsed-time counter.
#
# This is *not* pinned to the bottom of the terminal. It sits at the input
# position and scrolls up with output. We tried scroll-region pinning across
# multiple iterations; ZLE's cursor management plus the DEC save/restore
# create artifacts on real terminals that no amount of clamping fixed
# reliably. This shape works.

# Footer line: model | dir | recording state | line count | speaker-ID indicator.
footer_string() {
    local model="$(model_active)"
    local dir_short="${MK_TRANSCRIPTS_DIR/$HOME/~}"
    local sep='%F{8}│%f'
    local rec_part=""
    if _is_running; then
        local start_time=$(stat -f "%m" "$PID_FILE" 2>/dev/null)
        local now=$(date +%s)
        local elapsed=$(( now - ${start_time:-now} ))
        (( elapsed < 0 )) && elapsed=0
        local time_str=$(printf "%02d:%02d" $((elapsed / 60)) $((elapsed % 60)))
        local lines=0
        [[ -f "$MK_TRANSCRIPT" ]] && lines=$(wc -l < "$MK_TRANSCRIPT" 2>/dev/null | tr -d ' ')
        if (( lines > 0 )); then
            rec_part="%F{green}● ${time_str}%f ${sep} %F{8}${lines} lines%f"
        else
            rec_part="%F{green}● ${time_str}%f"
        fi
    else
        rec_part="%F{8}○ idle%f"
    fi

    # Speaker-ID indicator. Only shown when the sidecar is installed.
    local diarize_part=""
    if diarize_available 2>/dev/null; then
        if [[ "$(diarize_enabled_get)" != "true" ]]; then
            diarize_part=" ${sep} %F{8}👤 off%f"
        elif diarize_running; then
            local pcount=$(profile_count)
            diarize_part=" ${sep} %F{cyan}👤 ${pcount}%f"
        else
            diarize_part=" ${sep} %F{yellow}👤 …%f"
        fi
    fi

    print -n -- "%F{cyan}🎙 ${model}%f ${sep} %F{8}📁 ${dir_short}%f ${sep} ${rec_part}${diarize_part}"
}

# Two-line prompt: footer above, input below. Re-evaluated each redraw under
# PROMPT_SUBST so the time counter ticks live.
build_prompt() {
    local dot
    if _is_running; then
        dot='%F{green}●%f'
    else
        dot='%F{8}○%f'
    fi
    local footer="$(footer_string)"
    local input_line="%F{cyan}meetink%f ${dot} %F{8}>%f "
    printf "%s\n%s" "$footer" "$input_line"
}

# Backwards-compat alias.
repl_prompt_string() { build_prompt }

repl_help() {
    print ""
    print -P "${C[bright_yellow]}COMMANDS${C[reset]}"
    print -P "  ${C[bold]}/start${C[reset]}        ${C[dim]}begin recording (auto-opens transcript window)${C[reset]}"
    print -P "  ${C[bold]}/stop${C[reset]}         ${C[dim]}stop recording (closes transcript window)${C[reset]}"
    print -P "  ${C[bold]}/status${C[reset]}       ${C[dim]}show recording state and line count${C[reset]}"
    print -P "  ${C[bold]}/tail${C[reset]}         ${C[dim]}open or raise the live transcript window${C[reset]}"
    print -P "  ${C[bold]}/prompt${C[reset]}       ${C[dim]}edit custom whisper vocabulary in \$EDITOR${C[reset]}"
    print -P "  ${C[bold]}/transcripts${C[reset]}  ${C[dim]}list past transcripts${C[reset]}"
    print -P "  ${C[bold]}/model${C[reset]}        ${C[dim]}list/switch/download whisper models (/model use small.en)${C[reset]}"
    print -P "  ${C[bold]}/llm${C[reset]}          ${C[dim]}install/remove the AI-titling LLM (auto-titles transcripts)${C[reset]}"
    print -P "  ${C[bold]}/diarize${C[reset]}      ${C[dim]}install/manage the speaker-ID sidecar${C[reset]}"
    print -P "  ${C[bold]}/profile${C[reset]}      ${C[dim]}enroll voices: /profile add <name> | list | train | rm${C[reset]}"
    print -P "  ${C[bold]}/setup${C[reset]}        ${C[dim]}install dependencies + download whisper model${C[reset]}"
    print -P "  ${C[bold]}/clear${C[reset]}        ${C[dim]}clear screen${C[reset]}"
    print -P "  ${C[bold]}/help${C[reset]}         ${C[dim]}show this list${C[reset]}"
    print -P "  ${C[bold]}/quit${C[reset]}         ${C[dim]}exit (recording continues if active; re-launch to attach)${C[reset]}"
    print ""
}

# Dispatch one input line. Returns 0 to continue REPL, 1 to exit.
repl_dispatch() {
    local input="$1"
    local cmd args
    # zsh splits on IFS (whitespace) — first word goes to cmd, rest to args
    read -r cmd args <<< "$input"

    case "$cmd" in
        "")
            ;;
        /start)
            if ( cmd_start ); then
                window_open_tail "$MK_TRANSCRIPT"
            fi
            ;;
        /stop)
            window_close_tail
            ( cmd_stop ) || true
            ;;
        /status)
            ( cmd_status ) || true
            ;;
        /setup)
            ( cmd_setup ) || true
            ;;
        /model)
            # Pass through subcommand args ($args was set by `read` above)
            cmd_model ${=args}
            ;;
        /llm)
            cmd_llm ${=args}
            ;;
        /diarize)
            cmd_diarize ${=args}
            ;;
        /profile|/profiles)
            cmd_profile ${=args}
            ;;
        /tail|/follow)
            window_open_tail "$MK_TRANSCRIPT"
            ;;
        /prompt|/vocab)
            mkdir -p "${MK_PROMPT:h}"
            [[ -f "$MK_PROMPT" ]] || touch "$MK_PROMPT"
            # Open in TextEdit (or whatever the user's default .txt handler
            # is). `open -t` returns immediately, so the REPL stays interactive
            # while they edit. The Swift capture binary re-reads the prompt
            # file on each chunk, so changes apply mid-meeting.
            open -t "$MK_PROMPT"
            print -P "  ${C[dim]}Opened${C[reset]} ${C[bright_cyan]}$MK_PROMPT${C[reset]} ${C[dim]}— save the file and changes apply on the next chunk.${C[reset]}"
            ;;
        /transcripts|/ls)
            local dir="$MK_TRANSCRIPTS_DIR"
            if [[ ! -d "$dir" ]] || [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
                print -P "${C[dim]}No transcripts in ${dir/$HOME/~}${C[reset]}"
            else
                print -P "${C[dim]}${dir/$HOME/~}/${C[reset]}"
                ls -lhT "$dir" 2>/dev/null || ls -lh "$dir"
            fi
            ;;
        /clear|/cls)
            clear
            ;;
        /help|/\?|/h)
            repl_help
            ;;
        /quit|/exit|/q|:q)
            return 1
            ;;
        *)
            if [[ "${cmd[1]}" == "/" ]]; then
                print -P "${C[red]}unknown:${C[reset]} ${C[dim]}$cmd${C[reset]}  ${C[dim]}— try${C[reset]} ${C[bright_cyan]}/help${C[reset]}"
            else
                print -P "${C[dim]}commands start with / — try${C[reset]} ${C[bright_cyan]}/help${C[reset]}"
            fi
            ;;
    esac
    return 0
}

# When exiting with an active recording, ask whether to stop or detach.
repl_handle_active_on_exit() {
    _is_running || return 0
    print ""
    print -P "${C[yellow]}Recording is still active.${C[reset]}"
    print -nP "  ${C[bold]}(s)${C[reset]}top recording, or ${C[bold]}(d)${C[reset]}etach? "
    local choice
    if read -k1 choice 2>/dev/null; then
        print ""
        case "$choice" in
            s|S)
                window_close_tail
                ( cmd_stop ) || true
                ;;
            *)
                print -P "${C[dim]}Detaching. Recording continues — re-run \`meetink\` to attach.${C[reset]}"
                ;;
        esac
    fi
}

# Main REPL loop. Uses zsh's `vared` for input so we get line editing,
# Tab-completion of slash commands, and up-arrow recall of recent inputs.
# Live multi-line prompt: the status footer is line 1 of the prompt, the
# input is line 2. Re-rendered on a 1-second TMOUT/TRAPALRM tick under
# PROMPT_SUBST so the elapsed-time counter advances live.
#
# Note: the footer is *not* pinned to the bottom of the terminal — it sits
# at the prompt position and scrolls up with output. We tried scroll-region
# pinning across multiple iterations; ZLE's cursor management interleaves
# badly with raw cursor save/restore on real terminals.
repl_loop() {
    # Subcommand failures shouldn't kill the loop. PROMPT_SUBST lets vared's
    # `-p '$(build_prompt)'` re-evaluate the prompt on every redraw.
    setopt local_options no_errexit prompt_subst

    welcome_screen
    # Defensive cleanup: if a previous (buggy) run left our marker title on
    # this very window, clear it now so /stop can never match self.
    window_clear_self_title
    # One-time migration of legacy ~/.meetink/transcripts/ to the new
    # visible Documents location (no-op once moved).
    migrate_transcripts
    repl_attach_banner

    # Wire up the line editor (vared is a zsh builtin, no autoload needed):
    #   - emacs key bindings (consistent regardless of user's shell config)
    #   - register the slash-command completer as a ZLE widget
    #   - bind Tab to it
    bindkey -e
    zle -N _mk_complete 2>/dev/null
    bindkey '^I' _mk_complete

    # Boot the speaker-ID sidecar early so /profile add works before /start.
    # Quietly skipped if not installed.
    diarize_start

    # Live footer: ZLE wakes on a 1-second TMOUT and SIGALRM fires our trap,
    # which asks ZLE to redraw the prompt. Under PROMPT_SUBST that re-runs
    # build_prompt so the footer reflects current state (elapsed time, line
    # count, etc.). zle reset-prompt is a no-op outside ZLE so it's safe
    # even when a subcommand is running.
    TMOUT=1
    TRAPALRM() { zle reset-prompt }

    # Ctrl+C: vared returns non-zero; we use this trap to distinguish it
    # from EOF / other errors and clear-the-line.
    typeset -g _repl_interrupted=0
    trap '_repl_interrupted=1' INT

    local line=""
    while true; do
        _repl_interrupted=0
        line=""
        # Single quotes: deferred expansion. With PROMPT_SUBST set, vared
        # re-runs `build_prompt` on each redraw (per keystroke + per TMOUT).
        if ! vared -c -p '$(build_prompt)' line; then
            if (( _repl_interrupted )); then
                # Ctrl+C — discard input, fresh prompt
                print ""
                continue
            fi
            # vared error / EOF — exit the REPL
            print ""
            break
        fi
        # Add to in-session history so up-arrow recalls past commands.
        [[ -n "$line" ]] && print -s -- "$line"
        if ! repl_dispatch "$line"; then
            # /quit
            break
        fi
    done

    unset TMOUT
    unset -f TRAPALRM 2>/dev/null
    trap - INT
    diarize_stop

    repl_handle_active_on_exit
}
