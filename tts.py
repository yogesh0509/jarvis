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
"""

import threading
import time
import traceback
from pathlib import Path

import numpy as np
import sounddevice as sd
from piper.voice import PiperVoice
from piper.config import SynthesisConfig

VOICE = "northern_english_male"  # or "alan"
MODEL_DIR = Path(__file__).parent / "models" / f"piper-en_GB-{VOICE}-medium"
MODEL_FILE = f"en_GB-{VOICE}-medium.onnx"

# length_scale scales phoneme duration -- <1 is faster. Started at 1/1.5
# (50% faster), then 1/1.25, now settled at 1/1.1 (10% faster than the
# voice's natural pace) -- each step down because the previous one felt
# too rushed, especially once all conversational replies started being
# spoken, not just state transitions.
_SYN_CONFIG = SynthesisConfig(length_scale=1 / 1.1)

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
_ECHO_TAIL_SECONDS = 0.3


def is_speaking() -> bool:
    with _speaking_count_lock:
        return _speaking_count > 0


def _begin_speaking() -> None:
    global _speaking_count
    with _speaking_count_lock:
        _speaking_count += 1


def _end_speaking() -> None:
    global _speaking_count
    with _speaking_count_lock:
        _speaking_count = max(0, _speaking_count - 1)


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


def _speak_now(text: str) -> None:
    voice = _get_voice()
    # One lock so overlapping speak() calls queue up rather than
    # talking over each other -- state transitions are infrequent
    # enough that this never causes a noticeable backlog.
    try:
        with _playback_lock:
            primed = False
            for chunk in voice.synthesize(text, syn_config=_SYN_CONFIG):
                if not primed:
                    silence = np.zeros(int(_PRIMING_SECONDS * chunk.sample_rate), dtype=np.float32)
                    _write_audio(silence.reshape(-1, 1), chunk.sample_rate)
                    primed = True
                _write_audio(chunk.audio_float_array.reshape(-1, 1), chunk.sample_rate)
            time.sleep(_ECHO_TAIL_SECONDS)
    except Exception:
        traceback.print_exc()
        print("Couldn't get a word out there, sir -- audio output seems to be having trouble.")
    finally:
        _end_speaking()


def speak(text: str, blocking: bool = False) -> None:
    """
    Fire-and-forget by default. Pass blocking=True for anything said
    right before the process exits (quit) -- otherwise the process can
    end before a background thread gets to actually play it.
    """
    _begin_speaking()
    if blocking:
        _speak_now(text)
    else:
        threading.Thread(target=_speak_now, args=(text,), daemon=True).start()
