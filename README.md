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
apps_registry.py     the registered-apps table (chrome/discord/vscode/
                      file_explorer/settings/local_disk_d) -- the actual
                      security boundary for "open/minimize apps in general"
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
jarvisctl.py         one-shot control-socket client (status/pause/resume/quit)
jarvis.cmd           `jarvis <command>` wrapper, `start` handled here directly
launch.ps1           the actual `main.py` launch command (see "Terminal
                      theming" below for why this is a separate file)
_bench_llm.py        standalone latency/accuracy benchmark for llm_intent.py
                      -- not part of the live pipeline, run by hand after
                      changing the model/prompt/schema
requirements.txt      pinned dependencies (excl. llama-cpp-python, see Setup)
models/               model weights (gitignored -- see "Fetching the models")
logs/                 headless-mode output (gitignored; dead in the normal
                      flow, see the autostart section)
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
`resume` are a genuine hardware-level mic on/off (`stream.stop()`/
`.close()` on the main listening stream, so Windows' mic-in-use indicator
reflects it correctly) -- the only real "mic fully off" control, since
wake-word detection has to keep the mic hot no matter what to hear "hey
jarvis" at all.

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
generalized into a registry-driven pattern for six apps (chrome,
discord, vscode, file_explorer, settings, local_disk_d) plus
`focus_vscode_repo` (free-text substring match against `Code.exe` window
titles -- inherently safe regardless of the string, worst case is
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

`focus_or_launch_app`/`count_app_windows` only count a window as
"already up" if it's on the *current* virtual desktop --
`SetForegroundWindow` on a window on another desktop makes Windows
silently switch your active desktop to follow it, which is not what
"open X" should do to you. Uses the officially documented
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

**Terminal theming** -- a dedicated Windows Terminal profile named
"Jarvis" (own GUID, in `settings.json`), so none of this touches the
user's default/other profiles. Custom color scheme "Jarvis HUD":
background/foreground/cursor built around the Mark III suit's hot-rod red
and gold (`#FF2A1F` red, `#FFD8A8` warm gold-white foreground, `#0C0403`
near-black background), with cyan (`#00D9FF`) kept as the ANSI cyan accent
-- the original arc-reactor-blue HUD scheme, preserved as a "mix" rather
than fully replaced. Font: JetBrainsMono NFM (already the system default).

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
