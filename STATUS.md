# Status

Phase-by-phase build history, oldest decisions first. See `DECISIONS.md`
for the reasoning behind any of these, and `KNOWN_LIMITATIONS.md` for
what's still open.

- [x] Phase 0 -- native Windows Python 3.12, venv at `D:\jarvis\.venv`
- [x] Phase 1 -- faster-whisper `small.en` transcription (superseded the
      original `tiny.en` prototype, `phase1_transcribe.py`, since removed)
- [x] Phase 2 -- `intent_matcher.py` local embedding-based fast path
- [x] Phase 3 -- `actions.py`, virtual-desktop-aware, foreground-lock handling
- [x] Phase 3.5 -- `main.py` full loop wired end to end, single-instance guard
- [x] Phase 4 -- wake-word sessions replacing per-command push-to-talk as
      the primary trigger; command-line control; 30-minute idle model unload
- [x] Phase 0.5 -- autostart at logon, quit-on-sleep, restart-on-wake
- [x] Phase 6 -- spoken TTS replies on state transitions; themed fullscreen
      terminal
- [x] Phase 5 -- local LLM intent fallback (Qwen2.5-1.5B, grammar-constrained),
      generalized multi-app actions, virtual-desktop navigation, read-only
      query actions, transcription accuracy fixes
- [x] Phase 5 refinements -- broadened fast-path phrasing + manual seed
      promotion; all conversation replies spoken, not just state
      transitions; friendlier/faster-tuned voice; snappier end-of-utterance
      timing; session-open readiness cue; VAD onset debounce; TTS
      self-hearing fix; persistent audio streams (input and output) with
      device-failure recovery; listener-thread crash containment
- [x] Phase 7 -- hardening pass (thread-affinity fix for virtual-desktop
      COM calls; VAD rebuffered to Silero's native window size, cutting
      its noise false-positive rate roughly 5x; seed-promotion label
      allowlist + confidence gate against Whisper hallucinations; unload
      races closed with a reentrant processing lock; input-stream
      watchdog; a pytest suite for the previously-untested pure logic)
      plus a round of feature work: local-LLM small talk/general
      questions (the model also answers anything that isn't a command,
      honestly declining anything needing live data); one turn of
      pronoun memory ("open chrome" then "minimize it"); push-to-talk
      barge-in to cut off a long reply mid-sentence (see `tts.py` for
      two real concurrency bugs found and fixed getting this right);
      dictation mode (`take dictation`/`stop dictation`, typed via
      `keyboard.write()`); system/media controls (volume, mute, media
      transport, lock, maximize, Windows' own window-snap) as a new
      no-parameter action class; an auto-discovered app registry
      (`app_discovery.py` scans the Start Menu at startup, folded into
      `apps_registry.py`'s curated table); and a `jarvis stats` command
      for basic observability (match-rate breakdown, last 20
      utterances). Also closed two bugs found in real use after this
      phase: an unhandled `AttachThreadInput` failure in
      `_force_foreground` that could crash the listener thread, and the
      LLM fallback picking an unrelated app when nothing registered
      actually matched (now declines honestly instead of guessing) --
      see `actions.py`, `apps_registry.py`'s `terminal` entry, and
      `llm_intent.py`'s "don't guess" rule.
