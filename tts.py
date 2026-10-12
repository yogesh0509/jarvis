"""
Local neural text-to-speech via Piper -- a British English male voice.
This is the best available *free, fully local, reliable* option, not
an attempt to imitate Paul Bettany's performance or anyone else's
specific voice; cloning a real actor's protected performance would be
a different, unacceptable thing to build regardless of technical
feasibility.

VOICE below picks which fetched model to use -- swap it to try a
different one, both already downloaded:
- "alan": the original choice, a clear/formal RP-ish delivery.
- "northern_english_male": tried per a request for something a bit
  friendlier/less formal while keeping a British male "Jarvis" feel --
  currently the default; switch back to "alan" if it's not preferred.

Model files fetched by hand into models/piper-en_GB-<voice>-medium/
(see README's TLS notes -- huggingface.co downloads are unreliable
here via Python's own HTTPS client, PowerShell's is not).

speak() is fire-and-forget (runs on a background thread) so a state
transition line never blocks the listener loop or the control server
while it's being read out.

interrupt() lets push-to-talk (main.py's on_press) cut off whatever's
currently playing -- barge-in, so a long reply doesn't have to be
waited out. See interrupt()'s and _write_audio_interruptible()'s
docstrings for the two real races/gotchas this went through before
landing on its current design (cross-thread stream access, and a
one-chunk-per-sentence Piper quirk that made a naive per-chunk check
nearly useless).
"""

import threading
import time
import traceback
from pathlib import Path

import numpy as np
import sounddevice as sd

import hud
import phrases
from piper.voice import PiperVoice
from piper.config import SynthesisConfig

VOICE = "northern_english_male"  # or "alan"
MODEL_DIR = Path(__file__).parent / "models" / f"piper-en_GB-{VOICE}-medium"
MODEL_FILE = f"en_GB-{VOICE}-medium.onnx"

# length_scale scales phoneme duration -- <1 is faster, 1.0 is the
# voice's natural pace. Went 1/1.5 (50% faster) -> 1/1.25 -> 1/1.1 ->
# now back to 1.0, each step down because the previous one felt too
# rushed, especially once all conversational replies started being
# spoken, not just state transitions -- and 1/1.1 still wasn't enough of
# a step by request. Natural pace also makes the "write two short
# sentences rather than one" phrasing habit (phrases.py) matter more,
# not less: Piper chunks per sentence, so a shorter first sentence still
# gets audio out sooner regardless of how fast each word is spoken.
_SYN_CONFIG = SynthesisConfig(length_scale=1.0)

_voice = None
_playback_lock = threading.Lock()

# Tracks in-flight speak() calls (a count, not a flag) so main.py's
# listener can mute itself for as long as *any* reply is still playing
# or queued -- without this, the session listener (always-on, unlike
# push-to-talk) picks up its own spoken replies as new "speech" and acts
# on them. Confirmed directly: repeated desktop switches from a session
# hearing its own "Switching to the next desktop, sir" as a fresh command.
# A plain flag isn't enough here: two speak() calls close together would
# have the first one's cleanup clear it while the second is still queued
# on _playback_lock, reopening the exact same hole for that second reply.
# Incremented synchronously in speak() itself (not inside the worker
# thread) so there's no window between "speak() was called" and "the
# flag reflects it" for the listener to slip through.
_speaking_count = 0
_speaking_count_lock = threading.Lock()
# Decrements owed to calls that interrupt() already accounted for. Every
# in-flight speak()/play_samples() runs _end_speaking() in a finally, so
# zeroing _speaking_count in interrupt() (as this used to do) left those
# finallys to land on a count that by then belonged to a *newer*
# utterance -- is_speaking() could read False while audio was genuinely
# playing, which is exactly the hole the count exists to close (the
# always-on session listener hearing its own reply back). Bank them here
# and swallow precisely that many later decrements instead.
_pending_stale_ends = 0
_ECHO_TAIL_SECONDS = 0.3

# Barge-in: push-to-talk (main.py's on_press) can cut off whatever's
# currently playing. Each speak() call gets its own monotonic id,
# assigned synchronously in _begin_speaking() (same lock, same "no
# window" reasoning as _speaking_count below) -- interrupt() records a
# *threshold* id to cut short, rather than just flipping a shared
# boolean.
#
# A single shared flag was tried first and had a real race: _speak_now
# used to clear it right before starting its own synthesis loop (to
# wipe any *stale* flag left over from a previous interrupted
# utterance), but that same clear could fire *after* a genuine
# interrupt() call aimed at the utterance currently loading the voice
# model (_get_voice() runs before the lock/loop, so there's a window
# where an utterance is "in flight" but hasn't reached that clear yet)
# -- confirmed directly: an interrupt() issued during that window got
# silently erased, and the "interrupted" reply played out in full.
#
# An exact-match single id was tried next and had a *different* real
# race, this one about several speak() calls queued up close together
# (e.g. testing rapid press/release cycles): each interrupt() call
# only recorded the single latest id, so an older, still-queued call
# (blocked waiting for _playback_lock, not yet even started) would see
# a *later* id had since overwritten the one meant for it, never
# noticed anything was wrong, and played out in full -- confirmed
# directly, a burst of interrupted calls left a growing backlog that
# took many seconds to unwind instead of cutting off immediately.
#
# A threshold fixes both: any utterance id at or below the last
# interrupt() call's snapshot counts as interrupted, whether it's
# mid-playback, still loading the voice, or hasn't even started yet.
_next_utterance_id = 0
_interrupted_up_to_id = 0


def is_speaking() -> bool:
    with _speaking_count_lock:
        return _speaking_count > 0


def _begin_speaking() -> int:
    global _speaking_count, _next_utterance_id
    with _speaking_count_lock:
        _speaking_count += 1
        _next_utterance_id += 1
        return _next_utterance_id


def _end_speaking() -> None:
    global _speaking_count, _pending_stale_ends
    with _speaking_count_lock:
        # Order doesn't matter: interrupt() banked N owed decrements, and
        # exactly N of the next N+M _end_speaking() calls get swallowed,
        # so the M calls issued after the interrupt still net out to zero
        # whichever finally happens to run first.
        if _pending_stale_ends > 0:
            _pending_stale_ends -= 1
            return
        _speaking_count = max(0, _speaking_count - 1)


def interrupt() -> None:
    """
    Signals whatever's currently playing to stop, and clears the
    speaking count right away so is_speaking() drops without waiting
    for _speak_now's own cleanup. Safe to call even when nothing is
    playing (a no-op).

    Deliberately does NOT touch _output_stream itself -- only ever
    bookkeeping here (fast, no audio I/O, safe to call from any
    thread). A first version called stream.abort()/.start() directly
    from here, i.e. from whatever thread calls interrupt() (main.py's
    on_press, on the keyboard hook's thread) -- and that raced with the
    *other* thread that's actually mid-write() on the same stream
    (_speak_now's worker thread, inside _write_audio): confirmed
    directly, a rapid interrupt-while-writing cycle left the stream
    permanently wedged, hanging every subsequent write() forever, not
    just the interrupted one. PortAudio/sounddevice streams aren't
    documented as safe to abort() from a thread other than the one
    driving them. _speak_now below does its own abort()/start() from
    inside its own loop instead -- the one thread that's actually
    calling write() on this stream -- once it notices the id set here.
    """
    global _interrupted_up_to_id, _speaking_count, _pending_stale_ends
    with _speaking_count_lock:
        # Everything issued so far (there's normally at most one in
        # flight, but see _interrupted_up_to_id's comment for why a
        # threshold rather than just "the current one" matters) --
        # never regresses since _next_utterance_id only grows, so a
        # later speak() call's id is always safely above this.
        _interrupted_up_to_id = _next_utterance_id
        # Not a plain `= 0`: see _pending_stale_ends' comment. Each call
        # counted here still owes an _end_speaking() from its own finally.
        _pending_stale_ends += _speaking_count
        _speaking_count = 0


def _get_voice() -> PiperVoice:
    global _voice
    if _voice is None:
        _voice = PiperVoice.load(
            str(MODEL_DIR / MODEL_FILE),
            config_path=str(MODEL_DIR / f"{MODEL_FILE}.json"),
        )
    return _voice


# sd.play() opens and tears down a fresh output device connection every
# call -- on Windows that cold-open reliably clips/glitches the first
# few dozen ms written to it (a driver/DAC warm-up artifact, not
# anything about the synthesized audio), which is why only ever the
# first word of a reply came out unclear. Kept open for the process's
# whole life instead, written to via .write() rather than reopened per
# utterance. Piper's chunks for one voice all share one sample rate, so
# one stream suffices; re-created only if that ever changes (e.g. VOICE
# swapped to a model with a different rate).
_output_stream: "sd.OutputStream | None" = None
_PRIMING_SECONDS = 0.08  # tiny near-silent lead-in, belt-and-suspenders
# against the same warm-up artifact if the device idles/suspends between
# replies even with the stream nominally still open.


def _get_output_stream(samplerate: int) -> sd.OutputStream:
    global _output_stream
    if _output_stream is None or int(_output_stream.samplerate) != samplerate:
        if _output_stream is not None:
            try:
                _output_stream.stop()
                _output_stream.close()
            except Exception:
                pass
        _output_stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32")
        _output_stream.start()
    return _output_stream


def close_output() -> None:
    """
    Release the render device deliberately, rather than leaving it to
    interpreter teardown.

    main.py's _shutdown() closed both *input* streams and left this one
    open -- fine when the process is simply exiting, much less fine on
    the sleep-triggered quit, where the point is to hand every audio
    handle back to Windows *before* it suspends the audio stack rather
    than to still be holding one when the driver reinitialises on the
    other side. Best-effort and idempotent: a device already torn down
    under us throws here, and there is nothing useful left to do about
    it at this point in the shutdown.
    """
    global _output_stream
    if _output_stream is None:
        return
    try:
        _output_stream.stop()
        _output_stream.close()
    except Exception:
        pass
    _output_stream = None


def _write_audio(data: np.ndarray, samplerate: int) -> None:
    """
    Keeping the stream open for the process's life (above) fixed the
    clipped-first-word problem, but introduced a new failure: if Windows
    invalidates the device under an already-open stream (seen directly:
    PortAudioError "no driver installed" mid-session, likely an audio
    device/driver change or reset while the stream sat open), every
    write after that failed the same way for the rest of the process's
    life, since the broken stream object stayed cached. Recover once by
    discarding it and opening a fresh one before giving up.
    """
    global _output_stream
    stream = _get_output_stream(samplerate)
    try:
        stream.write(data)
    except Exception:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        _output_stream = None
        stream = _get_output_stream(samplerate)
        stream.write(data)


_INTERRUPT_CHECK_SECONDS = 0.15  # see _write_audio_interruptible


def _write_audio_interruptible(data: np.ndarray, samplerate: int, utterance_id: int) -> bool:
    """
    Writes `data` in small slices, checking the interrupt threshold
    between each, instead of one blocking write() for the whole thing.
    Returns True if cut short partway through (caller must stop
    immediately -- do not write anything more of this utterance), False
    if it completed normally.

    Needed because Piper doesn't chunk by any small fixed size -- it
    yields whatever it considers one unit, which for an ordinary
    single-sentence reply is often the *entire* sentence as one chunk
    (confirmed directly: a 20-word sentence came back as a single ~5s
    chunk). Checking the interrupt flag only *between* voice.synthesize()'s
    own chunks, as a first version of this did, meant an interrupt
    couldn't take effect until that whole multi-second chunk had
    already been handed to stream.write() -- i.e. never, in practice,
    for most replies. Slicing here instead bounds the worst-case delay
    between an interrupt and audio actually stopping to about
    _INTERRUPT_CHECK_SECONDS, regardless of Piper's own chunk size.
    """
    slice_len = max(1, int(_INTERRUPT_CHECK_SECONDS * samplerate))
    for start in range(0, len(data), slice_len):
        if utterance_id <= _interrupted_up_to_id:
            return True
        _write_audio(data[start : start + slice_len], samplerate)
    return False


def _speak_now(text: str, utterance_id: int) -> None:
    # Bail before doing anything at all (no voice load, no lock wait) if
    # this utterance was already interrupted before it even got a
    # chance to start -- see _interrupted_up_to_id's comment for the
    # backlog scenario this specifically covers.
    if utterance_id <= _interrupted_up_to_id:
        _end_speaking()
        return

    # _get_voice() is INSIDE the try, not above it. It was outside, and
    # that was a real (if rare) way to wedge the whole listener: it is
    # the one call here that does heavy first-use work (PiperVoice.load
    # -- file I/O plus an onnxruntime session), so it is the one that
    # can throw, and a throw from above the try skips the finally and
    # leaks the _speaking_count this utterance already incremented in
    # speak(). A leaked count means is_speaking() answers True forever,
    # and main.py's listener skips wake-word detection entirely while
    # that is true -- "hey jarvis" silently dead for the rest of the
    # process's life, with the HUD still showing ARMED because the mode
    # genuinely still is "wake". Nothing between _begin_speaking() and
    # this finally may sit outside it.
    try:
        voice = _get_voice()
        # One lock so overlapping speak() calls queue up rather than
        # talking over each other -- state transitions are infrequent
        # enough that this never causes a noticeable backlog.
        with _playback_lock:
            primed = False
            interrupted = False
            for chunk in voice.synthesize(text, syn_config=_SYN_CONFIG):
                if utterance_id <= _interrupted_up_to_id:
                    interrupted = True
                    break
                if not primed:
                    silence = np.zeros(int(_PRIMING_SECONDS * chunk.sample_rate), dtype=np.float32)
                    if _write_audio_interruptible(silence.reshape(-1, 1), chunk.sample_rate, utterance_id):
                        interrupted = True
                        break
                    primed = True
                if _write_audio_interruptible(chunk.audio_float_array.reshape(-1, 1), chunk.sample_rate, utterance_id):
                    interrupted = True
                    break

            if interrupted or utterance_id <= _interrupted_up_to_id:
                # Abort from *this* thread only -- the one that's
                # actually been calling write() on this stream -- never
                # from interrupt()'s caller thread (see interrupt()'s
                # docstring for why that's unsafe). abort() drops
                # whatever's still buffered for output instead of
                # letting it drain, which is the actual "cut off" the
                # user asked for; PortAudio then requires start() again
                # before the stream will accept more writes.
                if _output_stream is not None:
                    try:
                        _output_stream.abort()
                        _output_stream.start()
                    except Exception:
                        pass
            else:
                time.sleep(_ECHO_TAIL_SECONDS)
    except Exception:
        # phrases.PHRASES["audio_trouble"] was defined but never picked
        # from anywhere -- this line hardcoded its own near-duplicate of
        # the same wording instead, which is exactly the kind of drift
        # the pool exists to prevent (a wording tweak here would have
        # had to remember to also happen to the unused pool, or not).
        hud.line(phrases.pick("audio_trouble"), "warn")
        hud.note(traceback.format_exc())
    finally:
        _end_speaking()


def speak(text: str, blocking: bool = False) -> None:
    """
    Fire-and-forget by default. Pass blocking=True for anything said
    right before the process exits (quit) -- otherwise the process can
    end before a background thread gets to actually play it.
    """
    utterance_id = _begin_speaking()
    if blocking:
        _speak_now(text, utterance_id)
    else:
        threading.Thread(target=_speak_now, args=(text, utterance_id), daemon=True).start()


# --- non-verbal cues (earcons.py) ------------------------------------
# Short tones share the voice's playback path rather than opening their
# own device: same output stream (earcons are generated at the Piper
# medium voice's 22050Hz precisely so it never has to be re-opened),
# same lock so a cue can't play over a spoken reply, and crucially the
# same _begin_speaking/_end_speaking accounting so the always-on session
# listener stays muted for their duration too. See earcons.py's module
# docstring for why that last part isn't optional.
_EARCON_TAIL_SECONDS = 0.08  # shorter than _ECHO_TAIL_SECONDS -- a 60ms
# tone has far less room decay behind it than a spoken sentence.


def _play_samples_now(samples: np.ndarray, samplerate: int, utterance_id: int) -> None:
    try:
        if utterance_id <= _interrupted_up_to_id:
            return
        with _playback_lock:
            if utterance_id <= _interrupted_up_to_id:
                return
            _write_audio(samples.reshape(-1, 1), samplerate)
            time.sleep(_EARCON_TAIL_SECONDS)
    except Exception:
        # A cue failing is never worth surfacing, let alone crashing a
        # caller over -- it's decoration on top of an action that
        # already happened.
        pass
    finally:
        _end_speaking()


def play_samples(samples: np.ndarray, samplerate: int, blocking: bool = False) -> None:
    """
    Play raw float32 mono samples as a non-verbal cue. Mirrors speak()'s
    contract: fire-and-forget by default, blocking=True where ordering
    against a following speak() matters or the process is about to exit.
    """
    utterance_id = _begin_speaking()
    if blocking:
        _play_samples_now(samples, samplerate, utterance_id)
    else:
        threading.Thread(target=_play_samples_now, args=(samples, samplerate, utterance_id), daemon=True).start()
