# Design decisions

The decision log for this project -- why the system works the way it
does, not just what it does. Every entry here came from a real bug, a
measurement, or a rejected alternative; nothing is a guess dressed up as
a rationale. This is the first place to check before re-deriving a
design choice from scratch. Project overview and setup live in
`README.md`; pipeline shape and the module-by-module map live in
`ARCHITECTURE.md`; day-to-day behavior and state diagrams live in
`USER_GUIDE.md`.

## Contents

- [Listening: wake-word sessions, not per-command push-to-talk](#listening-wake-word-sessions-not-per-command-push-to-talk)
- [Push-to-talk: a persistent stream, not one opened per press](#push-to-talk-a-persistent-stream-not-one-opened-per-press)
- [Reliability: the listener thread doesn't die on error anymore](#reliability-the-listener-thread-doesnt-die-on-error-anymore)
- [Input health: a running stream that hears nothing](#input-health-a-running-stream-that-hears-nothing)
- [The stream-lock race: two threads, one capture device](#the-stream-lock-race-two-threads-one-capture-device)
- [System output: Spotify is not Jarvis's own voice](#system-output-spotify-is-not-jarviss-own-voice)
- [System output, round two: echo cancellation](#system-output-round-two-echo-cancellation)
- [State-machine audit against the documented diagram](#state-machine-audit-against-the-documented-diagram)
- [Command-line control (`jarvisctl.py`), not a tray icon](#command-line-control-jarvisctlpy-not-a-tray-icon)
- [Idle unload after 30 minutes ("deep sleep")](#idle-unload-after-30-minutes-deep-sleep)
- [Transcription accuracy](#transcription-accuracy)
- [Intent resolution: local embedding match, local LLM fallback](#intent-resolution-local-embedding-match-local-llm-fallback)
- [Mid-session wake-word swallow, and the "bdk wallet" false dispatches](#mid-session-wake-word-swallow-and-the-bdk-wallet-false-dispatches)
- [Latency: what the ">30s" actually was, and what changed](#latency-what-the-30s-actually-was-and-what-changed)
- [Virtual-desktop awareness](#virtual-desktop-awareness)
- [Screen and window context](#screen-and-window-context)
- [Voice replies and terminal theming](#voice-replies-and-terminal-theming)
- [Exact volume control via IAudioEndpointVolume](#exact-volume-control-via-iaudioendpointvolume)
- [Repairing mishearings after the fact, not biasing decode toward them](#repairing-mishearings-after-the-fact-not-biasing-decode-toward-them)
- [Feedback integrity: actions.Outcome, and the room lights actually knowing](#feedback-integrity-actionsoutcome-and-the-room-lights-actually-knowing)
- [Observability: durable timings, log rotation, and tools/_report.py](#observability-durable-timings-log-rotation-and-tools_reportpy)
- [The push-to-talk hold threshold](#the-push-to-talk-hold-threshold)
- [Persisted KV cache: a spike that won](#persisted-kv-cache-a-spike-that-won)
- [Social intents and the acoustic gate](#social-intents-and-the-acoustic-gate)
- [Proactive briefing on boot](#proactive-briefing-on-boot)
- [Removed: "how many files are in this folder"](#removed-how-many-files-are-in-this-folder)

## Listening: wake-word sessions, not per-command push-to-talk

Started on pure push-to-talk (hold a hotkey per command) for speed and
privacy -- no VAD delay, hotkey release is an instant end-of-utterance
signal. Real use showed the friction wasn't worth it: repeating a
physical hold for every single command got old fast. Landed on a middle
ground instead of pure always-on listening (which has a real
false-trigger risk -- any speech in the room could get matched and
*executed*, not just transcribed): say "hey jarvis" once to open a
session, then speak any number of commands, each auto-segmented by
silence (VAD), until a stop phrase ("power down", "we're done", ...)
closes it or 4 minutes of silence auto-closes it. RIGHT CTRL push-to-talk
still works too, any time, independent of session state -- see `main.py`'s
three-mode listener loop (`wake` / `session_listening` /
`session_recording`).

**A short spoken cue ("Yes, sir?") now plays the instant a session
opens.** openWakeWord needs a rolling window of audio to build
confidence, so its score crosses threshold a beat after "hey jarvis"
actually finishes -- anything said right on its heels with no pause could
start before the mode switch lands and come through clipped or garbled
(seen directly: "open chrome for me" transcribed as "or been grown for
me"). Since the mic mutes itself for the duration of any spoken reply
(see the TTS section below), nothing said *during* the cue is lost, just
deferred -- the cue gives an unambiguous "now" to speak after, at the
cost of a small, deliberate delay between the wake word and when Jarvis
can actually hear you.

**Speech-onset requires 3 consecutive VAD-positive frames (~240ms), not
one.** A single frame crossing `VAD_SPEECH_THRESHOLD` isn't strong
evidence of real speech -- continuous background noise can spike above
threshold for one frame without being speech at all (suspected cause of
an isolated word like "bdk"/"boom" getting transcribed out of a quiet
room, though the exact noise source was never confirmed). The
already-existing ~300ms pre-buffer means this debounce costs nothing
against real speech -- the actual head of a genuine utterance is captured
regardless of exactly when the debounce confirms; it only filters out
onsets that don't sustain.

Wake word: `openwakeword`'s pretrained `hey_jarvis` model. End-of-utterance
detection: the `silero_vad.onnx` model openWakeWord's own downloader
fetches (`vad.py`) -- both came from GitHub release assets, never blocked
by the TLS issue documented in `ENVIRONMENT.md`.

## Push-to-talk: a persistent stream, not one opened per press

Originally opened a brand-new `sd.InputStream` on every key-press and
tore it down on release. Windows reliably glitches/drops the first
chunk(s) written to *or read from* a freshly-opened audio device (a
driver/DAC warm-up artifact -- confirmed on the output side first, see
the TTS section below, then found to plausibly apply here too): for a
short press, a corrupted first chunk can be the entire utterance. Fixed
by opening the hotkey's `InputStream` once at startup and keeping it
running for the process's life; `on_press`/`on_release` now just toggle
whether its callback is actually buffering frames, rather than
opening/closing the device each time.

Push-to-talk also gates on VAD (`hotkey_vad`, a separate `SileroVAD`
instance -- it carries recurrent state across calls, so sharing one
between two independent audio streams would corrupt both) before ever
calling into Whisper: on near-silent audio, Whisper's `hotwords` bias
(see "Transcription accuracy" below) can hallucinate fragments of that
same vocabulary as if they'd been spoken (seen directly: a push-to-talk
tap with no real speech transcribed as literal chunks of
`TRANSCRIBE_HOTWORDS`, e.g. "how many files folder windows bdk wallet bdk
wallet..."). If no frame in the captured audio crosses
`VAD_SPEECH_THRESHOLD`, Whisper is never called at all.

Processing (transcribe + dispatch) runs on a worker thread, not inline in
`on_release`: the `keyboard` library dispatches `on_press`/`on_release`
from a single shared background thread
(`keyboard/_generic.py`'s `GenericListener.process()`), so running
several-second work there blocked *all* future key events, including the
very next press, until it finished -- a quick follow-up press-and-release
during that window had its `on_press` (which arms the capture) queued
behind the still-running `on_release`, genuinely losing whatever was said
in the gap. `on_release` now just snapshots the audio and hands the rest
to a daemon thread, returning immediately. A lock (`_processing_lock`,
shared with the wake-word session path below) serializes actual
transcription/dispatch, since both paths call into the same
`whisper_model`/`matcher`/`llm_intent` objects, which aren't safe to
drive from two threads at once.

## Reliability: the listener thread doesn't die on error anymore

An uncaught exception anywhere in the session listener's per-chunk
handling used to kill that whole thread silently -- the process kept
running and `jarvis status` kept reporting "Wake-word armed" as if
nothing were wrong, but nothing was actually listening until a full
restart. Root-caused live: `count_files_in_active_explorer_folder()`
calling `win32gui.GetClassName()` on a foreground-window handle that can
legitimately be `0`/stale (`pywintypes.error: Invalid window handle`) --
now guarded there directly, but the systemic fix is in `main.py`'s
listener loop: the whole mode-dispatch block is wrapped in try/except,
logging the traceback, saying something ("Sorry, sir, I stumbled on that
one"), and resetting to a known-good `wake` state instead of letting the
thread die, so any *future* unhandled exception (this one or a new one)
degrades gracefully instead of silently going deaf.

**The containment has a boundary, and it was crossed once already.** The
try/except above wraps the mode-dispatch block specifically -- it starts
at that `try:`, not at the top of the loop. When the output-gate check
(`output_monitor.py`, see "System output" below) was added between
`audio_q.get()` and that `try:`, it ran uncontained: on its very first
falling edge, an exception there killed the listener thread outright,
about 5 seconds after boot, while `jarvis status` went on reporting
"Wake-word armed" to a room nothing was listening to. Nothing printed it
either -- under `jarvis start`, Python's default `threading.excepthook`
writes to a terminal nobody reads. Two fixes, not one: the output-gate
check itself now has its own try/except, and `main.py` installs
`_log_dead_thread` as `threading.excepthook` so *any* thread that dies,
from any future addition in this gap, is recorded to `session.jsonl`
instead of vanishing silently. The lesson generalizes: anything inserted
between the queue read and the mode-dispatch `try:` needs its own
containment -- it is not covered by proximity to code that is.

## Input health: a running stream that hears nothing

Every liveness signal in this process answered the wrong question.

`INPUT_WATCHDOG_SECONDS` fires from the `queue.Empty` branch of
`listener_loop` -- it can only ever notice a stream that **stopped**.
`state["main_last_chunk"]` is stamped inside `audio_callback` regardless
of what the callback was handed. The listener heartbeat, the HUD's level
meter, the `jarvis status` line: all of them confirm the machinery is
turning, none of them look at what is going through it.

So a capture device that keeps calling back on schedule while delivering
pure digital silence is invisible to all of it, and that is not a
hypothetical. On 2026-09-17, after a 13-hour suspend, the Intel SST
driver reloaded its DSP firmware on resume and came back handing over a
constant `1/32768` -- one 16-bit LSB, identical every frame, on every
host API and every device index. Windows admitted nothing: endpoint
active, unmuted, 100%, Device Manager `OK`, and its own peak meter
**pinned at exactly `1.000000`**, which is itself a stuck reading rather
than a plausible one. `jarvis status` said "Wake-word armed (awake) · LLM
warm" for 50 minutes. Push-to-talk bailed at its VAD gate every time,
printing to the HUD and logging nothing, so the session log contained no
evidence that anything had been tried at all.

Three changes, and the ordering of their importance is the point:

1. **Say it.** `input_silent` latches after `INPUT_SILENCE_SECONDS` of
   unbroken silence and `_status_line` appends `NO MIC SIGNAL`. This
   matters most, because the status line is read precisely when
   something is already suspected, and "armed" was an active lie.
2. **Log it.** An `input_silent` event, and a peak on push-to-talk's
   no-speech bail -- which is otherwise indistinguishable after the fact
   from someone holding the key and saying nothing.
3. **Try a reopen.** Once, not repeatedly. The recoverable version of
   this exists, but a wedged *driver* survives a reopen and survives a
   full process restart (both confirmed here), so retry loops would only
   bury the log line that actually matters.

The window is two minutes -- long enough that a silent room at 3am never
trips it, short enough to catch a dead device inside one sitting.
Detection only; see `KNOWN_LIMITATIONS.md` for the open question of what
*causes* the wedge and `scheduler/README.md` for the silent-quit change
that removes Jarvis's most plausible contribution to it.

### Correction: amplitude alone was the wrong test

This section originally said the floor was "deliberately just above one
LSB: the wedged signature is exactly `3.05e-5`, and a genuinely quiet
room still fluctuates well above that." **The second half of that was
wrong, and it was never measured -- it was assumed.** Measured properly
on the same Intel SST array afterwards, in an ordinary quiet room:

```
frames=187   below-1e-4 frames=182 (97.3%)
distinct values among the quiet frames: 3  -> 1, 2 and 3 x LSB
```

**97% of frames in a normal quiet room sit under the floor**, because
the DSP's own noise suppression floors a quiet room to 1-3 LSB. So the
floor-only latch could not tell "dead device" from "nobody is talking",
and in practice it fired on ordinary silence roughly every two minutes,
each time reopening a perfectly healthy capture stream -- on a driver
this project already knows is fragile, and with the documented
cold-open glitch on every reopen. Reported from the HUD, where it showed
up as an endless pair of warnings.

What actually separates the two states is **variation, not level**:

| | peak level | distinct values |
|---|---|---|
| wedged converter | ~1 LSB | **exactly one**, forever |
| quiet room | 1-3 LSB | several, dithering |

So the latch now requires both conditions: under the floor for the whole
window **and** every quiet frame carrying the exact same value
(`INPUT_SILENCE_CONSTANT_ONLY`). A live converter dithers; a frozen one
repeats. Erring toward "silence is normal" is deliberate -- a false
alarm costs a scary message plus a needless reopen of a healthy stream,
while a late detection costs only lateness.

The general lesson, worth keeping: **an assumption about what the
hardware does is not evidence about what the hardware does.** The
original floor was picked from a single measurement of the *broken*
state without ever measuring the *healthy* one it had to be
distinguished from.

## The stream-lock race: two threads, one capture device

Asked directly, after a second mic-wedge incident: is Jarvis's own state
management causing this, independent of the sleep-suspend timing already
fixed? Auditing every touchpoint of `main.py`'s shared `stream` variable
(the main capture stream, reassigned across pause/resume/reopen/quit)
found a real one: **nothing synchronized it at all.**

Four sites wrote it, across two threads, with no lock:

- `_reopen_input_stream` (listener thread — the 5s watchdog, and the
  silence latch above) stops/closes the old stream and creates a new one.
- `_set_enabled`'s resume branch (control-socket thread) creates one if
  `stream is None`.
- `_set_enabled`'s pause branch (control-socket thread) stops/closes it.
- `_shutdown` (control-socket thread, or `KeyboardInterrupt`) stops/
  closes it.

None of this needs a sleep cycle to matter. An ordinary `jarvis pause`
landing at the same moment the listener thread's own watchdog decides
the stream needs reopening is enough: a resume could see `stream is
None` set by a reopen already in flight and open a *second* concurrent
capture stream on the same physical device — the first orphaned, never
stopped, its callback still attached — or a reopen's `stream.start()`
could race a pause's `stream.close()` on the very same object. Confirmed
this is not theoretical: a scratch copy of `main.py` with the lock
stripped out reliably left more than one stream simultaneously open
under a tight pause/resume-vs-reopen stress loop
(`test_concurrent_reopen_and_pause_resume_never_leaves_two_streams_open`);
with the lock restored, the same test holds regardless of how the two
threads interleave.

**Not confirmed as the cause of either past wedge incident** — both of
those align cleanly with the sleep-quit timing race instead (see "Input
health" above and `scheduler/README.md`'s "Why the sleep quit is
silent"), and neither showed direct evidence of a pause/resume landing
mid-reopen. But it's a real, previously-unsynchronized hazard regardless
of whether it has ever actually fired, and exactly the kind of thing
that could leave a flaky driver like Intel SST wedged without a sleep
cycle anywhere in the picture.

`_stream_lock` is a new, separate `threading.Lock()` — deliberately not
`_processing_lock`, which can be held for up to 30s by an in-flight LLM
call and which `_set_enabled`'s own docstring already refused to take
for exactly that reason (pause is the privacy control; it must not wait
behind a slow model call). `_stream_lock` only ever guards a few quick
PortAudio calls, so serializing on it costs nothing pause couldn't
already cost.

## System output: Spotify is not Jarvis's own voice

Reported directly: "Jarvis is listening to Spotify music and reacting
based on it." Confirmed in `logs/session.jsonl` -- a session's worth of
real fast-path dispatches (`volume_down`, `volume_up`) appeared shortly
after Spotify started playing, with **no wake-word transcript anywhere
between them and the previous logged session close**, and `peak: 1.0`
on nearly every one of them. The wake word almost certainly
false-triggered on the music itself and silently reopened a session --
"almost certainly" because that transition had zero trace in the
structured log until this incident (see `wake_triggered`, added
immediately after).

`is_speaking()` (tts.py) already solves the adjacent problem -- Jarvis
hearing its own spoken replies -- but it is a counted flag scoped to
Jarvis's *own* TTS stream specifically. It has no way to know anything
about Spotify, a video, or any other app's audio, because nothing hooks
into them. The only way to answer "is anything coming out of the
speakers right now" without an integration into every app that might be
playing something is to ask Windows directly: `output_monitor.py` polls
`IAudioMeterInformation` on the default render endpoint (the same
Core Audio API family already used by `virtual_desktop.py` and
`monitors.py`) and exposes a debounced `is_output_playing()`.

Two design choices worth recording:

- **Gates wake-word detection and session dispatch, not just the fast
  path.** The false trigger was in the *wake word itself*, not just a
  transcript that happened to match a seed -- gating only dispatch would
  have left the session-opening problem untouched. The check sits at the
  same choke point `is_speaking()` already uses, ahead of the
  `mode == "wake"` dispatch, so wake-word prediction itself is skipped
  while output plays.
- **Push-to-talk is deliberately exempt.** The alternative -- gating the
  hotkey too -- would mean you could never voice-control the very thing
  that's playing ("pause the music") without first turning it down,
  which defeats a large fraction of what this project is for. Holding
  Right Ctrl and talking over the music is the explicit override, same
  as every other "keep working no matter what else is true" case
  documented for it elsewhere.

The trade-off this accepts, stated plainly: **"hey jarvis" (hands-free)
will not fire while system output is playing.** This is not acoustic
echo cancellation -- it doesn't attempt to isolate a real voice speaking
over music, only answers "is the render endpoint active". A future
"pause the music with your voice while it's still audibly playing"
request would need actual AEC (subtracting a loopback reference from the
mic signal), which this deliberately does not attempt. *(That request
came -- see "System output, round two" below. The gate stays, but only
holds while echo cancellation isn't demonstrably covering the output.)*

Two smaller decisions fell out of the debounce values chosen
(`ON_SECONDS`/`OFF_SECONDS`, deliberately asymmetric): engaging fast
matters more than releasing fast, because releasing too eagerly during
an ordinary quiet beat mid-song would flap the gate open and shut
constantly, while engaging slowly would let a moment of loud audio
through before the gate catches up. Both are provisional starting
points, same status as `VAD_SPEECH_THRESHOLD`/`WAKE_THRESHOLD` --
reasonable, not measured, revisit from real data if either turns out
wrong in practice.

The deep-sleep idle-unload check (`DEEP_SLEEP_IDLE_SECONDS`) was hoisted
out of the `mode == "wake"` dispatch block specifically because of this
gate: left where it was, a long playlist playing uninterrupted for well
over 30 minutes would have pinned the heavy models in memory for as long
as the output kept playing, since the check would never have run at all
while gated. It now runs independent of the output gate, immediately
above it.

## System output, round two: echo cancellation

The request the section above predicted: "a lot of time I want to voice
control when system audio is playing, such as movies, Spotify songs" --
over laptop speakers, an HDMI monitor's speakers, a Bluetooth speaker,
and earbuds. The cost of the hard gate, from `tools/_report.py` at the
time: 550 playback stretches, ~29.5h deaf to hands-free wake in total
(4.5% of the 660h the log spanned), and 94 of 99 `listener_gate_stuck`
events were this gate.

**What it does now.** `loopback.py` captures exactly what Windows is
sending to the default output (WASAPI loopback), and `echo_canceller.py`
subtracts its echo from the mic with WebRTC's AEC3 (via the `livekit`
package's `AudioProcessingModule`), inside the main capture callback,
before wake word / VAD / Whisper see anything. The output gate is kept,
but `_check_output_gate()` now returns *playing and not covered*:
cancellation has to be demonstrably working -- reference flowing for the
current endpoint, canceller healthy, converged, and a low percentile of
the post-cancellation level under `AEC_RESIDUAL_MAX_DBFS`. Anything else
(`unavailable`, `off`, `converging`, `residual_high`, `unhealthy`) is
the old gate exactly. `jarvis aec off` forces that at runtime.

**Measured before trusting it** (`tools/_spike_aec.py`, laptop speakers,
70% volume, Piper saying "Hey Jarvis, turn the volume down. Hey Jarvis,
skip this track. Pause the music. Open Chrome for me." -- echo only,
nobody speaking):

| pipeline | level during playback | VAD onsets | wake max | transcript |
|---|---|---|---|---|
| raw (the old capture path) | -37 dBFS | 4 | 0.99 | the whole stimulus, word for word |
| raw + AEC3 (now shipped) | -86 dBFS | 0 | 0.00 | *(empty)* |
| WASAPI, SPEECH category | -86 dBFS | 0 | 0.00 | *(empty)* |
| WASAPI, COMMUNICATIONS category | -85 dBFS | 0 | 0.00 | "you" |

~49dB of cancellation, ~2ms of CPU per 80ms block.

**The first two runs failed, and why is the most important thing here.**
AEC3 initially managed 6dB and let the wake word straight through. The
reference was arriving ~125ms *after* its own echo -- the reference
queue was allowed to sit 16 frames deep before being trimmed, and every
queued 10ms frame makes the reference 10ms later relative to the mic. An
echo that arrives before its reference can't be predicted by any
canceller. (The first run's analysis misread this as a 513ms *capture*
latency -- the spike was comparing windows that didn't line up. Fixed in
the spike too: it now analyses exactly what AEC3 was fed, frame for
frame.) The queue is now trimmed all the way back to the hold-back once
it runs `REFERENCE_SLACK_FRAMES` (6) past it. The real echo path on the
laptop speakers is short: ~25ms from loopback tap to captured frame.

`DelayAligner` covers outputs where it isn't short: it re-measures the
delay AEC3 is being asked to find (GCC-PHAT, on the fed pair, while
audio plays) and holds the reference back so AEC3 is left ~80ms. AEC3's
own delay search spans only about half a second, and a Bluetooth
speaker adds its latency *after* the loopback tap. It starts at zero
hold-back and moves only on a clear correlation peak and by more than
30ms. Every move makes AEC3 re-adapt, so coverage restarts convergence.

**Why software AEC rather than the driver's built-in modes, which
measured just as well.** The mic driver here (Intel SST array, Waves APO
plus Intel's Voca effect pack) advertises SPEECH and COMMUNICATIONS
processing modes, and sounddevice can request them via
`PaWasapiStreamInfo.streamCategory`. Both cancelled the laptop speakers
as well as AEC3. Not chosen as the shipped path, for now:
- **Coverage.** A DSP-internal echo reference most likely covers only
  the internal speaker path. HDMI and Bluetooth outputs are a must here
  and are unmeasured. Loopback taps whatever the default output is.
- **Signal.** Those modes gate silence to exact digital zero (-120 dBFS
  between words in the spike). That is exactly what `_check_input_signal`'s
  constant-quiet test reads as a wedged capture driver (see "Input
  health" above). Switching modes would also change the NS/AGC every
  wake/VAD/Whisper threshold was tuned on.
- **Fail-closed.** The software path can measure its own residual and
  say "not covering". A driver mode gives no such signal.

The spike measures all four pipelines on every run, so this can be
revisited per output with data rather than re-argued.

Other choices:
- **Transparent while nothing plays.** After 2s of silent reference,
  `process()` returns the raw mic (still feeding AEC3 so its state stays
  continuous). With nothing playing, the pipeline is bit-identical to
  before, so no threshold needed re-tuning.
- **Echo cancellation and the high-pass filter only.** No WebRTC noise
  suppression or AGC: the SST DSP already does its own ahead of us.
- **Input health is judged on the raw signal**, before `process()`.
  Cancelled audio can sit far quieter than anything the device actually
  delivered.
- **Push-to-talk stays raw.** It's the independent override, and it
  keeps working even if every piece of this fails.
- **Echo floor for sessions.** Residual lyrics can still look like
  speech to Silero at 0.15. While output plays, a session frame also
  has to clear the median post-cancellation level by
  `ECHO_ONSET_MARGIN_DB` (12dB). The user's near-field voice does;
  residual doesn't. Applied to end-of-utterance as well, so a command
  spoken over music ends when the speaker stops, not at
  `MAX_COMMAND_FRAMES`. Refusals are logged (`echo_onset_rejected`,
  rate-limited), so a real command being refused would show.
- **Gate release now resets the acoustic models.** Frames are thrown
  away for the whole gated stretch, so openWakeWord's rolling window
  and Silero's recurrent state used to stitch the audio either side of
  the gap together. The falling edge now sets `reset_acoustic`, the same
  as a stream reopen or a resume. **So does every playback stop**,
  gated or not -- see "Ghost speech after replies" below.
- **Hand-rolled comtypes for loopback**, like `output_monitor.py`.
  sounddevice's bundled PortAudio exposes no loopback devices at all.
  The stream asks for 16kHz mono float directly (`AUTOCONVERTPCM`), so
  there's no resampler of ours. An idle endpoint sends *no* packets, so
  `GapFiller` synthesizes the silence. The default device is re-checked
  every second, and the reader reopens on a change, which also resets
  the canceller and its delay.

Every threshold in `echo_canceller.py` is provisional in the
`WAKE_THRESHOLD` sense. The spike runs above are one output at one
volume. `aec_stats` (every 30s of playback), `aec_coverage` and
`echo_onset_rejected` in `session.jsonl`, plus section 5 of
`tools/_report.py`, are what to revisit them from. Still to measure:
- double talk (`--talk`);
- the HDMI monitor, the Bluetooth speaker and earbuds;
- real music and film audio rather than TTS.

### Ghost speech after replies

The first live session with echo cancellation on looped: about a
second after every reply, a recording nobody spoke into, transcribed as
"you you" or "how many files" (the `TRANSCRIBE_HOTWORDS` tail), answered,
and that answer primed the next one. Every one of those transcripts
logged a raw peak of 1-2 LSB or 0.0. Whisper was being handed digital
silence. Across the month of logs before echo cancellation there were
182 transcripts and none quieter than 4e-3.

**Cause.** Silero v4's recurrent state, after even a faint residual of
speech (-40 to -85 dBFS) followed by *exact* zeros, climbs (0.37 ->
0.62) and holds above `VAD_SPEECH_THRESHOLD` for 3-20 frames. That is
enough for `SPEECH_ONSET_FRAMES`. This was reproduced offline with the
real model and Piper audio of the real replies. From a reset, the same
zeros score 0.04. AEC3's suppressor outputs exact zeros, and this mic's
SST DSP floors a quiet room to 1-3 LSB anyway. Before echo cancellation
this never surfaced: Jarvis's own reply tripped the output gate, and
the gate's release reset Silero. With coverage, a reply no longer
gates, so nothing reset it. The echo floor refused the ghost frames only
while `output_was_playing` held, and the onset landed right after.

**Fix, two halves.**
- `_is_session_speech` refuses any frame whose peak is at or below
  `INPUT_SILENCE_FLOOR`, whatever Silero says. That covers onset and
  end-of-utterance, so a real command also stops being padded with a
  second of ghost zeros. Refusals are logged as `silent_vad_rejected`,
  rate-limited like `echo_onset_rejected`.
- Every playback stop sets `reset_acoustic`, covered or gated. This
  restores the invariant that Silero's state never carries a reply, or a
  song, into the next listen. It is the half that still works on a mic
  whose quiet floor isn't digital zero.

**Rejected: dithering the VAD's input.** A -80 or -90 dBFS noise floor
under the zeros shrank the ghost but didn't remove it. In some runs it
still held for 3+ frames, which is an onset. A floor that sometimes
works isn't a fix.

## State-machine audit against the documented diagram

`USER_GUIDE.md`'s "States" section draws the runtime as a state diagram, and that
diagram was treated as the spec in an audit of what the code actually
does. Eleven divergences; the ones worth recording here are the ones
whose symptoms were visible but unattributable.

**`jarvis quit` reported failure on success.** The control loop sent
`"powering down, sir"` and then called `_announce_and_shutdown()` while
still inside the connection's `with` block. `jarvisctl.py` reads until
EOF on a socket built with `timeout=3`, and that timeout governs every
`recv()`, not just the connect -- while the goodbye reliably runs longer:
a blocking spin-down earcon, Piper synthesis and playback, then
`llm_intent.unload()`. So the client's second `recv()` timed out,
`socket.timeout` came out as an `OSError`, and a perfectly clean quit
printed "Can't reach jarvis -- is it running?" and exited 1, throwing
away the reply it had already received. Sitting right on the 3s boundary
is what made it intermittent. The announcement now happens after the
connection closes. `jarvis pause` had the same shape for a different
reason -- its unload waits on `_processing_lock`, which an in-flight LLM
fallback can hold for up to 30s -- so that unload moved to a background
thread, leaving the mic close (the part that actually matters) synchronous.

**A sleep/wake cycle could leave nothing running, silently.** Nothing
sequences "Jarvis Quit On Sleep" against "Jarvis Restart On Wake", and
the quit blocks on audio against an audio stack Windows is already
suspending -- so the outgoing process was routinely still alive when the
incoming one started, whereupon it hit the single-instance mutex and
called `sys.exit(0)`. Exit code 0, so Task Scheduler logged a success,
and under `wt.exe` the message flashed in a window that closed itself.
Result: no Jarvis until the next logon, with nothing anywhere saying so.
Fixed at both ends -- `_acquire_single_instance_lock` now waits out an
incumbent (`SINGLE_INSTANCE_WAIT_SECONDS`, logging a stand-down when it
does give up) and the wake task got a 15s delay. The three tasks are now
committed under `scheduler/` too; they had existed only in the live Task
Scheduler database, so a documented behaviour had no artifact in the repo.

**Pausing mid-conversation could reopen the conversation on resume.**
The listener's end-of-utterance `state["mode"]` write was dedented out of
`_processing_lock`, and `_set_enabled`'s own reset sat outside it too. A
pause landing mid-transcription set mode to `"wake"`, and the listener
then put it straight back to `"session_listening"` -- and
`_set_enabled(True)` never touched mode, so the next `jarvis resume` came
back into an open conversation nobody opened, acting on ambient speech
with no wake word said. The comment on that reset says it exists
precisely to stop this; the race defeated it. The listener's write is now
inside the lock and conditional on `state["enabled"]`, and `_set_enabled`
writes `enabled` before `mode` so both threads agree on `"wake"`
whichever goes second. Deliberately not solved by taking the lock across
all of `_set_enabled`: pause is the privacy control, and the mic has to
shut when asked, not once the model finishes thinking.

**Jarvis could hear its own "One moment, sir" and loop on it.** The
`is_speaking()` mute is evaluated when a chunk is *dequeued*, not when it
was captured, and `audio_q` is unbounded -- so a 3-30s LLM fallback banks
every frame recorded during it, including the "One moment, sir" spoken
before the call. Drained afterwards at machine speed, that backlog is
harmless only while a reply is still playing. On the paths that end
without speaking at all -- the LLM's own abstain, an empty transcript, a
dictated line -- the mute is already lifted, VAD fires on Jarvis's own
voice, and it transcribes itself as the next command, which misses the
fast path, says "One moment, sir" again, and abstains again. Fixed with
`_drain_audio_queue()` after every blocking section, on resume, and after
a stream reopen. This is the same class of bug as the runaway
desktop-switching that motivated the mute in the first place, just
reached by a different route.

**Every pause landed in deep sleep, however brief.** Pausing unloads
Whisper and the matcher; nothing loaded them again. So `jarvis status`
answered "Wake-word armed (deep sleep)" seconds after a deliberate
resume, the HUD said `ARMED` at the same moment (it hardcoded that rather
than using `_restore_steady_state`, which already knew better), and the
next command paid the reload and announced "Back at full power, sir" -- a
line that is supposed to mean "you left me alone for half an hour". The
diagram has Paused returning to WakeArmed *awake*, so a quiet background
reload now restores that. Neither acoustic model was being reset across
the pause gap either, despite every other transition into `"wake"` doing
so; that now happens too, flagged for the listener thread rather than
done from the control thread, which would race a `speech_prob()` call.

**Smaller, same audit:** dictation was a real state with no name anywhere
-- not in the diagram, not in `jarvis status`, not in the HUD colour
table -- despite being the one state where a closing phrase gets *typed*
rather than obeyed. `enable_listening` ignored `via_session` while
`disable_listening` honoured it, so "wake up" mid-conversation fired a
full resume. The 4-minute idle close was the only state change that
happened in silence. A partly-failed model load wedged permanently,
because `whisper_model` stayed non-`None` and the `is None` retry check
never fired again. `_reopen_input_stream` handed a live stream back even
if a pause had landed mid-reopen. And `tts.interrupt()` zeroed
`_speaking_count` outright, so an interrupted worker's `finally` later
decremented a count belonging to a newer utterance -- `is_speaking()`
reading False while audio was genuinely playing, which is exactly the
hole that count exists to close.

**What let all of this survive:** `_set_enabled` and the state machine had
no test coverage at all. `tests/test_state_machine.py` now drives the real
closure (same technique `tests/test_audio_callbacks.py` uses -- run `main()`
far enough with everything heavy faked, capture the frame, bail) and
asserts the two invariants the audit turned up: `not enabled` implies mode
`"wake"`, and mode `"wake"` implies dictation is off. `_status_line` was
lifted out to module level to make the state *naming* testable the same
way `_format_stats` already was.

## Command-line control (`jarvisctl.py`), not a tray icon

Built a pystray-based tray icon first (enable/disable toggle + quit), but
removed it entirely in favor of a local control socket
(`127.0.0.1:47123`); `jarvisctl.py status|stats|debug|pause|resume|quit`
is a one-shot client. `jarvis.cmd` forwards those straight through and handles
`start` itself (`wt.exe -F -p Jarvis` -- `jarvisctl.py` can only talk to
an already-running instance, it can't launch a fresh one). `pause`/
`resume` close/reopen the main listening stream (`stream.stop()`/
`.close()`) -- the only real "stop acting on anything I say" control,
since wake-word detection has to keep the mic hot no matter what to
hear "hey jarvis" at all. Push-to-talk's own stream is deliberately left
running through a pause, so it can still bring Jarvis back by voice --
which means Windows' mic-in-use indicator can stay lit while paused
(it reflects that stream sitting armed for a press, not the main
listening path, which really is fully stopped).

**Autostart is quit-on-sleep / restart-on-wake, not riding through
suspend.** Audio devices are a known weak point across a real sleep/wake
cycle -- the mic connection can come back broken, invisibly. A brand-new
stream every wake cycle sidesteps the whole class of bug rather than
trying to detect and recover from a possibly-broken one. State is NOT
preserved across the quit/restart -- Jarvis comes back wake-word-armed
regardless of what it was doing before sleep. The three Task Scheduler
entries that implement this, and the mutex-race fix mentioned above, are
documented in `scheduler/README.md` -- nothing in the Python code
observes power events at all.

**`jarvis status` reports LLM warm/cold state and listener liveness, not
just the session state.** `LLM warm` / `LLM warming` / `LLM cold` / `LLM
failed` is purely observational (see `llm_intent.prewarm_state`'s
docstring) -- it just tells you whether the next AI-fallback answer will
be near-instant or pay the cold-prefill cost. `listener stalled Ns`
exists because the crash containment above keeps the *process* alive
through a dead listener thread, so the socket would otherwise keep
answering "Wake-word armed" while nothing was actually listening at all
-- containment without a liveness signal just moves where the silence
comes from.

## Idle unload after 30 minutes ("deep sleep")

`small.en` (~500MB) and the embedding matcher stay resident in RAM
otherwise, even fully idle. After 30 minutes with no wake word, session
activity, or push-to-talk use, both get dereferenced and GC'd; wake-word
detection and the mic keep running regardless, so "hey jarvis" still
works and reloads them on demand (a couple seconds' delay on that first
command back). A RAM optimization, not a privacy one -- nothing about
what's listened to or when changes.

**Update, reversed for the LLM specifically:** this used to sweep up
`llm_intent`'s loaded model too (`llm_intent.unload()`, called from the
same `_unload_heavy_models`), on the theory that anything loaded should
be swept up uniformly. Measurement showed that's the wrong trade for the
LLM: `llama-cpp-python` mmaps the GGUF's weights by default
(`use_mmap=True`), so they're file-backed, evictable pages, not
committed RAM -- holding it resident is nearly free. What unloading it
was actually "saving" was forcing the *next* command after every deep
sleep or pause/resume cycle to re-pay the ~13-27s cold prompt-prefill
cost documented below, at exactly the moment (right after 30 idle
minutes, or right after resuming) it's most likely to land on a real
command. `main.py`'s `UNLOAD_LLM_ON_DEEP_SLEEP` (default `False`) now
keeps the LLM loaded through deep sleep and pause/resume -- Whisper and
the matcher are unaffected, unloaded either way -- with a background
prewarm (see below) re-arming it after boot/wake/resume as a safety net
regardless. Shutdown still always unloads it.

## Transcription accuracy

Evidence-based tuning, not guesses -- each fix below came from comparing
actual pasted transcripts, not assumption:

- **`hotwords`** (`main.py`'s `_transcribe`): "chrome" repeatedly landed
  on a near-rhyme ("room", "groom", "comb") while surrounding words in
  the same sentence transcribed cleanly -- a language-model-prior bias
  against a domain word, not a volume problem. `hotwords` boosts
  recognition of specific terms without conditioning on them as literal
  prior text (unlike `initial_prompt`). `TRANSCRIBE_HOTWORDS` covers app
  names, desktop-navigation vocabulary, window-action verbs, and query
  phrasing -- update it as new vocabulary gets added, but see the "bdk
  wallet" section below for why real repo/project names specifically no
  longer belong here (they did once; this paragraph is corrected from an
  earlier version that still described that). Tradeoff (see the
  VAD-gating fixes above): a hotwords bias can make Whisper hallucinate
  fragments of that same vocabulary out of near-silent audio, so this is
  never applied without a VAD gate confirming real speech was present
  first.
- **`VAD_SPEECH_THRESHOLD`**: needed three rounds of reduction (0.5 ->
  0.35 -> 0.2 -> 0.1) before "have to speak louder than normal" actually
  resolved -- 0.1 confirmed working at normal speaking volume, but proved
  too sensitive to background noise as a side effect; settled at **0.15**
  as a middle ground (paired with the 3-consecutive-frame onset debounce
  above, which does more of the noise-rejection work than the threshold
  value itself now). Revisit either direction based on further live use.
- **Peak-amplitude normalization** in `_transcribe`: a secondary,
  complementary fix for quieter-but-fully-captured audio, independent of
  VAD tuning.
- **`SILENCE_HANG_FRAMES`** (end-of-utterance silence hang): started at
  0.8s, halved to 0.4s -- felt laggy for one-sentence commands.

**Update (2026-09-29): the "no simple way to check this" half of the
above is no longer true, and the actual finding was the opposite of
what this section anticipated.** This paragraph originally worried about
the mic's level being set too *low*. Diagnosed instead from real
session data: the reference machine's mic was at **100%** (read via
`audio_endpoint.get_input_volume()` -- `IAudioEndpointVolume` on the
capture endpoint, the same Core Audio interface `output_monitor.py`
already used one layer up), and a real, distinct population of
mishearings in the log was consistent with clipping, not quiet input --
peak pinned at exactly 1.0 across six different manglings of "hey
jarvis" ("hey jobis"/"jobbers"/"jawless"/"jobters"), each with Whisper
*confident* (`avg_logprob` -0.55 to -1.2) and still wrong, a different
shape entirely from the near-silence hallucination class this project
already had a fix for. `main.py` now boots with a one-time, read-only
warning (`_HIGH_INPUT_LEVEL_WARNING`) when the capture level measures
suspiciously high, and logs a per-utterance `clipped_fraction` (fraction
of samples pinned at/near full scale, computed pre-normalization) that
feeds `_should_reject_transcript`'s existing shadow-only gate as a new
"clipped" reason -- same discipline as every other reason there:
logged, not yet enforced, pending real shadow data. `audio_endpoint.py`
deliberately never *writes* the capture level -- see its module
docstring for why (that setting may already be tuned for a call or a
recording this process knows nothing about). Software-side tuning
genuinely can't fix clipping after the fact either way, in either
direction: the fix is the physical level, which is why this stays a
detect-and-warn design, not a silent auto-correct.

## Intent resolution: local embedding match, local LLM fallback

**Fast path** (`intent_matcher.py`): compares a transcript's embedding
(`local_embedder.py`, ONNX `BAAI/bge-small-en-v1.5`, CLS-pooled +
L2-normalized, per the model card's documented usage) against a small
table of seed phrases per intent, cosine-similarity threshold 0.80.
Compound labels (`"focus_or_launch_app:chrome"`, `"switch_desktop:next"`)
parse into the same dict shape the LLM path produces
(`actions.parse_fast_path_intent`), so both paths dispatch through one
function. Seed lists are kept deliberately short (2-5 per intent, not
6-12) -- the embedder already generalizes "open"/"bring up"/"switch to"
as near-synonyms, and every extra seed is extra embedding work paid at
*every* startup.

**Fallback path** (`llm_intent.py`): Qwen2.5-1.5B-Instruct, GGUF Q4_K_M,
via `llama-cpp-python`, only reached when the fast path can't confidently
place a phrase. Grammar-constrained decoding (GBNF, built from a JSON
schema via `LlamaGrammar.from_json_schema`) makes it structurally
impossible for the model to emit anything outside the fixed action/app/
direction schema -- the same allowlist philosophy as everywhere else in
this project, enforced one layer earlier through constrained generation.
Every field is still re-validated against the real enums in `infer()`
regardless, including a check that an action's *required* parameter isn't
`"none"` (a bare `action` with no usable `app`/`direction` is treated as
"didn't understand," not dispatched with a guaranteed-to-fail parameter)
-- never trust a single layer, especially not an LLM's, even a
grammar-constrained one. Benchmarked standalone first (`_bench_llm.py`,
16/17 correct on a realistic paraphrase set) before wiring in. **The
"~2.5-2.8s steady-state" figure once recorded here was wrong** -- see
the latency section below for the corrected numbers and why the old
benchmark never actually measured a cold call. Both the model weights
*and the `llama_cpp` import itself* are
lazy -- importing `llm_intent` at `main.py`'s startup used to eagerly
load `llama_cpp`'s native backend regardless of whether a session ever
needed the fallback, adding several seconds to every single startup;
moved the `from llama_cpp import ...` line inside the lazy-load function
so it only pays that cost on first actual use.

**Auto-promotion** (`seed_promotion.py`): logs every LLM-resolved
utterance; a phrase seen `PROMOTION_THRESHOLD` (2) or more times can be
promoted into the fast path's seed list. **Not run automatically** -- an
earlier version checked once per startup ("has it been 24h+"), but a
bigger seed list is more startup embedding work every time, and the
30-second cost wasn't worth paying on every launch just in case a day had
passed. Run by hand instead: `.venv\Scripts\python.exe seed_promotion.py`
scans `logs/llm_usage_log.jsonl` and promotes anything that qualifies,
ignoring the time gate; restart Jarvis afterward for new seeds to take
effect.

**Generalized app actions** (`apps_registry.py`, `actions.py`): the
original Chrome-only `focus_or_launch_chrome`/window-finding code
generalized into a registry-driven pattern -- originally seven
hand-curated apps (chrome, discord, vscode, file_explorer, settings,
local_disk_d, terminal), later joined by whatever `app_discovery.py`
finds by scanning the Start Menu (Phase 7) -- plus `focus_vscode_repo`
(free-text substring match against `Code.exe` window titles --
inherently safe regardless of the string, worst case is
focusing the wrong window), `switch_desktop` (next/previous only, via
synthetic Ctrl+Win+arrow key events -- no undocumented COM interface, no
absolute desktop numbers, no stable public API for that), and two
read-only query actions (`count_app_windows`, `count_files_in_folder` --
the latter reads whatever path the *currently-focused* File Explorer
window shows, via `Shell.Application` COM automation, guarded against a
null/stale foreground handle after the crash above). `Chrome_WidgetWin_1`
is **not** unique to Chrome -- Electron apps (VS Code, Discord, Slack,
Teams, ...) share the identical window class, since Electron's native
windowing derives from Chromium's own views toolkit; window matching
always also checks the owning process' actual executable name
(`psutil`), never window class alone. A false-positive guard also exists
for desktop switching: "switch to desktop 2"/"switch to desktop two"
(digit or spelled out) both matched the `switch_desktop:next` fast-path
seeds with high confidence, so `main.py` explicitly checks for a
number in the transcript before trusting that match, falling back to an
honest "I can only do next/previous, sir" instead of silently doing the
wrong thing.

**Execution layer is a hardcoded allowlist** -- the matcher/LLM only ever
select a key into `actions.ACTIONS`, a fixed dict of plain Python
functions, never arbitrary generated code or shell. Read-only queries are
vetted, parameterized action templates for exactly this reason -- letting
an LLM generate-and-run a shell command and trusting a "is this actually
read-only" check would be a strictly weaker boundary (redirects, pipes,
and aliases can disguise a write as a read). **No confirmation step
exists** -- deferred until an action with real side effects
(delete/send/close-without-save) is added; the current allowlist is
non-destructive by construction ("close" means minimize, never actually
closing/quitting anything, per explicit user instruction).

## Mid-session wake-word swallow, and the "bdk wallet" false dispatches

Two user-reported bugs, both root-caused from `logs/session.jsonl` and
`logs/llm_usage_log.jsonl` rather than guessed at:

**"hey jarvis" sometimes does nothing for a few minutes, then works
again unprompted.** Two separate causes were found under this one
report, and the first one below is *not* the answer to it -- recorded
that way deliberately, because the first fix shipped before the second
cause was known, and reading this later without that order would be
misleading.

*Cause actually behind the reported dead window: int16 wraparound on
the wake path.* `main.py`'s wake branch is the only place in the whole
pipeline that converts audio to int16 (`openWakeWord` wants int16;
Whisper takes float). It did so with a bare
`(chunk * 32767).astype(np.int16)`, and `astype` **wraps rather than
saturates**. float32 capture can exceed unity when an APO does gain/AGC
ahead of the process -- this machine's mic runs Intel SST -- and every
recent transcript in `session.jsonl` records a raw peak of exactly
1.00, i.e. a capture path sitting at saturation. Measured directly:
`[1.0, 1.05, 1.2, -1.3] * 32767` -> `[32767, -31131, -26216, 22939]`.
A sample just over full scale comes back near-full-scale with the
*opposite sign*, injected into the loudest part of the waveform -- for
"hey jarvis", the part carrying the signal the model is looking for.
That produces exactly the observed shape: intermittent, level-dependent,
worse the louder you speak, recovering "on its own" because it is
stochastic rather than latched. Decisively, it also explains the
asymmetry the report kept pointing at -- **push-to-talk worked
throughout every dead window**, and push-to-talk never touches this
line. Fixed by clipping to [-1, 1] before scaling. The logged boot that
misbehaved (19:14:47) rules out every stateful explanation on its own:
no `wake_triggered` beforehand, so no session was open; the listener's
own output-gate transitions are in the log *during* the dead window, so
the thread was alive and ungated; no `input_silent`; and `ARMED` showing
red is simply its defined colour, `(120, 28, 16)`, not a state desync.

Two measurements were missing that would have made this findable in
minutes rather than across several rounds, and both now exist: sub-
threshold wake scores (`wake_near_miss`, logged as a 10s high-water mark
above `WAKE_NEAR_MISS_FLOOR` -- only scores *over* the threshold were
ever recorded before, so "it heard me and scored 0.45" and "it heard
nothing" were indistinguishable), and `listener_gate_stuck`, which names
which of the four skip gates is up once a stretch passes 30s. `jarvis
status` also reports `speaking` now, since a leaked tts count is the one
of those gates that can latch forever. Relatedly, `tts._speak_now`'s
`_get_voice()` call sat *outside* the try/finally that owns
`_end_speaking()` -- the one call in that function that does heavy
first-use work, and therefore the one that can throw and leak the count
permanently; moved inside.

*Cause found first, real but separate: a session already open swallows
the wake word.* `wake_model.predict()` only ever runs in the
`listener_loop`'s `"wake"` mode -- once a session is open
(`"session_listening"`/`"session_recording"`), every subsequent
utterance is VAD-segmented and transcribed as a *command*, never
re-checked against the wake model, and the session only closes itself
after `SESSION_IDLE_TIMEOUT_FRAMES` (4 minutes) of silence. Saying "hey
jarvis" again while a session is (unexpectedly, from the user's
perspective) still open doesn't re-arm anything -- it falls through to
the fast-path matcher (no seed matches a bare wake phrase), then a full
LLM fallback call, and comes back as the generic "Hello, sir. How may I
assist you today?" reply. That reads as "the wake word didn't work,"
prompting a retry, when what actually happened is the opposite: it was
heard, just not *as* a wake word. Confirmed directly: dozens of "hey
jarvis"/near-miss transcriptions ("hey jobters", "hey job is", "hey
jobbers") appear in the log as ordinary `transcript` events rather than
`wake_triggered` events. Compounding this, the idle-timeout close
(`session_silence_run >= SESSION_IDLE_TIMEOUT_FRAMES`) was the one
session-close path with **zero trace in session.jsonl** -- every other
close goes through `_record_stat`'s `"meta"`/`session_close` outcome,
this one didn't call `_log_event` at all, which is exactly backwards
for the path most likely to explain "it started working again on its
own." Fixed two ways: a bare wake-word echo (`"hey jarvis"`/`"jarvis"`,
already recognized by `_should_reject_transcript`'s `wake_echo` check,
now shared via the `_WAKE_ECHO_PHRASES` constant) landing mid-session is
now intercepted before fast-path/LLM matching, acknowledged instantly
with the existing `already_listening` phrase pool, and refreshes
`last_activity` -- no more multi-second LLM round trip for a phrase that
was never going to match anything; and the idle-timeout close now logs
a `session_idle_closed` event, closing the observability gap for next
time.

**Bare noise (`"bdk"`, `"for what"`) dispatching `focus_vscode_repo`
with `repo="bdk_wallet"`.** Not the LLM "being sensitive" in the sense
of picking up real ambient words -- two separate self-inflicted biases
compounding:
- `TRANSCRIBE_HOTWORDS` (see Transcription accuracy above) used to carry
  a tail of the user's real repo/project names ("bdk wallet bdk cli
  Hashstack djed shu programming bitcoin bitshala") for
  `focus_vscode_repo` recognition. `llm_usage_log.jsonl` showed this
  tail coming back as **literal transcribed text from near-silent
  audio** ("shu programming bitcoin bitshaka", "how many files folder
  windows bdk wallet bdk wallet") -- hotwords biases decoding *toward*
  these tokens regardless of what's actually in the audio, and the more
  obscure/low-frequency a word, the more that reads as a hallucination
  magnet rather than a helpful nudge. Removed; app names, desktop/window
  vocabulary, and query phrasing stay, since none of those showed up as
  hallucinated output.
- `llm_intent.py`'s few-shot prompt had exactly one concrete example of
  `focus_vscode_repo`'s free-text repo extraction, and it used the same
  real repo name ("bdk wallet" -> `repo="bdk_wallet"`). With only one
  worked example ever shown to the model, low-information input
  reliably collapsed onto it rather than genuinely extracting a repo
  name -- confirmed directly: bare `"bdk"` and `"for what"` both
  resolved to `focus_vscode_repo`/`repo="bdk_wallet"`. Replaced with a
  fictional placeholder (`"payments_api"`) that keeps the same teaching
  point (repo is free text, not a closed enum) without being an
  attractor for anything the user would plausibly say.
- The pre-LLM transcript gate (`_should_reject_transcript`,
  `TRANSCRIPT_GATE_ENFORCED`) already flagged both `"bdk"` dispatches
  via `avg_logprob` in shadow mode, and would have blocked them had it
  been enforced. **Left disabled, deliberately, after checking the
  actual shadow data rather than flipping it on** -- real, legitimately
  dispatched utterances in the same log ("how many files", asked
  repeatedly) score `avg_logprob` as low as -2.11, inside the same range
  as the noise it's meant to catch (`"bdk"` at -1.83/-2.58, `"for what"`
  at -2.58). A single global threshold on this signal, at the current
  sample size, cannot separate the two without also rejecting a real,
  frequently-used command. The gate keeps logging
  `transcript_would_reject` either way; revisit enforcing it once there
  is enough shadow data to place a threshold below the real-command
  floor, not just above the most obvious noise.

## Latency: what the ">30s" actually was, and what changed

User-reported "the LLM fallback sometimes takes 30+ seconds" turned out
to be two mostly-independent problems, both hidden by a benchmark that
had been measuring the wrong thing.

**It was never model *loading*.** `Llama(model_path=...)` construction
measured 1.5-1.8s. The 30+ seconds is prompt **prefill** -- ingesting the
system prompt's own tokens before the model can generate anything --
measured at ~48-55 tok/s on this project's reference hardware (an
11th-gen 4-core/8-thread laptop CPU, AVX2/FMA/F16C/OpenMP all present in
the build). At the original prompt's 1292 tokens that's ~26-32s, paid by
whichever call is first to actually run one: the first call after
process start, and -- before the change below -- the first call after
every deep-sleep unload or pause/resume cycle too. Subsequent calls in
the same process reuse `llama-cpp-python`'s implicit KV-prefix cache and
dropped to ~4-5s. `_bench_llm.py` used to call a warmup completion
*before* timing anything, so every number it ever reported was a
warm-cache hit -- that's the origin of the "~2.5-2.8s steady-state"
figure that stood in the earlier version of this file (and in
`llm_intent.py`'s own docstring) until this section replaced it; it never
measured what production actually paid on a cold call. `_bench_llm.py`
now times the cold call explicitly, separately from the warm average,
and reports prompt/completion token counts from the API's own `usage`
field.

**The fix that matters most: prefill less.** The prompt (`llm_intent.py`'s
`SYSTEM_PROMPT_TEMPLATE`) interpolated the *entire* `apps_registry.APPS`
table -- curated entries plus everything `app_discovery.py`'s Start Menu
scan finds, 40 apps on the reference machine -- into both the prompt
prose and the GBNF grammar's `app` enum. That cost ~190 of the prompt's
1292 tokens (~4s of prefill) for apps most machines will never be asked
about by voice, and it was actively harmful, not just slow: constrained
decoding *forces* a pick from whatever's in the enum, so a garbled
transcript that vaguely resembles a discovered junk entry
(`idle_python_3_12_64_bit`, `blackmagic_raw_speed_test`, ...) gets that
entry confidently selected rather than refused -- confirmed directly in
`logs/llm_usage_log.jsonl` ("maximize obvious code" -> `idle_python_3_12_64_bit`).
`llm_intent.LLM_APP_KEYS` now narrows the LLM's reach to
`apps_registry.CURATED_APP_KEYS` (the 7 hand-curated entries) for both
the prompt text and the enum -- a deliberate, real regression: an app
registered only via discovery is no longer reachable through the *LLM*
fallback (fast-path voice commands are unaffected either way, and
`actions.py`'s own `APPS` check is unaffected -- an LLM-picked app
still has to be a real registry entry regardless of which list produced
it). Combined with trimming 14 few-shot examples down to the ones that
each demonstrate something no other example does, and writing them in
compact JSON (no spaces -- the model tends to mirror an example's own
formatting, and this measurably shortened warm-call completions too),
the prompt dropped from 1292 tokens to ~750-800 -- roughly 40% less
prefill, i.e. cold calls around 13-15s instead of ~30s, at the same
CPU. Every cut was re-validated against `_bench_llm.py`'s test set (now
also checking each utterance's *action* against an expected value,
rather than leaving a human to eyeball raw JSON) before being kept, and
two real regressions the first draft introduced were caught and fixed
this way before shipping: a missing `maximize_app` demonstration, and a
"not registered" decline that needed its wording tightened so the model
reliably emits `action: "none"` rather than an app action with `app:
"none"` for a specifically-named app that isn't on the list.

**Background prewarm, not less blocking.** Shrinking the prompt reduces
the cold cost, it doesn't remove it. `llm_intent.prewarm()` runs one real
inference (a canned utterance, result discarded) purely to populate the
KV cache with the system prompt's prefix, from a background daemon
thread `main.py` starts ~2s after the listener thread does (so it
doesn't compete with wake-word/VAD setup or the boot announcement still
being spoken), and again on wake-from-deep-sleep and resume-from-pause
as a safety net. This is a refinement of the lazy-`llama_cpp`-import
decision above, not a reversal of it -- the import still only happens
inside `_get_llm()`, called from a background thread well after the
process is already armed and listening, so time-to-armed is unaffected.
A dedicated lock (`llm_intent._lock`) serializes `infer()` against an
in-flight `prewarm()` -- deliberately *not* the same lock that guards
transcription (`main.py`'s `_processing_lock`), since a prewarm holding
that lock for its full duration would stall a push-to-talk press before
it even got to transcribe the audio.

**A pre-LLM garbage gate, and letting the model abstain.** `_transcribe`
already computed `no_speech_prob`/`avg_logprob` per segment but, per an
earlier claim in this file, used them "only to decide whether an
LLM-fallback resolution is trustworthy enough to log as a seed-promotion
candidate, never to change what gets dispatched." That stance is
reversed: `logs/session.jsonl` and `logs/llm_usage_log.jsonl` showed *every*
LLM-fallback resolution logged at the time of this change was a
hallucinated transcript that still burned a full LLM call and got
confidently dispatched as a real action ("or been grown for me" ->
minimize chrome, "hey job is" -> open chrome, ...). `main._should_reject_transcript`
now gates the LLM-bound branch specifically (never the fast
embedding-match path -- a >=0.80 cosine match is its own evidence and
still dispatches regardless) using that same acoustic signal, plus
faster-whisper's own failure thresholds for `compression_ratio`, and a
small exact-match denylist/wake-echo check. It ships **disabled by
default** (`main.TRANSCRIPT_GATE_ENFORCED = False`) -- it only logs what
it would have rejected until enabled by hand after reviewing a few days
of real `transcript_would_reject` events in `logs/session.jsonl` and
confirming none of them also produced a real dispatched outcome for the
same utterance elsewhere. Separately, the schema gained a third top-level
outcome, `no_match` (alongside a real action and the existing small-talk
`"none"`), so the model can structurally abstain instead of being forced
to either pick a real action or write a chat reply for input that reads
like noise -- `validate_parsed` force-empties `reply` on this outcome
regardless of what the model wrote, since an abstain must never become
speech.

**Whisper's own temperature-fallback loop was a second, independent
>30s source**, and not one this project had previously diagnosed:
faster-whisper's default `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`
re-runs the *entire* `beam_size=5` search at every temperature whenever
`compression_ratio`/`log_prob`/`no_speech` thresholds trip on a decode --
measured directly at 42+ seconds for 3 seconds of white noise, versus
~2.2s for the same duration of clean speech. `condition_on_previous_text`
(already `False` here) removes the main driver of the degenerate
long-form decodes that ladder exists to recover from, so on this
project's <=7.2s single-utterance command audio it was mostly pure
worst-case cost. `_transcribe` now passes `temperature=0.0` (single
greedy decode) and `without_timestamps=True` (nothing here reads segment
timing). The peak-amplitude normalization mentioned above also gained a
gain cap (8x) -- uncapped, `audio / peak` amplifies pure room noise up to
full scale exactly as readily as quiet real speech, which is what was
feeding Whisper the input that tripped the fallback loop in the first
place.

## Virtual-desktop awareness

`focus_or_launch_app` only counts a window as "already up" if it's on
the *current* virtual desktop -- `SetForegroundWindow` on a window on
another desktop makes Windows silently switch your active desktop to
follow it, which is not what "open X" should do to you.
`count_app_windows` deliberately does the opposite -- "how many chrome
windows are open" is naturally a system-wide question, not one scoped
to whatever desktop you happen to be on, so it counts across all of
them. Uses the officially documented
`IVirtualDesktopManager` COM interface, not a reverse-engineered
third-party wrapper. Two real bugs hit and fixed here early on: the COM
object failed to instantiate with `CLSCTX_LOCAL_SERVER` ("Class not
registered" -- it's an in-process server, needs `CLSCTX_INPROC_SERVER`),
and the original fail-*open* exception handler silently masked that
total failure as "yes, on this desktop" for every window; fixed to fail
*closed* instead (worst case is a redundant extra window opened, versus
silently hijacking the user's active desktop).

## Screen and window context

**Ranking, not `windows[0]`** (`window_context.py`, `monitors.py`) --
`focus_or_launch_app` used to bring forward whatever `EnumWindows`
happened to enumerate first when several windows of the same app were
open, with no notion of which monitor you're looking at, which window
you used most recently, or any word you actually said to disambiguate.
`window_context.rank_windows` scores candidates lexicographically --
a title hint you spoke (`window_context.extract_hints`) dominates
everything; then whether the window is actually visible right now
(a minimized match loses to a visible one); then whether it's on the
monitor currently in use; then Z-order as a recency proxy (Windows
maintains Z-order by activation, so "topmost matching window" is a
free, accurate-enough MRU with no message-pump/event-hook machinery
needed). Repeating the same request within 12s cycles to the
next-best candidate instead of asking a clarifying question out loud
(`window_context.CycleTracker`) -- the candidate list is frozen on the
first request of a cycle and never re-ranked on a repeat, deliberately:
focusing a window changes its Z-order, so re-ranking between calls
would put the just-focused window back on top and make "the other one"
oscillate between the same two windows forever instead of advancing.
Generic across every registered app, not special-cased for Chrome.

**Monitor identification** (`monitors.py`) -- `EnumDisplayMonitors`'
primary-monitor flag is *not* a usable "which one is the laptop"
heuristic: confirmed live on the reference two-screen machine, the
*external* monitor is Windows' primary, not the internal panel.
`QueryDisplayConfig`'s `outputTechnology` (`DISPLAYCONFIG_OUTPUT_
TECHNOLOGY_INTERNAL` vs HDMI/DisplayPort/DVI) is the real signal, reached
via raw `ctypes` since pywin32 doesn't wrap it -- no new dependency.
Fails closed to neutral `screen_1`/`screen_2` names if the query is
unavailable (a VM, RDP, an unusual driver) rather than guessing which
screen is the laptop; a wrong guess there is the worst kind of wrong,
since "move this to my monitor" silently landing on the laptop instead
reads as doing the opposite of what was asked. `resolve_monitor_mention`
(the word -> monitor-key resolver for "my laptop screen"/"the other
screen") deliberately declines numbered screens ("screen 2") outright --
`EnumDisplayMonitors`' enumeration order carries no user-facing meaning
at all, confirmed live: it doesn't even agree with the primary flag,
let alone a left-to-right reading a user would expect.

**`describe_screen` speaks a description, not raw data**
(`actions.py`, `app_titles.py`) -- the first version read window titles
and registry keys straight into speech ("local_disk_d", "main.py
(Working Tree) (main.py) - jarvis - Visual Studio Code"), and ignored
any screen named in the utterance entirely. `app_titles.py` is a set of
pure per-app title parsers (VS Code's `[WSL: ...]`/`(Working Tree)`
noise, Chrome's `- Google Chrome`/unread-count prefixes and the PWA
case that carries no suffix at all, Spotify's idle-vs-playing shape),
validated against titles captured live, never raising -- a parser bug
degrades to the raw trimmed title rather than taking the action down.
With no screen named, the reply covers the focused window plus a
one-line summary of the other screen; with one named, it groups that
screen's contents by app ("Chrome, two VS Code, and Discord") rather
than reading out every title -- following the same "never speak the raw
identifier back" precedent `count_app_windows` and
`count_files_in_folder` already set (neither speaks the app name or
folder path, just a count). The screen parameter is plumbed as a lexical
hint from the raw utterance (the same mechanism `focus_or_launch_app`
already uses for its window hint), not a new LLM schema field -- zero
prompt-token cost, and it works identically on the fast path and the
LLM path.

**A pre-existing false-positive class, found while testing this: the
matcher confuses a question for its matching command.** All imperative
seeds ("open chrome", "lock my computer") sit close enough in embedding
space to the same phrase asked as a *question* that "is chrome open"
(0.931), "is my computer locked" (0.870), and "how many chrome windows
do i have open right now" (0.803, silently stealing a real
`count_app_windows` query from the LLM) all confidently dispatch as if
they were commands. `main.py`'s interrogative guard checks the
*resolved* action (catching the LLM path too) against a small opener-
word list, with an explicit exclusion for actions whose entire purpose
IS to answer a question (`describe_screen`, `now_playing`,
`count_app_windows`, `count_files_in_folder`). On trigger from the fast
path specifically, the match is discarded rather than declined outright
-- the utterance still gets a real shot at the LLM fallback, which
often resolves it correctly (the `count_app_windows` example above).
Deliberately excludes "can"/"could"/"would" from the opener list: those
are the matcher's own seeded polite-request phrasing ("can you open
chrome for me") and must keep dispatching normally despite the
grammatical resemblance to a question.

**A second, unrelated guard**: "move this (window) to the next desktop"
resolves to `switch_desktop:next` at 0.940 -- there's no action that
moves a window across desktops, so unguarded, saying "move" silently
switches which desktop *you're* viewing and leaves the window exactly
where it was, with no error at all. Declined by checking for "move" in
the transcript whenever the resolved action is `switch_desktop`.

**Spotify** (`apps_registry.py`) -- curated by hand, same reason as
`terminal`: this machine's install is the MSIX/Store build
(`SpotifyAB.SpotifyMusic`), not a classic Start Menu `.lnk` shortcut, so
`app_discovery.py`'s scan structurally can't see it. `window_class` is
`Chrome_WidgetWin_1` (Spotify's desktop client is CEF-based, same as
Chrome/Discord/VS Code), so the exe-name check in `_matches_app` is what
actually disambiguates it. The `WindowsApps\Spotify.exe` stub resolves
to `SpotifyMigrator.exe`, not the real client -- confirmed the `spotify:`
URI is registered in `HKEY_CLASSES_ROOT` and launches the real client
directly, same shape as `settings`' `start ms-settings:`. `now_playing`
("what's playing") reads Spotify's own window title, which only ever
carries "Artist - Track" while something is actually playing -- there's
no way to tell "paused" apart from "nothing queued" from the title
alone, so both degrade to the same honest "can't tell" reply rather than
guessing. **This, not the registry entry, was the risky half of adding
Spotify**: before seeding, "what's playing" resolved to `media_play_pause`
at 0.868 and "what song is this" to `media_previous_track` at 0.831 --
asking the question would have paused or rewound the music. The
interrogative guard above and the new seeds both had to land before this
shipped.

**Screen context in the LLM prompt, gated on need** (`main.py`,
`llm_intent.py`) -- the one-turn `context_app` memory (`state["last_app"]`)
only remembers the last app an action *touched*, not what you're
*looking at*, so "minimize this"/"what's this page about" had no way to
resolve. `actions.llm_screen_context()` (a terse cousin of
`describe_screen` -- plain text for the model, not a spoken reply)
fills that gap, but it costs real prefill tokens on every call it's
included in: measured directly, an interleaved A/B (n=12/arm, so
machine-load drift hits both sides equally) gave a median 2.02s -> 2.53s,
min-to-min +0.49s, for a 25-token context line -- matching the ~50 tok/s
prefill rate exactly. Injected only when `main._looks_deictic` finds one
of a small set of reference words (`this`/`that`/`it`/`here`/`current`/
`focused`/`screen`/`window`/`looking`) in the utterance, validated
against real commands: every genuine deictic phrasing fires, and every
ordinary app/desktop/media command that actually reaches the LLM does
not (a few phrasings fire spuriously, like "skip this track", but those
are seeded fast-path commands that never reach the LLM regardless, so
the gate firing on them is free). When present, it merges into the
*same* second system message `context_app` already uses -- never a
third message -- since llama-cpp-python's KV-prefix reuse keys off the
stable first system message staying in the same position every call;
splitting context across more messages multiplies the number of
distinct prefixes ever seen without buying anything.

**Moving windows across monitors, and why desktops were dropped**
(`actions.py`, `monitors.py`) -- `move_to_monitor` ("move this to my
laptop screen") is the first thing in this codebase that mutates window
geometry at all; everything before it only ever showed/hid/foregrounded
a window (`ShowWindow`, `SetForegroundWindow`, `BringWindowToTop`).

*Geometry, not a synthetic key chord.* `switch_desktop`/`snap_window`
both simulate the OS's own keyboard shortcut on principle -- but there
is no shortcut that means "the laptop" or "the monitor" regardless of
physical arrangement, and confirmed live, the reference machine's two
screens are stacked *vertically* and overlap horizontally, so even
`Win+Shift+Left/Right` would be geometrically ambiguous here. A named
target has to be addressed by `SetWindowPos` into the target's
`work_rect` (`monitors.plan_move_rect` -- pure, proportional remap,
preserving relative position/size rather than clipping or fixing pixel
size, the same thing Windows' own shortcut does). A deliberate
deviation from the synthetic-key precedent, not an oversight.

*A maximized window is restored, moved, and re-maximized* --
`SetWindowPos` directly on a maximized window snaps it straight back to
the monitor it re-maximizes onto, a well-known multi-monitor trap.
**A real race hit here during testing, not just a theoretical one**:
`ShowWindow`/`SetWindowPos` only ever *post* a request to a window's
message queue, they don't wait for a cross-process window to actually
act on it -- calling `ShowWindow(SW_MAXIMIZE)` immediately after
`SetWindowPos`, with no wait for the reposition to land first, let
Windows decide which monitor to re-maximize onto based on wherever the
window *still* was, so the whole sequence reported success while the
window silently stayed on the original screen. Fixed by polling for
each step's effect to actually be observable (a placement/monitor
check with a short retry budget) before trusting it and moving to the
next step, rather than a single immediate check.

*Never claims success without checking* -- the final reply is chosen
only after re-reading the window's actual landed monitor and comparing
it to the target, same fail-closed convention as everything else
correctness-adjacent in this project. A minimized foreground window is
declined outright rather than moved -- its placement can't be verified
against a real monitor (the iconic sentinel rect maps to none), and
it's a near-dead path regardless: confirmed live, minimizing a window
takes the OS foreground away from it entirely (focus falls through to
whatever was behind it), so "the foreground window is minimized" isn't
really a reachable state outside a genuine race.

*Moving to another virtual desktop was spiked and dropped.*
`IVirtualDesktopManager::MoveWindowToDesktop` and `GetWindowDesktopId`
were declared in `virtual_desktop.py` from Phase 5 onward but never
called by anything. Verified directly with `_spike_desktop_move.py`
(kept in the repo as the record of why, same "re-validation tool"
convention as `_bench_llm.py`): spawning a real console window (which
on this machine is owned by `WindowsTerminal.exe`, not `cmd.exe` --
Windows Terminal is the default console host) and calling
`MoveWindowToDesktop` on it raised `E_ACCESSDENIED`
(`(-2147024891, 'Access is denied.', ...)`) even from a normal,
unelevated process moving a window it doesn't own. There is also no
keyboard-shortcut fallback to fall back to here (unlike the monitor
case) -- Windows publishes `Win+Ctrl+D`/`Win+Ctrl+F4`/`Win+Ctrl+Left/
Right` for creating/closing/switching desktops, and nothing for moving
a window between them; the only supported gesture is dragging in Task
View. Reaching for `IVirtualDesktopManagerInternal` (the undocumented
interface some third-party tools use for this) was considered and
rejected -- the same "no reverse-engineered interfaces" stance already
applied to desktop *detection* in this project applies here too. A
guard against the resulting false-positive was still worth shipping on
its own: "move this to the next desktop" resolves to `switch_desktop:
next` at 0.940 confidence, which would otherwise switch the user's
desktop and silently leave the window exactly where it was.

## Voice replies and terminal theming

**`tts.py`** -- local neural TTS via Piper. Not an attempt to imitate any
real actor's performance or clone a real person's voice -- that would be
a real ethical/legal line regardless of technical feasibility, a
different thing entirely from picking a stock voice with a similar
accent. Also chosen over cloud TTS (ElevenLabs, Azure, etc.), which would
sound more cinematic but reintroduces exactly the network dependency this
project already fought through, for something this frequent.

- **Voice**: `en_GB-northern_english_male-medium` (switched from
  `en_GB-alan-medium` per a request for something a bit friendlier/less
  formal while keeping a British-male "Jarvis" feel; `VOICE` in `tts.py`
  swaps back easily, both models stay on disk).
- **Speed**: `SynthesisConfig(length_scale=1.0)` -- went 1.5x (too fast)
  -> 1.25x -> 1.1x once *all* conversational replies started being
  spoken (not just state transitions), since a faster rate that was
  fine for occasional short status lines felt rushed once every reply
  was read aloud. **Reverted to the voice's natural 1.0x** (2026-09-28)
  on direct request -- 1.1x still read as rushed once it was actually
  lived with day to day. If speed becomes worth revisiting again, treat
  it as a per-category question rather than one global constant: an
  error or a decline might reasonably stay slower/clearer than a terse
  one-word confirmation, which `_SYN_CONFIG` doesn't currently
  distinguish.
- **Tone: "warmer by default", reversing `phrases.py`'s own original
  "no time-of-day asides" rule** (2026-09-30), on direct request. The
  original rule was a deliberate reaction to a worse problem --
  *generated*-sounding replies from 100%-"sir"-density templates -- and
  that reasoning still holds everywhere else in the file (still no
  jokes, still no commentary on what's being done). The one exception
  is the boot-time proactive briefing (see "Proactive briefing on
  boot" below), which opens with a real time-of-day greeting
  (`briefing_greeting_morning`/`_afternoon`/`_evening`/`_night`) --
  narrow enough that it doesn't reopen the original problem (still no
  per-reply commentary, still pooled variants, still capped at one
  greeting per boot rather than per reply) while giving the one thing
  "warmer by default" was actually asking for: an assistant that
  sounds like it knows what time it is when it first speaks to you.
- **All conversation replies are spoken now**, not just the six original
  state transitions (startup, pause, resume, deep-sleep entry/exit,
  quit) -- action confirmations, "I'm not sure how to do that yet, sir,"
  and the desktop-number-guard message all get read out too, per
  explicit request once the action set broadened past a handful of
  commands.
- **Microphone mutes itself for the duration of any spoken reply, plus a
  0.3s tail.** The wake-word session listener is always-on (unlike
  push-to-talk, which only listens while a key is held), so without this
  it picked up its own spoken replies as new "speech" and acted on them
  -- confirmed directly: "Switching to the next desktop, sir" kept
  re-triggering itself, causing runaway repeated desktop switches, and
  other replies came back nearly verbatim as bogus follow-up commands.
  Tracked as an in-flight *count*, not a boolean flag -- a flag would
  have the first of two closely-spaced replies clear it while the second
  was still queued on the playback lock, reopening the same hole for
  that second reply. Incremented synchronously the instant `speak()` is
  called (not inside the background playback thread), so there's no race
  window for the listener to slip through.
- **Persistent output stream, not one opened per reply.** `sd.play()`
  used to open a fresh device connection every call and tear it down
  after -- Windows reliably clips/glitches the first few dozen ms written
  to a freshly-opened output device, which is why only ever the *first
  word* of a reply came out unclear. Kept open for the process's whole
  life instead; a short (80ms) near-silent priming write happens before
  each utterance's real audio too, as insurance against the same
  warm-up artifact if the device idles between replies even with the
  stream nominally still open. This introduced a new failure mode of its
  own: if Windows invalidates the device under an already-open stream
  (seen directly: `PortAudioError: no driver installed`, likely an audio
  device/driver change while the stream sat open), every write after
  that used to fail identically for the rest of the process's life,
  since the broken stream object stayed cached. Recovers now instead --
  a failed write discards the stale stream, opens a fresh one, and
  retries once before giving up and logging, rather than staying
  silently broken.

**Non-verbal cues (`earcons.py`)** -- short tones synthesized with numpy
at import (~40KB of float32 arrays total, no model, no files, no
per-play synthesis), played through the output stream `tts.py` already
keeps open. Purely additive: nothing that was spoken before is spoken
any less.

The case for them is latency, not decoration. The wake word firing used
to be signalled only by a printed line and then a ~1s spoken "Yes,
sir?", with no confirmation at all in between that anything had been
heard; an 80ms rising blip lands immediately and makes the whole system
feel instant even when the work behind it isn't. Cues also mark
"understood", declines, errors, session close, entering/leaving low
power, boot, and shutdown.

They are generated at 22050Hz specifically to match the Piper medium
voice, so the shared output stream is never torn down and re-opened
between a cue and a reply -- that would reintroduce the cold-open glitch
this project already fought (see above). Each one has a raised-cosine
envelope on both edges: a bare sine that starts or stops at a nonzero
sample clicks, and on a 60ms tone the click is most of what you hear.

Critically they go through `tts._begin_speaking`/`_end_speaking` rather
than straight to the device. The session listener is always-on and mutes
itself only for the duration of *tracked* speech, so an untracked tone
would be heard back through the mic as input -- the same bug class
documented above for spoken replies. Silero is speech-specific and
probably wouldn't fire on a pure tone, but "probably wouldn't" isn't
something to build on.

**Reply phrasing (`phrases.py`)** -- replies are drawn from pools of
variants rather than being one hardcoded string per outcome.

The specific problem: `actions.py` returned the same `"Done, sir."` for
minimize, maximize, play/pause and mute. Hearing the identical four
syllables for five unrelated actions was the most robotic thing about
the system -- considerably more so than anything about the voice model.
Every reply also ended in "sir", at 100% density, which reads as a
template rather than a butler; that's now roughly half, and not always
trailing.

Deliberately straight -- no jokes, no time-of-day asides, no commentary
on what you're doing. The goal was that it stop sounding *generated*,
not that it become a character. `pick()` never returns the same variant
twice running for a given key, which matters more than pool size:
back-to-back repeats are what actually gets noticed. If a wit dial is
ever wanted, `phrases.py` is the only file that would need to change.

Two defects this shook out, both caught by the tests rather than in use:
a variant leading with a lowercase placeholder rendered as a fragment
("next desktop, sir."), and a pool whose variants disagreed about which
placeholder they wanted would have raised `KeyError` only on the
unlucky pick.

**The HUD (`hud.py`)** -- the single writer for everything user-facing.

Output used to be ~40 scattered `print()` calls across `main.py` and
`tts.py` on several threads. That ruled out anything that redraws: two
threads printing into one line interleave, and nothing knew where the
cursor was. Routing it all through one locked module is what made the
rest possible.

- **Layout via a DECSTBM scroll region** (`ESC[{top};{bottom}r`) --
  reserves row 1 and the last three rows so ordinary printing scrolls
  only the middle band, leaving a pinned colour band at the top and a
  status strip between two more bands at the bottom. No dependency, no
  TUI framework. `rich`/`textual` were considered and rejected: `Live`
  at 10fps is measurable CPU next to Whisper, and Textual would have
  meant restructuring `main()` for no visual gain.
- **Each utterance is one block that evolves, not three lines that
  repeat.** The transcript used to print twice -- once as an immediate
  echo, once again alongside the resolution -- which was the single
  strongest "this is a log file" tell. The echo now opens a status line
  underneath it that is erased and redrawn in place (`\r` + `ESC[2K`) as
  the resolution lands, so nothing scrolls and nothing repeats. While
  the LLM fallback runs, that line is a spinner with a live elapsed
  count, which reassures better than the old static
  `[checking with the local model...]` *and* costs one line instead of
  two. Raw parse dicts and stack traces no longer appear at all unless
  debug is on.
- **Confidence is a bar, not just a number** -- gold at ≥0.85, amber to
  0.60, red below. A suspect resolution is visible without anyone having
  to remember what 0.62 means.
- **Live mic meter**, fed one float per frame from the audio callbacks.
  This is what makes the display read as a machine listening to the room
  rather than a log. The audio callbacks do nothing else for it:
  anything that blocks there drops frames and costs real transcription
  accuracy, so all drawing happens on the render thread.
- **Boot is a staged self-test** with real per-stage timings, filling
  latency that was being spent anyway -- the old boot printed flat lines
  and then stalled silently for three seconds loading Whisper.
- **Steady state vs flash.** The listener owns the steady state (armed /
  session / recording / off duty / asleep); success gold, error red and
  thinking amber ride on top as transient flashes. An earlier version
  had `flash()` snapshot the current state and restore it on a timer,
  which clobbered the steady state whenever the loop changed mode
  mid-flash -- e.g. a reply landing just as a session closed silently
  reverted the close. Splitting the two fixed it; there's a test for it.
- **Degrades to plain `print()` when stdout isn't a terminal.** Under
  `pythonw.exe` (the Task Scheduler autostart entry) stdout is
  `logs/jarvis.log`, and escape sequences there help nobody. No render
  thread starts in that mode at all.

Cost: one thread at 15fps writing a few hundred bytes. Nothing touches
the recognition path.

Three real bugs came out of actually booting this and living with it,
rather than only running the tests:

1. **`UnicodeEncodeError` at boot with stdout redirected.** Python picks
   the console codepage for stdout on Windows -- cp1252 for a redirected
   or non-UTF-8 console -- which cannot encode the box drawing, arrows or
   `≥` used in the boot detail lines. The process died on the very first
   boot step, before the listener ever started. `hud.init()` now forces
   `reconfigure(encoding="utf-8", errors="replace")` before anything is
   written, ahead of the `isatty` check, since the plain-text fallback is
   just as exposed as the styled path.
2. **The bands scrolled the screen and the HUD walked down it.** The
   bands are exactly `cols` wide, so the last one fills the
   bottom-right cell of the screen -- and with autowrap (DECAWM) on,
   writing that cell scrolls the display by a line. `DECSC` saved the
   cursor, the band paint scrolled the screen underneath it, and `DECRC`
   restored to a row that had since moved; the spinner's next in-place
   redraw then landed on the wrong line. At 15fps the whole display
   walked steadily downward, stranding copies of the status band in the
   scrollback with runs of blank lines between them. Only ever visible
   while the spinner was running, because every other write ends in a
   newline and self-corrects -- which is why it looked like an
   LLM-fallback bug rather than a rendering one. Autowrap is now
   disabled for the duration of each band paint and restored
   immediately after, inside the cursor save/restore.
3. **A pause displayed as LOW POWER.** `_unload_heavy_models()` is
   reached two ways -- the 30-minute idle timeout and an explicit
   `jarvis pause` -- and setting the display state inside that shared
   helper overwrote the OFF DUTY its caller had just set. State now
   belongs to each caller.

**An exception in a sounddevice callback kills the stream outright.**
Worth stating on its own, because the symptom looks nothing like the
cause and this cost real debugging time.

`sounddevice` registers its PortAudio callback with
`_ffi.callback('PaStreamCallback', error=_lib.paAbort)`. Any exception
escaping the Python callback therefore returns `paAbort` (2), and
PortAudio **permanently stops the stream** -- no further callbacks, for
the life of the process. It is not a dropped frame; it is the end of
listening.

What triggered it: `state` was assigned *after* `stream =
_create_stream()`, so the first callback could fire while the name was
still unbound and raise `NameError`. Latent since the original build and
timing-dependent, so it fired on some boots and not others.

The symptom was "hey jarvis" doing nothing at all with the display stuck
on ARMED, while push-to-talk kept working perfectly -- because
`hotkey_stream` is opened further down, after `state` exists. The 5s
input watchdog would eventually notice the silence and reopen the main
stream (by which point `state` was bound), so it recovered on its own
and looked intermittent rather than deterministic.

Fixed twice over: `state` is now defined before any stream is opened,
and both callbacks wrap their entire body in `try/except` that logs to
`session.jsonl` and returns. Neither may ever let an exception reach
PortAudio, whatever goes wrong inside. `tests/test_audio_callbacks.py`
covers both halves, and also asserts that sounddevice still uses
`error=_lib.paAbort` -- if that ever changes, the guards stop being
load-bearing and the test should be what says so.

**llama-cpp's teardown noise at exit.** `Llama.__del__` ran during
interpreter shutdown, by which point the module globals its cleanup
chain depends on were already `None`, printing a `TypeError: 'NoneType'
object is not callable` traceback *after* the goodbye. Harmless, but the
last thing on screen and it reads like a crash. `_shutdown()` now calls
`llm_intent.unload()` explicitly, freeing the model while the
interpreter is still fully alive.

**Structured logging + a runtime debug toggle** -- every event is
appended to `logs/session.jsonl` (transcript, match score, parsed
action, per-stage timings, errors) regardless of what the terminal is
showing. The HUD is a *view*; it elides detail to stay readable, and
none of that should be lost because the pretty layer chose not to draw
it. `jarvis debug on|off` (or bare `jarvis debug` to toggle) flips
terminal verbosity over the existing control socket with no restart --
the point being to turn detail on the moment something misbehaves and
catch the *next* occurrence, rather than restarting and losing the state
that produced the bug. `jarvis stats` also reports p50/p95/worst latency
per stage now.

**Terminal theming** -- a dedicated Windows Terminal profile named
"Jarvis" (own GUID, in `settings.json`), so none of this touches the
user's default/other profiles. Custom color scheme "Jarvis HUD":
background/foreground/cursor built around the Mark III suit's hot-rod red
and gold (`#FF2A1F` red, `#FFD8A8` warm gold-white foreground, `#0C0403`
near-black background), with cyan (`#00D9FF`) kept as the ANSI cyan accent
-- the original arc-reactor-blue HUD scheme, preserved as a "mix" rather
than fully replaced. Font: JetBrainsMono NFM (already the system default),
whose Nerd Font glyph coverage is what the HUD's symbol set relies on.

The profile launches fullscreen (`wt.exe -F -p Jarvis`), which matters
for more than looks: the monitor-sync lights behind the display mirror
whatever colour is on screen, sampling the edge zones. A coloured glyph
in a sea of near-black moves the room barely at all, so the HUD drives
those zones deliberately -- full-width bands top and bottom, plus a
two-column state-coloured gutter down the left of every content line.
State colour is eased toward its target over ~300ms rather than snapped
(stepped colour reads as a glitch), and while an utterance is being
recorded the bands' brightness tracks live mic level, so the room
breathes with whatever is being said to it. The red-to-cyan snap when
the wake word lands is the most visible thing the system does.

Optional profile settings worth trying, none of them wired up here:
`"experimental.retroTerminalEffect": true` for CRT scanlines and
phosphor glow, an animated arc-reactor GIF as `backgroundImage` at
`backgroundImageOpacity` around 0.07, and `"cursorShape": "filledBox"`.
Avoid acrylic/transparency -- it bleeds the desktop through and muddies
what the sync lights see.

`python hud.py` runs a standalone live preview of the whole display
(boot sequence, states, spinner, mic meter) without starting the stack,
which is the quickest way to judge how it looks in the room.

Two silent failures hit getting the launch itself right (both exit code
0, nothing printed -- diagnosed by testing the inner command directly,
bypassing `wt.exe`, to isolate launcher from program):

1. The profile's `commandline` cannot embed the real launch command
   inline as a nested-quoted string -- the escaping-inside-JSON-inside-a-
   commandline-string silently made Windows Terminal fall back to a bare
   default shell instead of erroring. Fixed by pointing `commandline` at
   `launch.ps1` instead (`powershell.exe -NoExit -File D:\jarvis\launch.ps1`).
2. `wt.exe`'s fullscreen flag must come *before* the profile flag --
   `wt.exe -F -p Jarvis` works, `wt.exe -p Jarvis -F` silently launches
   nothing at all.

## Exact volume control via IAudioEndpointVolume

The log showed a repeated, confirmed failure: "reduce the volume by 5",
"reduce volume by 10" (twice), "reduce the volume by 10" -- all asking
for an amount `volume_up`/`volume_down` had no way to honour, since both
were a single `VK_VOLUME_UP`/`VK_VOLUME_DOWN` tap (~2% per press) with
no notion of "by how much" at all.

**The amount is a lexical hint, not a schema field** -- continuing the
pattern `describe_screen`/`move_to_monitor` already established (see
"Screen and window context" above): `slots.parse_amount()` reads the
raw utterance text for a relative step ("by 10") or an absolute target
("to 30", or a bare "volume 30"), returning `None` when nothing was
said so the caller falls back to the old single-tap behaviour
unchanged. Zero prompt tokens, works identically on both the fast path
and the LLM fallback, same reasoning as every other hint-based
parameter in this codebase.

**No new action was added.** "Set the volume to 30" carries no
"up"/"down" of its own, so which of `volume_up`/`volume_down` catches
it by cosine similarity is arbitrary -- confirmed directly (`local_
embedder`, live): both phrasings land on `volume_up`'s seeds at
0.83-0.85, comfortably clear of the 0.80 threshold, purely because of
surface resemblance to "turn the volume up", not because either means
"up". This turned out to not matter: an *absolute* amount is applied
exactly regardless of which action fired, and only a *relative* amount
("by 10") needs the matched direction at all -- which the seeds
resolved correctly in every case tested. Adding a dedicated
`set_volume` action was considered and rejected as unnecessary
complexity once this was confirmed.

**The actual control surface is `audio_endpoint.py`**, a new module
wrapping `IAudioEndpointVolume` -- the interface behind Windows' own
volume slider, both the speaker one (read/write, for this) and the
microphone one (read-only, see the next entry). Same COM shape
`output_monitor.py` already established (`IMMDeviceEnumerator` ->
`GetDefaultAudioEndpoint` -> `Activate`), one interface deeper: 7 vtable
slots declared (only 2 ever called -- `SetMasterVolumeLevelScalar`/
`GetMasterVolumeLevelScalar`; the 5 ahead of them are declared with
accurate signatures purely to keep vtable offsets correct, per
KNOWN_LIMITATIONS.md's own standing caution about hand-declared
vtables). Thread-local COM apartment, exactly `virtual_desktop.py`'s
`_get_manager` pattern -- volume actions dispatch from either the
listener thread or a per-press push-to-talk worker thread, and a COM
pointer can't safely cross threads without marshalling.

Verified live end to end, not just unit-tested: a relative down-by-5
followed by up-by-5 round-tripped the system volume exactly (80% ->
75% -> 80%, confirmed via readback); an absolute set to the
already-current value was a confirmed no-op; the old no-amount tap
path was confirmed unchanged.

## Repairing mishearings after the fact, not biasing decode toward them

A second look at the "bdk wallet" false-dispatch problem (see that
section above), after the mic-clipping finding made clear the
transcription complaints weren't one problem but at least two.
`TRANSCRIBE_HOTWORDS` primed repo names at decode time and got reverted
because that biases recognition *toward* those words regardless of
what's in the audio -- the fix can't be "prime the words better," it
has to work a different way entirely.

**Tested and rejected: a whole-transcript word-repair pass.** The
original idea was a general fuzzy-correction step -- any word close to
a known command vocabulary gets rewritten before the text reaches the
matcher/LLM ("bng up chrome" -> "bring up chrome"), using
`difflib.SequenceMatcher.ratio()` against a small, closed vocabulary
derived from `intent_matcher.INTENTS`' own real seed words (deliberately
not a new hand-guessed list -- and this caught a real mistake: "launch"
was in an early draft of the vocabulary despite never actually
appearing in a real seed phrase, only in the internal action name
`focus_or_launch_app`).

Checked against real English words before shipping it, per this
project's stated method of deciding from data rather than a guess --
and it failed. At the one cutoff that catches the target correction
("bng"/"bring", ratio 0.75), ordinary conversational words score
*higher* against unrelated vocabulary entries than the real target
does: "love"/"move" 0.75, "star"/"start" 0.89, "sleepy"/"sleep" 0.91,
"man"/"many" 0.86. There is no threshold that keeps the wanted
correction and excludes those -- plain per-word edit-distance can't
tell "a genuine mistranscription" apart from "a real, different, shorter
word that happens to be nearby" without more context than one isolated
word carries. Shelved, not shipped; `fuzzy_match.py`'s own docstring
keeps this account so the idea doesn't get quietly retried without
re-deriving why it didn't work.

**What shipped instead: `focus_vscode_repo`'s fallback, narrowed to a
domain where the failure mode is actually safe.** Same scoring
primitive (`fuzzy_match.best_match`/`ngrams`), applied only when the
existing exact-substring check finds nothing, and only against
titles of VS Code windows that are *actually open* -- a handful of real
candidates, never "any English word." The candidate set being small and
real is what makes a looser cutoff (0.72, vs. 0.80 elsewhere in this
codebase) defensible here specifically: the worst case is focusing a
different real, already-open repo, not corrupting arbitrary
conversational text into a false command.

Verified against the live machine's actual open windows, not just
synthetic examples -- four real VS Code windows were open at the time
(`jarvis`, `haystack`, `notes`, and, notably, `bdk wallet` -- the exact
repo from the original false-dispatch incident). A simulated mishearing
("big wallet") correctly resolved to the real "bdk wallet" window;
mishearings of the other three each resolved to their own correct
window and no other; three genuinely unrelated conversational phrases
("i love you my friend", "what is the weather today", "thank you very
much" -- the last one drawn straight from `_HALLUCINATION_DENYLIST`)
each correctly matched nothing at all.

## Feedback integrity: actions.Outcome, and the room lights actually knowing

Found while auditing this codebase for gaps, not reported as a live
symptom -- but confirmed as a real, structural correctness defect, not
a theoretical one. Every one of `actions.py`'s 18 functions returned a
bare `str`: the spoken reply, and nothing else. `main.py`'s dispatch
site played the `understood` chirp, called the action, and handed its
return value straight to `hud.reply()` and `speak()` with no way for
either to tell "Minimised, sir." apart from "That didn't take, sir --
it's still where it was." Checked directly rather than assumed:
`hud.reply()` has taken an `ok` parameter since it was written, and
`hud.ERROR` (colour `(255, 42, 31)`, label `PROBLEM`) has been in
`_STATES` the whole time -- grepping every call site in the running
application found exactly two calls to `hud.reply()`, both with the
default `ok=True`. The most alarming colour in the palette, on a band
that drives the room's monitor-sync lights, had never once fired.
Same story one level down: `jarvis stats` counted every dispatch under
`fast_path`/`llm_action` regardless of outcome, so the one number meant
to answer "is this working" was silently counting failures as
successes.

**`actions.Outcome(text, status)`** -- a `NamedTuple`, not a dataclass
or a dict, so it destructures like the plain string every existing
caller/test already expected while adding exactly one more field.
Three statuses, not two, because the phrase pools already drew this
distinction and always had: `"ok"` (it worked), `"no_effect"`
(understood correctly, genuinely nothing to do -- already focused,
single-screen machine, already muted), and `"failed"` (attempted and it
didn't, or the request couldn't be serviced at all). Classified by
hand against every one of the ~45 return sites across all 18 actions,
reading each phrase's actual wording rather than guessing from the key
name alone -- caught real distinctions a name-based guess would have
missed: `now_playing_unknown` ("nothing seems to be playing") is `ok`,
an honest answer, not a failure, per that function's own stated design
of degrading to "can't tell" rather than guessing; `screen_single`
("there's only the one screen") is `ok` for the *query*
(`describe_screen`) but `no_effect` for the *command*
(`move_window_to_monitor`) asking to move something there, since a
command that structurally cannot be carried out is a different thing
from a question that got its true, complete answer.

`dispatch_llm_action` normalizes: a bare `str` (an action not yet
converted, or a test's monkeypatched stand-in -- `test_actions.py` mocks
`ACTIONS` entries with plain lambdas throughout) becomes
`Outcome(text, "ok")` automatically, so nothing else needed to change
in lockstep. An unrouted action name is itself `Outcome(..., "failed")`
now, not just a decline -- dispatch failing to even place the request is
a real failure to service it.

**The `understood` chirp stays exactly where it was -- before dispatch,
not after.** Moving it to reflect the real outcome would mean delaying
the one immediate "heard you" signal by however long dispatch takes (up
to ~5s for a cold app launch), which is precisely the latency this cue
exists to hide (see "Voice replies and terminal theming" above). A
second, quieter cue fires *after*, only for `no_effect`/`failed` --
success stays silent, since the spoken reply already carries that.
`no_effect` gets a new earcon (a single flat tone at a pitch none of
the others use, so it can't be mistaken for `decline`'s double-tap or
`error`'s falling two-parter); `failed` reuses `error`, the same cue an
exception already played, rather than growing the palette for what the
room lights already treat as "something is wrong."

**`ACTING` was a 0.45s flash-after, never a steady state during** -- a
cold app launch (up to ~5s, `focus_or_launch_app` polling for the new
window every 0.25s) showed nothing at all for that whole stretch.
`begin_flash(ACTING)` right before dispatch, closed out for free by
`hud.reply()`'s own `flash()` call once the outcome is known (that call
already unconditionally overwrites whatever's in the flash slot, gold
again on success or red on a real failure, so no separate `end_flash()`
was needed). Whisper transcription got the identical treatment for the
identical reason: nothing showed between "recording stopped" and
"resolved" except when the LLM fallback happened to run -- fast-path
matches, the majority of dispatches, transcribed in total silence,
band-wise. `begin_flash(THINKING)` now brackets both `_transcribe()`
call sites (push-to-talk and session-listening), closed by
`_handle_transcript`'s existing `finally: hud.end_flash()`, which
already ran unconditionally regardless of how far processing got --
including the empty-text early return.

**A persistent "you are not being heard" indicator** --
`hud.set_input_blocked(reason | None)`, rendered in the status strip
with the mic meter going flat instead of showing a level that's
meaningless while blocked. Five conditions gate the listener
(`is_speaking()`, system output playing, push-to-talk held, paused, and
the wedged-capture-device latch); before this, exactly one of them
printed anything at all, and it scrolled away. The worst case is
already on this record (see "Input health" above): `NO MIC SIGNAL`
reached `jarvis status` only, for 50 minutes, while the band kept
showing ordinary `ARMED` red throughout.

The four routine conditions share one slot with a priority order
(paused outranks the three transient ones, since it's the one actually
asked for); the wedge is a hard fault and outranks all four of *those*
-- confirmed worth the extra care by re-reading this project's own
incident record rather than assuming: KNOWN_LIMITATIONS.md's actual
wedge incident notes push-to-talk being tried *during* it, which is
exactly the overlap that would otherwise let a routine "push-to-talk
held" silently paper over a genuine "no mic signal at all" for as long
as the key stayed down, and then *clear* the wedge indicator entirely
the moment it was released, even though the wedge itself was never
resolved. The wedge also gets its own band state
(`begin_flash(hud.ERROR)`) in addition to the text -- reusing `ERROR`
rather than growing the palette further, since a dispatch failure and a
hard input fault are both, at the level the room lights operate at,
"something is wrong." A resume that lands right after a pause that
started mid-wedge clears both explicitly too:
`_check_input_signal`'s own recovery path (which also clears these)
never runs for that transition, so without its own clearing the flash
would have been left stuck open-ended, with nothing left in the system
that would ever call `end_flash()` again.

**`ASLEEP` was a darker version of `ARMED`'s exact hue** ((26, 8, 5) vs
(120, 28, 16) -- same warm red-orange ratio, just dimmer), so deep sleep
and armed-but-awake were indistinguishable at a glance and on the room
lights, the one thing the whole band design exists to avoid. Changed to
a dim blue-violet -- a genuinely different hue family, not just a
different brightness, so it can't collapse into "a dim version of red"
regardless of where the eased colour lerp has settled.

**Three smaller fixes, found while touching the adjacent code, not
sought out separately:**
- `hud.line("Mic signal is back.", "ok")` has read this way from the
  start, but `_GLYPH` had no `"ok"` entry at all -- it silently fell
  back to `_GLYPH["system"]`'s dim grey, identical to routine notes like
  "dropping models to save memory." There was no green anywhere in the
  glyph table. Mic recovery is arguably the single most reassuring
  event in this system; it now looks like one. A regression test scans
  every `hud.line(..., "kind")` call site in `main.py` against
  `_GLYPH`'s actual keys, the same shape as `test_phrases.py`'s existing
  key-existence checks, so this exact class of typo can't recur silently
  again.
- `hud.last_render_error()` existed as a public function, and its own
  comment said it was "surfaced by `jarvis status`" -- nothing had ever
  actually called it from there. Wired into `_status_line` as one more
  optional, testable parameter (last traceback line only; the full
  trace has no place in a one-line status reply).
- `phrases.PHRASES["audio_trouble"]` was defined and picked from
  nowhere; the one call site that should have used it (`tts.py`'s
  playback-exception handler) hardcoded its own near-duplicate wording
  instead, so a wording tweak would have had to remember to happen
  twice. A new test (`test_every_phrase_pool_is_referenced_by_some_call_site`)
  scans `actions.py`/`main.py`/`tts.py` together and fails if any pool
  is ever orphaned like this again. `phrases.pick()`'s own unknown-key
  path (returns `""`, by design -- a missing phrase must degrade to
  silence, not take down an action that already succeeded) now also
  calls `hud.note()`, so that silence is at least traceable afterward
  instead of leaving zero record anywhere that anything had gone wrong.

## Observability: durable timings, log rotation, and tools/_report.py

`_TIMED_STAGES` (transcribe/match/llm/action) only ever lived in an
in-memory rolling deque (`state["stats"]["timings"]`), read by `jarvis
stats`. It dies at every restart -- 40 `shutdown` records in the real
log meant 40 discarded sessions' worth of where-the-time-went data, on
a project whose own stated method is to decide from logged data, not a
guess. Fixed with a second, parallel structure: `state["current_
timings"]`, reset once per utterance (at the earliest point one starts
processing -- right before `_transcribe`, the same place `THINKING`'s
new `begin_flash` call landed) and read once by `_record_stat`, which
now logs it into that utterance's own `"outcome"` event. The rolling
deque keeps working unchanged for `jarvis stats`'s live view; this is
the durable half nothing read before.

**`logs/session.jsonl` now rotates.** `KNOWN_LIMITATIONS.md` used to say
it "grows without bound... delete it whenever; nothing reads it back" --
true until `tools/_report.py` (below), which does now read it back.
Single-generation (`session.jsonl.1`, one backup, nothing older) at
10MB, checked every 50th `_log_event` call rather than on every one --
a `stat()` syscall on a line written several times per utterance is
needless overhead on what both the audio and control-socket paths treat
as latency-sensitive.

**`tools/_report.py`** -- a hand-run reader, same "not part of the live
pipeline, not wired into jarvisctl.py" convention as `_bench_llm.py` and
`seed_promotion.py` (the control socket has a 3s client read timeout;
this reads the whole log, which can take longer on a long-running
install). Answers exactly what this project's own decision log says is
blocked on data, not a guess: the clipping-vs-confidence correlation
this project diagnosed by hand once (now repeatable), the
`transcript_would_reject`-vs-real-dispatch check `TRANSCRIPT_GATE_
ENFORCED` is waiting on, `wake_triggered` scores against `WAKE_
THRESHOLD` (tuned, by its own comment, without ever having a record of
what a real trigger scores), `llm_seconds` split by `llm_cold`, and
total time spent deaf to system output.

**The would-reject/real-dispatch check needed a real correction before
it was trustworthy.** The first version counted any `would_reject`
candidate whose exact text *ever* also appeared in a successful
`resolved` event as counter-evidence the gate would reject a real
command. Checked directly against this project's own log rather than
trusted on paper -- and it produced a false positive immediately: "oh"
and "mobile key" both "resolved to a real action" (via_llm=True, a
plausible-looking action attached) exactly once each, but that
resolution *is* the LLM hallucinating a confident action from noise --
precisely the failure mode this gate exists to catch, not a
counter-example to it. A `resolved` event records "the LLM produced
*something*", never "the user actually meant this." Fixed to require
the *same* text resolving to the *same* action *more than once* --
real, recurring intent is far less likely to repeat identically by
chance than a one-off hallucination landing on a plausible action is.
This is also, checked directly, exactly how this project's own prior
manual finding about "how many files" was actually reasoned about, not
a new standard invented for this tool -- and running the corrected
version surfaced a second thing worth recording: "bdk" repeating 3x to
`focus_vscode_repo` is the exact historical false-dispatch case this
project already has on record, caused by a hotwords bias removed since
(see "Mid-session wake-word swallow" above) -- a real illustration that
even "resolved the same way more than once" isn't airtight proof of
intent, just the strongest signal this log can offer. The tool's own
output says so rather than overclaiming; the remaining judgment call is
left to whoever reads it, on purpose.

## The push-to-talk hold threshold

Diagnosed directly from `logs/session.jsonl`: 215 of 216 logged
push-to-talk presses produced no speech at all. The instinct to blame
the specific key (Right Ctrl, a modifier held constantly while typing)
doesn't survive contact with the actual fix, though: swapping it for
Esc or any other single key would move the accidental-press problem,
not solve it. What actually fixes it is requiring the press carry some
minimum duration before anything commits to treating it as real --
`HOTKEY_HOLD_THRESHOLD_SECONDS = 0.22`, imperceptible for a press
someone means, comfortably above an incidental one.

**Both of the hotkey's jobs move behind the threshold, not just
capture.** `on_press` used to call `interrupt_speech()` (the barge-in
that cuts off a reply mid-sentence) instantly and unconditionally --
left alone, a stray tap while Jarvis was speaking would cut it off for
no reason. Confirming a hold now gates both: arming capture and the
barge-in happen at the same moment, for the same reason, which also
makes the key's two behaviours consistent with each other instead of
one being instant and the other debounced.

**A rolling pre-buffer, so the threshold costs no leading audio** --
the exact same trick the session-listening path already uses
(`PRE_BUFFER_FRAMES`, 0.3s), reused rather than reinvented. Without it,
confirming a hold at 220ms would mean the first 220ms of whatever was
actually said, starting right at the press, is exactly what gets
clipped off -- the opposite of what a hold-threshold fix should cost.
The buffer stays live at all times regardless of key state, and gets
handed off to `hotkey_frames` (and cleared) the instant a hold is
confirmed.

**Confirmation runs on its own `threading.Timer` thread, deliberately
neither inline in `on_press` nor inside the audio callback.** Not
`on_press`: the whole point is waiting out the threshold before
committing to anything, and `on_press` only fires once, at the start.
Not `hotkey_callback`: that runs on sounddevice's realtime audio
thread, where HUD writes and `interrupt_speech()`'s bookkeeping do not
belong -- this project's own standing rule that nothing may block an
audio callback (see the audio-callback-exception containment section
above) applies just as much to *slow*, non-exception-raising work as it
does to a crash. A dedicated lock (not `_processing_lock`, which can be
held up to 30s by an in-flight LLM call and has no business gating a
key-hold decision) serializes the timer's confirmation against
`on_release`, which runs on `keyboard`'s own callback thread and can
genuinely race it -- a release landing at the exact instant the timer
fires needs one consistent answer to "did this count as a hold", not
two threads independently guessing. Whichever side reaches the lock
first decides correctly either way: a release-first path leaves nothing
armed; a confirm-first path means the hold is processed as real, which
it was, if only by a hair.

A tap now produces zero trace at all -- no `hotkey_no_speech` log
event, no HUD line, nothing -- matching what 215 of 216 real presses
actually were. Verified with real `threading.Timer` wiring in the test
suite, not just by calling the confirmation function directly: one test
drops the threshold to 10ms and waits for the real timer to fire; a
second confirms a release before a (slightly longer) real threshold
genuinely cancels it, rather than just asserting the logic would do so
in isolation.

**Update (2026-10-08): raised to 1.0s, on direct request, once the
0.22s threshold had been live long enough to log its own result.**
`hotkey_no_speech`/`vad_gate` events -- a confirmed hold (passed 0.22s)
that still captured no real speech -- numbered 813 in `session.jsonl`
by this point, 97% of them under peak 0.01 (near silence, not a quiet
real attempt). The exact hold duration isn't logged directly, but the
also-logged `frames` count reconstructs it: `hotkey_frames` always
starts from the pre-buffer's frames at confirm time (4, at the 0.22s
threshold) plus whatever's appended afterward, so `duration ≈ 0.22 +
max(0, frames - 4) * FRAME_SECONDS` (80ms/frame). Reconstructed median
was 0.54s. Checked every round-number candidate against this
distribution before picking one: 0.6s would have caught 59.2% of these
813 before they ever reached `RECORDING`, 0.8s catches 78.2%, 1.0s
catches 83.9%, and 1.2s only adds another 5.5 points to that --
1.0s sits right where the marginal gain per extra 0.1-0.2s drops off,
which is as close to data-driven as a single global threshold gets
here. **No equivalent data exists for genuine quick commands** --
nothing in the log distinguishes a push-to-talk-origin dispatch from a
wake-word-session one, so there's no way to directly check how often a
real one-word command (press, say "mute", release) finishes inside the
new, longer window. This is a real, currently-unmeasured trade-off,
not a free win: revisit if short deliberate presses start getting
silently dropped, the same failure mode this whole mechanism exists to
avoid, just on the other side of the threshold now.

**`HOTKEY_PRE_BUFFER_FRAMES` had to be introduced as its own constant,
not just bumped in place, because `PRE_BUFFER_FRAMES` (0.3s) already
had a different job.** The session-listening path's own pre-buffer
(covering VAD detection lag, a few lines below `hotkey_pre_buffer` in
`main.py`) happened to share that same module-level constant purely by
coincidence of both landing on a similar tuning value, not because the
two problems are the same one. Reusing it for the hotkey side would
have meant either leaving the hotkey's pre-buffer at 0.3s (too short
to cover a 1.0s hold threshold -- reopening the exact clipping bug
`PRE_BUFFER_FRAMES` was written to prevent, just on push-to-talk) or
accidentally changing the session path's VAD-lag buffer along with it
for no reason connected to this change at all. `HOTKEY_PRE_BUFFER_FRAMES
= _frames(HOTKEY_HOLD_THRESHOLD_SECONDS + FRAME_SECONDS)` keeps the
same one-frame-of-margin relationship the original 0.22s/0.3s pair had,
automatically, if this threshold is ever tuned again.

## Persisted KV cache: a spike that won

`tools/_spike_kv_cache.py` (kept in the repo as the record, same
convention as `tools/_spike_desktop_move.py`, whichever way a spike
lands) set out to answer three questions before wiring anything real:
how big is a saved KV state, does loading one on a fresh `Llama()`
instance -- what a process restart looks like from llama.cpp's own
perspective -- actually beat the documented 13-15s cold-prefill
baseline, and does it survive `_system_prompt()`'s live date/time
having moved on since the state was saved. All three came back
favorable, measured against the real model and a genuinely separate
process invocation, not simulated: a ~20-24s cold call fell to
well under a second to reload, correct both against the exact original
date/time and one 47 minutes stale.

**The naive version would have shipped something needlessly bloated.**
`Llama.save_state()`'s return value pickled at over 300MB -- checked
directly why, not assumed acceptable: `LlamaState.scores` is a
full-vocabulary logits array (151936-wide for Qwen2.5) with one row per
retained token position, meant for a caller that wants to inspect past
per-token probabilities. Nothing in this codebase's use case does that;
confirmed live that zeroing it to a minimal placeholder shape leaves
`load_state()` and the inference that follows both working exactly the
same, correctness and speed both intact, while dropping the ~10x
majority of the file that was dead weight. The real payload
(`llama_state_size`) is ~25MB at this prompt's length; the persisted
file is that same ~25-26MB, not the ~322MB the naive approach would
have written to disk on every prewarm.

**Wired into `llm_intent.prewarm()` itself, not left as a spike-only
finding**, since the plan this was scoped from was explicit that a
winning result should be wired in, and this one won clearly.
`KV_CACHE_PATH` (`logs/llm_kv_cache.pkl`) lives alongside this
project's other gitignored, locally-regenerable runtime state
(`learned_seeds.json`, `seed_promotion_state.json`) -- not a model
file, not a log line, but the same category of "derived, safe to
delete, regenerates itself". `prewarm()` tries loading it first; only
falls back to the original real-inference path (unchanged) on any
failure at all -- file missing, corrupt pickle, or a fingerprint
mismatch against the *current* `SYSTEM_PROMPT_TEMPLATE` (hashed, not
stored as a raw string copy) protecting against the one scenario the
spike itself didn't test: the prompt template actually changing, a
bigger mismatch than a shifted date/time string and not assumed to be
equally safe just because that one measured fine. A successful cache
load also updates `call_count()` the same way a real prewarm inference
would have, so `main.py`'s `llm_cold` log field keeps telling the truth
about whether the next real dispatch is actually paying a cold cost --
it usually now isn't, for every boot after the first.

## Social intents and the acoustic gate

`"thank you"`, `"thanks"`, `"okay"`, `"bye"` needed handling -- real
conversation, logged verbatim in `session.jsonl` with no reply at all
today -- but every one of them is also a confirmed near-silence Whisper
hallucination, sitting in `_HALLUCINATION_DENYLIST` for exactly that
reason (the same list also carries `"subscribe"` and `"thanks for
watching"`, straight out of YouTube training data). That denylist only
ever gated the LLM-bound branch; the fast embedding-match path has no
acoustic gate at all by design, on the reasoning that a >=0.80 cosine
match against a seeded phrase is its own evidence something real was
said. Seeding these four as ordinary fast-path intents would have
bypassed the denylist entirely -- a hallucinated `"thank you"` on
silence would come back as an unprompted "You're welcome, sir."

**Resolution: social intents are the one fast-path class that also
gets an acoustic confidence check**, narrowly justified by the denylist
being about these exact phrases and nothing else --
`_ACOUSTICALLY_GATED_SEEDS` in `main.py`, checked against
`MatchResult.closest_seed` right after the match, before either social
intent or the context-dependent half of `disable_listening` can fire.
A rejection is treated as though nothing were heard at all: no cue, no
reply, nothing on the HUD -- silence answered with silence, the same
treatment `TRANSCRIPT_GATE_ENFORCED`'s shadow gate gives a rejected
LLM-bound transcript.

**The first implementation reused `_should_reject_transcript` wholesale
for this check, which was a real bug, not just an untested corner: it
never once let a social reply through, on any audio quality.**
`_should_reject_transcript`'s last two checks reject on denylist/wake-
echo *membership*, and every single `_ACOUSTICALLY_GATED_SEEDS` phrase
is, by construction, also a `_HALLUCINATION_DENYLIST` entry -- that's
*why* those five phrases were picked as the gated set in the first
place (see `intent_matcher.py`'s own comment on `social_thanks`/
`social_ack`). So calling the full function against "thank you" hit
the denylist branch every time, regardless of `avg_logprob`/
`no_speech_prob`/anything acoustic -- confirmed directly with a clean
quality dict (`avg_logprob -0.30`, no clipping) still getting rejected.
Caught by writing the first real end-to-end tests for this code path
(`test_state_machine.py`, driving `_handle_transcript` itself rather
than just the predicate in isolation) and finding all four "confident"
cases failed, not just the deliberately-noisy ones. Fixed by splitting
the acoustic-only checks into their own `_acoustic_reject_reason(quality)`,
used by the social-intent gate; `_should_reject_transcript` keeps its
full lexical-plus-acoustic behaviour for the pre-LLM gate, which
genuinely wants both.

**This path had never been exercised by any test before**, confirmed
directly: `test_state_machine.py`'s shared `_FakeMatcher.match()`
deliberately raises `AssertionError("not reached in this test")`, and
every test using the `live` fixture up to this point had avoided ever
calling it. Testing the gate meant giving individual tests a way to
control what the matcher returns without losing that trap for
everything else -- the fixture now also exposes the real matcher
instance (`live["matcher"]`), and a small `_stub_match()` helper
overrides just its `.match()` method per test via the test's own
`monkeypatch`, leaving the shared trap intact for every test that
doesn't opt in.

## Proactive briefing on boot

Half of what `main.py` says at boot was a single flat phrase --
"Online and at your service, sir." -- while `psutil` (battery/memory/
disk, already wired for `system_status`) and the window-enumeration
`describe_screen` already answers on demand both sat unused at exactly
the moment they'd matter most: the first thing Jarvis says after
starting up.

**`actions.morning_briefing()` replaces that flat phrase**, composing
up to three sentences from real state rather than reusing
`describe_screen`/`system_status`'s machinery a second, parallel way:
a time-of-day greeting, the battery fact (`_battery_fact`, unchanged),
and a plain count of what's already open (`_describe_screen_entries`/
`_count_by_app`/`_phrase_app_counts`, the same three functions
`describe_screen` itself calls). Any piece with nothing true to report
-- no battery sensor, an empty desktop -- is silently omitted rather
than guessed at; a bare greeting with nothing else to add is a
complete, honest briefing on a machine with no battery and nothing
open, not a broken one. Wired at the exact call site the old
`phrases.pick("boot")` used to occupy, with a `try`/`except` fallback
back to that same flat phrase -- a `psutil`/`win32gui` hiccup at boot
must never cost the one thing that already worked, an audible sign the
process is up.

**"Still where you left them" is only said when it's genuinely true.**
`memory.json`'s `last_layout` key existed before this (in `_DEFAULTS`)
but nothing read or wrote it -- this is what finally gives it a job.
`actions.save_current_layout()` snapshots the current window summary
and is called from `main.py`'s `_shutdown()`, not from boot: shutdown
is the last true state of the session that just ended, which is
exactly what the *next* boot needs to compare against, and writing it
at boot instead would compare a session against itself. An unclean
shutdown (crash, kill) just leaves the snapshot stale until the next
clean one -- a known, low-stakes gap, not worth a second write path
for. `memory.json` also defines `notes` and `preferences` keys, and
the briefing deliberately says nothing about either: nothing in this
project writes to them yet (both are placeholders for a feature that
was never built), and inventing content to fill them would be a guess
dressed up as a fact -- the same reasoning `_battery_fact`'s "honest
omission, not a guess" already applies to a missing sensor.

**Confirmed live against this machine**, not just against mocks:
`morning_briefing()` correctly read real battery/window state
("Battery at 55 percent, plugged in. You've got three Chrome, three VS
Code, and Spotify open, sir."), and a `save_current_layout()` /
`morning_briefing()` round-trip correctly produced the "still where you
left them" phrasing on the second call once the snapshot matched.

**This surfaced the same class of test-infrastructure gap as the
social-intent gate above, for the same underlying reason.** Two test
helpers run `main.main()` for real up to a stop point --
`test_state_machine.py`'s `live` fixture and `test_audio_callbacks.py`'s
`_callbacks_from_a_live_main` -- and both predate this feature, so
neither mocked it. `morning_briefing()` sitting in the middle of the
real boot sequence meant every test built on either helper was making a
real, unmocked `win32gui`/COM call (via `_describe_screen_entries` ->
`window_context.enrich_windows` -> `virtual_desktop`'s
`is_window_on_current_desktop`), caching a real COM manager onto
`virtual_desktop._thread_local` for the rest of the pytest process --
confirmed directly as the cause of `test_virtual_desktop.py`'s own
isolation test failing, but only when the full suite ran, never in
isolation. Both helpers now mock `morning_briefing` the same way they
already mock `speak`/`WhisperModel`/`output_monitor.start`/everything
else heavy at that point in `main()` -- the module docstring's rule
("no real audio device, no real models, no real window manager") always
implied this, it just didn't have a name to mock yet.

## Removed: "how many files are in this folder"

`count_files_in_folder` (read the focused File Explorer window's path via
`Shell.Application` COM, count files with `os.scandir`) is gone from
`actions.ACTIONS`, the LLM schema/prompt, and the query-shaped exclusion
list. Its phrasing also came out of `TRANSCRIBE_HOTWORDS` -- "files
folder" were there only for this action, and were among the fragments
Whisper hallucinated back from near-silence (see "the 'bdk wallet' false
dispatches" above); "how many windows" stays for `count_app_windows`.
Earlier sections still mention it as history. An LLM that somehow emits
the old name fails the enum re-validation in `infer()`, and
`dispatch_llm_action` would decline it as an unknown action regardless.
