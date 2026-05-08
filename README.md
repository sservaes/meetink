# meetink

Local-first meeting transcription for the macOS terminal. Captures system audio (Zoom, Meet, Teams) and microphone simultaneously, runs them through whisper.cpp on your machine, and writes a labelled transcript file. No cloud, no API keys.

## Why

If you take a lot of meetings on your Mac and want a private transcript, this gives you one with a single command. Audio never leaves your laptop. Works alongside any video conferencing app.

## Requirements

- macOS 14 (Sonoma) or later
- Apple Silicon (Intel may work but is untested)
- Xcode Command Line Tools (`xcode-select --install`)
- [Homebrew](https://brew.sh)

## Install

```sh
git clone https://github.com/sservaes/meetink.git
cd meetink
./bin/meetink setup
```

`setup` installs whisper.cpp via Homebrew, downloads the `small.en` model (~500 MB), and builds the Swift capture binary.

Optionally, symlink the launcher onto your `PATH`:

```sh
ln -s "$(pwd)/bin/meetink" /usr/local/bin/meetink
```

## Usage

```sh
meetink start    # begin recording + transcribing
meetink stop     # stop and save
meetink status   # is it running?
meetink tail     # follow the transcript live
meetink help     # full help
```

Run `meetink` with no arguments to see the welcome screen with current state.

The transcript writes to `~/.meetink/transcripts/live.txt` with timestamped lines:

```
[14:32:08] ME: yeah I think we should ship it next week
[14:32:14] THEM: agreed, let's get the design review on the calendar
```

## Permissions

On first run macOS will ask for two things:

1. **Screen & System Audio Recording** — required to capture system audio (i.e. the other people in your meeting). Grant via *System Settings → Privacy & Security → Screen & System Audio Recording*.
2. **Microphone** — required to capture your voice.

Both prompts target whichever terminal app you launched `meetink` from.

## Custom vocabulary

Whisper does better when you tell it what jargon and proper nouns to expect. Edit:

```sh
~/.meetink/prompts/default.txt
```

Add comma-separated names, acronyms, and domain-specific words. Example template at `src/capture/prompts/example.txt`.

## How it works

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ ScreenCaptureKit │ ──▶ │ Swift binary     │ ──▶ │ whisper.cpp      │
│ + AVAudioEngine  │     │ (16 kHz mono     │     │ (Metal-accelerated│
│                  │     │  3-sec chunks)   │     │  small.en model) │
└─────────────────┘     └──────────────────┘     └──────────────────┘
                                                          │
                                                          ▼
                                          ~/.meetink/transcripts/live.txt
```

- **Capture:** `src/capture/Sources/main.swift` — Swift binary, ScreenCaptureKit for system audio, AVAudioEngine for mic. Mixed to 16 kHz mono and chunked every 3 seconds.
- **Transcription:** `whisper-server` runs locally on port 8178, model loaded once. Each chunk is HTTP-POSTed for inference.
- **Output:** Hallucination filter strips common whisper artefacts (`(soft music)`, `thanks for watching`, repetition loops). Sentence merger combines back-to-back same-speaker chunks into readable lines.

## Limitations

- macOS only (uses ScreenCaptureKit and AVAudioEngine)
- English-only by default (the `small.en` model). For other languages, override `MEETINK_MODEL` to point at a multilingual `ggml-*.bin` file.
- Single-microphone source. If you want speaker diarization (assigning names to voices), that's planned but not in v0.1.

## Configuration

All paths can be overridden via environment variables:

| Variable | Default |
|---|---|
| `MEETINK_HOME` | `~/.meetink` |
| `MEETINK_MODEL` | `$MEETINK_HOME/models/ggml-small.en.bin` |
| `MEETINK_TRANSCRIPT` | `$MEETINK_HOME/transcripts/live.txt` |
| `MEETINK_PROMPT` | `$MEETINK_HOME/prompts/default.txt` |
| `MEETINK_CHUNK_DIR` | `/tmp/meetink-chunks` |

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built on top of:
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) by Georgi Gerganov
- Apple's [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) and [AVAudioEngine](https://developer.apple.com/documentation/avfaudio/avaudioengine)
