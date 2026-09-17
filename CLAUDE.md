# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A fully local/offline voice-control assistant for Windows: wake word ("hey
jarvis") or push-to-talk (Right Ctrl) -> local transcription -> intent
resolution against a fixed allowlist of actions -> execution -> spoken
reply. No cloud dependency anywhere in the runtime pipeline.

## Environment — read this before running anything

- **Native Windows Python process, not WSL2.** Mic capture, global hotkeys,
  window management, and autostart are all unavailable from WSL2.
- Always invoke the venv interpreter explicitly:
  `.venv\Scripts\python.exe` (PowerShell) — never bare `python`, which
  resolves to the Microsoft Store stub alias, not this project's interpreter.
- **Model weights are not in the repo** (gitignored under `models/`). See
  the README's "Fetching the models" table for what's required and where
  each comes from. If a model is missing, model-loading code will fail —
  that's an environment problem, not a code bug.
- **`llama-cpp-python` is not in `requirements.txt`** — its sdist's nested
  path structure breaks Windows' `MAX_PATH` during extraction. Install
  separately from the prebuilt CPU wheel index (see README Setup section).
- Python's own HTTPS client gets TLS-reset on this machine (antivirus
  inspection, unconfirmed which product) — never add code that fetches
  models via `huggingface_hub`/`fastembed` auto-download at runtime. Every
  model loader reads from a local `models/<name>/` directory only.

## Commands

```powershell
# install
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install llama-cpp-python --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

# run the full stack (opens themed Windows Terminal profile)
jarvis start

# control an already-running instance (talks to localhost:47123)
jarvis status | stats | pause | resume | quit
jarvis debug [on|off]        # toggle terminal verbosity without restart

# tests — no audio device, no models, no window manager required
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests/test_actions.py
.\.venv\Scripts\python.exe -m pytest tests/test_actions.py::test_name_here -v

# HUD preview only (no mic/models/listener started)
.\.venv\Scripts\python.exe hud.py

# standalone LLM-fallback benchmark (run by hand after changing
# llm_intent.py's model/prompt/schema)
.\.venv\Scripts\python.exe tools\_bench_llm.py

# promote frequently-seen LLM-resolved phrases into the fast path
# (manual only — restart jarvis afterward for new seeds to take effect)
.\.venv\Scripts\python.exe seed_promotion.py
```

There is no separate lint/format/build step configured in this repo.

## Architecture

```
mic --> openWakeWord ("hey jarvis") --> Silero VAD (utterance segmentation)
      --> faster-whisper small.en (transcription)
      --> intent_matcher.py (fast embedding match)
            |-- confident match --> actions.py (dispatch)
            \-- no match --> llm_intent.py (local Qwen2.5 LLM, grammar-constrained)
                                --> actions.py (dispatch)
      --> tts.py (spoken reply)
```

Push-to-talk (Right Ctrl) is a second, independent entry point into the
same `intent_matcher.py` -> `llm_intent.py` -> `actions.py` -> `tts.py`
pipeline, bypassing the wake-word/VAD-session layer entirely. `main.py`
runs a three-mode listener loop (`wake` / `session_listening` /
`session_recording`) plus the control server and single-instance guard.

**The security boundary is `actions.ACTIONS`.** The embedding matcher and
the LLM only ever select a *key* into this fixed dict of plain Python
functions — neither path ever generates or executes arbitrary code or
shell commands. Every LLM-produced field is re-validated against real
enums in `llm_intent.py`'s `infer()` before it reaches `actions.py`; the
grammar constraint (GBNF from a JSON schema) is never trusted as the only
layer. The current action set is non-destructive by construction — "close"
always means minimize, never actually closing/quitting an app — and there
is deliberately no confirmation step yet (would be needed before adding
any action with real side effects like delete/send).

### Module map

| Module | Role |
|---|---|
| `main.py` | orchestrator: audio streams, wake word, VAD, session state machine, control server, single-instance guard |
| `actions.py` | execution layer — every allowlisted action as a plain function; the only thing ever dispatched to |
| `apps_registry.py` | registered-apps table (8 curated entries) + whatever `app_discovery.py` finds — the actual security boundary for "open/minimize apps in general" |
| `app_discovery.py` | scans the Start Menu at startup to expand the registry; never overrides a curated entry |
| `intent_matcher.py` | fast path: local sentence-embedding similarity match (cosine threshold 0.80) |
| `local_embedder.py` | ONNX inference for `bge-small-en-v1.5`, bypasses fastembed's HF auto-download |
| `llm_intent.py` | fallback path: local Qwen2.5-1.5B-Instruct via `llama-cpp-python`, grammar-constrained JSON output; model + `llama_cpp` import are both lazy; also runs a background prewarm (see `main.py`'s `_start_llm_prewarm`) |
| `seed_promotion.py` | logs LLM-resolved phrases; promotes frequent ones into the fast path (manual only) |
| `monitors.py` | physical-display identification (laptop vs external — `QueryDisplayConfig`, not the primary-monitor flag) and spoken-mention resolution ("my laptop screen", "the other screen") |
| `window_context.py` | ranks candidate windows when several of the same app are open — title-hint matching, monitor bias, recency, cycle-on-repeat |
| `app_titles.py` | per-app window-title cleanup for spoken replies (VS Code/Chrome/Spotify/Discord) — pure string functions, no win32 |
| `vad.py` | Silero VAD wrapper — carries recurrent state, so never share one instance across independent audio streams |
| `virtual_desktop.py` | `IVirtualDesktopManager` COM wrapper (current-desktop check); thread-local, `CLSCTX_INPROC_SERVER` |
| `tts.py` | Piper TTS playback, mic-mute-while-speaking (count-based, not boolean), persistent output stream with device-failure recovery |
| `hud.py` | single locked writer for all user-facing terminal output; degrades to plain `print()` when stdout isn't a terminal |
| `earcons.py` | short non-verbal audio cues synthesized with numpy at import; routed through `tts._begin_speaking`/`_end_speaking` so they get mic-muted too |
| `phrases.py` | reply phrasing as pools of variants — the one file to change for wording/tone |
| `jarvisctl.py` | one-shot control-socket client (`127.0.0.1:47123`) for status/stats/debug/pause/resume/quit |
| `jarvis.cmd` | `jarvis <command>` wrapper; `start` launches `wt.exe -F -p Jarvis` directly since `jarvisctl.py` can only talk to an already-running instance |

### Conventions that matter here

- **Never let an exception escape a `sounddevice` callback.** PortAudio's
  ffi callback returns `paAbort` on any escaping exception and **permanently
  kills the stream** for the process's life — not a dropped frame, the end
  of listening. Every audio callback wraps its full body in try/except
  that logs and returns. `tests/test_audio_callbacks.py` also asserts
  `sounddevice` still uses `error=_lib.paAbort`; if that assumption ever
  changes upstream, this guard stops being load-bearing.
- **Audio device streams are opened once at startup and kept open for the
  process's life**, never per-call/per-press. A freshly-opened Windows
  audio device reliably glitches its first chunk(s), both directions.
- **Fail closed, not open**, for anything security/correctness-adjacent
  (e.g. virtual-desktop membership checks) — a redundant extra window is
  a fine worst case; silently doing the wrong destructive-adjacent thing
  is not.
- **The listener loop must never die on an unhandled exception.** `main.py`
  wraps the whole mode-dispatch block in try/except, logs, speaks an
  apology, and resets to a known-good state — a silently-dead listener
  thread is worse than any single misfire.
- Structured event logging goes to `logs/session.jsonl` regardless of what
  the HUD chooses to display — the HUD is a view, not the source of truth.
- `phrases.pick()` never returns the same variant twice in a row for a
  given key — back-to-back repeats are what actually reads as robotic.
- Tests run with no audio device, no models, and no window manager
  present — anything added to `tests/` must stay mockable against that
  constraint (see existing tests for the pattern per module).

For the *why* behind any of the above (real bugs hit, tuning history,
rejected alternatives), see `DECISIONS.md` — it's written as a decision
log, not just a feature list, and is the first place to check before
re-deriving a design choice from scratch.

## Documentation map

The docs were split by audience — check the right one before re-deriving
something that's already written down:

| File | Covers |
|---|---|
| `README.md` | Project overview, setup, fetching models, starting Jarvis |
| `USER_GUIDE.md` | Day-to-day usage, the four-state diagram, full `jarvis` command reference |
| `ARCHITECTURE.md` | Pipeline shape, security boundary, full module-by-module layout |
| `DECISIONS.md` | The decision log — why, not just what, organized by area |
| `ENVIRONMENT.md` | This dev machine's quirks (TLS resets, double-process-spawn) |
| `KNOWN_LIMITATIONS.md` | Known gaps, deliberately deferred rather than fixed |
| `STATUS.md` | Phase-by-phase build history |
| `scheduler/README.md` | The Task Scheduler entries behind autostart/sleep/wake |
