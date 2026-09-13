"""
Main loop: say "hey jarvis" once to open a session, then speak any
number of commands -- each is segmented by silence (VAD), transcribed,
matched, and executed, with no wake word needed in between. A stop
phrase ("power down", "stop listening", ...) closes the session back
to wake-word-armed. RIGHT CTRL push-to-talk works too, any time, via
its own independent audio stream -- a manual override that works
regardless of whether the main listener is on, off, or asleep.
Controlled from the command line via jarvisctl.py (status/pause/
resume/quit) talking to a local control socket -- no tray icon, no
GUI. See README.md for the phase-by-phase build and the design
rationale behind each piece.
"""

import json
import queue
import socket
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import keyboard
import win32api
import win32event
import winerror
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel

from intent_matcher import IntentMatcher
from actions import dispatch_llm_action, parse_fast_path_intent
from vad import SileroVAD
from tts import VOICE as tts_voice, speak, is_speaking, interrupt as interrupt_speech
import earcons
import hud
import llm_intent
import phrases
import seed_promotion

DOT = "·"  # separator used throughout the HUD lines

SESSION_LOG_PATH = Path(__file__).parent / "logs" / "session.jsonl"


def _log_event(kind: str, **fields) -> None:
    """
    Append one structured record to logs/session.jsonl.

    Always on, regardless of what the terminal is currently showing.
    The HUD is a *view* -- it elides raw parse dicts, match scores and
    per-stage timings to stay readable, and none of that should be lost
    just because the pretty layer chose not to draw it. When something
    misbehaves, this file is what actually answers "what did it hear,
    what did it match, and where did the time go"; `jarvis debug on`
    only changes how much of it is also mirrored to the screen.

    Best-effort by design: a logging failure must never propagate into
    the listener thread, which is the one thread that has to survive
    everything (see listener_loop's crash-containment comment).
    """
    try:
        SESSION_LOG_PATH.parent.mkdir(exist_ok=True)
        record = {"ts": time.time(), "kind": kind, **fields}
        with open(SESSION_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + chr(10))
    except Exception:
        pass


CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 47123

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms at 16kHz -- openWakeWord's native chunk size
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
HOTKEY = "right ctrl"
WAKE_WORD = "hey_jarvis"
WAKE_THRESHOLD = 0.5
VAD_SPEECH_THRESHOLD = 0.15  # 0.5 -> 0.35 -> 0.2 -> 0.1 -> 0.15: 0.1 caught normal
# speaking volume reliably but proved too sensitive to background noise/room
# chatter as false speech-onset; nudged back up slightly -- still noticeably
# more sensitive than the original 0.5, just not maximally so. Revisit toward
# 0.1 if quiet speech starts getting missed again, or up toward 0.2 if it's
# still picking up too much ambient noise.
# A likely root cause of this whole tuning history was found and fixed
# separately: vad.py was being fed straight off this file's native
# 1280-sample chunk size, well off Silero's trained 512-sample window,
# which measurably inflated its false-positive rate on noise (see
# vad.py's docstring). vad.py now rebuffers to its own native window
# internally regardless of caller chunk size -- 0.15 may end up more
# conservative than needed now; worth re-tuning from here once there's
# been a while of live use on the fixed VAD.

# "switch to desktop 2"/"switch to desktop two" -- both digit and spelled-out
# forms verified (in testing) to false-positive match "next desktop" seeds
# with high confidence (0.87+). Absolute desktop numbers aren't supported.
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}

# Dictation mode's exit phrase -- deliberately a fixed set checked
# against the raw transcript, not routed through the embedding matcher
# at all (see intent_matcher.py's start_dictation comment for why).
_DICTATION_STOP_PHRASES = {
    "stop dictation",
    "end dictation",
    "finish dictation",
    "done dictating",
    "stop dictating",
}


def _is_dictation_stop_phrase(text: str) -> bool:
    """Module-level (not nested in main()) so this is testable without starting the app."""
    return text.strip().lower().rstrip(".,!?") in _DICTATION_STOP_PHRASES


# Human-readable labels for _record_stat's categories, in a fixed
# display order -- a category with no entries yet just doesn't print,
# rather than every category always showing a "0".
# Stages timed on every utterance that reaches them, so `jarvis stats`
# can answer "where does the time actually go" with numbers instead of
# a guess. Kept as a rolling window per stage rather than a running
# mean -- a mean hides exactly the thing worth seeing here, which is
# that the LLM fallback's worst case is an order of magnitude off its
# median.
_TIMED_STAGES = ("transcribe", "match", "llm", "action")

_STAT_CATEGORY_LABELS = [
    ("fast_path", "fast-path matches"),
    ("llm_action", "LLM-resolved actions"),
    ("llm_chat", "LLM chat replies"),
    ("dictated", "dictated (typed verbatim)"),
    ("meta", "meta (pause/resume/dictation control)"),
    ("declined", "declined (e.g. desktop number)"),
    ("no_match", "no match at all"),
    ("off_duty", "heard while off duty"),
    ("empty", "heard nothing intelligible"),
]


def _percentile(values: list, fraction: float) -> float:
    """
    Nearest-rank on an already-sorted list. Deliberately not pulling in
    numpy.percentile for this -- the sample here is a few dozen floats
    and this runs on the control socket, not in any hot path.
    """
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[index]


def _format_stats(stats: dict) -> str:
    """Module-level (not nested in main()) so this is testable without starting the app."""
    total = stats.get("total", 0)
    if total == 0:
        return "No utterances processed yet, sir."

    lines = [f"{total} utterance(s) processed since startup:"]
    for category, label in _STAT_CATEGORY_LABELS:
        count = stats.get(category, 0)
        if count:
            lines.append(f"  {label}: {count}")

    timings = stats.get("timings", {})
    timed = [(stage, sorted(timings.get(stage, ()))) for stage in _TIMED_STAGES]
    timed = [(stage, values) for stage, values in timed if values]
    if timed:
        lines.append("Stage latency (p50 / p95 / worst, seconds):")
        for stage, values in timed:
            lines.append(
                f"  {stage:<11} {_percentile(values, 0.50):6.2f} / "
                f"{_percentile(values, 0.95):6.2f} / {values[-1]:6.2f}   (n={len(values)})"
            )

    recent = list(stats.get("recent", []))
    if recent:
        lines.append(f"Last {len(recent)}:")
        for entry in recent:
            ts = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            note = f" ({entry['note']})" if entry.get("note") else ""
            lines.append(f'  [{ts}] "{entry["text"]}" -> {entry["category"]}{note}')

    return "\n".join(lines)


def _frames(seconds: float) -> int:
    return max(1, round(seconds / FRAME_SECONDS))


SILENCE_HANG_FRAMES = _frames(0.4)  # quiet after speech ends one command -- was
# 0.8s, felt laggy for one-sentence commands; halved. Revisit upward if it starts
# cutting off mid-sentence pauses.
SPEECH_ONSET_FRAMES = _frames(0.24)  # consecutive VAD-positive frames required
# before actually committing to "recording started" -- a single frame crossing
# VAD_SPEECH_THRESHOLD isn't enough evidence of real speech. Continuous
# background noise (a fan, say) can spike above threshold for one frame even
# though it's not speech at all (confirmed directly: fan noise alone got
# transcribed as stray words like "bdk"/"boom"). Real speech sustains across
# several consecutive frames; a noise spike usually doesn't. The pre_buffer
# below already covers the actual head of the utterance regardless of exactly
# when this debounce confirms, so this costs no real audio, just ~240ms of
# certainty before starting to record.
MAX_COMMAND_FRAMES = _frames(7.2)  # hard cap per command
SESSION_IDLE_TIMEOUT_FRAMES = _frames(4 * 60)  # session auto-closes after this much silence
PRE_BUFFER_FRAMES = _frames(0.3)  # kept rolling so speech-onset isn't clipped by VAD detection lag
DEEP_SLEEP_IDLE_SECONDS = 30 * 60  # unload the heavy models after this long with nothing happening
INPUT_WATCHDOG_SECONDS = 5.0  # no audio callback at all for this long, on a
# stream that's supposed to be open and running, means the stream/device
# itself broke (a driver reset, a device unplugged, ...) -- not just a
# quiet room, since a healthy callback fires continuously regardless of
# whether anything's being said. Reopening once un-wedges it; see tts.py's
# matching output-side recovery for the same class of Windows audio-stack
# flakiness, just on the input side and detected by silence instead of a
# thrown exception.

WHISPER_MODEL_SIZE = "small.en"
# huggingface_hub's auto-download is unreliable here (see README) --
# fetched by hand into a local dir; fall back to the model-name string
# (which triggers the normal, occasionally-flaky auto-download) if the
# local copy isn't there, e.g. on a fresh machine.
WHISPER_LOCAL_DIR = Path(__file__).parent / "models" / "faster-whisper-small.en"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\JarvisVoiceControlSingleInstance"


def _acquire_single_instance_lock():
    """
    Something on this machine transparently spawns a second process
    whenever the venv's python.exe runs at all (reproduces even on a
    trivial no-op script -- looks like security-software process
    verification, not anything in our code; see README). It hasn't
    been seen to run our actual logic, but a named mutex makes that
    moot: only one process ever gets past this, full stop.
    """
    mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("Another copy of me is already running, sir. Standing down.")  # pre-HUD: nothing is initialised yet
        sys.exit(0)
    return mutex


def _redirect_output_if_headless():
    """
    pythonw.exe (used by the Task Scheduler autostart entry, so no
    console window flashes at every login) sets sys.stdout/stderr to
    None -- there's no console to attach to. Redirect to a log file in
    that case so the status output isn't just lost; leave it alone when
    running interactively (python.exe with a real console), which is
    what every test in this project so far has used.
    """
    if sys.stdout is not None:
        return
    log_path = Path(__file__).parent / "logs" / "jarvis.log"
    log_path.parent.mkdir(exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file


def main():
    _redirect_output_if_headless()
    _mutex = _acquire_single_instance_lock()
    # After the headless redirect, so hud.init() sees the real stdout and
    # correctly declines to emit escape sequences into a log file.
    hud.init()

    # whisper_model / matcher start unloaded and get loaded by
    # _ensure_heavy_models_loaded() below, right before the first real
    # use -- keeps startup path and the deep-sleep reload path identical.
    whisper_model = None
    matcher = None

    # Split in two so the boot sequence can time and report them as
    # separate stages -- they're the two slowest things that happen at
    # startup and lumping them into one "loading..." line was most of
    # why boot felt like a stall. _load_heavy_models() keeps the old
    # combined behaviour for the deep-sleep reload path, which has no
    # boot display to drive.
    def _load_whisper():
        nonlocal whisper_model
        whisper_source = str(WHISPER_LOCAL_DIR) if WHISPER_LOCAL_DIR.is_dir() else WHISPER_MODEL_SIZE
        whisper_model = WhisperModel(whisper_source, device="cpu", compute_type="int8")
        list(whisper_model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])

    def _load_matcher():
        nonlocal matcher
        matcher = IntentMatcher()

    def _load_heavy_models():
        _load_whisper()
        _load_matcher()

    def _unload_heavy_models():
        nonlocal whisper_model, matcher
        if whisper_model is None:
            return
        whisper_model = None
        matcher = None
        # llm_intent lazy-loads itself independently (only on first actual
        # fallback use, not at startup -- most sessions may never need it),
        # but once loaded it should still be swept up by deep sleep/pause.
        llm_intent.unload()
        hud.line("Dropping the heavy models to save memory.", "system")
        _log_event("models_unloaded")

    def _ensure_heavy_models_loaded():
        if whisper_model is None:
            # A flash, not a steady state -- the caller has usually just
            # set the state this belongs under (SESSION, straight after a
            # wake word), and overwriting it here left the display amber
            # until some later mode change happened to correct it.
            hud.begin_flash(hud.THINKING)
            hud.pending("waking the heavy circuits", spinner=True)
            earcons.play("wake_up")
            speak(phrases.pick("wake_up"))  # started before loading, not after -- fills the wait rather than adding to it
            _load_heavy_models()
            hud.resolve("back at full power", "system")
            hud.end_flash()
            _log_event("wake_up")

    hud.banner()
    with hud.boot_step("acoustic frontend", f"silero vad \u00b7 {SAMPLE_RATE}Hz \u00b7 {int(FRAME_SECONDS * 1000)}ms frames"):
        vad = SileroVAD(SAMPLE_RATE)
        # Separate instance for push-to-talk (below) -- SileroVAD carries
        # recurrent state (_h/_c) across calls, so sharing one object between
        # the two independent audio streams would corrupt both.
        hotkey_vad = SileroVAD(SAMPLE_RATE)
    with hud.boot_step("transcription core", f"whisper {WHISPER_MODEL_SIZE} \u00b7 int8 \u00b7 cpu"):
        _load_whisper()
    with hud.boot_step("intent lattice", "") as _lattice_step:
        _load_matcher()
        _lattice_step.detail = f"{len(matcher._seeds)} seeds"
    with hud.boot_step("wake sentinel", f'"{WAKE_WORD.replace("_", " ")}" \u2265 {WAKE_THRESHOLD:.2f}'):
        wake_model = WakeWordModel(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    with hud.boot_step("voice", f"piper \u00b7 en_GB {tts_voice}"):
        pass
    with hud.boot_step("control link", f"{CONTROL_HOST}:{CONTROL_PORT}"):
        pass

    hud.line(
        f'say "hey jarvis" to open a session \u00b7 "power down" to close it \u00b7 '
        f"RIGHT CTRL any time \u00b7 unloads after {DEEP_SLEEP_IDLE_SECONDS // 60}min idle",
        "dim",
    )
    hud.line("jarvis status | stats | debug | pause | resume | quit", "dim")
    hud.set_state(hud.ARMED)
    earcons.play("boot")
    speak(phrases.pick("boot"))
    _log_event("boot", whisper=WHISPER_MODEL_SIZE, voice=tts_voice)

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()

    # Defined *before* any stream is opened, not after.
    #
    # audio_callback closes over `state`, and PortAudio starts calling
    # it the instant stream.start() returns -- so with the assignment
    # below the open, the first callback could fire while `state` was
    # still unbound and raise NameError. That is not a harmless startup
    # hiccup: sounddevice registers its callback with
    # `error=_lib.paAbort`, so an exception escaping the callback
    # returns paAbort (2) to PortAudio, which *permanently stops the
    # stream*. No further callbacks, ever.
    #
    # Observed live: NameError at boot, then the wake word simply did
    # not work -- saying "hey jarvis" did nothing and the display stayed
    # on ARMED -- until the 5s input watchdog noticed the silence and
    # reopened the stream (by which time `state` existed). Push-to-talk
    # kept working throughout, because hotkey_stream is opened further
    # down, well after `state` is bound.
    state = {
        "enabled": True,
        "hotkey_active": False,
        "hotkey_frames": [],
        "stop": False,
        "last_activity": time.time(),
        "mode": "wake",
        "main_last_chunk": time.time(),
        "hotkey_last_chunk": time.time(),
        # One turn of pronoun memory -- the app a dispatched action last
        # actually touched, so "minimize it"/"close that" right after
        # "open chrome" can resolve without naming the app again. Fed to
        # the LLM fallback only (llm_intent.infer's context_app); the
        # fast embedding path has no notion of context at all.
        "last_app": None,
        # Dictation mode -- see _handle_transcript's dictation branch.
        # Reset to False at every point a session closes (see those
        # sites) so a stale True can never carry into a brand-new
        # session the user never asked to dictate into.
        "dictating": False,
        # Observability -- see _record_stat/_format_stats. In-memory
        # only, never persisted -- resets to zero on every restart, same
        # lifetime as everything else in `state`.
        "stats": {
            "total": 0,
            "recent": deque(maxlen=20),
            "timings": {stage: deque(maxlen=60) for stage in _TIMED_STAGES},
        },
    }

    def audio_callback(indata, frames, time_info, status):
        # Wrapped whole. sounddevice aborts the stream permanently on any
        # exception escaping here (error=_lib.paAbort), so a transient
        # failure in any one of these lines would silently kill all
        # listening for the rest of the process's life rather than
        # dropping a single frame. Nothing in here is worth that.
        try:
            if status:
                hud.note(f"main input stream status: {status}")
            state["main_last_chunk"] = time.time()
            mono = indata[:, 0].copy()
            # The only thing this callback does for the display: hand
            # over one float. hud.feed_level stores it and returns; all
            # drawing happens on the render thread. Anything that blocks
            # here drops audio frames, and dropped frames cost real
            # transcription accuracy -- this is the one place in the HUD
            # work with a genuine downside if done carelessly.
            hud.feed_level(float(np.abs(mono).max()))
            audio_q.put(mono)
        except Exception:
            _log_event("audio_callback_error", traceback=traceback.format_exc())

    def _create_stream():
        s = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=audio_callback,
            blocksize=FRAME_SAMPLES,
        )
        s.start()
        return s

    stream = _create_stream()

    def _record_stat(category: str, text: str, note: str = "") -> None:
        """
        Cheap, best-effort usage tracking for `jarvis stats` (the
        control socket) -- a running count per outcome category plus a
        rolling log of the last few, so "why does this feel slow/wrong"
        has something better to go on than re-reading scrollback. Called
        at every meaningful exit point of _handle_transcript.
        """
        stats = state["stats"]
        stats["total"] += 1
        stats[category] = stats.get(category, 0) + 1
        stats["recent"].append({"ts": time.time(), "text": text, "category": category, "note": note})
        hud.set_counters(
            cmds=stats.get("fast_path", 0) + stats.get("llm_action", 0),
            llm=stats.get("llm_action", 0) + stats.get("llm_chat", 0),
        )
        _log_event("outcome", category=category, text=text, note=note)

    def _record_timing(stage: str, seconds: float) -> None:
        state["stats"]["timings"][stage].append(seconds)

    def _restore_steady_state() -> None:
        """
        Put the display back to whatever the listener's actual mode
        implies.

        Push-to-talk is the one path with no mode transition of its own
        to hang a state change off -- pressing the hotkey doesn't move
        the listener's state machine at all. Without this, the display
        (and the room lights behind it) stayed stuck on bright cyan
        RECORDING after every single push-to-talk command, until the
        next wake word happened to move it along.
        """
        if not state["enabled"]:
            hud.set_state(hud.OFFLINE)
        elif state["mode"] in ("session_listening", "session_recording"):
            hud.set_state(hud.SESSION)
        elif whisper_model is None:
            hud.set_state(hud.ASLEEP)
        else:
            hud.set_state(hud.ARMED)

    def _reopen_input_stream():
        """
        Recovery for a stream that's gone silent while it's supposed to
        be running -- the input-side counterpart to tts.py's output
        stream recovery. last_chunk is bumped up front, before the
        reopen is even attempted, so a repeat failure (device still
        genuinely gone) waits out a full INPUT_WATCHDOG_SECONDS before
        the watchdog tries again instead of hammering it every 0.5s.
        """
        nonlocal stream
        state["main_last_chunk"] = time.time()
        hud.line("Main listening stream looks stuck -- reopening.", "warn")
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass
        stream = _create_stream()

    # Push-to-talk used to open a brand-new sd.InputStream on every single
    # press and tear it down on release -- the same class of Windows
    # cold-open glitch already found and fixed on the *output* side (see
    # tts.py) applies here too: a freshly-opened capture device can drop or
    # corrupt its first chunk(s), which for a short press can be the whole
    # utterance -- plausibly why push-to-talk kept reporting "didn't catch
    # actual speech" even when something was said. Opened once here instead
    # and kept running for the process's life; on_press/on_release below
    # just toggle whether the callback is actually buffering frames.
    def hotkey_callback(indata, frames, time_info, status):
        # Same unconditional guard as audio_callback, for the same
        # reason -- and it matters more here, since push-to-talk is the
        # documented manual override that is supposed to keep working
        # whatever else has gone wrong.
        try:
            if status:
                hud.note(f"push-to-talk input stream status: {status}")
            state["hotkey_last_chunk"] = time.time()
            if state["hotkey_active"]:
                mono = indata[:, 0].copy()
                hud.feed_level(float(np.abs(mono).max()))
                state["hotkey_frames"].append(mono)
        except Exception:
            _log_event("hotkey_callback_error", traceback=traceback.format_exc())

    hotkey_stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=hotkey_callback,
        blocksize=FRAME_SAMPLES,
    )
    hotkey_stream.start()

    def _reopen_hotkey_stream():
        nonlocal hotkey_stream
        state["hotkey_last_chunk"] = time.time()
        hud.line("Push-to-talk stream looks stuck -- reopening.", "warn")
        try:
            hotkey_stream.stop()
            hotkey_stream.close()
        except Exception:
            pass
        hotkey_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=hotkey_callback,
            blocksize=FRAME_SAMPLES,
        )
        hotkey_stream.start()

    def _set_enabled(enabled: bool):
        """
        Closes (or reopens) the main always-on session-listening stream
        -- the thing that would otherwise pick up ambient conversation
        and try to match/act on it. Push-to-talk's own stream
        (hotkey_stream, opened once at process start and kept running
        for the process's life -- see its definition below) is
        deliberately left running either way: it's the one manual
        override documented to keep working even while "paused", and
        reopening it fresh on every press would reintroduce exactly the
        cold-open glitch this project already worked around on the
        output side (tts.py). Practically, this means Windows' mic-in-use
        indicator can stay lit while paused -- that reflects hotkey_stream
        sitting armed for a press, not this process actually listening to
        or acting on anything; the wake-word/session path really is fully
        stopped.
        """
        nonlocal stream
        state["enabled"] = enabled
        if enabled:
            if stream is None:
                stream = _create_stream()
            hud.set_state(hud.ARMED)
            speak(phrases.pick("resume"))
        else:
            hud.set_state(hud.OFFLINE)
            if stream is not None:
                stream.stop()
                stream.close()
                stream = None
            # _processing_lock is an RLock specifically so this is safe
            # to call from inside an already-locked context too (e.g.
            # "stop listening" said via push-to-talk, mid-_process()) --
            # without it, unloading here could race an in-flight
            # transcribe() on another thread and crash it with a null
            # model reference.
            with _processing_lock:
                _unload_heavy_models()
            # Reset the session state machine too, not just the stream --
            # otherwise pausing mid-conversation and resuming later left
            # the listener stuck in "session_recording" with the models
            # unloaded, crashing on the next transcribe attempt.
            state["mode"] = "wake"
            state["dictating"] = False
            speak(phrases.pick("pause"))

    # Push-to-talk's worker thread (below) and the wake-word listener
    # both call into whisper_model/matcher/llm_intent -- serialized here
    # since neither is safe to drive from two threads at once. Reentrant
    # (RLock, not Lock) so _set_enabled's own unload -- called, among
    # other places, from inside an already-locked _process() when
    # "stop listening" is said via push-to-talk -- can safely take it
    # again on the same thread instead of deadlocking.
    _processing_lock = threading.RLock()

    # Domain vocabulary Whisper otherwise biases away from toward more common
    # words -- evidenced repeatedly in testing: "chrome" kept transcribing as
    # "room"/"groom"/"comb" even when the rest of the same sentence came
    # through cleanly. hotwords boosts recognition of exactly these terms
    # without conditioning on it as literal prior text (unlike initial_prompt).
    # Update this list as new app/action vocabulary gets added (Phase 5's
    # registered-apps table, new query phrasing, etc.) -- covers, in order:
    # apps, desktop navigation, window actions, query phrasing, and the
    # user's actual repo names (for VS Code repo-switching in Phase 5).
    TRANSCRIBE_HOTWORDS = (
        "Chrome Discord Visual Studio Code VS Code File Explorer Settings "
        "local disk D desktop virtual desktop next desktop previous desktop "
        "minimize focus switch open close "
        "how many files folder windows "
        "bdk wallet bdk cli Hashstack djed shu programming bitcoin bitshala"
    )

    # Segment-level confidence, gated on separately from the transcript
    # text itself -- used only to decide whether an LLM-fallback
    # resolution is trustworthy enough to log as a seed-promotion
    # candidate (seed_promotion.py), never to change what gets dispatched.
    # Whisper can hallucinate literal fragments of TRANSCRIBE_HOTWORDS
    # from near-silence (see that constant's comment) -- confirmed
    # directly in llm_usage_log.jsonl: two near-duplicate hallucinated
    # entries were one utterance away from being promoted into a
    # permanent bad fast-path seed before this existed.
    LOW_CONFIDENCE_NO_SPEECH_PROB = 0.6
    LOW_CONFIDENCE_AVG_LOGPROB = -1.0

    def _transcribe(audio: np.ndarray) -> "tuple[str, bool]":
        # Peak-normalize before transcribing -- helps quieter (but fully
        # captured) utterances regardless of how the mic/VAD are tuned.
        peak = np.max(np.abs(audio))
        if peak > 1e-4:
            audio = audio / peak
        segments, _ = whisper_model.transcribe(
            audio,
            language="en",
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=False,
            hotwords=TRANSCRIBE_HOTWORDS,
        )
        segments = list(segments)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        confident = bool(segments) and all(
            seg.no_speech_prob < LOW_CONFIDENCE_NO_SPEECH_PROB and seg.avg_logprob > LOW_CONFIDENCE_AVG_LOGPROB
            for seg in segments
        )
        return text, confident

    def _handle_transcript(text: str, confident: bool, via_session: bool) -> bool:
        """
        Thin wrapper whose only job is to guarantee the open-ended
        "thinking" colour is released no matter which of the dozen-odd
        return paths below fires. Held amber after an early return would
        strand the HUD (and the room lights behind it) in a state that
        never clears.
        """
        try:
            return _handle_transcript_inner(text, confident, via_session)
        finally:
            hud.end_flash()

    def _handle_transcript_inner(text: str, confident: bool, via_session: bool) -> bool:
        """Returns True if this call should close an open session."""
        state["last_activity"] = time.time()

        if not text:
            hud.line("Nothing intelligible there.", "dim")
            _record_stat("empty", text)
            return False

        # Immediate, unconditional echo the moment transcription is
        # done -- printed before any matching/dispatch happens, so
        # there's always some visible confirmation the mic actually
        # caught something, however long resolving it takes from here.
        # Added specifically because the LLM fallback below can run
        # ~3-30s (worse right after a (re)load -- see llm_intent.py) with
        # nothing else printed in the meantime, which read as Jarvis
        # having gone silently unresponsive rather than still working.
        hud.utterance(text)

        # Dictation mode: session-recorded utterances only -- never
        # push-to-talk, which stays a quick one-off command channel and
        # doubles as a reliable escape hatch out of dictation if the
        # exact stop phrase is forgotten. Typed verbatim into whatever
        # window currently has focus via keyboard.write(), never matched
        # against intents at all -- running dictated prose through the
        # embedding matcher risked accidentally executing something the
        # user was just narrating (e.g. dictating a sentence that
        # happens to mention "open chrome"). Ended only by an exact,
        # hardcoded phrase (_DICTATION_STOP_PHRASES) rather than the
        # matcher, for the same reason.
        if state["dictating"] and via_session:
            if _is_dictation_stop_phrase(text):
                state["dictating"] = False
                hud.resolve("dictation ended")
                earcons.play("dictation_off")
                speak(phrases.pick("dictation_stop"))
                _record_stat("meta", text, "dictation_stop")
                return False
            keyboard.write(text + " ")
            hud.resolve("typed verbatim")
            _record_stat("dictated", text)
            return False

        _t0 = time.time()
        result = matcher.match(text)
        _record_timing("match", time.time() - _t0)

        # "disable_listening" is context-dependent: mid-session it just
        # closes the session (wake word opens a new one later); said via
        # push-to-talk with no session open, it's the master mic-off.
        if result.intent == "disable_listening":
            if via_session:
                hud.resolve("session closed")
                earcons.play("session_close")
                speak(phrases.pick("session_close"))
                state["dictating"] = False  # never carry into the next session
                _record_stat("meta", text, "session_close")
                return True
            _set_enabled(False)
            hud.resolve('mic off ' + DOT + ' hold the hotkey and say "start listening" to return')
            earcons.play("session_close")
            _record_stat("meta", text, "pause")
            return False
        if result.intent == "enable_listening":
            _set_enabled(True)
            hud.resolve("back online")
            _record_stat("meta", text, "resume")
            return False

        if not state["enabled"]:
            hud.resolve("off duty", "warn")
            earcons.play("decline")
            speak(phrases.pick("off_duty"))
            _record_stat("off_duty", text)
            return False

        via_llm = False
        llm_seconds = 0.0
        # "start_dictation" is converted here rather than left to
        # parse_fast_path_intent (actions.py) -- it's a meta intent with
        # nothing in ACTIONS to dispatch it to, same category as
        # disable_listening/enable_listening above, just checked against
        # the *resolved* action below (with the LLM path too) rather
        # than against result.intent directly, so a phrasing the fast
        # path's small seed list misses can still be caught by the LLM
        # fallback instead of silently falling through to some unrelated
        # guess (confirmed directly: an unmatched dictation-start phrase
        # used to reach the LLM with no notion of dictation at all).
        if result.intent == "start_dictation":
            parsed = {"action": "start_dictation", "app": "none", "direction": "none", "repo": ""}
        else:
            parsed = parse_fast_path_intent(result.intent) if result.intent else None

        if parsed is None and result.intent is None:
            # Fast embedding match couldn't confidently place this --
            # fall back to the local LLM before giving up. Only reached
            # for phrasing the seed list doesn't cover, so this is the
            # occasional ~2.5-3s-to-30s path (see llm_intent.py's system
            # prompt for why it's not always at the low end), not the
            # common case. Spoken cue fired *before* the (possibly long)
            # call, not after -- confirmed directly this was needed:
            # with nothing said or printed between "Heard: ..." above
            # and the eventual reply, a slow fallback read as Jarvis
            # having stopped responding rather than still thinking.
            # state["last_app"] (one turn of pronoun memory) lets
            # "minimize it" resolve without naming the app again -- see
            # llm_intent.infer's context_app.
            hud.begin_flash(hud.THINKING)
            hud.pending("consulting local model", spinner=True)
            speak(phrases.pick("thinking"))
            _t0 = time.time()
            parsed = llm_intent.infer(text, context_app=state["last_app"])
            llm_seconds = time.time() - _t0
            _record_timing("llm", llm_seconds)
            via_llm = True

        if parsed is not None and parsed["action"] == "start_dictation":
            if not via_session:
                hud.resolve("dictation needs an open conversation", "warn")
                earcons.play("decline")
                speak(phrases.pick("dictation_declined"))
                _record_stat("meta", text, "dictation_declined")
                return False
            state["dictating"] = True
            hud.resolve("dictation started")
            earcons.play("dictation_on")
            speak(phrases.pick("dictation_start"))
            _record_stat("meta", text, "dictation_start")
            return False

        # Checked against the *resolved* action rather than just the
        # fast-path label, so this catches a number slipping through via
        # the LLM path too -- its schema can only express next/previous,
        # so a number anywhere in the phrase means it had to guess;
        # decline rather than risk silently landing on the wrong desktop.
        # "switch to desktop 2"/"desktop two" verified (in testing) to
        # false-positive match "next desktop" fast-path seeds with high
        # confidence (0.87+).
        text_lower_words = text.lower().split()
        _has_number = any(c.isdigit() for c in text) or any(w.strip(".,!?") in _NUMBER_WORDS for w in text_lower_words)
        if parsed is not None and parsed["action"] == "switch_desktop" and _has_number:
            hud.resolve("declined " + DOT + " numbered desktop", "warn")
            earcons.play("decline")
            speak(phrases.pick("desktop_number_declined"))
            _record_stat("declined", text, "desktop_number")
            return False

        if parsed is not None and parsed["action"] == "none":
            # Local LLM small-talk fallback (llm_intent.py's SYSTEM_PROMPT_TEMPLATE) --
            # not a command at all, so this never reaches dispatch_llm_action
            # and is never logged for seed promotion (compound_label has no
            # bucket for "none" anyway); the model's free-text "reply" is
            # spoken directly instead.
            reply = parsed["reply"].strip() or phrases.pick("no_match")
            hud.resolve(f"chat {DOT} local llm  {llm_seconds:.1f}s")
            hud.reply(reply)
            speak(reply)
            _record_stat("llm_chat", text, reply[:40])
            return False

        if parsed is None:
            if result.intent is not None:
                hud.resolve(f'matched "{result.intent}" but nothing is wired up for it', "warn")
                earcons.play("error")
            else:
                hud.resolve("no match", "warn")
                earcons.play("decline")
                speak(phrases.pick("no_match"))
            _record_stat("no_match", text)
            return False

        if via_llm:
            if confident:
                seed_promotion.log_llm_resolution(text, parsed)
            else:
                hud.note("low-confidence transcript -- not logged as a seed-promotion candidate")
            target = parsed["app"] if parsed["app"] != "none" else parsed["direction"]
            detail = f" {DOT} {target}" if target and target != "none" else ""
            hud.resolve(f"{parsed['action']}{detail}   local llm  {llm_seconds:.1f}s")
        else:
            # The raw score still prints, but the bar is what actually
            # gets read at a glance -- and it colours itself, so a match
            # that only just scraped through is visible without anyone
            # having to remember what 0.62 means.
            # Fast-path labels are "action:target" ("focus_or_launch_app:chrome");
            # the LLM path renders the same thing as "action · target".
            # Split here so both read identically -- the resolution is
            # the interesting part, not which path found it.
            label = result.intent.replace(":", f" {DOT} ", 1)
            hud.resolve(f"{label}   {result.score:.2f} {hud.confidence_bar(result.score)}")
        hud.note(f"parsed: {parsed}")
        _log_event(
            "resolved",
            text=text,
            via_llm=via_llm,
            parsed=parsed,
            score=None if via_llm else round(result.score, 3),
            llm_seconds=round(llm_seconds, 2) if via_llm else None,
        )

        # Remembered *before* dispatch, not after -- so it reflects what was
        # asked for regardless of whether the action itself actually
        # succeeded (e.g. "open catbook" failing doesn't erase context).
        if parsed["action"] in (
            "focus_or_launch_app",
            "minimize_app",
            "maximize_app",
            "count_app_windows",
        ) and parsed["app"] != "none":
            state["last_app"] = parsed["app"]
        elif parsed["action"] == "focus_vscode_repo":
            state["last_app"] = "vscode"

        earcons.play("understood")
        _t0 = time.time()
        message = dispatch_llm_action(parsed)
        _record_timing("action", time.time() - _t0)
        hud.reply(message)
        speak(message)
        _record_stat("llm_action" if via_llm else "fast_path", text, parsed["action"])
        return False

    # --- push-to-talk: its own independent stream, always available ---
    # regardless of the main listener's state (on, mic-off, or deep
    # asleep) -- this is the one manual control guaranteed to work.
    def on_press(_event):
        if state["hotkey_active"]:
            return
        if is_speaking():
            # Barge-in: a manual press cuts off whatever's still being
            # said instead of making the user wait it out -- push-to-talk
            # is the one control documented to work no matter what state
            # Jarvis is in, including mid-reply. Voice alone can't safely
            # do this (the session listener is fully muted while
            # speaking specifically to avoid hearing its own voice back
            # through the mic -- see is_speaking()'s other callers), but
            # a physical key press is unambiguously the user, not an echo.
            interrupt_speech()
            hud.line("Cutting myself off.", "dim")
        # Stamped here, not just after transcription finishes (inside
        # _handle_transcript) -- otherwise a long hold that starts right
        # before the deep-sleep timeout leaves last_activity stale for
        # the whole hold+transcribe duration, so the listener thread's
        # idle check could fire (well, block on _processing_lock and
        # then fire) right as this press is being processed.
        state["last_activity"] = time.time()
        state["hotkey_frames"] = []
        state["hotkey_active"] = True
        hud.set_state(hud.RECORDING)
        hud.line("All ears.", "dim")

    def on_release(_event):
        if not state["hotkey_active"]:
            return
        state["hotkey_active"] = False
        if not state["hotkey_frames"]:
            _restore_steady_state()
            hud.line("Dead air -- didn't catch a thing.", "dim")
            return

        frames = state["hotkey_frames"]
        # Whisper's hotwords bias (TRANSCRIBE_HOTWORDS below) makes it
        # recognize domain vocabulary better, but on near-silent audio it
        # can hallucinate fragments of that same vocabulary as if they'd
        # been spoken -- seen directly: a push-to-talk tap with no real
        # speech transcribed as literal chunks of TRANSCRIBE_HOTWORDS
        # ("how many files folder windows bdk wallet bdk wallet ...").
        # Gate on the same VAD the wake-word session already trusts for
        # this, rather than asking Whisper to recognize its own silence.
        hotkey_vad.reset()
        if not any(hotkey_vad.speech_prob(f) >= VAD_SPEECH_THRESHOLD for f in frames):
            _restore_steady_state()
            hud.line("Held the key, but didn't catch actual speech.", "dim")
            return
        audio = np.concatenate(frames)

        # keyboard's own hook dispatches on_press/on_release from a single
        # shared processing thread (keyboard/_generic.py's GenericListener),
        # so running transcription+dispatch here inline used to block that
        # thread for the full duration -- up to several seconds for a cold
        # app launch or an LLM fallback. A quick follow-up press-and-release
        # during that window had its on_press (which arms the capture
        # stream) queued behind the still-running on_release, so speech
        # spoken in the gap was genuinely never captured. Offloading the
        # slow part to a worker thread lets on_release return immediately.
        def _process(audio=audio):
            try:
                with _processing_lock:
                    _ensure_heavy_models_loaded()
                    text, confident = _transcribe(audio)
                    _handle_transcript(text, confident, via_session=False)
            except Exception:
                # Traceback goes to the structured log and to the HUD
                # only in debug mode -- a raw stack trace dumped into the
                # scrolling region tears the display apart, and it was
                # never the useful half of this handler anyway.
                _log_event("hotkey_error", traceback=traceback.format_exc())
                hud.line("Hit a snag processing that.", "warn")
                hud.note(traceback.format_exc())
                earcons.play("error")
                speak(phrases.pick("stumbled"))
            finally:
                # Every exit path, including the failure above -- a
                # display stuck mid-command is exactly the case where
                # knowing the real state matters most.
                _restore_steady_state()

        threading.Thread(target=_process, daemon=True).start()

    keyboard.on_press_key(HOTKEY, on_press)
    keyboard.on_release_key(HOTKEY, on_release)

    # --- wake word + session listener thread ---
    # Three modes: "wake" (watching only for the wake word),
    # "session_listening" (session open, watching for the next
    # utterance to start), "session_recording" (capturing one
    # utterance until silence ends it).
    def listener_loop():
        # state["mode"] (rather than a local var) so the control server's
        # status command can report it -- "wake" / "session_listening" /
        # "session_recording", matching the guide's state names.
        state["mode"] = "wake"
        command_frames: list[np.ndarray] = []
        silence_run = 0
        frame_count = 0
        session_silence_run = 0
        speech_onset_run = 0
        # Rolling buffer of recent frames kept while waiting for the next
        # utterance to start -- prepended once speech is detected, since
        # VAD needs a moment of evidence before it fires and a hard cut
        # right at detection tends to clip the first word.
        pre_buffer: "deque[np.ndarray]" = deque(maxlen=PRE_BUFFER_FRAMES)

        while not state["stop"]:
            try:
                chunk = audio_q.get(timeout=0.5)
            except queue.Empty:
                # Watchdog: a healthy stream's callback fires continuously
                # regardless of silence, so no chunk at all for this long
                # means the stream/device itself broke, not just a quiet
                # room. Own try/except so a failed reopen attempt (device
                # genuinely still gone) can't kill this thread the way an
                # uncaught exception used to (see the crash-containment
                # comment on the outer except below).
                try:
                    now = time.time()
                    if state["enabled"] and now - state["main_last_chunk"] > INPUT_WATCHDOG_SECONDS:
                        _reopen_input_stream()
                    if not state["hotkey_active"] and now - state["hotkey_last_chunk"] > INPUT_WATCHDOG_SECONDS:
                        _reopen_hotkey_stream()
                except Exception:
                    _log_event("stream_recovery_failed", traceback=traceback.format_exc())
                    hud.line("Couldn't recover an input stream -- will keep trying.", "warn")
                    hud.note(traceback.format_exc())
                continue

            # Push-to-talk has its own stream now -- just don't let the
            # main listener also react to the same speech while it's held.
            # Also mute while Jarvis is talking (is_speaking(), tts.py) --
            # otherwise the session listener, always-on unlike push-to-talk,
            # hears its own spoken replies and acts on them as new commands
            # (confirmed directly: "Switching to the next desktop, sir" kept
            # re-triggering itself, causing runaway desktop switches).
            if state["hotkey_active"] or not state["enabled"] or is_speaking():
                continue

            try:
                if state["mode"] == "wake":
                    if whisper_model is not None and time.time() - state["last_activity"] > DEEP_SLEEP_IDLE_SECONDS:
                        # Same lock the push-to-talk/session transcription
                        # paths hold while actually using the models --
                        # without it, this could unload out from under an
                        # in-flight transcribe() on another thread (e.g. a
                        # long push-to-talk hold that started just before
                        # the timeout, since last_activity is only stamped
                        # again once that transcription finishes) and crash
                        # it with a null model reference.
                        with _processing_lock:
                            _unload_heavy_models()
                        hud.set_state(hud.ASLEEP)
                        earcons.play("sleep")
                        speak(phrases.pick("sleep"))
                        _log_event("deep_sleep")

                    chunk_i16 = (chunk * 32767).astype(np.int16)
                    scores = wake_model.predict(chunk_i16)
                    if scores.get(WAKE_WORD, 0.0) > WAKE_THRESHOLD:
                        # State flips the instant the wake word is
                        # detected, before anything audio-related is
                        # even attempted. This used to come *after* a
                        # blocking earcon call, on the theory that the
                        # blip should land before the spoken "Yes, sir?"
                        # queued below -- but blocking=True meant this
                        # thread (the *only* thread that ever reads
                        # audio_q or moves state["mode"] along) had to
                        # acquire tts._playback_lock itself, a single
                        # lock shared with every speak()/earcon call in
                        # the process. If anything else -- most
                        # obviously the boot announcement's speak(),
                        # still synthesizing and mid-write on its own
                        # thread -- was holding that lock when the wake
                        # word landed, the entire listener sat frozen
                        # until it let go: no state change, no mode
                        # transition, nothing pulled off audio_q.
                        # Confirmed directly: the display stuck on
                        # ARMED after saying "hey jarvis" right after
                        # boot, until whatever was speaking finished.
                        # The earcon is now fire-and-forget like every
                        # other one -- it no longer has the power to
                        # stall the one thread this whole pipeline runs
                        # on, at the cost of no longer *guaranteeing*
                        # it lands before "Yes, sir?" (in practice it
                        # still does: it's a canned 115ms clip with no
                        # synthesis step, racing a phrase that has to be
                        # synthesized first).
                        hud.set_state(hud.SESSION)
                        earcons.play("wake")
                        _ensure_heavy_models_loaded()
                        state["last_activity"] = time.time()
                        hud.line('Session open -- say "power down" when you\'re done.', "system")
                        state["mode"] = "session_listening"
                        state["dictating"] = False  # a fresh session never starts already dictating
                        session_silence_run = 0
                        speech_onset_run = 0
                        pre_buffer.clear()
                        vad.reset()
                        wake_model.reset()
                        # openWakeWord needs a rolling window of audio to
                        # build confidence, so its score crosses threshold
                        # a beat after "hey jarvis" actually finished --
                        # anything said right on its heels with no pause
                        # could start before this mode switch lands, and
                        # come through clipped/garbled (seen directly: "open
                        # chrome for me" -> "or been grown for me"). A short
                        # spoken cue gives an unambiguous "now" to speak
                        # after -- and since is_speaking() already mutes the
                        # listener for its duration, nothing said *during*
                        # the cue is lost either, just deferred.
                        speak(phrases.pick("session_open"))

                elif state["mode"] == "session_listening":
                    pre_buffer.append(chunk)
                    prob = vad.speech_prob(chunk)
                    if prob >= VAD_SPEECH_THRESHOLD:
                        session_silence_run = 0
                        speech_onset_run += 1
                        if speech_onset_run >= SPEECH_ONSET_FRAMES:
                            state["mode"] = "session_recording"
                            hud.set_state(hud.RECORDING)
                            command_frames = list(pre_buffer)
                            silence_run = 0
                            frame_count = len(command_frames)
                            speech_onset_run = 0
                    else:
                        speech_onset_run = 0
                        session_silence_run += 1
                        if session_silence_run >= SESSION_IDLE_TIMEOUT_FRAMES:
                            hud.line("Quiet for a while -- closing the session.", "dim")
                            earcons.play("session_close")
                            hud.set_state(hud.ARMED)
                            state["mode"] = "wake"
                            state["dictating"] = False
                            wake_model.reset()

                elif state["mode"] == "session_recording":
                    command_frames.append(chunk)
                    frame_count += 1

                    prob = vad.speech_prob(chunk)
                    silence_run = 0 if prob >= VAD_SPEECH_THRESHOLD else silence_run + 1

                    if silence_run >= SILENCE_HANG_FRAMES or frame_count >= MAX_COMMAND_FRAMES:
                        audio = np.concatenate(command_frames)
                        # Same lock the push-to-talk worker thread uses -- see
                        # its definition above for why.
                        with _processing_lock:
                            _ensure_heavy_models_loaded()  # defense in depth -- see _set_enabled's mode reset for the actual fix
                            text, confident = _transcribe(audio)
                            vad.reset()
                            should_close = _handle_transcript(text, confident, via_session=True)
                        state["mode"] = "wake" if should_close else "session_listening"
                        hud.set_state(hud.ARMED if should_close else hud.SESSION)
                        session_silence_run = 0
                        speech_onset_run = 0
                        pre_buffer.clear()
                        if state["mode"] == "wake":
                            wake_model.reset()
            except Exception:
                # An uncaught exception here used to kill this whole thread
                # silently -- the process kept running and `jarvis status`
                # kept reporting "Wake-word armed", but nothing was actually
                # listening anymore until a full restart (this is exactly
                # how a stray pywintypes error from one bad action call took
                # the whole listener down). Log it, say something, and reset
                # to a known-good state instead of letting the thread die.
                _log_event("listener_error", traceback=traceback.format_exc())
                hud.line("Hit a snag there -- picking back up.", "warn")
                hud.note(traceback.format_exc())
                earcons.play("error")
                speak(phrases.pick("stumbled"))
                hud.set_state(hud.ARMED)
                state["mode"] = "wake"
                state["dictating"] = False
                session_silence_run = 0
                speech_onset_run = 0
                pre_buffer.clear()
                vad.reset()
                wake_model.reset()

    listener_thread = threading.Thread(target=listener_loop, daemon=True)
    listener_thread.start()

    def _shutdown():
        state["stop"] = True
        if stream is not None:
            stream.stop()
            stream.close()
        hotkey_stream.stop()
        hotkey_stream.close()
        keyboard.unhook_all()
        # Drop the GGUF model here, while the interpreter is still fully
        # alive. Left to be collected at exit instead, llama_cpp's
        # Llama.__del__ runs during interpreter teardown, by which point
        # the module globals its cleanup chain depends on may already be
        # None -- which surfaces as a "TypeError: 'NoneType' object is
        # not callable" traceback printed after the goodbye. Harmless,
        # but it's the last thing on screen and it reads like a crash.
        llm_intent.unload()

    def _announce_and_shutdown():
        # Blocking, not fire-and-forget -- the process exits right
        # after this, so a background thread would risk never
        # actually getting to play it.
        hud.set_state(hud.DORMANT)
        # Both blocking, and in this order: the process exits the moment
        # the goodbye finishes, so a fire-and-forget spin-down would
        # either be talked over or cut off by the exit entirely.
        earcons.play("power_down", blocking=True)
        speak(phrases.pick("shutdown"), blocking=True)
        _shutdown()

    # --- command-line control server: jarvisctl.py talks to this ---
    # localhost-only, fixed port; runs on the main thread and doubles
    # as the "keep the process alive" loop.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_EXCLUSIVEADDRUSE, not SO_REUSEADDR -- on Windows, SO_REUSEADDR's
    # semantics are looser than the POSIX name suggests: it can let a
    # second, unrelated process bind the same host:port even while this
    # one is actively listening (a local port-hijack vector for a socket
    # with no auth of its own beyond "localhost-only"). EXCLUSIVEADDRUSE
    # is Windows' actual "no, really, mine" flag.
    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    server.bind((CONTROL_HOST, CONTROL_PORT))
    server.listen(1)
    server.settimeout(0.5)

    try:
        while not state["stop"]:
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                command = conn.recv(64).decode("utf-8", "ignore").strip().lower()
                if command == "status":
                    if not state["enabled"]:
                        reply = "Paused (mic off)"
                    elif state["mode"] in ("session_listening", "session_recording"):
                        reply = "Conversation open"
                    elif whisper_model is None:
                        reply = "Wake-word armed (deep sleep)"
                    else:
                        reply = "Wake-word armed (awake)"
                elif command == "stats":
                    reply = _format_stats(state["stats"])
                elif command == "pause":
                    _set_enabled(False)
                    reply = "going quiet, sir"
                elif command == "resume":
                    _set_enabled(True)
                    reply = "back online, sir"
                elif command.startswith("debug"):
                    # Runtime verbosity toggle over the existing control
                    # socket -- no restart. The point is to be able to
                    # turn detail on the moment something misbehaves and
                    # catch the *next* occurrence, rather than restarting
                    # (and losing the state that produced the bug) to get
                    # a look at it. Nothing is actually lost either way:
                    # logs/session.jsonl records everything regardless,
                    # this only changes what is mirrored to the screen.
                    argument = command[len("debug"):].strip()
                    if argument in ("on", "off"):
                        hud.set_debug(argument == "on")
                    else:
                        hud.set_debug(not hud.is_debug())
                    reply = f"debug output {'on' if hud.is_debug() else 'off'}"
                elif command == "quit":
                    reply = "powering down, sir"
                    conn.sendall(reply.encode())
                    _announce_and_shutdown()
                    break
                else:
                    reply = f"unrecognized command: {command!r}"
                conn.sendall(reply.encode())
    except KeyboardInterrupt:
        # Only reachable at all when run in a visible console (Ctrl+C
        # has nothing to interrupt under pythonw.exe/Task Scheduler,
        # which has no console) -- still go through the same clean
        # shutdown path as jarvisctl.py quit rather than dying mid-way.
        _announce_and_shutdown()

    server.close()
    _log_event("shutdown")
    hud.shutdown()
    print("Powering down. Try not to miss me.")


if __name__ == "__main__":
    main()
