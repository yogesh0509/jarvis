"""
Earcons -- short non-verbal audio cues, synthesized with numpy at
import time and played through the output stream tts.py already keeps
open for the process's life.

These are purely additive: nothing that was spoken before is spoken any
less. They exist for the moments where speech is too slow to be useful.
The wake blip is the clearest case -- the wake word firing used to be
signalled only by a printed line and then a ~1s spoken "Yes, sir?",
during which there was no confirmation at all that anything had been
heard. An 80ms rising blip lands immediately and makes the whole system
feel instant even when the work behind it isn't.

Cost is about as close to nothing as it gets: eight short float32
arrays, ~40KB of RAM total, generated once. No model, no file I/O, no
network, no per-play synthesis.

They go through tts's _begin_speaking/_end_speaking accounting rather
than straight to the device. This matters -- the session listener is
always-on and mutes itself only for the duration of tracked speech, so
an untracked tone would be heard back through the mic as input. That is
the same bug class the README documents for spoken replies (runaway
desktop switches from Jarvis hearing its own voice); Silero is
speech-specific and probably wouldn't fire on a pure tone, but "probably
wouldn't" is not a thing to build on top of.
"""

import numpy as np

import tts

RATE = 22050  # matches the Piper medium voice, so the shared output stream is never re-opened

# Chirps, not alerts. Loud enough to register over a keyboard, quiet
# enough to sit under the spoken voice without competing with it.
_PEAK = 0.16


def _tone(freq_start, freq_end, seconds, attack=0.006, release=None, harmonic=0.25):
    """
    One swept sine with a second harmonic mixed in and a raised-cosine
    envelope on both ends. The envelope isn't optional polish -- a bare
    sine that starts and stops at a nonzero sample produces an audible
    click at each edge, which on a 60ms cue is most of what you hear.
    """
    n = max(1, int(seconds * RATE))
    t = np.arange(n, dtype=np.float32) / RATE
    freq = np.linspace(freq_start, freq_end, n, dtype=np.float32)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    wave = np.sin(phase) + harmonic * np.sin(2 * phase)

    release = seconds * 0.45 if release is None else release
    env = np.ones(n, dtype=np.float32)
    a = max(1, int(attack * RATE))
    r = max(1, int(release * RATE))
    env[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a, dtype=np.float32))
    env[-r:] = np.minimum(env[-r:], 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r, dtype=np.float32)))
    return (wave * env).astype(np.float32)


def _gap(seconds):
    return np.zeros(int(seconds * RATE), dtype=np.float32)


def _seq(*parts):
    out = np.concatenate(parts)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * _PEAK).astype(np.float32)


# Frequencies picked off a pentatonic-ish set so any two cues landing
# close together still sound intentional rather than accidental.
_A5, _CS6, _E6, _A6 = 880.0, 1108.7, 1318.5, 1760.0
_E4, _A4, _CS5 = 329.6, 440.0, 554.4

_LIBRARY = {
    # Wake word caught -- the fastest possible "I heard you".
    "wake": _seq(_tone(_A5, _A5, 0.045), _tone(_E6, _E6, 0.070)),
    # An utterance was resolved and is about to be acted on.
    "understood": _seq(_tone(_E6, _A6, 0.055)),
    # Something went wrong, or an action couldn't be carried out.
    "error": _seq(_tone(_CS5, _CS5, 0.055), _gap(0.02), _tone(_A4, _E4, 0.110)),
    # Understood, but deliberately not doing it (desktop numbers, off duty).
    "decline": _seq(_tone(_A4, _A4, 0.050), _gap(0.025), _tone(_A4, _A4, 0.050)),
    # Session closed on its own or on request.
    "session_close": _seq(_tone(_E6, _A5, 0.120)),
    # Dropping the heavy models -- arc reactor easing down.
    "sleep": _seq(_tone(_A5, _E4, 0.340, harmonic=0.15)),
    # Models back, full power.
    "wake_up": _seq(_tone(_E4, _A5, 0.300, harmonic=0.15)),
    # Boot complete -- the one flourish, once per process.
    "boot": _seq(
        _tone(_A4, _A4, 0.060),
        _gap(0.015),
        _tone(_CS5, _CS5, 0.060),
        _gap(0.015),
        _tone(_E6, _E6, 0.075),
        _gap(0.01),
        _tone(_A6, _A6, 0.180),
    ),
    # Shutting down -- the spin-down, deliberately the longest cue here.
    "power_down": _seq(_tone(_A6, _E4, 0.520, harmonic=0.12)),
    "dictation_on": _seq(_tone(_A5, _CS6, 0.070)),
    "dictation_off": _seq(_tone(_CS6, _A5, 0.070)),
}


def play(name, blocking=False):
    """
    blocking=True is for the two places ordering actually matters: the
    wake blip, which must land before the spoken "Yes, sir?" that
    follows it on the same thread, and the shutdown sweep, where the
    process is about to exit. Everywhere else fire-and-forget is fine
    and keeps the listener thread free.
    """
    samples = _LIBRARY.get(name)
    if samples is None:
        return
    tts.play_samples(samples, RATE, blocking=blocking)
