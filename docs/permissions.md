# Granting macOS permissions

`local-speech` needs two macOS privacy permissions to work. Both attach to the **terminal app** you launched the tool from (Terminal.app, iTerm2, Ghostty, Warp, etc.) — not to `local-speech` itself.

## 1. Screen & System Audio Recording

Required for capturing system audio (the other people in your Zoom / Meet / Teams call).

1. Open **System Settings** → **Privacy & Security** → **Screen & System Audio Recording**
2. Toggle ON for your terminal app
3. macOS will ask you to quit and reopen the terminal — do it

## 2. Microphone

Required for capturing your own voice.

1. Open **System Settings** → **Privacy & Security** → **Microphone**
2. Toggle ON for your terminal app

## Verifying

Run:

```sh
local-speech start
```

If permissions are missing, the Swift binary prints a clear error pointing you to the right setting. If you see no error and recording starts, you're good.

## Removing permissions

You can revoke at any time in the same panels. `local-speech` will fail loudly the next time you `start`.
