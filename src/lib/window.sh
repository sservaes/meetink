#!/bin/zsh
# Tail-window management — open / close / raise a separate Terminal.app
# window running `tail -f` on the live transcript.
#
# Tracks "our" windows by setting a custom title marker. On every operation
# that could affect a window, we additionally compare the window's tty to
# the calling shell's tty so we can never accidentally act on the REPL
# window itself (e.g. if a stale title got stuck on it).
#
# Sourced by bin/meetink.

MK_TAIL_PIDFILE="/tmp/meetink-tail.tailpid"
MK_TAIL_TITLE="meetink-tail"

# /dev/ttysNNN of the current shell. Used to exclude self from window matches.
window_self_tty() {
    tty 2>/dev/null
}

# Returns 0 if at least one tracked tail window exists OTHER than our own.
window_tail_exists() {
    local self_tty=$(window_self_tty)
    local count
    count=$(osascript 2>/dev/null <<APPLESCRIPT
tell application "Terminal"
    set n to 0
    repeat with w in windows
        try
            if custom title of w is "$MK_TAIL_TITLE" then
                if tty of (selected tab of w) is not "$self_tty" then
                    set n to n + 1
                end if
            end if
        end try
    end repeat
    return n
end tell
APPLESCRIPT
)
    [[ -n "$count" && "$count" != "0" ]]
}

# Bring the (non-self) tracked tail window to the front (no-op if none).
window_raise_tail() {
    local self_tty=$(window_self_tty)
    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
    activate
    repeat with w in windows
        try
            if custom title of w is "$MK_TAIL_TITLE" then
                if tty of (selected tab of w) is not "$self_tty" then
                    set frontmost of w to true
                    exit repeat
                end if
            end if
        end try
    end repeat
end tell
APPLESCRIPT
}

# Open a Terminal.app window running tail -f. Sets the title on the window
# that actually contains the new tab (not on `front window`, which is racy).
# Args: $1 = transcript path
window_open_tail() {
    local transcript="$1"

    if [[ ! -f "$transcript" ]]; then
        print -P "${C[red]}error:${C[reset]} no transcript at $transcript"
        return 1
    fi

    if window_tail_exists; then
        window_raise_tail
        return 0
    fi

    # `do script` opens a new login-shell tab — that means the user's
    # .zshrc banner ("Last login: …"), the prompt, the typed-in command,
    # and job-control noise ([2] 12345) all show up before tail starts.
    # Workaround: write a tiny runner script that resets the terminal
    # (\033c clears screen + scrollback), prints a header, then `exec tail`.
    # AppleScript only has to invoke a path — no escape-code juggling.
    #
    # The runner *also* sets the window's custom title via the OSC-2 escape
    # sequence (\033]2;TITLE\007). We used to set it via AppleScript
    # (`set custom title of w to ...`) right after `do script`, but that's
    # racy and unreliable — Terminal.app silently drops it once bash runs
    # its profile / PS1, which prints its own title escape and overwrites
    # ours. Emitting the escape from the runner runs *after* the profile,
    # so the title sticks. window_close_tail relies on this title to find
    # the window again.
    local runner="/tmp/meetink-tail-runner.sh"
    cat > "$runner" <<'RUNNER'
#!/bin/bash
# Wipe screen + scrollback (the .zshrc banner and command-echo noise).
printf '\033c'
# $3 is the window-title marker; OSC-2 sets the Terminal "custom title".
printf '\033]2;%s\007' "$3"
printf '\033[1;36m✎  meetink\033[0m  \033[2mlive transcript\033[0m\n'
printf '\033[2m%s\033[0m\n\n' "$1"
echo $$ > "$2"
# `tail -F` (capital): follow-by-name, reopen on rename/replace. We need
# this because /profile assign rewrites the transcript file, and old-style
# `tail -f` keeps the inode of the original file — meaning the live window
# silently freezes after the first rewrite even though Swift keeps writing.
exec tail -F "$1"
RUNNER
    chmod +x "$runner"

    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "exec $runner '$transcript' '$MK_TAIL_PIDFILE' '$MK_TAIL_TITLE'"
end tell
APPLESCRIPT
}

# Close any tracked tail window OTHER than self. Two-step: kill tail (so the
# shell exits cleanly via the trailing `; exit 0`), then AppleScript-close
# any window that wasn't auto-closed by the user's profile.
window_close_tail() {
    if [[ -f "$MK_TAIL_PIDFILE" ]]; then
        local pid=$(< "$MK_TAIL_PIDFILE")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
        fi
        rm -f "$MK_TAIL_PIDFILE"
    fi
    sleep 0.3

    local self_tty=$(window_self_tty)
    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
    repeat with w in (every window)
        try
            if custom title of w is "$MK_TAIL_TITLE" then
                if tty of (selected tab of w) is not "$self_tty" then
                    close w saving no
                end if
            end if
        end try
    end repeat
end tell
APPLESCRIPT
}

# Defensive cleanup: if our own window has the tail-marker title stuck on
# it (from a previous buggy run), clear it so future close operations will
# never even consider it. Called once on REPL entry.
window_clear_self_title() {
    local self_tty=$(window_self_tty)
    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
    repeat with w in windows
        try
            if custom title of w is "$MK_TAIL_TITLE" then
                if tty of (selected tab of w) is "$self_tty" then
                    set custom title of w to ""
                    exit repeat
                end if
            end if
        end try
    end repeat
end tell
APPLESCRIPT
}
