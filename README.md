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
silently reset on this machine (see "Known environment issue" below).
Required:

| Directory | What | Used by |
|---|---|---|
| `models/faster-whisper-small.en/` | faster-whisper `small.en`, CTranslate2 format | `main.py` (transcription) |
| `models/bge-small-en-v1.5-onnx-q/` | `BAAI/bge-small-en-v1.5`, ONNX, quantized | `local_embedder.py` (intent matcher) |
| `models/qwen2.5-1.5b-instruct-gguf/` | Qwen2.5-1.5B-Instruct, GGUF Q4_K_M | `llm_intent.py` (LLM fallback) |
| `models/piper-en_GB-northern_english_male-medium/` | Piper TTS voice (active) | `tts.py` |
| `models/piper-en_GB-alan-medium/` | Piper TTS voice (switchable fallback) | `tts.py` (`VOICE = "alan"`) |

openWakeWord's `hey_jarvis` model and the bundled `silero_vad.onnx` are
fetched automatically by the `openwakeword` package itself on first run,
from GitHub release assets -- never blocked by the TLS issue below (only
huggingface.co is), so no manual-download workaround was ever needed there.

## Project layout

```
main.py              orchestrator: audio streams, wake word, VAD, session
                      state machine, control server, single-instance guard
actions.py           the execution layer -- every allowlisted action as a
                      plain Python function; the only thing ever dispatched to
apps_registry.py     the registered-apps table -- 7 hand-curated entries
                      (chrome/discord/vscode/file_explorer/settings/
                      local_disk_d/terminal) plus whatever app_discovery.py
                      finds; the actual security boundary for "open/
                      minimize apps in general"
app_discovery.py     scans the Start Menu at startup to expand apps_registry's
                      table automatically -- never overrides a curated entry
intent_matcher.py    fast path: local sentence-embedding similarity match
local_embedder.py    ONNX inference for bge-small-en-v1.5 (bypasses fastembed's
                      HF auto-download entirely)
llm_intent.py         fallback path: local Qwen2.5-1.5B-Instruct via
                      llama-cpp-python, grammar-constrained JSON output
seed_promotion.py    logs LLM-resolved phrases; promotes frequent ones into
                      the fast path on request (manual, not automatic -- see
                      "Known limitations" below)
vad.py               Silero VAD wrapper (speech/silence probability per frame)
virtual_desktop.py   IVirtualDesktopManager COM wrapper (current-desktop check)
tts.py               Piper TTS playback, mic-mute-while-speaking, persistent
                      output stream
hud.py               the terminal display -- single locked writer for all
                      user-facing output, pinned status bands, live mic meter,
                      in-place line rewriting. `python hud.py` runs a
                      standalone preview
earcons.py           short non-verbal cues synthesized with numpy at import
                      (wake blip, understood, decline, error, spin-down)
phrases.py           reply phrasing as pools of variants, one place to change
                      anything Jarvis says
jarvisctl.py         one-shot control-socket client (status/stats/debug/
                      pause/resume/quit)
jarvis.cmd           `jarvis <command>` wrapper, `start` handled here directly
launch.ps1           the actual `main.py` launch command (see "Terminal
                      theming" below for why this is a separate file)
_bench_llm.py        standalone latency/accuracy benchmark for llm_intent.py
                      -- not part of the live pipeline, run by hand after
                      changing the model/prompt/schema
requirements.txt      pinned dependencies (excl. llama-cpp-python, see Setup)
models/               model weights (gitignored -- see "Fetching the models")
tests/               pytest suite -- no audio device, no models,
                      no window manager required
logs/                 headless-mode output plus session.jsonl, the structured
                      event log written in every mode (gitignored)
```

## Known environment issue: TLS 1.3 gets reset

Something on this machine -- almost certainly antivirus HTTPS/web-shield
inspection, though never confirmed which product -- resets TLS 1.3
handshakes made by Python's TLS stack (`ConnectionResetError: [WinError
10054]`) while leaving TLS 1.2 and non-Python HTTPS clients (PowerShell's
`Invoke-WebRequest`, i.e. .NET's stack) untouched. Confirmed by hand:

- `Invoke-WebRequest -Uri https://huggingface.co` from PowerShell: works
- Python `ssl` handshake capped at `maximum_version = TLSv1_2`: works
- Python `ssl` handshake with default (TLS 1.3 offered): connection reset

Even with the TLS version capped, retries to huggingface.co from Python
stayed unreliable (worked once, failed ten times straight right after) --
this looks like TLS client fingerprinting, not just a protocol-version
block. **The practical resolution was to stop trying to make Python
download from huggingface.co at all**: every model above was fetched by
hand via `Invoke-WebRequest` into its `models/<name>/` directory, and
every module that loads a model (`local_embedder.py`, `llm_intent.py`,
`main.py`'s Whisper loading, `tts.py`) reads directly from that local
directory, bypassing `huggingface_hub`/`fastembed`'s auto-download
entirely. There is no code left in this repo that makes an HTTPS call at
runtime, so this is now purely a "how do I fetch the next model" note,
not a live workaround (`tls_compat.py`, a monkeypatch shim for exactly
this, existed early on for the abandoned in-Python-download approach and
was removed once every model moved to the manual-fetch pattern above).

Worth identifying which security product is doing the inspection at some
point and either updating it (several vendors have shipped TLS 1.3
handshake fixes) or excluding this dev environment -- the manual-fetch
workaround is fine indefinitely for this project specifically since every
model is a one-time download, but it'll resurface for any *other* Python
tool that needs to hit huggingface.co from this machine.

## Known environment quirk: every venv python.exe launch spawns a second process

Any script run via `D:\jarvis\.venv\Scripts\python.exe` -- even an empty
one -- spawns a child process a moment later running the *same* script
under the base interpreter (`C:\Users\agraw\...\Python312\python.exe`).
Reproduced with zero project imports, so it's not our code; almost
certainly the same security software again, doing some kind of
process-reputation check by shadow-executing new/unrecognized binaries.
The child has never been observed to reach real program logic (a log-write
smoke test only ever saw the parent's PID) but `main.py` takes a named
mutex (`_acquire_single_instance_lock`) as a hard guarantee regardless --
correct practice for an autostart background service anyway, independent
of this quirk.

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
pipeline, bypassing the wake-word/VAD-session layer entirely (see
"Design decisions" below).

`actions.py`'s `ACTIONS` dict is the actual security boundary: the
matcher/LLM only ever select a key into a fixed dict of plain Python
functions (never generate or execute arbitrary code or shell commands),
and every LLM-produced field is re-validated against real enums in
`llm_intent.py` before it ever reaches `actions.py` -- defense in depth,
the LLM's grammar constraint is never trusted as the only layer.

## Design decisions

### Listening: wake-word sessions, not per-command push-to-talk

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
by the TLS issue above.

### Push-to-talk: a persistent stream, not one opened per press

Originally opened a brand-new `sd.InputStream` on every key-press and
tore it down on release. Windows reliably glitches/drops the first
chunk(s) written to *or read from* a freshly-opened audio device (a
driver/DAC warm-up artifact -- confirmed on the output side first, see
the TTS section, then found to plausibly apply here too): for a short
press, a corrupted first chunk can be the entire utterance. Fixed by
opening the hotkey's `InputStream` once at startup and keeping it running
for the process's life; `on_press`/`on_release` now just toggle whether
its callback is actually buffering frames, rather than opening/closing
the device each time.

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

### Reliability: the listener thread doesn't die on error anymore

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

### Command-line control (`jarvisctl.py`), not a tray icon

Built a pystray-based tray icon first (enable/disable toggle + quit), but
removed it entirely in favor of a local control socket
(`127.0.0.1:47123`); `jarvisctl.py status|pause|resume|quit` is a
one-shot client. `jarvis.cmd` forwards those straight through and handles
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

### Idle unload after 30 minutes ("deep sleep")

`small.en` (~500MB) and the embedding matcher stay resident in RAM
otherwise, even fully idle. After 30 minutes with no wake word, session
activity, or push-to-talk use, both get dereferenced and GC'd; wake-word
detection and the mic keep running regardless, so "hey jarvis" still
works and reloads them on demand (a couple seconds' delay on that first
command back). A RAM optimization, not a privacy one -- nothing about
what's listened to or when changes.

### Transcription accuracy

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

### Intent resolution: local embedding match, local LLM fallback

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
16/17 correct on a realistic paraphrase set) before wiring in; accepted
~2.5-2.8s steady-state latency per call as the cost of the occasional
fallback, in exchange for the common case staying near-instant on the
fast path. Both the model weights *and the `llama_cpp` import itself* are
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
scans `llm_usage_log.jsonl` and promotes anything that qualifies,
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

### Virtual-desktop awareness

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

### Voice replies and terminal theming

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

## Known limitations / open items

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
  launch that has to wait for a window to appear, ~2.5-3s for an LLM
  fallback) runs inline in the same thread that watches for the next
  utterance. Nothing is lost during this window -- incoming audio still
  queues and gets processed once the thread frees up -- but Jarvis is
  genuinely unresponsive-feeling for that stretch. Fixing this properly
  means the session state machine tracking an in-flight command while
  still watching for the next utterance concurrently; not done, since it
  is a real restructuring of the state machine, not a contained fix.
- **"Hey jarvis" occasionally not registering for a stretch right after
  boot** was reported once; the persistent push-to-talk stream fix
  addresses one plausible contributor (the same device cold-open glitch
  class), but this wasn't confirmed as the full explanation. If it
  recurs, the next step is adding wake-score logging around startup to
  see what's actually happening, rather than another guess.
- **`VAD_SPEECH_THRESHOLD` (0.15) and the onset debounce (3 frames) are
  provisional** -- reasonable given the evidence so far, but the exact
  cause of at least one false-positive ("bdk"/"boom" transcribed from
  what was assumed to be background fan noise) was never independently
  confirmed. Revisit either value if quiet speech starts getting missed,
  or if background noise still occasionally gets treated as speech.

## Autostart, sleep, and command-line control

Three Task Scheduler entries, registered once via PowerShell
(`Register-ScheduledTask` + `New-CimInstance -CimClass
MSFT_TaskEventTrigger` for the event-based ones -- `New-ScheduledTaskTrigger`
has no built-in event-log trigger type):

- **"Jarvis Voice Control"** -- `AtLogOn` trigger for the current user.
  Action: `wt.exe -F -p Jarvis`.
- **"Jarvis Quit On Sleep"** -- event trigger on System log, Provider
  `Microsoft-Windows-Kernel-Power`, Event ID 42 (system entering sleep).
  Action: `jarvisctl.py quit`, hidden window, tolerant of "already not
  running."
- **"Jarvis Restart On Wake"** -- event trigger on System log, Provider
  `Microsoft-Windows-Power-Troubleshooter`, Event ID 1 (resume from
  sleep). Action: same themed launch as the logon task.

**Why quit-on-sleep/restart-on-wake instead of riding through suspend:**
audio devices are a known weak point across a real sleep/wake cycle (the
mic connection can come back broken, invisibly). A brand-new stream every
wake cycle sidesteps the whole class of bug rather than trying to detect
and recover from a possibly-broken one. State is NOT preserved across the
quit/restart -- comes back wake-word-armed regardless of what it was
doing before sleep.

**Command-line control**: `jarvis.cmd`, with `D:\jarvis` on the user
`PATH`. Forwards `status`/`pause`/`resume`/`quit` straight to
`jarvisctl.py`'s control-socket client; `start` is handled directly in
the batch file since `jarvisctl.py` can only talk to an already-running
instance.

## Status

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
