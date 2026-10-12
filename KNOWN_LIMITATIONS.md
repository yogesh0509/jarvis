# Known limitations / open items

Things that are known to be incomplete, provisional, or deliberately
deferred -- not bugs waiting to be fixed by accident, but real gaps worth
knowing about before relying on or extending the relevant area. See
`DECISIONS.md` for the reasoning behind the design these limitations sit
on top of.

- **No UI Automation / Accessibility API integration.** Everything this
  project knows about a window's content comes from its title string
  (`app_titles.py`) -- it can say what VS Code file or Chrome tab you're
  on, but it can't read Chrome's actual URL, a document's text, or
  anything a title doesn't already carry. UIA is reachable from this
  codebase's existing dependencies with no new install (raw `comtypes`
  already used by `virtual_desktop.py` can load `UIAutomationCore.dll`
  directly), but was deliberately not pursued: `IUIAutomationElement`
  exposes on the order of 80 methods where `IVirtualDesktopManager` has
  3, and a wrong hand-written vtable declaration doesn't raise a
  catchable exception the way a COM error does -- it calls whatever
  function pointer happens to sit at that offset, a hard crash in the
  one process that must not lose its listener thread over it. Revisit
  only if a concrete need survives everything title-based context
  already covers, and only via the `uiautomation` PyPI package (a
  maintained, tested vtable binding) rather than hand-declared
  `COMMETHOD`s.
- **`logs/session.jsonl` is never rotated or pruned.** One line per
  event, so realistically kilobytes per day -- but it grows without
  bound across a long-running install. Delete it whenever; nothing reads
  it back.
- **The HUD's pinned bands depend on the terminal honouring DECSTBM
  scroll regions.** Windows Terminal does, and there's a fallback to
  plain text when stdout isn't a terminal at all -- but a terminal that
  accepts VT sequences while ignoring `ESC[{top};{bottom}r` would scroll
  the reserved rows away rather than degrading cleanly. `python hud.py`
  renders the whole display standalone, which is the fastest way to
  check a given terminal before wiring anything else to it.

- **The session listener blocks synchronously while handling one
  command.** Transcription + action dispatch (up to ~5s for a cold app
  launch that has to wait for a window to appear; a warm LLM fallback is
  a few seconds) runs inline in the same thread that watches for the
  next utterance. A genuinely cold LLM call -- no persisted KV cache yet
  to load (see "Latency" in DECISIONS.md for the original 13-15s
  post-shrink measurement, and "Persisted KV cache" for what changed) --
  is still the worst case, but this now only happens once per install
  (or once after `llm_intent.SYSTEM_PROMPT_TEMPLATE` itself changes,
  which invalidates the persisted cache by design): every later boot
  loads the saved state in well under a second instead of re-running the
  full prefill. Nothing is lost during this window -- incoming audio
  still queues (`audio_q`, unbounded) and gets processed once the thread
  frees up, though nothing currently trims that backlog if a very long
  dispatch lets it grow -- but Jarvis is genuinely unresponsive-feeling
  for that stretch, worst-case or not. Fixing this properly means the
  session state machine tracking an in-flight command while still
  watching for the next utterance concurrently; not done, since it is a
  real restructuring of the state machine, not a contained fix.
- **"Hey jarvis" occasionally not registering for a stretch right after
  boot** was reported once; the persistent push-to-talk stream fix
  addresses one plausible contributor (the same device cold-open glitch
  class), but this wasn't confirmed as the full explanation.

  **One mechanism for this is now confirmed (2026-09-17), and it was not
  the wake word at all.** After a 13-hour suspend, the Intel SST capture
  driver reloaded its DSP firmware on resume and came back delivering a
  constant 1 LSB (`1/32768`) on every frame, via every host API and every
  device index. Windows reported the endpoint active, unmuted, at 100%,
  Device Manager said `OK`, and its own peak meter sat pinned at exactly
  `1.000000` -- so nothing Windows exposes admitted a fault. Jarvis was
  structurally unable to notice: `INPUT_WATCHDOG_SECONDS` only fires when
  callbacks *stop*, and they never did. `jarvis status` reported
  "Wake-word armed (awake)" for 50 minutes while nothing could be heard,
  and push-to-talk bailed at its VAD gate to the HUD only, logging
  nothing. A full Jarvis restart did not clear it; only an audio-stack
  reset did.

  Now detected: see `INPUT_SILENCE_SECONDS` in `main.py`, which latches
  after two minutes of unbroken digital silence on a *running* stream,
  attempts one reopen, logs `input_silent`, and makes `jarvis status` say
  `NO MIC SIGNAL` instead of implying health. Push-to-talk's no-speech
  bail now logs its peak, so "said nothing" and "device is dead" are
  distinguishable after the fact.

  Detection, not prevention -- **the remaining open question is whether
  Jarvis contributes to causing the wedge.** It never observes power
  events (see `scheduler/README.md`), it holds a capture stream open
  across suspend even while paused, and on 09-17 its sleep-triggered quit
  was frozen mid-teardown by the suspend and finished 3 seconds after
  resume, 4 seconds into the DSP firmware reload -- closing
  suspend-invalidated handles into a driver that was still
  reinitialising. The one earlier cycle that also survived a suspend
  finished its teardown 24s after wake, clear of the reload, and the mic
  came back fine. That is n=1 with a plausible mechanism, not proof;
  these DSPs also wedge on their own. `jarvisctl.py quit silent` (now
  used by `Jarvis Quit On Sleep`) removes the several seconds of blocking
  Piper playback from that path so the teardown finishes before the
  suspend rather than straddling it. If the wedge recurs on a cycle that
  used the silent quit, Jarvis is exonerated and it is purely a driver
  bug to report upstream.

  **Update (2026-09-22): the silent-quit fix had never actually taken
  effect.** The XML template was edited but `register_tasks.ps1` was
  never re-run, so the live scheduled task kept calling plain `quit`
  through a second full incident -- a 12h10m suspend, shutdown finishing
  30s after wake this time, worse than the first incident's 3s. Now
  registered live and confirmed via `Get-ScheduledTask`; the next sleep
  cycle is the first real test of it.

  **Update (2026-09-24): a second, independent mechanism was found and
  fixed, unrelated to sleep.** Auditing every touchpoint of the shared
  `stream` variable (asked directly: is Jarvis's own state management
  responsible?) found it was never synchronized against concurrent
  access at all -- the listener thread's own watchdog/reopen and the
  control-socket thread's pause/resume/quit could genuinely race on the
  same capture stream with no sleep cycle involved. Confirmed real (not
  just theoretical) with a scratch copy of `main.py` with the lock
  stripped: a tight pause/resume-vs-reopen stress loop reliably left more
  than one capture stream open on the same device. See DECISIONS.md's
  "The stream-lock race" for the full account. Not confirmed as the
  cause of either past wedge -- both align cleanly with the sleep-quit
  timing race instead -- but fixed regardless (`_stream_lock`), since a
  real hazard doesn't need to have fired yet to be worth closing.

  **If `jarvis status` says `NO MIC SIGNAL`:** restarting Jarvis will
  **not** fix it -- Windows will keep reporting the microphone as
  enabled, unmuted, and "OK" in Device Manager throughout. What actually
  clears it:
  1. Settings -> System -> Sound -> Input, and switch the input device
     away and back.
  2. If that doesn't do it, an elevated PowerShell:
     `Restart-Service AudioEndpointBuilder -Force`.
  3. If it still won't come back, disable and re-enable the microphone's
     driver in Device Manager under **Sound, video and game
     controllers**.
  Then `jarvis quit` and `jarvis start`. This is deliberately hard to
  trigger by accident -- a merely quiet room does not set off
  `INPUT_SILENCE_SECONDS` (see "Correction: amplitude alone was the
  wrong test" in DECISIONS.md) -- so if you see it, it's real.
- **`VAD_SPEECH_THRESHOLD` (0.15) and the onset debounce (3 frames) are
  provisional** -- reasonable given the evidence so far, but the exact
  cause of at least one false-positive ("bdk"/"boom" transcribed from
  what was assumed to be background fan noise) was never independently
  confirmed. Revisit either value if quiet speech starts getting missed,
  or if background noise still occasionally gets treated as speech.
- **The fast embedding-match path has no acoustic-confidence gate at
  all, by design -- except for the five social/farewell seeds in
  `_ACOUSTICALLY_GATED_SEEDS`** (see DECISIONS.md's "Social intents and
  the acoustic gate"), which do get one, narrowly, because those exact
  phrases are also confirmed near-silence hallucinations. Every other
  seed still dispatches unconditionally: `TRANSCRIPT_GATE_ENFORCED`'s
  own comment is explicit that a cosine match ≥0.80 against a seeded
  phrase "must keep dispatching regardless" of Whisper's own
  no_speech_prob/avg_logprob/compression_ratio -- that gate (and now
  the social-intent one) only ever applies to the phrases they were
  built for. `output_monitor.py` and `echo_canceller.py` (see
  DECISIONS.md) close the specific case of the system's *own* audio
  output leaking back in, but nothing
  stops some other source of ambient noise -- a TV in another room, a
  conversation nearby, a notification sound this machine didn't play --
  from being transcribed into something that happens to cosine-match an
  ordinary command seed and dispatching anyway. Deliberately not
  changed further here: extending the quality gate to the rest of the
  fast path is a real behavior change with real trade-offs (risking
  false-rejection of quiet legitimate commands) that this project's own
  stated pattern is to make from logged data, not a guess -- revisit if
  `wake_triggered`/`transcript_would_reject` logging surfaces a real
  pattern worth tuning against.
- **Echo cancellation has been measured on one output so far.** The
  numbers in DECISIONS.md's "System output, round two" are the laptop
  speakers at 70% with a TTS stimulus and nobody talking. Still
  unmeasured:
  - double talk (`tools/_spike_aec.py --talk`);
  - the HDMI monitor's speakers, the Bluetooth speaker and earbuds;
  - real music and film audio.

  Every threshold in `echo_canceller.py` is provisional until those
  runs exist. Known ways it can fall short, all of which fail closed
  (the output gate re-engages, exactly as before) rather than open:
  - **Very loud playback on small speakers.** Speaker distortion is
    nonlinear and a linear canceller can't model it, so the residual
    rises past `AEC_RESIDUAL_MAX_DBFS`.
  - **Apps rendering in WASAPI exclusive mode.** Their audio bypasses
    the shared mix that loopback taps, so there is no reference.
  - **The first second or two of any playback, or after switching
    output devices**, while it converges.

  The failure that does *not* fail closed: the session echo floor
  (`ECHO_ONSET_MARGIN_DB`) could refuse a genuinely quiet command
  spoken over loud audio. It's logged as `echo_onset_rejected` for
  exactly that reason. Commands spoken over loud audio can also simply
  be misheard: nothing ducks the volume while Jarvis listens (deferred
  deliberately -- it changes a system setting and turns Jarvis's own
  voice down with it).
- **`memory.json`'s `notes` and `preferences` keys exist and are read
  by nothing.** Both were added to `memory._DEFAULTS` anticipating a
  note-taking/preferences feature that was never built -- there is no
  voice command that writes to either one. `morning_briefing` (see
  DECISIONS.md's "Proactive briefing on boot") deliberately omits both
  from what it reports for exactly this reason, rather than reporting
  an empty list/dict as if it meant something. `last_layout`, the third
  key, *is* wired up (same section) but only written at a clean
  shutdown -- a crash or `taskkill` leaves it stale until the next
  clean one, a known, low-stakes gap rather than a second write path
  added just to close it.
