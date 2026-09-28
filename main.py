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
GUI. See STATUS.md for the phase-by-phase build and DECISIONS.md for the
design rationale behind each piece.
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
from actions import dispatch_llm_action, llm_screen_context, parse_fast_path_intent
from vad import SileroVAD
from tts import (
    VOICE as tts_voice,
    speak,
    is_speaking,
    interrupt as interrupt_speech,
    close_output as close_tts_output,
)
import earcons
import hud
import llm_intent
import output_monitor
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

# Near-miss wake scores, the measurement this project never had.
#
# Only scores *above* WAKE_THRESHOLD were ever recorded (see
# "wake_triggered" at its call site) -- so "I said hey jarvis and
# nothing happened" had no evidence behind it at all, and the two
# explanations it could have ("the model never heard anything" vs "the
# model heard it and scored 0.45") are indistinguishable from the log.
# That is precisely the gap that made a reported dead-window-after-boot
# un-diagnosable: the listener thread was provably alive and ungated
# through it (its own output-gate transitions are in session.jsonl for
# that exact window), the mode was provably "wake" (no wake_triggered,
# so no session was open to swallow anything), and yet nothing fired.
#
# Logged as a periodic high-water mark rather than per frame: at 12.5
# frames/sec a line per frame would be ~45k lines an hour of almost
# entirely zeros. Only windows whose best score actually cleared
# WAKE_NEAR_MISS_FLOOR are written, so a genuinely silent room stays
# silent in the log and a run of 0.3-0.49 near-misses -- the signature
# of a marginal trigger rather than a deaf one -- shows up plainly.
WAKE_NEAR_MISS_FLOOR = 0.2
WAKE_NEAR_MISS_WINDOW_SECONDS = 10.0

# The other half of "I said hey jarvis and nothing happened": the
# listener skips wake-word detection entirely while any of four gates is
# up (push-to-talk held, paused, Jarvis speaking, system audio playing).
# Each is individually correct and each is *meant* to be brief, but
# nothing ever recorded one being up for a long stretch -- so a gate
# stuck on (a leaked tts speaking-count, an output meter that latched)
# looked exactly like a wake word that simply didn't work, with the HUD
# still showing ARMED because the mode genuinely still is "wake".
# 30s is comfortably longer than any legitimate hold: Jarvis's longest
# reply is a few seconds, and a long music playlist -- the one case that
# legitimately stays up for minutes -- is worth a single log line too,
# since it is also a real answer to "why didn't it hear me".
LISTENER_GATE_STUCK_SECONDS = 30.0

# Raw (pre-normalization) peak at which captured audio counts as
# clipped. Not 1.0 exactly: float32 capture that saturated comes back
# at or a hair under full scale, and a flat-topped waveform is already
# distorted before it reaches the last fraction of a percent.
_CLIPPING_PEAK = 0.99
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


# --- pre-LLM transcript gate -------------------------------------------
#
# Ships OFF by default -- this only *logs* what it would have rejected
# (see the "transcript_would_reject" event at its call site in
# _handle_transcript_inner) without changing what actually gets
# dispatched, until TRANSCRIPT_GATE_ENFORCED is flipped to True by hand.
#
# Why gate at all: logs/session.jsonl and llm_usage_log.jsonl showed
# that *every* logged LLM-fallback resolution at the time this was
# written was a hallucinated transcript that still burned a full
# ~3-30s LLM call and got confidently dispatched as a real action
# ("or been grown for me" -> minimize chrome, "hey job is" -> open
# chrome, ...). Text-only heuristics (a denylist, a length floor) catch
# only a small fraction of these -- most are fluent-sounding multi-word
# phrases indistinguishable from a real command by their words alone.
# The signal that actually catches them is acoustic: Whisper's own
# per-segment no_speech_prob/avg_logprob/compression_ratio, already
# computed by faster-whisper for its own internal temperature-fallback
# decision (see _transcribe) and, until now, discarded immediately
# after being reduced to the single `confident` bool that main.py used
# *only* to gate seed-promotion logging, never dispatch. That is a
# reversal of a documented decision (see the `confident` computation's
# own comment below) -- recorded here, not silently changed.
#
# Why only gate the LLM-bound branch, never the fast embedding-match
# path: a >=0.80 cosine match against a seeded phrase is its own
# evidence a real command was heard, and must keep dispatching
# regardless of whatever this predicate would say. This is also what
# keeps the false-rejection blast radius small: the worst case is "a
# phrase the embedding matcher already couldn't place is now also
# refused a slow guess", not "a legitimate short fast-path command like
# 'mute' stops working" -- those never reach this function at all.
#
# Checked against real shadow data (see DECISIONS.md's "Mid-session
# wake-word swallow, and the 'bdk wallet' false dispatches") before ever
# considering flipping this: real, legitimately-dispatched utterances
# ("how many files", asked repeatedly) scored avg_logprob as low as
# -2.11 -- inside the same range as confirmed noise this gate is meant
# to catch ("bdk" at -1.83/-2.58). At the current sample size, no single
# global threshold on this signal separates the two without also
# rejecting a real, frequently-used command. Left False on that basis,
# not by default inertia -- revisit once there's enough
# transcript_would_reject data to place a threshold below the real-
# command floor, not just above the most obvious noise.
TRANSCRIPT_GATE_ENFORCED = False

# faster-whisper's own defaults for when its internal temperature-
# fallback loop considers a decode to have failed (compression_ratio_
# threshold / log_prob_threshold / no_speech_threshold) -- reused here
# rather than invented, since a segment that already reads as a failure
# to the transcriber itself is not evidence worth dispatching an action
# on.
_REJECT_NO_SPEECH_PROB = 0.6
_REJECT_AVG_LOGPROB = -1.0
_REJECT_COMPRESSION_RATIO = 2.4

# Deliberately NOT a minimum-word-count rule anywhere in this gate --
# "mute", "louder", "play", "pause", "lock" are all legitimate one-word
# seeded fast-path commands (intent_matcher.INTENTS); a word-count floor
# would make Jarvis selectively deaf to exactly its shortest, most
# common commands. Checked as an exact match on the whole normalized
# transcript, never a substring -- substring matching would eat "thank
# you, now open chrome" (confirmed as a real failure mode to avoid, not
# a hypothetical one).
_HALLUCINATION_DENYLIST = {
    "you",
    "thank you",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "bye",
    "okay",
    "ok",
    "yeah",
    "uh",
    "um",
    "hmm",
    "so",
    "and",
    "the",
    "music",
    "subscribe",
}

# Shared with the mid-session interception in _handle_transcript_inner
# below (see that check's comment) -- both places need the exact same
# notion of "this transcript IS the wake word, nothing else", just for
# two different purposes (discard vs. re-arm).
_WAKE_ECHO_PHRASES = {"hey jarvis", "jarvis"}


def _normalize_transcript(text: str) -> str:
    """Module-level, matching _is_dictation_stop_phrase's normalization convention."""
    return " ".join(text.lower().strip(" .,!?").split())


def _should_reject_transcript(text: str, quality: dict) -> "str | None":
    """
    Module-level (not nested in main()) so this is testable without
    starting the app -- see test_main.py's docstring for why that
    matters here specifically: it's the one piece of gating logic that
    must be independently verifiable, not just plumbing.

    Returns a short machine-readable reason to reject, or None to
    proceed to the LLM fallback as normal. `quality` is the dict
    _transcribe returns alongside the text (no_speech_prob, avg_logprob,
    compression_ratio -- see its definition).
    """
    normalized = _normalize_transcript(text)
    if not normalized:
        return "empty"

    if quality.get("no_speech_prob", 0.0) >= _REJECT_NO_SPEECH_PROB:
        return "no_speech_prob"
    if quality.get("avg_logprob", 0.0) <= _REJECT_AVG_LOGPROB:
        return "avg_logprob"
    if quality.get("compression_ratio", 0.0) >= _REJECT_COMPRESSION_RATIO:
        return "compression_ratio"

    if normalized in _HALLUCINATION_DENYLIST:
        return "denylist"
    if normalized in _WAKE_ECHO_PHRASES:
        return "wake_echo"

    return None


# --- interrogative-question guard ---------------------------------------
#
# The embedding matcher's seeds are all imperative commands ("open
# chrome", "minimize discord", "lock my computer") -- an utterance that
# instead ASKS about the same state sits close enough to the imperative
# seed in embedding space to false-positive against it with high
# confidence and get dispatched as if it were the command. Confirmed
# directly against the real matcher, every one a real action with a real
# side effect:
#   "is my computer locked"                              -> lock_workstation        0.870
#   "is chrome open"                                      -> focus_or_launch_app:chrome 0.931
#   "is discord minimized"                                -> minimize_app:discord    0.932
#   "how many chrome windows do i have open right now"    -> focus_or_launch_app:chrome 0.803
# That last one is a genuinely answerable query --
# count_app_windows exists specifically for it -- so the fast-path
# collision was silently stealing a real question from the LLM, which
# resolves it correctly given the chance.
#
# _QUERY_SHAPED_ACTIONS is the exclusion list: actions whose entire
# purpose IS to answer a question. Declining those would break the
# feature they exist for. "none"/"no_match"/"start_dictation" are also
# excluded -- they carry no side effect to guard against.
_QUERY_SHAPED_ACTIONS = frozenset(
    {
        "describe_screen",
        "now_playing",
        "count_app_windows",
        "count_files_in_folder",
        "none",
        "no_match",
        "start_dictation",
    }
)

# Deliberately does NOT include "can"/"could"/"would"/"will" -- those are
# the codebase's own seeded polite-request phrasing ("can you open
# chrome for me", "can you minimize discord for me") and must keep
# dispatching normally; they are commands, not questions, despite the
# grammatical resemblance. Confirmed directly: including them would
# have broken two of the matcher's own highest-confidence real seeds.
_INTERROGATIVE_OPENERS = {
    "is",
    "are",
    "am",
    "was",
    "were",
    "does",
    "did",
    "do",
    "what",
    "what's",
    "whats",
    "which",
    "who",
    "who's",
    "whos",
    "where",
    "when",
    "why",
    "how",
}


def _looks_interrogative(text: str) -> bool:
    """
    Module-level (not nested in main()) so this is testable without
    starting the app. Deliberately checks only the transcript's first
    word -- a cheap, high-precision signal (see _INTERROGATIVE_OPENERS'
    comment for what it was tuned against), not a general question
    detector.
    """
    words = _normalize_transcript(text).split()
    return bool(words) and words[0] in _INTERROGATIVE_OPENERS


def _is_dangerous_interrogative_match(action: str, text: str) -> bool:
    """Shared by both the fast-path override and the final pre-dispatch check below."""
    return action not in _QUERY_SHAPED_ACTIONS and _looks_interrogative(text)


# --- "move" vs switch_desktop guard --------------------------------------
#
# A second, unrelated false-positive class: "move this (window) to the
# next desktop" resolves to switch_desktop:next at 0.940 confidence --
# the matcher has no seed for moving a window across desktops (that
# action doesn't exist yet; when it does, this guard still matters,
# since "move" phrasing should never silently become "switch which
# desktop I'm viewing" instead of moving the window). Ships and applies
# regardless of whether a move-to-desktop action exists: right now, the
# wrong behaviour is switching your desktop and leaving the window
# behind, with no error at all -- worth declining honestly instead.
def _looks_like_a_move_request(text: str) -> bool:
    return "move" in _normalize_transcript(text).split()


# --- deictic gate for LLM screen context ---------------------------------
#
# actions.llm_screen_context() costs real prefill tokens on every LLM
# fallback call it's included in (measured directly: +0.51s median,
# +0.49s min-to-min, for a ~25-token context line, at the ~50 tok/s
# prefill this project's hardware sees -- see README's decision log).
# Most commands never need it: "open chrome" doesn't care what's
# currently focused. Gated on whether the utterance actually references
# what's on screen at all ("minimize this", "what's this page about",
# "move that to the other screen") -- validated against real commands:
# every genuine deictic phrasing fires, and every ordinary app/desktop/
# media command that actually reaches the LLM does not. A few phrasings
# fire spuriously ("what time is it", "skip this track") but all of
# those are seeded FAST-PATH commands that never reach the LLM at all,
# so the gate firing on them costs nothing.
_DEICTIC_WORDS = {"this", "that", "it", "here", "current", "focused", "screen", "window", "looking"}


def _looks_deictic(text: str) -> bool:
    return bool(set(_normalize_transcript(text).split()) & _DEICTIC_WORDS)


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
    ("rejected", "rejected as noise/misheard"),
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


# A listener thread that is merely busy looks identical to a dead one
# from the control socket, so this has to clear the worst *legitimate*
# block: a cold LLM fallback is documented at 3-30s, with transcription
# ahead of it. A genuinely dead thread never recovers, so erring long
# costs nothing but a slightly later diagnosis.
LISTENER_STALL_SECONDS = 60.0


def _status_line(
    enabled: bool,
    mode: str,
    whisper_loaded: bool,
    dictating: bool,
    llm_state: str,
    seconds_since_listener_beat: float = 0.0,
    input_silent: bool = False,
    speaking: bool = False,
) -> str:
    """
    The `jarvis status` reply, as a pure function of the state it
    reports. Module-level (not nested in main()) so this is testable
    without starting the app -- same reasoning as _format_stats and
    _should_reject_transcript above, and it earns it here because this is
    the string someone reads *when they already suspect something is
    wrong*. Every state the process can actually be in needs a name, and
    every name it can print needs to be reachable.
    """
    if not enabled:
        name = "Paused (mic off)"
    elif mode in ("session_listening", "session_recording"):
        # Dictation used to report as a plain "Conversation open", hiding
        # the one state in which a closing phrase does *not* close the
        # conversation -- it gets typed out instead.
        name = "Dictation (conversation open)" if dictating else "Conversation open"
    elif not whisper_loaded:
        name = "Wake-word armed (deep sleep)"
    else:
        name = "Wake-word armed (awake)"

    line = f"{name} {DOT} LLM {llm_state}"
    if enabled and input_silent:
        # The whole point of this line is to be read when something is
        # already suspected to be wrong, and "armed" is an active lie
        # when the capture device is handing over nothing. Reported even
        # though the stream, the thread and the models are all genuinely
        # healthy -- they were, for 50 minutes, while nothing could be
        # heard at all. Not folded into the name above because it is
        # orthogonal to it: a conversation can be open and deaf too.
        line += f" {DOT} NO MIC SIGNAL (capture device delivering silence)"
    if enabled and speaking:
        # Orthogonal to the name above, same as the mic line: the
        # listener skips wake-word detection entirely while Jarvis is
        # speaking, so "armed" and "actually listening right now" are
        # not the same claim. Normally true for a second or two at a
        # time -- but tts.py's speaking count is the one gate here that
        # can leak and stay up forever (see _speak_now's finally), and
        # when it does, this line is the difference between "the wake
        # word is broken" and "something never said it had stopped
        # talking". Two `jarvis status` calls ten seconds apart tell
        # those apart instantly.
        line += f" {DOT} speaking (wake word paused)"
    if seconds_since_listener_beat > LISTENER_STALL_SECONDS:
        # The listener loop has crash containment, but a thread that dies
        # anyway leaves this socket answering cheerfully while nothing is
        # listening at all -- the precise failure that containment was
        # added for. Say so rather than imply health.
        line += f" {DOT} listener stalled {seconds_since_listener_beat:.0f}s"
    return line


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

INPUT_SILENCE_FLOOR = 1e-4  # peak amplitude below which a frame counts as
# carrying no real audio. Just above one 16-bit LSB (1/32768 = 3.05e-5).
#
# NOT sufficient on its own to call a device dead, which is what this
# comment used to claim ("a genuinely quiet room still fluctuates well
# above it"). Measured directly on this machine's Intel SST array and
# that is simply false: in an ordinary quiet room **97% of frames sit at
# or below this floor**, because the DSP's own noise suppression floors
# a quiet room to 1-3 LSB. Amplitude alone therefore cannot tell "dead
# device" from "nobody is talking" -- and acting on it alone made the
# latch fire on ordinary silence roughly every two minutes, each time
# reopening a perfectly healthy capture stream for no reason. See
# INPUT_SILENCE_CONSTANT_ONLY below for what actually separates them.
INPUT_SIGNAL_FRAMES = _frames(0.24)  # consecutive above-floor frames required
# before believing the device is actually carrying audio again. Exactly the
# reasoning behind SPEECH_ONSET_FRAMES above, applied to device health
# instead of speech: one frame is not evidence.
#
# What it exists for specifically: a freshly-opened Windows capture device
# glitches its first chunk(s) (the reason streams are opened once and kept
# open at all -- see tts.py and the push-to-talk stream). So the reopen
# that the silence latch itself triggers hands back a chunk with a nonzero
# peak, which read as "signal is back" and cleared the latch 0.19s after
# setting it. Confirmed twice against a genuinely dead mic, whose own
# noise floor still spiked to 1.5e-4 on a cold open while its mean stayed
# at 3.4e-5. Real audio sustains across frames; a cold-open artifact does
# not.
INPUT_SILENCE_SECONDS = 120.0  # unbroken digital silence for this long on a
# stream that is *running* -- callbacks arriving on schedule, queue never
# empty -- means the device came back wrong, not that nobody spoke.
#
# INPUT_WATCHDOG_SECONDS above cannot see this case at all, and that is
# the hole it fell through: main_last_chunk is stamped by the callback
# regardless of what the callback was handed, so a driver feeding pure
# silence keeps every liveness signal in this process looking perfectly
# healthy. Confirmed live after a 13h sleep -- the Intel SST DSP reloaded
# its firmware on resume and came back delivering a constant 1 LSB;
# Windows reported the endpoint active, unmuted, at 100%, and `jarvis
# status` cheerfully said "Wake-word armed (awake)" for 50 minutes while
# nothing could possibly be heard. Two minutes is long enough that an
# actually-quiet room at 3am never trips it, and short enough to catch
# this before a day's worth of commands are lost to it.
#
# Reopening is tried once, since the recoverable version of this does
# exist -- but a wedged *driver* survives it (and survives a full process
# restart, also confirmed), so the latch and the status line matter more
# than the reopen: say "deaf" rather than imply health.

INPUT_SILENCE_CONSTANT_ONLY = True  # the second condition the latch needs,
# and the one that makes it mean anything.
#
# Amplitude cannot separate "dead device" from "quiet room" here (see
# INPUT_SILENCE_FLOOR). What does separate them is *variation*. Measured
# on the same hardware, both states:
#
#   wedged   : max peak == mean peak == 0.0000305, every frame identical,
#              one single distinct value for as long as you care to look
#   quiet room: 1, 2 and 3 LSB all present (0.0000305 / 0.000061 /
#              0.0000916) -- still below the floor, but genuinely dithering
#
# So the latch additionally requires that every quiet frame in the whole
# window carried the *exact same* value. A live converter dithers; a
# frozen one repeats. Erring toward silence-is-normal is deliberate: a
# false alarm costs a scary message plus a needless reopen of a healthy
# stream on a driver already known to be fragile, while a late detection
# costs only that -- lateness.

WHISPER_MODEL_SIZE = "small.en"
# huggingface_hub's auto-download is unreliable here (see README) --
# fetched by hand into a local dir; fall back to the model-name string
# (which triggers the normal, occasionally-flaky auto-download) if the
# local copy isn't there, e.g. on a fresh machine.
WHISPER_LOCAL_DIR = Path(__file__).parent / "models" / "faster-whisper-small.en"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\JarvisVoiceControlSingleInstance"

# How long a starting instance waits for a departing one to finish before
# giving up -- see _acquire_single_instance_lock. Sized against the quit
# path's own worst case (a blocking earcon, Piper synthesis and playback,
# then llm_intent.unload()), with room for that to be running against an
# audio stack Windows is already mid-way through suspending.
SINGLE_INSTANCE_WAIT_SECONDS = 20.0
SINGLE_INSTANCE_POLL_SECONDS = 0.5


def _acquire_single_instance_lock():
    """
    Something on this machine transparently spawns a second process
    whenever the venv's python.exe runs at all (reproduces even on a
    trivial no-op script -- looks like security-software process
    verification, not anything in our code; see README). It hasn't
    been seen to run our actual logic, but a named mutex makes that
    moot: only one process ever gets past this, full stop.

    Waits rather than standing down instantly, which is what the sleep/
    wake cycle actually needs. Two Task Scheduler entries drive that
    cycle -- "Jarvis Quit On Sleep" (Kernel-Power 42) and "Jarvis Restart
    On Wake" (Power-Troubleshooter 1) -- and nothing sequences them. The
    quit path deliberately blocks on audio right as Windows tears the
    audio stack down for suspend, so the outgoing process is routinely
    still alive when the incoming one starts. Standing down immediately
    meant the restart lost that race and left *nothing* running until the
    next logon -- and lost it silently, since this exits 0, so Task
    Scheduler recorded a success. Logged either way now, so a stand-down
    is at least visible in logs/session.jsonl after the fact.
    """
    deadline = time.monotonic() + SINGLE_INSTANCE_WAIT_SECONDS
    waited = 0.0
    while True:
        mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        # Read before anything else can clobber this thread's last error.
        already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
        if not already_running:
            if waited:
                _log_event("single_instance_acquired_after_wait", waited_seconds=round(waited, 1))
            return mutex
        # Ours only by name -- drop the duplicate handle before retrying,
        # or every poll leaks one for the life of the process.
        try:
            win32api.CloseHandle(mutex)
        except Exception:
            pass
        if time.monotonic() >= deadline:
            print("Another copy of me is already running, sir. Standing down.")  # pre-HUD: nothing is initialised yet
            _log_event("single_instance_stand_down", waited_seconds=round(waited, 1))
            sys.exit(0)
        time.sleep(SINGLE_INSTANCE_POLL_SECONDS)
        waited += SINGLE_INSTANCE_POLL_SECONDS


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


def _log_dead_thread(args):
    """
    Last-resort net for a thread dying on an uncaught exception.

    Python's default hook prints a traceback to stderr and nothing else
    -- which under `jarvis start` means it lands in a Windows Terminal
    window nobody is reading, and the thread is simply gone. That is how
    a dead listener came to look exactly like a healthy one: the process
    kept running, `jarvis status` kept saying "Wake-word armed", and the
    only symptom was that nothing was heard ever again.

    listener_loop has its own containment and should never reach here.
    This exists precisely for the case where that containment has a hole
    in it -- as it did when the output gate was first added above the
    try block rather than inside it.
    """
    _log_event(
        "thread_died",
        thread=getattr(args.thread, "name", "?"),
        exc_type=getattr(args.exc_type, "__name__", str(args.exc_type)),
        traceback="".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        ),
    )
    try:
        hud.line(
            f"A background thread ({getattr(args.thread, 'name', '?')}) died -- "
            "see logs/session.jsonl. Restart jarvis.",
            "warn",
        )
    except Exception:
        pass


def main():
    _redirect_output_if_headless()
    _mutex = _acquire_single_instance_lock()
    threading.excepthook = _log_dead_thread
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
        # cpu_threads explicit (maps to CTranslate2's intra_threads) --
        # left at the library default (0, meaning "let CTranslate2
        # choose") this machine's logical-core count could oversubscribe
        # its physical cores, which matters more now that the LLM
        # prewarm (llm_intent.prewarm, see below) may be running
        # concurrently on the same 4 physical cores.
        whisper_model = WhisperModel(whisper_source, device="cpu", compute_type="int8", cpu_threads=4)
        list(whisper_model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])

    def _load_matcher():
        nonlocal matcher
        matcher = IntentMatcher()

    def _load_heavy_models():
        # All-or-nothing. Left half-loaded (Whisper up, the matcher's own
        # load having thrown), whisper_model is non-None, so
        # _ensure_heavy_models_loaded's `is None` check never retries --
        # and every matcher.match() afterwards raises into the listener's
        # crash handler instead, for the rest of the process's life, with
        # a spoken "picking back up" apology each time. Drop both so the
        # next utterance gets a real retry.
        nonlocal whisper_model, matcher
        try:
            _load_whisper()
            _load_matcher()
        except Exception:
            whisper_model = None
            matcher = None
            raise

    # llama.cpp mmaps the GGUF's weights by default (use_mmap=True) --
    # they're file-backed, evictable pages, not committed RAM, so holding
    # the LLM resident is mostly free. The real cost unloading it was
    # "saving" is the ~13-27s one-time prompt-prefill cost measured on
    # this project's hardware (see llm_intent.py/README's decision log),
    # which unconditional unloading was forcing the very next command
    # after every deep-sleep or pause/resume cycle to pay again -- deep
    # sleep fires after 30 idle minutes, which is exactly the state
    # you're in right before the *next* thing you say. Kept False by
    # default; flip to True to go back to unloading the LLM alongside
    # Whisper/the matcher if RAM pressure on a given machine makes that
    # the better trade -- Whisper (~500MB) and the matcher are unloaded
    # either way, unaffected by this flag. Shutdown always unloads the
    # LLM regardless (see _shutdown() below) -- the mmap'd pages don't
    # need to survive the process exiting.
    UNLOAD_LLM_ON_DEEP_SLEEP = False

    def _start_llm_prewarm(reason: str, delay: float = 0.0) -> None:
        """
        Fires llm_intent.prewarm() on a background daemon thread. Not
        loading the model -- constructing the Llama object is only
        ~1.5s -- but *using* it once so the KV cache holds the system
        prompt's prefix, which is where the measured ~13-27s cold cost
        actually is (see llm_intent.prewarm's docstring). No-ops
        immediately (no thread started, nothing logged) if a prewarm is
        already running or already warm, so this is safe to call
        speculatively and often (e.g. from _ensure_heavy_models_loaded,
        which runs on every utterance as a defense-in-depth check).

        Never awaited: if a real command needs the LLM before this
        finishes, llm_intent.infer() simply blocks on the same internal
        lock prewarm holds (llm_intent._lock) and gets the benefit of
        whatever prefill already happened by then -- worst case is
        today's cold cost, not worse.
        """
        if llm_intent.prewarm_state() in ("warming", "warm"):
            return

        def _run():
            if delay:
                time.sleep(delay)
            try:
                llm_intent.prewarm()
            except Exception:
                # Must never propagate -- this runs on a daemon thread
                # with nothing to catch it, and a stuck "warming" state
                # would otherwise silently disable every future prewarm
                # attempt this process's life (see the early-return
                # above). llm_intent.prewarm() already guards its own
                # body and sets prewarm_state to "failed" on error, so
                # reaching this except is itself unexpected -- logged as
                # such rather than assumed impossible.
                _log_event("llm_prewarm_error", reason=reason, traceback=traceback.format_exc())

        threading.Thread(target=_run, name=f"llm-prewarm-{reason}", daemon=True).start()
        _log_event("llm_prewarm_started", reason=reason)

    def _unload_heavy_models():
        nonlocal whisper_model, matcher
        if whisper_model is None:
            return
        whisper_model = None
        matcher = None
        # llm_intent lazy-loads itself independently (only on first actual
        # fallback use, not at startup -- most sessions may never need
        # it) -- see UNLOAD_LLM_ON_DEEP_SLEEP above for why it's no
        # longer swept up here by default the way Whisper/the matcher are.
        if UNLOAD_LLM_ON_DEEP_SLEEP:
            llm_intent.unload()
        hud.line("Dropping the transcription models to save memory.", "system")
        _log_event("models_unloaded")

    def _ensure_heavy_models_loaded():
        # Takes _processing_lock itself rather than trusting every caller
        # to. Two of the three call sites already held it (it is an RLock,
        # so re-entering on the same thread is free) -- the wake-word path
        # did not, so a push-to-talk worker part-way through loading the
        # models could be joined by the listener kicking off a *second*
        # concurrent load: two ~500MB WhisperModel constructions and two
        # spoken "waking the heavy circuits" racing each other across four
        # physical cores.
        with _processing_lock:
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
                # Safety net, not the primary path -- with UNLOAD_LLM_ON_DEEP_SLEEP
                # False (the default) the LLM was never actually dropped, so
                # prewarm_state() is almost always already "warm" here and
                # this no-ops instantly. Matters if that flag is ever flipped
                # back to True, or the LLM was unloaded for some other reason.
                _start_llm_prewarm("wake")

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
    with hud.boot_step("output guard", f"peak \u2265 {output_monitor.PEAK_THRESHOLD:.2f} \u00b7 render endpoint"):
        output_monitor.start()
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
        # Last time the main stream carried anything above
        # INPUT_SILENCE_FLOOR, and whether the "this device is delivering
        # nothing" latch is currently set. Separate from main_last_chunk
        # on purpose: that one answers "is the stream running", these
        # answer "is it actually hearing anything", and a broken audio
        # stack can say yes to the first and no to the second.
        "main_signal_at": time.time(),
        "input_silent": False,
        # Consecutive above-floor frames seen so far -- see
        # INPUT_SIGNAL_FRAMES. Owned by audio_callback alone.
        "signal_run": 0,
        # Range of the below-floor frames seen since real audio last
        # arrived -- see INPUT_SILENCE_CONSTANT_ONLY. None until the
        # first quiet frame. Owned by audio_callback alone.
        "quiet_peak_lo": None,
        "quiet_peak_hi": None,
        # See _check_output_gate: tracks the system-output gate's own
        # edge, separately from is_speaking()/hotkey_active/enabled.
        "output_was_playing": False,
        "output_playing_since": 0.0,
        # Rolling high-water mark for sub-threshold wake scores -- see
        # WAKE_NEAR_MISS_FLOOR. Owned by the listener thread alone.
        "wake_best_score": 0.0,
        "wake_window_started": time.time(),
        # How long the combined skip condition has been up, and whether
        # this stretch has already been reported -- see
        # LISTENER_GATE_STUCK_SECONDS. Owned by the listener thread.
        "gate_blocked_since": None,
        "gate_blocked_logged": False,
        # Liveness for `jarvis status` -- stamped on every listener_loop
        # iteration. That loop has crash containment, but a thread that
        # dies anyway used to leave the control socket cheerfully
        # reporting "Wake-word armed" while nothing was listening at all.
        "listener_beat": time.time(),
        # Raised by the push-to-talk worker, consumed by listener_loop:
        # the conversation's idle countdown is a frame counter local to
        # that loop, so the hotkey thread cannot reset it directly.
        "renew_session": False,
        # Raised by _set_enabled on resume, consumed by listener_loop --
        # same reasoning: the VAD and wake-word models are driven by that
        # thread and must not be reset out from under it.
        "reset_acoustic": False,
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
            peak = float(np.abs(mono).max())
            hud.feed_level(peak)
            # One comparison, no allocation -- the peak is already
            # computed for the display above, so tracking "when did this
            # stream last carry any signal at all" costs nothing extra in
            # the one place that must not block. The evaluation of it
            # happens on the listener thread (_check_input_signal).
            if peak > INPUT_SILENCE_FLOOR:
                state["signal_run"] += 1
                if state["signal_run"] >= INPUT_SIGNAL_FRAMES:
                    state["main_signal_at"] = time.time()
                    state["quiet_peak_lo"] = None
                    state["quiet_peak_hi"] = None
            else:
                state["signal_run"] = 0
                # Range of the quiet frames themselves -- the only thing
                # that separates a frozen converter from a quiet room,
                # since both sit under the floor. Two floats, no
                # allocation, in the one place that must not block.
                lo, hi = state["quiet_peak_lo"], state["quiet_peak_hi"]
                state["quiet_peak_lo"] = peak if lo is None else min(lo, peak)
                state["quiet_peak_hi"] = peak if hi is None else max(hi, peak)
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
    # Guards every open/close/reassignment of `stream` (never
    # `_processing_lock` -- that one can be held for up to 30s by an
    # in-flight LLM call, and pause must not wait behind that). Four
    # places touch this variable across two threads with no prior
    # synchronization at all: the listener thread's own watchdog/silence
    # recovery (_reopen_input_stream), and the control-socket thread's
    # _set_enabled (pause/resume) and _shutdown (quit). Found by
    # deliberately auditing every touchpoint after two mic-wedge
    # incidents that a sleep-timing race explained on its own -- this
    # race needs no sleep at all, just an ordinary pause/resume/quit
    # landing at the same moment the listener thread's own watchdog
    # decides the stream needs reopening. Without a lock: a resume could
    # see `stream is None` set by a reopen already in flight and open a
    # *second* concurrent capture stream on the same physical device
    # (the first orphaned, never stopped -- callback still attached),
    # or a reopen's stream.start() could race a pause's stream.close()
    # on the very same object. Either is exactly the class of thing that
    # could leave a flaky driver like Intel SST wedged, independent of
    # the sleep-suspend timing already fixed elsewhere. Not confirmed as
    # the cause of either past incident (both align cleanly with the
    # sleep-quit race instead), but a real, previously-unsynchronized
    # hazard regardless of whether it has fired yet.
    _stream_lock = threading.Lock()

    def _drain_audio_queue() -> int:
        """
        Throw away everything captured while something else was blocking.

        The mute that stops the always-on session listener hearing
        Jarvis's own replies (is_speaking(), checked in listener_loop) is
        evaluated when a chunk is *dequeued*, not when it was captured --
        and audio_q is unbounded, so a 3-30s LLM fallback banks every
        frame recorded during it. Drained afterwards at machine speed,
        those frames are only harmless while a reply is still playing.

        On the paths that end without speaking at all -- the LLM's own
        abstain, an empty transcript, a dictated line -- the mute is
        already lifted by then, and the backlog still holds the "One
        moment, sir" spoken *before* the call. VAD fires on it (three
        frames is the onset bar), the listener records it, and Jarvis
        transcribes its own voice as the next command -- which misses the
        fast path, says "One moment, sir" again, and abstains again.
        Dropping the backlog is the fix; that audio is stale by
        definition, and anything still being said is still being captured
        live.
        """
        dropped = 0
        try:
            while True:
                audio_q.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        if dropped:
            hud.note(f"dropped {dropped} stale audio frame(s)")
        return dropped

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
            # Dictation gets its own colour rather than borrowing
            # SESSION's: it is the one state in which a closing phrase
            # does NOT close the conversation (it gets typed out like any
            # other words), so it must not look identical to the state
            # where it does.
            hud.set_state(hud.DICTATING if state["dictating"] else hud.SESSION)
        elif whisper_model is None:
            hud.set_state(hud.ASLEEP)
        else:
            hud.set_state(hud.ARMED)

    def _reopen_input_stream(reason: str = "no callbacks arriving"):
        """
        Recovery for a stream that's gone silent while it's supposed to
        be running -- the input-side counterpart to tts.py's output
        stream recovery. last_chunk is bumped up front, before the
        reopen is even attempted, so a repeat failure (device still
        genuinely gone) waits out a full INPUT_WATCHDOG_SECONDS before
        the watchdog tries again instead of hammering it every 0.5s.

        `reason` exists because this has two callers that mean different
        things, and saying "looks stuck" for both was actively
        misleading: the 5s watchdog fires when no callbacks arrive at
        all (genuinely stuck), while the silence latch fires on a stream
        that is running perfectly and delivering a frozen value. The
        latch already prints its own fuller explanation, so this line
        stays short and just names which of the two it was.
        """
        nonlocal stream
        state["main_last_chunk"] = time.time()
        hud.line(f"Reopening the main listening stream ({reason}).", "warn")
        with _stream_lock:
            try:
                if stream is not None:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
            stream = None
            if not state["enabled"]:
                # A pause landed while the old stream was being torn down.
                # Fail closed -- the project's standing rule for anything
                # security-adjacent -- and leave the mic shut rather than hand
                # a paused Jarvis a live capture stream back. _set_enabled(True)
                # opens a fresh one on resume.
                return
            stream = _create_stream()
        # The audio either side of this gap is discontinuous. openWakeWord
        # builds confidence over a rolling window and Silero carries
        # recurrent hidden state, so both would otherwise carry a splice
        # across the reopen -- and a splice reads as neither silence nor
        # speech to either of them.
        vad.reset()
        wake_model.reset()
        # main_signal_at is deliberately NOT stamped here, unlike
        # main_last_chunk above. It answers "when did this device last
        # carry audio", which a reopen does not change -- and stamping it
        # made the silence latch clear itself 0.2s after every time it
        # fired (confirmed live against a genuinely dead mic: latch,
        # reopen, instant unlatch, relatch 120s later, forever). Only a
        # frame above INPUT_SILENCE_FLOOR clears it, which is the only
        # evidence that actually means anything here.
        _drain_audio_queue()

    def _check_input_signal() -> None:
        """
        The other half of input health, and the half the 5s watchdog
        structurally cannot see.

        INPUT_WATCHDOG_SECONDS fires from listener_loop's `queue.Empty`
        branch -- it only ever runs when the stream has stopped feeding
        the queue. A capture device that is still running but handing
        over pure digital silence never empties that queue, so the
        watchdog never gets a turn, and every other liveness signal in
        the process (main_last_chunk, the listener heartbeat, the HUD's
        own level meter) keeps reading healthy. This is called from the
        chunk-consuming path instead, which is the only path that
        actually sees what the frames contain.

        Latched rather than repeated: one log line and one reopen attempt
        per silent spell, cleared the moment real audio comes back. See
        INPUT_SILENCE_SECONDS for why reopening is only a first guess.
        """
        if not state["enabled"]:
            # Paused means the mic is deliberately shut. Silence is the
            # correct outcome, not a fault to report.
            return
        silent_for = time.time() - state["main_signal_at"]
        if silent_for <= INPUT_SILENCE_SECONDS:
            if state["input_silent"]:
                state["input_silent"] = False
                _log_event("input_signal_returned", silent_seconds=round(silent_for, 1))
                hud.line("Mic signal is back.", "ok")
            return
        if state["input_silent"]:
            return  # already latched, already reported, already reopened once

        lo, hi = state["quiet_peak_lo"], state["quiet_peak_hi"]
        if INPUT_SILENCE_CONSTANT_ONLY and (lo is None or hi is None or hi != lo):
            # Quiet, but still dithering -- a live converter in a quiet
            # room, not a frozen one. This is the common case by far (97%
            # of frames in an ordinary quiet room sit under the floor),
            # and treating it as a fault is what made this latch fire
            # every two minutes and reopen a healthy stream each time.
            return

        state["input_silent"] = True
        _log_event(
            "input_silent",
            seconds=round(silent_for, 1),
            floor=INPUT_SILENCE_FLOOR,
            frozen_at=lo,
        )
        hud.line(
            f"No mic signal for {silent_for:.0f}s -- the capture device is repeating one "
            "constant value, which means it is wedged, not merely quiet. Reopening once.",
            "warn",
        )
        _reopen_input_stream(reason="frozen capture device")

    def _check_output_gate() -> bool:
        """
        Returns True while the system's default audio output is
        playing -- see output_monitor.py. Named and extracted the same
        way _check_input_signal is, for the same reason: this is state
        that needs to persist across calls (state["output_was_playing"],
        the falling-edge drain below) and be testable on its own,
        against the real listener_loop closure, without having to run
        the whole loop.

        Not folded into the combined hotkey/enabled/is_speaking skip
        condition at its call site -- that combined condition only ever
        needs the current value, but this needs to notice the specific
        transition back to "not playing" to drain the backlog it caused,
        which none of those other three gates require (they only ever
        block for seconds; see the drain's own comment for why that
        distinction matters here).
        """
        output_playing = output_monitor.is_output_playing()
        if output_playing and not state["output_was_playing"]:
            state["output_was_playing"] = True
            state["output_playing_since"] = time.time()
            # Structured, not just HUD -- same reasoning as
            # wake_triggered/input_silent: this is exactly the fact
            # someone reconstructing "why didn't hey jarvis work just
            # now" needs, and the HUD line alone leaves no durable trace.
            _log_event("output_playing_started")
            hud.line("System audio playing -- \"hey jarvis\" is paused; Right Ctrl still works.", "dim")
        elif not output_playing and state["output_was_playing"]:
            state["output_was_playing"] = False
            _log_event(
                "output_playing_stopped",
                seconds=round(time.time() - state["output_playing_since"], 1),
            )
            # Unlike is_speaking() (seconds), this gate can hold for the
            # length of a song or a playlist -- draining here stops
            # audio_q from banking minutes of stale frames that would
            # otherwise all get burst-processed the instant output
            # stops, at replay speed, as if freshly captured.
            _drain_audio_queue()
            hud.line("System audio stopped -- listening again.", "dim")
        return output_playing

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

    def _start_pause_unload() -> None:
        """
        Drop Whisper and the matcher after a pause, on a background
        thread.

        Off the control thread because the unload needs _processing_lock,
        which an in-flight LLM fallback can hold for up to 30s -- and
        blocking `jarvis pause` behind that blew jarvisctl.py's own 3s
        socket timeout, so a pause that had already taken effect reported
        "Can't reach jarvis -- is it running?" instead. Nothing the user
        can observe waits on this: the mic is shut and the state machine
        reset synchronously in _set_enabled before this is even started.
        """

        def _run():
            try:
                with _processing_lock:
                    # Re-checked under the lock: the wait for it can be
                    # seconds long, and a resume may have landed since.
                    if state["enabled"]:
                        return
                    _unload_heavy_models()
            except Exception:
                _log_event("pause_unload_error", traceback=traceback.format_exc())

        threading.Thread(target=_run, name="pause-unload", daemon=True).start()

    def _start_resume_reload() -> None:
        """
        Quietly bring Whisper and the matcher back after a resume, on a
        background thread.

        Pausing unloads them (see _set_enabled's disable branch) and
        nothing used to load them again -- so every resume landed in what
        is functionally deep sleep, however brief the pause was.
        `jarvis status` answered "Wake-word armed (deep sleep)" seconds
        after a deliberate resume, the HUD said ARMED at the same moment,
        and the next command paid the full reload and announced "Back at
        full power, sir" -- a line that is supposed to mean "you left me
        alone for half an hour". The documented state diagram has Paused
        returning to WakeArmed *awake*; this restores that rather than
        leaving it to be tripped over.

        Silent on purpose: _ensure_heavy_models_loaded's earcon and spoken
        cue exist to fill a wait the user didn't ask for, and here they
        would stack straight on top of "Back online, sir" for no benefit.
        Off the control thread so `jarvis resume` still answers at once.
        """

        def _run():
            try:
                with _processing_lock:
                    # Both re-checked under the lock rather than before
                    # it: the wait can be seconds long, and either a fresh
                    # pause or a real utterance may have landed meanwhile.
                    if not state["enabled"] or whisper_model is not None:
                        return
                    _load_heavy_models()
                _restore_steady_state()
                _log_event("resume_reload")
            except Exception:
                # Daemon thread with nothing to catch it, and the failure
                # is recoverable anyway -- _ensure_heavy_models_loaded
                # still loads on the next utterance, just audibly.
                _log_event("resume_reload_error", traceback=traceback.format_exc())

        threading.Thread(target=_run, name="resume-reload", daemon=True).start()

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
        # Deliberately lock-free, and deliberately in this order.
        #
        # "enabled" is written FIRST, before "mode". That ordering is what
        # actually closes the race this used to lose: the listener writes
        # state["mode"] at the end of a session utterance, and it now
        # makes that write under _processing_lock *and* conditional on
        # state["enabled"] (see listener_loop). Whichever of the two
        # threads goes second, both agree on "wake" -- so the reset can
        # no longer be silently undone, which is what left a paused Jarvis
        # on mode "session_listening" and had the next `jarvis resume`
        # come back into an open conversation nobody opened.
        #
        # Taking _processing_lock across this whole function would also
        # close it, but at a price worth refusing: an in-flight LLM
        # fallback holds that lock for up to 30s, and `jarvis pause` is
        # the privacy control -- the mic has to shut when asked, not once
        # the model finishes thinking.
        state["enabled"] = enabled
        if enabled:
            state["mode"] = "wake"
            state["dictating"] = False
            state["last_activity"] = time.time()
            # Stamped *before* the stream is created, matching
            # _reopen_input_stream's own discipline -- otherwise the 5s
            # input watchdog compares against a timestamp from before the
            # pause and tears down the brand-new stream on its first idle
            # tick, reintroducing exactly the Windows cold-open glitch
            # that keeping one stream open exists to avoid.
            state["main_last_chunk"] = time.time()
            # Same discipline, same reason, one line further: a pause
            # longer than INPUT_SILENCE_SECONDS is silent by definition
            # (the stream was closed for it), so without this the
            # silence check would latch "no mic signal" the instant the
            # listener drained its first chunk after any real pause.
            state["main_signal_at"] = time.time()
            state["input_silent"] = False
            state["signal_run"] = 0
            state["quiet_peak_lo"] = None
            state["quiet_peak_hi"] = None
            # Same reason as the three above: a pause that spanned a
            # stretch of music would otherwise resume still believing
            # output was playing, and fire a spurious drain plus
            # "System audio stopped" on the first chunk after resume.
            # The gate's actual reading is always live
            # (output_monitor polls independently); only this
            # edge-tracking bookkeeping can go stale.
            state["output_was_playing"] = False
            # _stream_lock, not _processing_lock -- see its own comment.
            # This is the one place resume can race the listener
            # thread's own watchdog reopen (both see `stream is None`
            # and could otherwise both open a fresh capture stream).
            with _stream_lock:
                if stream is None:
                    stream = _create_stream()
            _drain_audio_queue()
            # Both acoustic models carry state across the pause gap --
            # openWakeWord a rolling confidence window, Silero its
            # recurrent hidden state -- and the audio either side of that
            # gap is discontinuous. Every *other* transition into "wake"
            # already resets them. Flagged rather than reset here: both
            # objects are driven by the listener thread, and resetting
            # them from this one would race a speech_prob()/predict()
            # call mid-flight.
            state["reset_acoustic"] = True
            # Not a hardcoded ARMED: the models are still unloaded at this
            # point, and _restore_steady_state is the helper that already
            # knows how to say so. _start_resume_reload then brings them
            # back and repaints.
            _restore_steady_state()
            speak(phrases.pick("resume"))
            _start_resume_reload()
            _start_llm_prewarm("resume")  # safety net -- see _ensure_heavy_models_loaded's identical call
        else:
            hud.set_state(hud.OFFLINE)
            # The privacy-relevant half, and the only part that has to be
            # synchronous: the mic is shut before this call returns.
            # _stream_lock only ever guards a few quick PortAudio calls,
            # never the model-loading work _processing_lock protects --
            # taking it here does not reintroduce the "pause waits behind
            # a 30s LLM call" problem this function's own docstring
            # already refused once.
            with _stream_lock:
                if stream is not None:
                    stream.stop()
                    stream.close()
                    stream = None
            # Reset the session state machine too, not just the stream --
            # otherwise pausing mid-conversation and resuming later left
            # the listener stuck in "session_recording" with the models
            # unloaded, crashing on the next transcribe attempt.
            state["mode"] = "wake"
            state["dictating"] = False
            _drain_audio_queue()
            speak(phrases.pick("pause"))
            _start_pause_unload()

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
    # apps, desktop navigation, window actions, and query phrasing.
    #
    # Deliberately NOT a place for repo/project names any more (a "bdk
    # wallet bdk cli Hashstack djed shu programming bitcoin bitshala" tail
    # used to live here, for focus_vscode_repo recognition). Confirmed live
    # as the actual source of false dispatches, not a hypothetical one:
    # llm_usage_log.jsonl showed hotwords fragments coming back as literal
    # transcript from near-silent/ambient audio ("shu programming bitcoin
    # bitshaka" -> focus_or_launch_app, "how many files folder windows bdk
    # wallet bdk wallet" -> count_files_in_folder), each burning a full LLM
    # call and, twice, an actual dispatch (bare "bdk" -> focus_vscode_repo,
    # repo="bdk_wallet"). hotwords biases recognition *toward* these words
    # regardless of what's actually in the audio -- the more obscure and
    # low-frequency a word is, the more that bias reads as a hallucination
    # magnet rather than a helpful nudge. Real repo names are still heard
    # fine without being seeded here (Whisper doesn't need "bdk" primed to
    # transcribe "bdk" correctly); what they bought was a false-positive
    # source on quiet/ambient input, not a needed accuracy fix.
    TRANSCRIBE_HOTWORDS = (
        "Chrome Discord Visual Studio Code VS Code File Explorer Settings Spotify "
        "local disk D desktop virtual desktop next desktop previous desktop "
        "minimize focus switch open close laptop monitor screen "
        "how many files folder windows"
    )

    # Segment-level confidence, gated on separately from the transcript
    # text itself. Originally used *only* to decide whether an
    # LLM-fallback resolution was trustworthy enough to log as a
    # seed-promotion candidate (seed_promotion.py) -- that comment used
    # to say "never to change what gets dispatched", but that stance is
    # reversed as of the pre-LLM transcript gate above
    # (_should_reject_transcript/TRANSCRIPT_GATE_ENFORCED), which reuses
    # this same signal (via the quality dict _transcribe returns) to
    # decide whether to attempt the LLM fallback at all. Two consumers,
    # two separate thresholds now: seed-promotion's LOW_CONFIDENCE_*
    # below (unchanged) and the gate's _REJECT_* constants above (a
    # stricter bar -- a transcript can be too shaky to promote into a
    # permanent seed while still being clear enough to act on once).
    # Whisper can hallucinate literal fragments of TRANSCRIBE_HOTWORDS
    # from near-silence (see that constant's comment) -- confirmed
    # directly in llm_usage_log.jsonl: nine consecutive entries were all
    # hallucinations, each burning a full LLM call, and two near-
    # duplicates were one utterance away from being promoted into a
    # permanent bad fast-path seed before the seed-promotion guard
    # existed. That evidence is what motivated reversing the "never
    # change what gets dispatched" stance above.
    LOW_CONFIDENCE_NO_SPEECH_PROB = 0.6
    LOW_CONFIDENCE_AVG_LOGPROB = -1.0

    # Peak-normalization gain cap -- see _transcribe's comment. Real
    # speech at a sane mic level rarely needs more than 3-4x; a cap of
    # 8x keeps the original benefit (boosting a quieter-but-fully-
    # captured utterance) while no longer amplifying pure room noise
    # (peak ~0.01, i.e. ~100x) up to full scale.
    _MAX_NORMALIZE_GAIN = 8.0

    def _transcribe(audio: np.ndarray) -> "tuple[str, bool, dict]":
        # Peak-normalize before transcribing -- helps quieter (but fully
        # captured) utterances regardless of how the mic/VAD are tuned.
        # Gain is capped (see _MAX_NORMALIZE_GAIN above), not the
        # uncapped 1/peak this used to be: an uncapped gain amplifies
        # *any* buffer to full scale, including several seconds of
        # ambient room noise with a peak near zero -- which is exactly
        # the kind of input that used to trip Whisper's internal
        # temperature-fallback loop (see the `temperature=0.0` note
        # below). Measured directly: uncapped, 3s of white noise
        # transcribed as "how many files folder windows bdk wallet bdk"
        # and took 42+ seconds.
        peak = np.max(np.abs(audio))
        if peak > 1e-4:
            gain = min(1.0 / peak, _MAX_NORMALIZE_GAIN)
            audio = audio * gain
        segments, _ = whisper_model.transcribe(
            audio,
            language="en",
            beam_size=5,
            # Single greedy pass, not faster-whisper's default 6-entry
            # temperature ladder (0.0/0.2/0.4/0.6/0.8/1.0). That ladder
            # exists to recover from degenerate long-form decodes by
            # re-running the *entire* beam_size=5 search at each
            # temperature whenever compression_ratio/log_prob/no_speech
            # thresholds trip -- on noisy short command audio this is
            # almost pure cost: condition_on_previous_text=False (below)
            # already removes the main driver of those degenerate
            # decodes. Measured directly: the same 3s white-noise clip
            # that took 42+ seconds under the old default ladder took
            # ~2.5s here, with clean speech unaffected either way (it
            # never enters the fallback loop in the first place).
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            hotwords=TRANSCRIBE_HOTWORDS,
            # Nothing downstream reads segment start/end times -- timestamp
            # tokens were pure decode overhead for this project's use.
            without_timestamps=True,
        )
        segments = list(segments)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        confident = bool(segments) and all(
            seg.no_speech_prob < LOW_CONFIDENCE_NO_SPEECH_PROB and seg.avg_logprob > LOW_CONFIDENCE_AVG_LOGPROB
            for seg in segments
        )
        # Worst-case (most failure-like) value across segments in each
        # case -- one bad segment in a multi-segment utterance is enough
        # to make the whole transcript suspect. Consumed by
        # _should_reject_transcript (the pre-LLM gate) and logged
        # unconditionally below so real thresholds can be tuned from
        # actual usage rather than guessed.
        quality = {
            "no_speech_prob": max((seg.no_speech_prob for seg in segments), default=1.0),
            "avg_logprob": min((seg.avg_logprob for seg in segments), default=-999.0),
            "compression_ratio": max((seg.compression_ratio for seg in segments), default=0.0),
            "max_temperature": max((seg.temperature for seg in segments), default=0.0),
            "n_segments": len(segments),
            "peak": float(peak),
        }
        return text, confident, quality

    def _handle_transcript(text: str, confident: bool, quality: dict, via_session: bool) -> bool:
        """
        Thin wrapper whose only job is to guarantee the open-ended
        "thinking" colour is released no matter which of the dozen-odd
        return paths below fires. Held amber after an early return would
        strand the HUD (and the room lights behind it) in a state that
        never clears.
        """
        try:
            return _handle_transcript_inner(text, confident, quality, via_session)
        finally:
            hud.end_flash()

    def _handle_transcript_inner(text: str, confident: bool, quality: dict, via_session: bool) -> bool:
        """Returns True if this call should close an open session."""
        state["last_activity"] = time.time()

        if not text:
            hud.line("Nothing intelligible there.", "dim")
            _record_stat("empty", text)
            return False

        # Clipping check, before anything tries to interpret this text.
        # A raw peak at (or effectively at) full scale means the capture
        # path saturated -- the waveform is flat-topped, and both the
        # wake-word model and Whisper are being handed a distorted
        # signal. Worth saying out loud because no amount of software
        # tuning fixes it: the only real remedy is the Windows input
        # level / "Microphone Boost" on the device itself (see
        # DECISIONS.md's transcription-accuracy note, which anticipated
        # the opposite problem -- a level set too *low*). Surfaced as a
        # HUD note plus a structured field rather than a warning line:
        # one clipped utterance is unremarkable, a run of them is the
        # finding, and that pattern is only visible in the log.
        if quality.get("peak", 0.0) >= _CLIPPING_PEAK:
            _log_event("input_clipped", text=text, peak=quality.get("peak", 0.0))
            hud.note(f"input clipped (peak {quality.get('peak', 0.0):.3f}) -- check the Windows mic level/boost")

        # Logged on *every* non-empty utterance, not just rejected ones --
        # this is the dataset TRANSCRIPT_GATE_ENFORCED's threshold gets
        # tuned from (see that constant's comment): reading back which
        # no_speech_prob/avg_logprob values corresponded to a real
        # fast_path/llm_action outcome versus obvious noise, from actual
        # usage rather than a guess.
        _log_event("transcript", text=text, confident=confident, **quality)

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

        # A bare wake-word echo landing here means "hey jarvis" was said
        # again while a session from an earlier trigger was still open --
        # confirmed as a real, repeated pattern in session.jsonl (the wake
        # word arriving as an ordinary mid-session transcript -- "hey
        # jarvis", near-misses like "hey jobters"/"hey job is" -- rather
        # than as a fresh wake_triggered event), not a hypothetical one.
        # Previously this fell straight through to fast-path/LLM matching
        # like any other command: no seed matches a bare "hey jarvis", so
        # it burned a full ~3-30s LLM fallback call and came back as the
        # generic "Hello, sir. How may I assist you today?" -- which reads
        # as Jarvis having missed the wake word entirely (prompting the
        # user to repeat it) rather than already being open and listening.
        # via_session-only: said via push-to-talk it's just an ordinary
        # (if odd) utterance, not evidence of anything stuck.
        # session_silence_run can't be reset from here -- that counter is
        # local to listener_loop, same reasoning as state["renew_session"]
        # elsewhere -- so refreshing last_activity (already done above) and
        # acknowledging immediately, the same way already_listening does
        # for a redundant "wake up" mid-conversation, is the fix available
        # from inside this function.
        if via_session and _normalize_transcript(text) in _WAKE_ECHO_PHRASES:
            hud.resolve("already listening")
            speak(phrases.pick("already_listening"))
            _log_event("wake_echo_in_session", text=text)
            _record_stat("meta", text, "wake_echo")
            return False

        _t0 = time.perf_counter()
        result = matcher.match(text)
        _record_timing("match", time.perf_counter() - _t0)

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
            if state["enabled"]:
                # Nothing to resume. disable_listening above is already
                # context-sensitive; this is the matching half, and it
                # matters because "wake up" and "start listening" are
                # ordinary things to say mid-conversation -- both are
                # enable_listening seeds, so they used to fire a full
                # resume: a spoken "Back online, sir." and the HUD (and
                # the room lights behind it) slammed to ARMED while the
                # conversation was still open.
                hud.resolve("already listening")
                speak(phrases.pick("already_listening"))
                _record_stat("meta", text, "resume_noop")
                return False
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
        llm_cold = False
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

        # Interrogative guard, fast-path half -- discard a fast-path
        # match that looks like a question rather than a command,
        # so the utterance gets a real shot at the LLM fallback (which
        # has full context and a real grammar, not a bare cosine
        # score, and can often resolve it correctly -- see
        # _INTERROGATIVE_OPENERS' comment for the "how many chrome
        # windows" example) instead of silently dispatching the wrong
        # side-effecting action. Tracked separately from result.intent
        # so the later "matched X but nothing is wired up for it"
        # message (meant for a real plumbing gap) doesn't fire here by
        # mistake -- this is a deliberate override, not a missing action.
        _interrogative_override = parsed is not None and _is_dangerous_interrogative_match(parsed["action"], text)
        if _interrogative_override:
            parsed = None

        if parsed is None and (result.intent is None or _interrogative_override):
            # Pre-LLM garbage gate -- see TRANSCRIPT_GATE_ENFORCED's
            # comment for the full rationale and why it only applies to
            # this branch. Logged either way (as "transcript_rejected"
            # once actually enforced, "transcript_would_reject" while
            # only observing) so the threshold can be validated against
            # real usage before it's allowed to change behaviour.
            reject_reason = _should_reject_transcript(text, quality)
            if reject_reason is not None:
                _log_event(
                    "transcript_rejected" if TRANSCRIPT_GATE_ENFORCED else "transcript_would_reject",
                    text=text,
                    reason=reject_reason,
                    enforced=TRANSCRIPT_GATE_ENFORCED,
                    **quality,
                )
                if TRANSCRIPT_GATE_ENFORCED:
                    hud.resolve("didn't catch that " + DOT + " ignored", "dim")
                    hud.note(
                        f"rejected: {reject_reason} {DOT} no_speech={quality['no_speech_prob']:.2f} "
                        f"logprob={quality['avg_logprob']:.2f} cr={quality['compression_ratio']:.2f}"
                    )
                    if not via_session:
                        earcons.play("decline")
                    _record_stat("rejected", text, reject_reason)
                    return False

            # Fast embedding match couldn't confidently place this --
            # fall back to the local LLM before giving up. Only reached
            # for phrasing the seed list doesn't cover. llm_intent.prewarm
            # (kicked off in the background at boot/wake/resume -- see
            # _start_llm_prewarm) means this is usually already warm by
            # the time real use gets here; llm_cold below records whether
            # this particular call actually paid the cold prefill cost,
            # for the "resolved" log below. Spoken cue fired *before* the
            # (possibly long) call, not after -- confirmed directly this
            # was needed: with nothing said or printed between "Heard:
            # ..." above and the eventual reply, a slow fallback read as
            # Jarvis having stopped responding rather than still
            # thinking. state["last_app"] (one turn of pronoun memory)
            # lets "minimize it" resolve without naming the app again --
            # see llm_intent.infer's context_app. screen_context (see
            # _looks_deictic's comment for the cost/benefit) additionally
            # lets "minimize this"/"what's this page about" work without
            # state["last_app"] having been set at all.
            hud.begin_flash(hud.THINKING)
            hud.pending("consulting local model", spinner=True)
            speak(phrases.pick("thinking"))
            llm_cold = llm_intent.call_count() == 0
            screen_context = llm_screen_context() if _looks_deictic(text) else None
            _t0 = time.perf_counter()
            parsed = llm_intent.infer(text, context_app=state["last_app"], screen_context=screen_context)
            llm_seconds = time.perf_counter() - _t0
            _record_timing("llm", llm_seconds)
            via_llm = True

            if parsed is not None and parsed["action"] == "no_match":
                # The LLM's own abstain (llm_intent.py's "no_match" rule)
                # -- it decided this doesn't read like a real command
                # rather than being forced to guess one. Treated the same
                # as the pre-LLM gate's rejection above: no speech (an
                # abstain must never sound like a reply), a quiet HUD
                # note, never logged as a seed-promotion candidate.
                hud.resolve(f"didn't catch that {DOT} local llm declined  {llm_seconds:.1f}s", "dim")
                if not via_session:
                    earcons.play("decline")
                _record_stat("rejected", text, "llm_abstain")
                return False

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

        # "Move this (window) to the next desktop" resolves to
        # switch_desktop:next at high confidence (0.94 -- confirmed
        # directly) -- there's no action yet that moves a window across
        # desktops, so without this, saying "move" switches which
        # desktop *you're* viewing and silently leaves the window right
        # where it was. Checked against the resolved action, same as
        # the number guard above, so it catches the LLM path too.
        if parsed is not None and parsed["action"] == "switch_desktop" and _looks_like_a_move_request(text):
            hud.resolve("declined " + DOT + " move not supported", "warn")
            earcons.play("decline")
            speak(phrases.pick("move_window_declined"))
            _record_stat("declined", text, "move_not_supported")
            return False

        # Interrogative guard, final half -- catches whatever the LLM
        # fallback itself resolved to (via_llm's own grammar/rules
        # usually get this right, but "never trust a single layer" is
        # the project's own standing principle, and this is a cheap
        # symmetric check) and the case where the fast-path override
        # above fired but result.intent was already None (so the "was
        # this dangerous" question still needs asking once more against
        # whatever the LLM produced).
        if parsed is not None and _is_dangerous_interrogative_match(parsed["action"], text):
            hud.resolve("declined " + DOT + " sounded like a question", "warn")
            earcons.play("decline")
            speak(phrases.pick("declined_question"))
            _record_stat("declined", text, "interrogative")
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
            llm_cold=llm_cold if via_llm else None,
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
        _t0 = time.perf_counter()
        message = dispatch_llm_action(parsed, text)
        _record_timing("action", time.perf_counter() - _t0)
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
            _log_event("hotkey_no_speech", reason="no_frames", peak=0.0)
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
            # Logged with the peak, because this bail is indistinguishable
            # from the outside between "held the key and said nothing" and
            # "the capture device is dead" -- and it used to go to the HUD
            # only, so push-to-talk against a silent mic produced no
            # spoken reply, no log line, and no way to tell the two apart
            # afterwards. A peak at the noise floor says which it was.
            _log_event(
                "hotkey_no_speech",
                reason="vad_gate",
                peak=round(max(float(np.abs(f).max()) for f in frames), 6),
                frames=len(frames),
            )
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
                    _t0 = time.perf_counter()
                    text, confident, quality = _transcribe(audio)
                    _record_timing("transcribe", time.perf_counter() - _t0)
                    _handle_transcript(text, confident, quality, via_session=False)
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
                # Using the hotkey is using Jarvis: renew the open
                # conversation's idle countdown rather than letting it run
                # out mid-use. The counter itself is local to
                # listener_loop, which owns it and consumes this flag.
                state["renew_session"] = True

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
            # Liveness for `jarvis status`, stamped before the blocking
            # get so a thread simply parked on an empty queue still reads
            # as alive -- that is a quiet room, not a dead listener. A
            # genuinely long block (a cold LLM fallback) lets this go
            # stale, which is why LISTENER_STALL_SECONDS clears it.
            state["listener_beat"] = time.time()
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
                # Evaluated on this path too, not just the chunk path
                # below. What the speakers are doing has nothing to do
                # with whether the *mic* is delivering anything, and this
                # branch is reached in exactly the cases where that
                # distinction bites: a wedged capture device (no chunks
                # at all -- the failure this codebase has now hit twice),
                # and a paused Jarvis (main stream deliberately closed).
                # Left only on the chunk path, the gate silently froze at
                # whatever it last saw -- confirmed live, a tone played
                # while the queue was empty produced no gate transition
                # at all.
                try:
                    _check_output_gate()
                except Exception:
                    _log_event("output_gate_check_failed", traceback=traceback.format_exc())
                    hud.note(traceback.format_exc())
                continue

            # Evaluated here, on the path that actually sees frame
            # contents, and before the guards below -- a stuck
            # hotkey_active or is_speaking() would otherwise `continue`
            # past it forever, which is exactly the kind of silent
            # deafness this exists to name. Own try/except for the same
            # reason the watchdog has one: a failed reopen must not take
            # the listener thread down with it.
            try:
                _check_input_signal()
            except Exception:
                _log_event("input_signal_check_failed", traceback=traceback.format_exc())
                hud.note(traceback.format_exc())

            # Deep-sleep idle-unload, checked here rather than down in
            # the "wake" mode dispatch below (where it used to live)
            # specifically so the output gate just below cannot block it
            # indefinitely. The gates above/below it (hotkey held,
            # paused, is_speaking()) only ever last seconds, so leaving
            # this nested inside "wake" dispatch never mattered before --
            # system output playing continuously well past the 30-minute
            # idle mark is a completely ordinary case (a long playlist),
            # and would otherwise pin the heavy models in memory for as
            # long as the music kept playing.
            #
            # Contained, like every other step between the queue read and
            # the main try below. Everything up here runs OUTSIDE that
            # block's crash containment, so an exception in any of it
            # kills the listener thread outright -- which is exactly the
            # failure that block's own comment says must never happen
            # again, and exactly what happened when the output gate was
            # first added here uncontained (thread dead ~5s after boot,
            # `jarvis status` still cheerfully reporting "Wake-word
            # armed" while nothing was listening at all).
            try:
                if (
                    state["mode"] == "wake"
                    and whisper_model is not None
                    and time.time() - state["last_activity"] > DEEP_SLEEP_IDLE_SECONDS
                ):
                    # Same lock the push-to-talk/session transcription
                    # paths hold while actually using the models --
                    # without it, this could unload out from under an
                    # in-flight transcribe() on another thread and crash
                    # it with a null model reference.
                    with _processing_lock:
                        _unload_heavy_models()
                    hud.set_state(hud.ASLEEP)
                    earcons.play("sleep")
                    speak(phrases.pick("sleep"))
                    _log_event("deep_sleep")
            except Exception:
                _log_event("deep_sleep_failed", traceback=traceback.format_exc())
                hud.note(traceback.format_exc())

            # System-output gate: see output_monitor.py and
            # _check_output_gate. Spotify, a video, anything rendering
            # through the speakers -- not just Jarvis's own voice
            # (is_speaking(), immediately below). Confirmed live as a
            # real failure mode, not a theoretical one: a session opened
            # with no "hey jarvis" anywhere in the transcript (the wake
            # word almost certainly false-triggering on loud music), and
            # several chunks of the song that followed embedded close
            # enough to real seed phrases to actually dispatch
            # (volume_down/up), peak=1.0 on nearly every one.
            #
            # Contained for the same reason as the block above: this runs
            # outside the main try, and an exception in it killed the
            # listener thread silently the first time round. Failing to
            # "not playing" on error keeps the listener working rather
            # than leaving it permanently gated by a gate that threw.
            try:
                output_playing = _check_output_gate()
            except Exception:
                output_playing = False
                _log_event("output_gate_check_failed", traceback=traceback.format_exc())
                hud.note(traceback.format_exc())

            # Push-to-talk has its own stream now -- just don't let the
            # main listener also react to the same speech while it's held.
            # Also mute while Jarvis is talking (is_speaking(), tts.py) --
            # otherwise the session listener, always-on unlike push-to-talk,
            # hears its own spoken replies and acts on them as new commands
            # (confirmed directly: "Switching to the next desktop, sir" kept
            # re-triggering itself, causing runaway desktop switches).
            #
            # Deliberately NOT applied to push-to-talk (hotkey_active is
            # checked, output_playing is not, on that path -- see
            # on_press/on_release below): holding Right Ctrl and talking
            # over the music is exactly the override this project's own
            # convention keeps working no matter what else is true.
            _speaking_now = is_speaking()
            if state["hotkey_active"] or not state["enabled"] or _speaking_now or output_playing:
                # Deaf-listener diagnosis -- see
                # LISTENER_GATE_STUCK_SECONDS. Records *which* of the
                # four gates is holding, once per continuous stretch,
                # so "nothing happened when I said hey jarvis" stops
                # being indistinguishable from "the wake word scored
                # 0.45". Structured only (plus a debug-only aside): a
                # long playlist legitimately trips this, and that is not
                # worth a warning line in the scrolling region.
                _now = time.time()
                if state["gate_blocked_since"] is None:
                    state["gate_blocked_since"] = _now
                elif (
                    not state["gate_blocked_logged"]
                    and _now - state["gate_blocked_since"] > LISTENER_GATE_STUCK_SECONDS
                ):
                    state["gate_blocked_logged"] = True
                    _log_event(
                        "listener_gate_stuck",
                        seconds=round(_now - state["gate_blocked_since"], 1),
                        hotkey_active=bool(state["hotkey_active"]),
                        paused=not state["enabled"],
                        speaking=bool(_speaking_now),
                        output_playing=bool(output_playing),
                    )
                    hud.note(
                        f"wake-word detection gated for "
                        f"{_now - state['gate_blocked_since']:.0f}s "
                        f"(hotkey={state['hotkey_active']} paused={not state['enabled']} "
                        f"speaking={_speaking_now} output={output_playing})"
                    )
                continue
            if state["gate_blocked_since"] is not None:
                state["gate_blocked_since"] = None
                state["gate_blocked_logged"] = False

            # Push-to-talk renews the conversation's idle countdown.
            # session_silence_run is a frame counter local to this loop,
            # so the hotkey worker cannot reset it directly -- it raises a
            # flag and this consumes it. Without this, a push-to-talk
            # command inside an open conversation bought no extra time at
            # all: the conversation still closed four minutes after the
            # last thing said *to the session*, however recently the
            # hotkey had been used.
            if state["renew_session"]:
                state["renew_session"] = False
                session_silence_run = 0

            # Raised by _set_enabled on resume. Consumed here rather than
            # done there because both objects belong to this thread --
            # resetting them from the control thread would race a
            # speech_prob()/predict() call already in flight.
            if state["reset_acoustic"]:
                state["reset_acoustic"] = False
                vad.reset()
                wake_model.reset()
                speech_onset_run = 0
                session_silence_run = 0
                pre_buffer.clear()

            try:
                if state["mode"] == "wake":
                    # Deep-sleep idle-unload now lives above, outside this
                    # dispatch block -- see its own comment at the check
                    # site for why (the output gate above must not be
                    # able to block it for the length of a whole playlist).
                    # np.clip is NOT optional here, and this is the only
                    # place in the pipeline that converts to int16 at all.
                    #
                    # float32 capture can come back slightly outside
                    # [-1, 1] -- WASAPI shared mode with an APO doing
                    # gain/AGC ahead of us (this machine's mic runs Intel
                    # SST, see CLAUDE.md) overshoots unity on loud input.
                    # astype(np.int16) does NOT saturate, it wraps: a
                    # sample of 1.05 becomes -31131, i.e. a near-full-
                    # scale sample of the *opposite polarity*, injected
                    # into exactly the loudest part of the waveform --
                    # which for "hey jarvis" is the part carrying the
                    # signal openWakeWord is looking for. Measured
                    # directly: [1.0, 1.05, 1.2, -1.3] * 32767 ->
                    # [32767, -31131, -26216, 22939].
                    #
                    # Consistent with what this machine's logs show --
                    # every recent transcript records a raw peak of 1.00,
                    # i.e. the capture path is running at saturation --
                    # and with the shape of the reported bug: the wake
                    # word intermittently not firing while push-to-talk
                    # kept working throughout. Push-to-talk never touches
                    # this line; it hands float audio straight to Whisper.
                    # A single distorting conversion on the wake path
                    # alone is exactly the asymmetry that produces.
                    chunk_i16 = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16)
                    scores = wake_model.predict(chunk_i16)
                    # Near-miss bookkeeping before the threshold test, so
                    # a score that never fires still leaves a trace -- see
                    # WAKE_NEAR_MISS_FLOOR for why this exists at all.
                    # Two floats and a comparison per frame; the window
                    # flush below is what actually writes anything.
                    _wake_score = scores.get(WAKE_WORD, 0.0)
                    if _wake_score > state["wake_best_score"]:
                        state["wake_best_score"] = _wake_score
                    if time.time() - state["wake_window_started"] >= WAKE_NEAR_MISS_WINDOW_SECONDS:
                        if WAKE_NEAR_MISS_FLOOR <= state["wake_best_score"] <= WAKE_THRESHOLD:
                            _log_event(
                                "wake_near_miss",
                                best_score=round(float(state["wake_best_score"]), 4),
                                threshold=WAKE_THRESHOLD,
                                window_seconds=WAKE_NEAR_MISS_WINDOW_SECONDS,
                            )
                        state["wake_best_score"] = 0.0
                        state["wake_window_started"] = time.time()
                    if _wake_score > WAKE_THRESHOLD:
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
                        # The only place a session ever opens, and until
                        # now the only state transition in the whole
                        # listener loop with zero trace in session.jsonl.
                        # Diagnosed the hard way: a burst of dispatched
                        # fast-path commands appeared ~10s after a logged
                        # "power down" (session_close) with no wake-word
                        # transcript in between and peak=1.0 on nearly
                        # every one of them -- consistent with the wake
                        # word false-triggering on loud Spotify playback
                        # and reopening a session nobody asked for, but
                        # unconfirmable from the log as it stood, since
                        # this line simply didn't exist. The score is the
                        # point: WAKE_THRESHOLD (0.5) was tuned without
                        # ever having a record of what real triggers
                        # (wanted or not) actually scored.
                        _log_event("wake_triggered", score=round(float(_wake_score), 4))
                        state["wake_best_score"] = 0.0
                        state["wake_window_started"] = time.time()
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
                            # Spoken, not just chimed. This is a documented
                            # state-diagram edge, and the guide promises
                            # every state change is said out loud -- it was
                            # the only close that happened in silence, which
                            # made the difference between "it closed" and
                            # "it stopped hearing me" invisible.
                            speak(phrases.pick("session_timeout"))
                            # Structured, not just HUD/spoken -- this was
                            # the one session-close path with genuinely
                            # zero trace in session.jsonl (every other one
                            # goes through _record_stat's "meta"/
                            # session_close outcome). Confirmed as a real
                            # diagnostic gap, not a hypothetical one: with
                            # no session_open/session_close pair to
                            # correlate against, "how long was a session
                            # left open, swallowing wake-word attempts,
                            # before this timeout finally closed it" was
                            # unanswerable from the log alone.
                            _log_event("session_idle_closed", seconds=SESSION_IDLE_TIMEOUT_FRAMES * FRAME_SECONDS)
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
                            _t0 = time.perf_counter()
                            text, confident, quality = _transcribe(audio)
                            _record_timing("transcribe", time.perf_counter() - _t0)
                            vad.reset()
                            should_close = _handle_transcript(text, confident, quality, via_session=True)
                            # Inside the lock, and failing closed on a
                            # pause that landed while this was busy.
                            # Dedented out of it, this write raced
                            # _set_enabled's own mode reset and silently
                            # undid it: a push-to-talk "stop listening"
                            # (or `jarvis pause`) resolving mid-
                            # transcription left mode on
                            # "session_listening" while paused, and the
                            # next `jarvis resume` came back into an open
                            # conversation nobody opened -- acting on
                            # ambient speech with no wake word said.
                            if should_close or not state["enabled"]:
                                state["mode"] = "wake"
                            else:
                                state["mode"] = "session_listening"
                        _restore_steady_state()
                        # Everything captured while the lock was held is
                        # stale by now, and on the paths that end without
                        # speaking it would otherwise be drained with the
                        # is_speaking() mute already lifted -- letting
                        # Jarvis record and transcribe its own "One
                        # moment, sir" as the next command. See
                        # _drain_audio_queue for the full mechanism.
                        _drain_audio_queue()
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

    # Delayed slightly so the LLM's native-backend import and the
    # subsequent ~13-27s prefill (see llm_intent.prewarm) don't compete
    # for this machine's 4 physical cores against the wake-word/VAD
    # frames the listener thread just started processing, or against
    # Piper still synthesizing the boot announcement above. This does
    # NOT reintroduce the eager-llama_cpp-import cost that was
    # deliberately moved out of startup (see llm_intent._get_llm's
    # comment) -- the import still only happens here, on a background
    # daemon thread, well after the process is already fully armed and
    # listening; time-to-armed is unaffected.
    _start_llm_prewarm("boot", delay=2.0)

    def _shutdown():
        state["stop"] = True
        # _stream_lock: without it, this could run concurrently with the
        # listener thread's own _reopen_input_stream (its watchdog runs
        # every 0.5s regardless of shutdown -- state["stop"] is only
        # checked at the top of that thread's loop, not mid-reopen) and
        # close a stream the other thread is simultaneously calling
        # .start() on.
        with _stream_lock:
            if stream is not None:
                stream.stop()
                stream.close()
        hotkey_stream.stop()
        hotkey_stream.close()
        output_monitor.stop()
        # Both directions, not just input. Every audio handle this
        # process holds is released here, before the (potentially slow)
        # model teardown below -- on the sleep-triggered quit this is a
        # race against Windows suspending the audio stack, and losing it
        # means closing streams onto a device that is already being
        # reinitialised on the far side of the suspend.
        close_tts_output()
        keyboard.unhook_all()
        # Drop the GGUF model here, while the interpreter is still fully
        # alive. Left to be collected at exit instead, llama_cpp's
        # Llama.__del__ runs during interpreter teardown, by which point
        # the module globals its cleanup chain depends on may already be
        # None -- which surfaces as a "TypeError: 'NoneType' object is
        # not callable" traceback printed after the goodbye. Harmless,
        # but it's the last thing on screen and it reads like a crash.
        llm_intent.unload()

    def _announce_and_shutdown(silent: bool = False):
        # Blocking, not fire-and-forget -- the process exits right
        # after this, so a background thread would risk never
        # actually getting to play it.
        hud.set_state(hud.DORMANT)
        if silent:
            # The sleep-triggered quit takes this path, and the whole
            # reason it exists is timing.
            #
            # `Jarvis Quit On Sleep` fires on Kernel-Power 42, i.e. once
            # Windows has *already begun* suspending. The spin-down
            # earcon plus Piper synthesis plus playback below is several
            # seconds of blocking audio work, and scheduler/README.md
            # already noted it runs "against an audio stack Windows is
            # already tearing down". What that cost turned out to be:
            # on 09-17 the process was frozen mid-teardown for a 13-hour
            # suspend and finished it 3 seconds after resume -- 4 seconds
            # after the Intel SST DSP reloaded its firmware -- issuing
            # close calls on suspend-invalidated handles into a driver
            # that was still reinitialising. The capture pipeline came
            # back delivering a constant 1 LSB and stayed that way
            # through a full process restart.
            #
            # Not proof that the goodbye caused it (n=1, and these DSPs
            # wedge on their own), but the one earlier cycle that also
            # survived a suspend finished its teardown 24s after wake,
            # clear of the reload, and came back fine. Skipping the audio
            # turns seconds of work into milliseconds, which is the
            # difference between finishing before the suspend and
            # straddling it. Nobody is listening to a goodbye from a
            # machine that is already going to sleep anyway.
            _log_event("shutdown_silent", reason="sleep")
            _shutdown()
            return
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
            quitting = False
            quit_silently = False
            with conn:
                command = conn.recv(64).decode("utf-8", "ignore").strip().lower()
                if command == "status":
                    # Derived by a module-level pure function so the state
                    # naming is testable without starting the app, and so
                    # every combination it can print is one the process
                    # can actually be in. The LLM suffix is purely
                    # observational -- see llm_intent.prewarm_state's
                    # docstring; the fallback works regardless of it.
                    reply = _status_line(
                        enabled=state["enabled"],
                        mode=state["mode"],
                        whisper_loaded=whisper_model is not None,
                        dictating=state["dictating"],
                        llm_state=llm_intent.prewarm_state(),
                        seconds_since_listener_beat=time.time() - state["listener_beat"],
                        input_silent=state["input_silent"],
                        speaking=is_speaking(),
                    )
                elif command == "stats":
                    reply = _format_stats(state["stats"])
                elif command == "pause":
                    # Logged, unlike before. The voice path records a
                    # pause via _record_stat("meta", ...), but the
                    # control socket recorded nothing at all -- so a
                    # `jarvis pause`, a sleep, and a `jarvis resume` left
                    # session.jsonl with no trace that any of it had
                    # happened, which is precisely the sequence someone
                    # is trying to reconstruct when they come to read it.
                    _set_enabled(False)
                    _log_event("control_pause")
                    reply = "going quiet, sir"
                elif command == "resume":
                    _set_enabled(True)
                    _log_event("control_resume")
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
                elif command in ("quit", "quit silent"):
                    # Announced *after* this connection closes, not
                    # before. jarvisctl.py reads until EOF on a socket
                    # whose 3s timeout governs every recv(), and the
                    # goodbye reliably outlasts that: a blocking spin-down
                    # earcon, then Piper synthesis and playback, then
                    # llm_intent.unload(). Announcing inside the `with
                    # conn:` block held EOF back past the client's
                    # deadline, so a perfectly successful quit printed
                    # "Can't reach jarvis -- is it running?" and exited 1
                    # -- throwing away the reply it had already received,
                    # and reporting failure to the sleep-triggered
                    # scheduled task as well as to anyone at a prompt.
                    reply = "powering down, sir"
                    quitting = True
                    quit_silently = command.endswith("silent")
                else:
                    reply = f"unrecognized command: {command!r}"
                conn.sendall(reply.encode())
            if quitting:
                _announce_and_shutdown(silent=quit_silently)
                break
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
