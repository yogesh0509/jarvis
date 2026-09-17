# jarvis

Custom voice-control assistant for Windows. Built because Windows Voice
Access (Settings -> Accessibility -> Speech) only matches a fixed command
grammar -- natural paraphrasing fails there. This listens for a wake word
("hey jarvis") or a push-to-talk hotkey, transcribes locally, resolves the
phrase to one of a fixed set of allowlisted actions (a fast local embedding
matcher first, a local LLM fallback for anything it can't confidently
place), executes it, and replies out loud. Fully local/offline -- no cloud
dependency anywhere in the pipeline.

Runs as a **native Windows Python process** -- mic capture, global hotkeys,
window management, and autostart are all unavailable from WSL2. This repo
lives on `D:\` by design; the Python *interpreter* itself is installed
under `C:\Users\agraw\AppData\Local\Programs\Python\Python312\` (native
Windows PATH is shadowed by the Microsoft Store `python.exe` stub alias --
always invoke via `.venv\Scripts\python.exe` or the interpreter's absolute
path, not bare `python`).

## Setup

```powershell
cd D:\jarvis
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install llama-cpp-python --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

`llama-cpp-python` is installed separately, from a prebuilt wheel index
rather than `requirements.txt`'s default resolution -- its sdist bundles
llama.cpp's full source tree, including a deeply-nested Svelte web UI
folder that exceeds Windows' 260-character `MAX_PATH` during extraction on
this machine. The prebuilt CPU wheel sidesteps the whole problem.

### Fetching the models

None of the model weights are in this repo (see `.gitignore` --
`models/`); they're pre-downloaded, on-disk artifacts, not source. Fetch
each one by hand into `models/<name>/` via PowerShell's
`Invoke-WebRequest` -- **not** Python's own HTTPS client, which gets
silently reset on this machine (see `ENVIRONMENT.md`). Required:

| Directory | What | Used by |
|---|---|---|
| `models/faster-whisper-small.en/` | faster-whisper `small.en`, CTranslate2 format | `main.py` (transcription) |
| `models/bge-small-en-v1.5-onnx-q/` | `BAAI/bge-small-en-v1.5`, ONNX, quantized | `local_embedder.py` (intent matcher) |
| `models/qwen2.5-1.5b-instruct-gguf/` | Qwen2.5-1.5B-Instruct, GGUF Q4_K_M | `llm_intent.py` (LLM fallback) |
| `models/piper-en_GB-northern_english_male-medium/` | Piper TTS voice (active) | `tts.py` |
| `models/piper-en_GB-alan-medium/` | Piper TTS voice (switchable fallback) | `tts.py` (`VOICE = "alan"`) |

openWakeWord's `hey_jarvis` model and the bundled `silero_vad.onnx` are
fetched automatically by the `openwakeword` package itself on first run,
from GitHub release assets -- never blocked by the TLS issue above, so no
manual-download workaround was ever needed there.

## Starting Jarvis

```powershell
jarvis start      # launches it in its own themed, fullscreen terminal
jarvis status     # check what it's doing
jarvis pause      # mic off, stop listening entirely
jarvis resume     # bring it back
jarvis quit       # stop it completely
```

It also starts itself automatically at Windows logon and after the
machine wakes from sleep -- see `scheduler/README.md`. For everything
else -- what you can actually say, the wake-word/push-to-talk flow,
dictation, the state diagram, and the full `jarvis` command reference --
see `USER_GUIDE.md`.

## Documentation

This README covers setup and getting started only. Everything else lives
in its own file:

| File | Covers |
|---|---|
| `USER_GUIDE.md` | Day-to-day usage: what you can say, the wake-word/push-to-talk flow, dictation, the four-state diagram, and the full `jarvis` command reference |
| `ARCHITECTURE.md` | The recognition pipeline, the `actions.ACTIONS` security boundary, and a module-by-module project layout |
| `DECISIONS.md` | The decision log -- why the system works the way it does, organized by area, with real bugs and measurements behind each choice |
| `ENVIRONMENT.md` | Quirks specific to this development machine (TLS resets, a double-process-spawn quirk) that aren't project bugs |
| `KNOWN_LIMITATIONS.md` | Known gaps and provisional tuning, deliberately deferred rather than fixed |
| `STATUS.md` | Phase-by-phase build history |
| `scheduler/README.md` | The three Task Scheduler entries that start/stop Jarvis around logon and sleep/wake |

`tests/` is a pytest suite requiring no audio device, no models, and no
window manager -- run it with `.venv\Scripts\python.exe -m pytest`.
