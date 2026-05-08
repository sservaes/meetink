#!/bin/zsh
# /watch — auto-record meetings from your calendar (and impromptu calls).
#
# Two surfaces:
#
# Long-running watcher (lifecycle commands live in src/repl/repl.py
# because the watcher thread has to outlive a single subcommand):
#
#   /watch on        start the watcher; persists across REPL restarts
#                    via watch_enabled in ~/.meetink/config
#   /watch off       stop the watcher (does not stop an in-flight
#                    recording)
#   /watch status    state of the watcher + next event ETA
#   /watch skip      mark the soonest pending event as skipped
#
# Diagnostics (one-shot, dispatched here):
#
#   /watch events    list upcoming events from Calendar.app
#   /watch notify    send a test notification with action buttons
#   /watch detect    show whether a video call is currently active
#
# Sourced by bin/meetink AFTER index.sh, repl.sh.

MK_AGENT="$MK_HOME/bin/MeetinkAgent.app/Contents/MacOS/meetink-agent"


# Resolve the agent binary, fail with a friendly hint if missing.
_watch_agent_path() {
    if [[ ! -x "$MK_AGENT" ]]; then
        print -P "${C[red]}error:${C[reset]} ${C[bold]}meetink-agent${C[reset]} not built"
        print -P "  Run ${C[bright_cyan]}meetink setup${C[reset]} ${C[dim]}or${C[reset]} ${C[bright_cyan]}meetink _build_agent${C[reset]} to build the app bundle."
        return 1
    fi
    print -n -- "$MK_AGENT"
}


# /watch events [--hours N] — pretty-prints upcoming calendar events.
# First run triggers macOS Full Calendar Access prompt; grant via
# System Settings → Privacy & Security → Calendar.
watch_events() {
    local agent
    agent=$(_watch_agent_path) || return 1
    local hours="${1:-8}"
    if ! [[ "$hours" =~ ^[0-9]+$ ]]; then
        print -P "${C[red]}usage:${C[reset]} /watch events [hours]"
        return 1
    fi

    print -P ""
    print -P "${C[bright_yellow]}UPCOMING EVENTS${C[reset]} ${C[dim]}(next ${hours}h)${C[reset]}"
    print -P ""

    # The agent prints a JSON array on stdout. Capture stdout / stderr
    # separately so the parser doesn't choke on warning lines mixed in.
    local errfile=$(mktemp -t meetink-watch.XXXXXX)
    local out
    out=$("$agent" events --hours "$hours" 2>"$errfile")
    local rc=$?
    if (( rc != 0 )); then
        local err_msg=$(<"$errfile")
        rm -f "$errfile"
        print -P "${C[red]}agent error:${C[reset]} ${err_msg:-(unknown)}"
        if [[ "$err_msg" == *"Calendar access denied"* ]]; then
            print -P "  ${C[dim]}Grant access via${C[reset]} ${C[bright_cyan]}System Settings → Privacy & Security → Calendar${C[reset]}"
        fi
        return 1
    fi
    rm -f "$errfile"

    "$MK_PY_VENV/bin/python" - "$out" <<'PY' 2>/dev/null || print -P "${C[red]}error:${C[reset]} couldn't parse agent output"
import sys, json
data = json.loads(sys.argv[1])
if not data:
    print("  (none)")
    sys.exit(0)
RSVP_COLOUR = {
    "accepted":  "\033[32m",
    "declined":  "\033[31m",
    "tentative": "\033[33m",
    "pending":   "\033[33m",
    "none":      "\033[90m",
}
for e in data:
    start = e["start"][11:16]   # HH:MM
    end   = e["end"][11:16]
    rsvp  = e["rsvpStatus"]
    col   = RSVP_COLOUR.get(rsvp, "\033[90m")
    title = e["title"]
    n_att = len(e.get("attendees", []))
    cal   = e.get("calendarTitle", "")
    cal_label = f" \033[90m· {cal}\033[0m" if cal else ""
    att_label = f" \033[90m· {n_att} attendees\033[0m" if n_att else ""
    print(f"  \033[2m{start}\033[0m–\033[2m{end}\033[0m  "
          f"{col}{rsvp:9}\033[0m  \033[1m{title}\033[0m{att_label}{cal_label}")
PY
    print -P ""
}


# /watch notify — sends a test notification + waits up to 10s.
# First run prompts for Notifications permission. The notification has
# Skip/Continue action buttons; clicking either prints the choice back
# in the REPL.
watch_notify() {
    local agent
    agent=$(_watch_agent_path) || return 1
    print -P "${C[dim]}Sending test notification (10s timeout, default Continue)...${C[reset]}"
    print -P "${C[dim]}If the banner doesn't appear, grant Notifications via${C[reset]} ${C[bright_cyan]}System Settings → Notifications${C[reset]}"
    local result
    result=$("$agent" notify \
        --title "meetink test" \
        --body "click Skip or wait 10 s" \
        --actions "Skip,Continue" \
        --timeout 10 \
        --default Continue 2>&1)
    print -P "${C[green]}✓${C[reset]} You clicked: ${C[bold]}${result}${C[reset]}"
}


# /watch detect — runs meeting-active and pretty-prints which signals
# fired. Useful for verifying detection in real conditions: open Zoom,
# run /watch detect, see "active=true source=zoom".
watch_detect() {
    local agent
    agent=$(_watch_agent_path) || return 1
    # Capture stdout and stderr separately. Earlier we 2>&1'd both
    # together and any AppleScript / AVCapture warning leaked into the
    # JSON, breaking the parser. Now stderr goes to a tempfile we only
    # surface when we genuinely couldn't parse the JSON.
    local errfile=$(mktemp -t meetink-watch.XXXXXX)
    local out
    out=$("$agent" meeting-active 2>"$errfile")
    if "$MK_PY_VENV/bin/python" - "$out" <<'PY' 2>/dev/null
import sys, json
d = json.loads(sys.argv[1])
active = d.get("active", False)
source = d.get("source") or "(none)"
signals = d.get("signals", [])
icon = "\033[32m●" if active else "\033[90m○"
state = "\033[32mactive\033[0m" if active else "\033[90minactive\033[0m"
print(f"\n  {icon}\033[0m  Meeting: {state}")
print(f"  \033[2m  source:\033[0m  {source}")
if signals:
    print(f"  \033[2m signals:\033[0m  " + ", ".join(signals))
print()
PY
    then
        rm -f "$errfile"
        return 0
    fi
    print -P "${C[red]}error:${C[reset]} couldn't parse agent output"
    print -P "  ${C[dim]}stdout:${C[reset]} ${out:-(empty)}"
    if [[ -s "$errfile" ]]; then
        print -P "  ${C[dim]}stderr:${C[reset]}"
        sed 's/^/    /' "$errfile"
    fi
    rm -f "$errfile"
    return 1
}


# /watch dispatcher
cmd_watch() {
    local sub="$1"
    case "$sub" in
        events|list)        watch_events "$2" ;;
        notify|notification) watch_notify ;;
        detect|status|active) watch_detect ;;
        # on/off/status/skip live inside the REPL because the watcher is
        # a thread of the python REPL process, not the launcher (the
        # launcher is one-shot). Anyone hitting these from a bare
        # `meetink watch on` shell call gets a hint to use the REPL.
        on|start|off|stop|status|skip)
            print -P "${C[dim]}/watch ${sub} only works inside the meetink REPL.${C[reset]}"
            print -P "  Run ${C[bright_cyan]}meetink${C[reset]} ${C[dim]}with no args, then${C[reset]} ${C[bright_cyan]}/watch ${sub}${C[reset]}${C[dim]}.${C[reset]}"
            ;;
        ""|help)
            print -P ""
            print -P "${C[bright_yellow]}/watch${C[reset]} ${C[dim]}— auto-record meetings from your calendar (and impromptu calls)${C[reset]}"
            print -P ""
            print -P "  ${C[bright_cyan]}/watch on${C[reset]}                start the auto-recorder ${C[dim]}(persists across REPL restarts)${C[reset]}"
            print -P "  ${C[bright_cyan]}/watch off${C[reset]}               stop the auto-recorder"
            print -P "  ${C[bright_cyan]}/watch status${C[reset]}            running state, current recording, upcoming events"
            print -P "  ${C[bright_cyan]}/watch skip${C[reset]}              mark the soonest pending event as skipped"
            print -P ""
            print -P "  ${C[bright_cyan]}/watch events${C[reset]} ${C[dim]}[hours]${C[reset]}    list upcoming calendar events ${C[dim]}(diagnostic)${C[reset]}"
            print -P "  ${C[bright_cyan]}/watch notify${C[reset]}            send a test notification ${C[dim]}(diagnostic)${C[reset]}"
            print -P "  ${C[bright_cyan]}/watch detect${C[reset]}            check if a video call is active right now ${C[dim]}(diagnostic)${C[reset]}"
            print -P ""
            ;;
        *)
            print -P "${C[red]}unknown:${C[reset]} ${C[dim]}/watch $sub${C[reset]}"
            print -P "  ${C[dim]}/watch${C[reset]} ${C[dim]}for help${C[reset]}"
            ;;
    esac
}
