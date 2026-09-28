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
  a few seconds, a cold one -- first call in a process, before the
  background prewarm finishes, or after the pre-LLM gate's rejection
  path -- more like 13-15s post-shrink, see DECISIONS.md's "Latency"
  section) runs inline in the same thread that watches for the next
  utterance. Nothing is lost during this window -- incoming audio still
  queues (`audio_q`, unbounded) and gets processed once the thread frees
  up, though nothing currently trims that backlog if a very long dispatch
  lets it grow -- but Jarvis is genuinely unresponsive-feeling for that
  stretch. Fixing this properly means the session state machine tracking
  an in-flight command while still watching for the next utterance
  concurrently; not done, since it is a real restructuring of the state
  machine, not a contained fix.
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
  all, by design.** `TRANSCRIPT_GATE_ENFORCED`'s own comment is explicit:
  a cosine match ≥0.80 against a seeded phrase "must keep dispatching
  regardless" of Whisper's own no_speech_prob/avg_logprob/
  compression_ratio -- that gate only ever applies to the LLM-fallback
  branch. `output_monitor.py` (see DECISIONS.md) closes the specific
  case of the system's *own* audio output leaking back in, but nothing
  stops some other source of ambient noise -- a TV in another room, a
  conversation nearby, a notification sound this machine didn't play --
  from being transcribed into something that happens to cosine-match a
  seed and dispatching anyway. Deliberately not changed here: applying
  the quality gate to the fast path too is a real behavior change with
  real trade-offs (risking false-rejection of quiet legitimate commands)
  that this project's own stated pattern is to make from logged data,
  not a guess -- revisit if `wake_triggered`/`transcript_would_reject`
  logging surfaces a real pattern worth tuning against.
