# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

Local-first meeting transcription for macOS. Three cooperating pieces, not a single binary:

1. **`bin/meetink`** — zsh launcher. The CLI users actually invoke. Manages PIDs, builds the capture binary, starts/stops `whisper-server`, dispatches subcommands.
2. **`src/capture/Sources/main.swift`** — Swift executable (`meetink-capture`). Captures system audio (ScreenCaptureKit) + mic (AVAudioEngine), chunks both at 16 kHz mono / 3 s, POSTs WAVs to `whisper-server`, writes the transcript.
3. **`whisper-server`** — Homebrew-installed `whisper-cpp` daemon, run on `127.0.0.1:8178` by the launcher with the `small.en` model loaded once. Inference happens here; the Swift binary is a client.

A fourth, optional service runs on `127.0.0.1:8179`: a **diarize-server** (`/identify` endpoint) for speaker identification on the THEM stream. It is *not* installed by `setup` and is *not* mentioned in the README — `main.swift` tolerates its absence (3-strike fail counter, periodic retry). If you're adding diarization features, that's where the contract lives (`diarizeSpeaker` in `main.swift`).

```
zsh launcher ──spawns──▶ whisper-server (:8178)  ◀── HTTP /inference ── Swift capture binary
                            (loads model once)                                │
                                                                              ├─▶ transcript file
                                                  diarize-server (:8179)  ◀───┘   (optional)
```

## Common commands

```sh
# First-time install: brew installs whisper-cpp, downloads model, builds Swift binary
./bin/meetink setup

# Build only the Swift capture binary (after editing main.swift)
cd src/capture && swiftc -O -o meetink-capture Sources/main.swift \
    -framework ScreenCaptureKit -framework AVFoundation \
    -framework CoreMedia -framework CoreAudio \
    -parse-as-library -target arm64-apple-macosx14.0 \
    -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.5.sdk
# …or just re-run `./bin/meetink start` — it rebuilds if the binary is missing.

# Run / stop / inspect
./bin/meetink start       # spawns whisper-server then capture binary, both backgrounded
./bin/meetink stop        # SIGINTs both, cleans PID files
./bin/meetink status      # checks /tmp/meetink-capture.pid
./bin/meetink tail        # tail -f the transcript

# Logs (useful when something fails silently)
tail -f /tmp/meetink-whisper.log
tail -f /tmp/meetink-capture.log
```

There is **no test suite** and no linter config. The Package.swift exists for tooling/IDE support, but the launcher builds with `swiftc` directly (not `swift build`) so it can pin the SDK and target.

## Things that bite

- **Two install locations for the binary.** `setup` / `build_capture` copies it to `$MK_HOME/bin/meetink-capture` (default `~/.meetink/bin`). The source-tree copy at `src/capture/meetink-capture` is gitignored and only used as a fallback. `find_capture_binary` in `bin/meetink` checks both. After editing `main.swift`, you have to either re-run `setup` or copy the new binary into `~/.meetink/bin/` — running `start` won't auto-rebuild if the old binary still exists.
- **SDK pinning matters.** `find_sdk` prefers `MacOSX15.5.sdk` then walks down. Newer SDKs from a freshly upgraded Xcode have caused Swift CLI mismatches; don't blindly switch to `MacOSX.sdk`. If a build breaks after an Xcode update, check which SDK `build_capture` actually picked.
- **Permissions attach to the terminal app, not the binary.** Granting Screen & System Audio Recording or Microphone to `meetink` itself does nothing — it has to be the terminal you launched from (Terminal.app, iTerm2, Ghostty, Warp). See `docs/permissions.md`.
- **`whisper-server` is global state.** It binds `:8178` for the whole machine. If a user already runs whisper-cpp for something else, `start_whisper_server` in the launcher will refuse (port collision, fails the 30 s readiness probe) — the existing PID-file check only catches *our own* server.
- **Hallucination filter is opinionated and aggressive.** `isHallucination` in `main.swift` drops common whisper artefacts (`(soft music)`, "thanks for watching", repetition loops, `©`/copyright strings, anything fully parenthesised under 40 chars). If a real utterance gets eaten, look here before assuming the transcription failed.
- **Transcript file is append-only with timestamped headers.** `main.swift` writes `# Meeting Transcript\nStarted: …` on start and `---\nEnded: …` on stop. The line format is `[HH:MM:SS] SPEAKER: text`. `TranscriptMerger` coalesces back-to-back same-speaker chunks (2 s gap or 5 s buffer max), so one user utterance ≠ one transcript line.

## Code map (the parts that need cross-file context)

- **Audio pipeline (`main.swift`):** `AudioBuffer` (lock-protected sample queues for sys + mic) → `tryExtractChunks` (every 1 s loop tick, pulls ≥3 s if available) → `writeWAV` → `transcribe` (multipart POST to whisper-server, includes the `MEETINK_PROMPT` file *plus* a 200-char rolling context per speaker via `TranscriptContext`) → `isHallucination` → `TranscriptMerger.add` → `live.txt`.
- **Speaker labelling.** Mic chunks always get `ME`. System chunks default to `THEM`; if the diarize-server is up, `DiarizeAudioBuffer` accumulates 10 s windows (with 3 s overlap), POSTs to `:8179/identify`, and the returned name uppercased becomes the live label until the next identification.
- **CLI dispatch.** `bin/meetink` switches on `$1` at the bottom of the file. New subcommands go there + a `cmd_<name>` function. `src/lib/ui.sh` provides the colour table `$C` and box-drawing helpers; `src/lib/welcome.sh` is the no-arg landing page (the one that detects `_has_binary`/`_has_model`/`_has_whisper`/`_is_running` to colour the status dots).

## Conventions

- The user-facing data dir is `$MEETINK_HOME` (default `~/.meetink`) with subdirs `bin/`, `models/`, `transcripts/`, `prompts/`. All paths in both the launcher and `main.swift` are env-overridable — see the `ENV OVERRIDES` block in `cmd_help` and the top of `main.swift` for the canonical list. Add new paths the same way (env var → fallback default).
- Custom whisper vocabulary lives in `~/.meetink/prompts/default.txt`, seeded from `src/capture/prompts/default.txt` on first `setup`. The example template at `src/capture/prompts/example.txt` is what users are pointed to in the README.
- Keep the launcher zsh-only (it uses `${0:A}`, `${name:h}`, `typeset -gA`, glob qualifiers). Don't port to bash without a reason.
