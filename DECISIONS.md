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
- [State-machine audit against the documented diagram](#state-machine-audit-against-the-documented-diagram)
- [Command-line control (`jarvisctl.py`), not a tray icon](#command-line-control-jarvisctlpy-not-a-tray-icon)
- [Idle unload after 30 minutes ("deep sleep")](#idle-unload-after-30-minutes-deep-sleep)
- [Transcription accuracy](#transcription-accuracy)
- [Intent resolution: local embedding match, local LLM fallback](#intent-resolution-local-embedding-match-local-llm-fallback)
- [Latency: what the ">30s" actually was, and what changed](#latency-what-the-30s-actually-was-and-what-changed)
- [Virtual-desktop awareness](#virtual-desktop-awareness)
- [Screen and window context](#screen-and-window-context)
- [Voice replies and terminal theming](#voice-replies-and-terminal-theming)

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

## State-machine audit against the documented diagram

`USER_GUIDE.md` section 5 draws the runtime as a state diagram, and that
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
  names, desktop-navigation vocabulary, window-action verbs, query
  phrasing, and the user's actual repo names -- update it as new
  vocabulary gets added. Tradeoff (see the VAD-gating fixes above): a
  hotwords bias can make Whisper hallucinate fragments of that same
  vocabulary out of near-silent audio, so this is never applied without
  a VAD gate confirming real speech was present first.
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

A manual check of the actual Windows microphone input level (Sound
settings -> input device -> volume/boost) is still worth doing
independently -- there's no simple built-in PowerShell way to read or
adjust this, so it wasn't automated, but software-side tuning can only
compensate so much if the mic's own recorded level is set low at the OS
level.

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
- **Speed**: `SynthesisConfig(length_scale=1/1.1)` -- tuned down twice
  from an initial 1.5x (too fast), then 1.25x, settling at 1.1x once
  *all* conversational replies started being spoken (not just state
  transitions), since a faster rate that was fine for occasional short
  status lines felt rushed once every reply was read aloud.
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
