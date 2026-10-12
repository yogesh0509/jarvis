# Architecture

The pipeline shape and a map of what every module is responsible for.
Project overview and setup live in `README.md`; the reasoning behind
each of these choices (real bugs hit, tuning history, rejected
alternatives) lives in `DECISIONS.md`; day-to-day behavior and state
diagrams live in `USER_GUIDE.md`.

## Pipeline

```
mic --> echo_canceller.py (WebRTC AEC3, loopback.py reference -- removes
        whatever the speakers are playing; raw passthrough when silent)
      --> openWakeWord ("hey jarvis") --> Silero VAD (utterance segmentation)
      --> faster-whisper small.en (transcription)
      --> intent_matcher.py (fast embedding match)
            |-- confident match --> actions.py (dispatch)
            \-- no match --> llm_intent.py (local Qwen2.5 LLM, grammar-constrained)
                                --> actions.py (dispatch)
      --> tts.py (spoken reply)
```

Push-to-talk (Right Ctrl) is a second, independent entry point into the
same `intent_matcher.py` -> `llm_intent.py` -> `actions.py` -> `tts.py`
pipeline, bypassing the wake-word/VAD-session layer entirely (see
`DECISIONS.md`). It also bypasses echo cancellation: it stays on the raw
mic, the override that works even if every other layer fails.

`actions.py`'s `ACTIONS` dict is the actual security boundary: the
matcher/LLM only ever select a key into a fixed dict of plain Python
functions (never generate or execute arbitrary code or shell commands),
and every LLM-produced field is re-validated against real enums in
`llm_intent.py` before it ever reaches `actions.py` -- defense in depth,
the LLM's grammar constraint is never trusted as the only layer.

## Project layout

```
main.py              orchestrator: audio streams, wake word, VAD, session
                      state machine, control server, single-instance guard
actions.py           the execution layer -- every allowlisted action as a
                      plain Python function; the only thing ever dispatched to
apps_registry.py     the registered-apps table -- 8 hand-curated entries
                      (chrome/discord/vscode/file_explorer/settings/
                      local_disk_d/terminal/spotify) plus whatever
                      app_discovery.py finds; the actual security boundary
                      for "open/minimize apps in general"
app_discovery.py     scans the Start Menu at startup to expand apps_registry's
                      table automatically -- never overrides a curated entry
intent_matcher.py    fast path: local sentence-embedding similarity match
local_embedder.py    ONNX inference for bge-small-en-v1.5 (bypasses fastembed's
                      HF auto-download entirely)
llm_intent.py         fallback path: local Qwen2.5-1.5B-Instruct via
                      llama-cpp-python, grammar-constrained JSON output
monitors.py           physical-display identification -- laptop vs external
                      monitor, via QueryDisplayConfig (not the primary-monitor
                      flag, which is backwards for this) -- and the spoken-
                      mention resolver ("my laptop screen", "the other screen")
window_context.py    window ranking for "which one did you mean" when several
                      windows of the same app are open -- title-hint matching,
                      monitor bias, recency, and a cycle-on-repeat tracker
app_titles.py         turns a raw window title into something worth saying
                      out loud, per app (VS Code/Chrome/Spotify/Discord) --
                      pure string functions, no win32
seed_promotion.py    logs LLM-resolved phrases; promotes frequent ones into
                      the fast path on request (manual, not automatic -- see
                      KNOWN_LIMITATIONS.md)
vad.py               Silero VAD wrapper (speech/silence probability per frame)
virtual_desktop.py   IVirtualDesktopManager COM wrapper (current-desktop check)
tts.py               Piper TTS playback, mic-mute-while-speaking, persistent
                      output stream
output_monitor.py    detects whether the system's default audio output is
                      playing anything at all -- Spotify, a video, anything,
                      not just Jarvis's own voice (tts.py's is_speaking()
                      only covers that) -- via IAudioMeterInformation on the
                      render endpoint; gates wake-word detection and session
                      dispatch while it's true *and* echo cancellation isn't
                      covering it (push-to-talk is unaffected)
loopback.py          WASAPI loopback capture of whatever Windows is sending
                      to the default output (hand-rolled comtypes, 16kHz mono
                      via AUTOCONVERTPCM) -- the echo canceller's reference;
                      gap-fills idle silence, reopens when the default
                      output changes
echo_canceller.py    WebRTC AEC3 (livekit's AudioProcessingModule, lazily
                      imported) on the main capture stream, plus the
                      fail-closed "is cancellation actually working on this
                      output right now" decision the output gate consults,
                      the reference delay aligner, and the session echo floor
hud.py               the terminal display -- single locked writer for all
                      user-facing output, pinned status bands, live mic meter,
                      in-place line rewriting. `python hud.py` runs a
                      standalone preview
earcons.py           short non-verbal cues synthesized with numpy at import
                      (wake blip, understood, decline, error, spin-down)
phrases.py           reply phrasing as pools of variants, one place to change
                      anything Jarvis says
jarvisctl.py         one-shot control-socket client (status/stats/debug/
                      aec/pause/resume/quit)
jarvis.cmd           `jarvis <command>` wrapper, `start` handled here directly
launch.ps1           the actual `main.py` launch command (see DECISIONS.md's
                      "Voice replies and terminal theming" section for why
                      this is a separate file)
scheduler/           Task Scheduler XML templates + register_tasks.ps1 for
                      autostart/quit-on-sleep/restart-on-wake -- see
                      scheduler/README.md
tools/               standalone, hand-run scripts -- not part of the live
                      pipeline, never imported by main.py or anything else:
  _bench_llm.py         latency/accuracy benchmark for llm_intent.py, run
                      by hand after changing the model/prompt/schema
  _spike_desktop_move.py  one-off verification that MoveWindowToDesktop
                      works on a window this process doesn't own -- kept
                      as the record of why move-to-desktop was dropped
                      (E_ACCESSDENIED, see DECISIONS.md's "Screen and
                      window context" section)
  _spike_aec.py         measures echo cancellation on the current default
                      output -- raw vs AEC3 vs the driver's own SPEECH/
                      COMMUNICATIONS modes; run once per output device
                      (see DECISIONS.md's "System output, round two")
requirements.txt      pinned dependencies (excl. llama-cpp-python, see
                      README.md's Setup section)
models/               model weights (gitignored -- see README.md's
                      "Fetching the models")
tests/               pytest suite -- no audio device, no models,
                      no window manager required
logs/                 headless-mode output, structured event log
                      (session.jsonl), and LLM usage/seed-promotion state,
                      all gitignored
```
