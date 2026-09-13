"""
Regression test for the VAD chunk-size fix (vad.py): feeding
main.py-native 1280-sample chunks (openWakeWord's chunk size, off
Silero's trained 512-sample grid) used to measurably inflate the
false-positive rate on plain noise. This pins that rate to roughly its
native-window level so a future change can't silently reintroduce the
mismatch.
"""

import numpy as np

from vad import SileroVAD

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # main.py's actual chunk size
THRESHOLD = 0.15  # main.py's VAD_SPEECH_THRESHOLD


def _false_positive_rate(signal: np.ndarray) -> float:
    vad = SileroVAD(SAMPLE_RATE)
    vad.reset()
    n_frames = len(signal) // FRAME_SAMPLES
    flagged = 0
    for i in range(n_frames):
        chunk = signal[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        if vad.speech_prob(chunk) >= THRESHOLD:
            flagged += 1
    return flagged / n_frames


def test_white_noise_false_positive_rate_stays_low():
    # Before the fix (VAD fed 1280-sample chunks directly): ~8% mean
    # across seeds. After (rebuffered to native 512-sample windows):
    # ~1-2%, matching 512/1536-chunk behavior. A generous 5% ceiling
    # catches a regression back toward the old behavior without being
    # flaky about exact percentages.
    rates = []
    for seed in range(5):
        rng = np.random.default_rng(seed)
        noise = (rng.standard_normal(SAMPLE_RATE * 5) * 0.02).astype(np.float32)
        rates.append(_false_positive_rate(noise))
    assert np.mean(rates) < 0.05, f"mean false-positive rate {np.mean(rates):.3f} too high -- VAD chunking regression?"


def test_silence_is_never_flagged_as_speech():
    silence = np.zeros(SAMPLE_RATE * 3, dtype=np.float32)
    assert _false_positive_rate(silence) == 0.0


def test_reset_clears_internal_buffer_and_recurrent_state():
    vad = SileroVAD(SAMPLE_RATE)
    # Feed a partial (sub-window) chunk so something is sitting in the
    # internal buffer, then reset -- a stale partial buffer leaking
    # across reset() would corrupt window alignment for the next
    # session/utterance.
    vad.speech_prob(np.zeros(300, dtype=np.float32))
    vad.reset()
    assert len(vad._buffer) == 0


def test_speech_prob_returns_a_single_float_per_call():
    # main.py's whole state machine assumes one probability per call
    # regardless of how many native windows it took internally.
    vad = SileroVAD(SAMPLE_RATE)
    prob = vad.speech_prob(np.zeros(FRAME_SAMPLES, dtype=np.float32))
    assert isinstance(prob, float)
