"""
Tests for the synthesized audio cues.

Two things are worth asserting and neither is about how they sound.

First, the waveforms have to be valid: anything outside [-1, 1], any
NaN, or a non-float32 dtype either clips audibly or is rejected outright
by the output stream, and both would surface as "the cues just don't
work" with nothing in the logs.

Second, and more important, every cue has to go through tts's speaking
accounting. The session listener is always-on and mutes itself only for
tracked speech -- a cue played straight to the device would be heard
back through the microphone as input, which is exactly the failure mode
the README documents for spoken replies (runaway desktop switches from
Jarvis acting on its own voice).
"""

import numpy as np
import pytest

import earcons
import tts


@pytest.mark.parametrize("name", sorted(earcons._LIBRARY))
def test_every_cue_is_a_valid_playable_waveform(name):
    samples = earcons._LIBRARY[name]
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert len(samples) > 0
    assert np.all(np.isfinite(samples))
    assert np.max(np.abs(samples)) <= 1.0


@pytest.mark.parametrize("name", sorted(earcons._LIBRARY))
def test_every_cue_is_short_enough_to_be_a_cue(name):
    """
    These sit in front of spoken replies and in the wake path, where
    latency is the entire point. Anything approaching a second stops
    being an acknowledgement and starts being a delay.
    """
    assert len(earcons._LIBRARY[name]) / earcons.RATE < 0.7


@pytest.mark.parametrize("name", sorted(earcons._LIBRARY))
def test_every_cue_starts_and_ends_near_silence(name):
    """
    The envelope isn't cosmetic -- a waveform that starts or ends at a
    nonzero sample clicks, and on a 60ms tone the click is most of what
    you hear.
    """
    samples = earcons._LIBRARY[name]
    assert abs(float(samples[0])) < 0.01
    assert abs(float(samples[-1])) < 0.01


def test_cue_matches_the_voice_sample_rate():
    """
    Off by one here and every cue tears down and re-opens the shared
    output stream, reintroducing the cold-open glitch tts.py went to
    some trouble to eliminate.
    """
    assert earcons.RATE == 22050


def test_play_mutes_the_listener_for_the_duration(monkeypatch):
    """The cue must already be counted as 'speaking' by the time audio reaches the device."""
    seen = {}

    def spy_write(data, samplerate):
        seen["speaking"] = tts.is_speaking()
        seen["rate"] = samplerate

    monkeypatch.setattr(tts, "_write_audio", spy_write)
    monkeypatch.setattr(tts, "_EARCON_TAIL_SECONDS", 0.0)
    earcons.play("wake", blocking=True)
    assert seen["speaking"] is True
    assert seen["rate"] == earcons.RATE
    # ...and released again once it's done.
    assert tts.is_speaking() is False


def test_play_samples_always_releases_the_speaking_count(monkeypatch):
    """
    A cue that failed to play used to be able to leave the count
    incremented, which mutes the listener permanently -- the microphone
    appears to stop working entirely, with nothing logged.
    """
    def exploding_write(*args, **kwargs):
        raise RuntimeError("device gone")

    monkeypatch.setattr(tts, "_write_audio", exploding_write)
    before = tts.is_speaking()
    tts.play_samples(np.zeros(64, dtype=np.float32), earcons.RATE, blocking=True)
    assert tts.is_speaking() == before


def test_unknown_cue_is_a_no_op(monkeypatch):
    called = []
    monkeypatch.setattr(tts, "play_samples", lambda *a, **k: called.append(a))
    earcons.play("no_such_cue")
    assert called == []


def test_no_effect_is_acoustically_distinct_from_decline_and_error():
    """
    actions.Outcome's three statuses need three genuinely different
    sounds -- "no_effect" (nothing to do) must not be confusable with
    "decline" (refused on purpose) or "error" (something broke).
    """
    no_effect = earcons._LIBRARY["no_effect"]
    decline = earcons._LIBRARY["decline"]
    error = earcons._LIBRARY["error"]
    # Different overall length -- decline's double-tap and error's
    # two-part fall are both longer than one plain beep.
    assert len(no_effect) != len(decline)
    assert len(no_effect) != len(error)
    # Different peak frequency content -- a flat tone at a pitch none
    # of the others use should not share dominant energy with either.
    def dominant_bin(samples):
        spectrum = np.abs(np.fft.rfft(samples))
        return int(np.argmax(spectrum))
    assert dominant_bin(no_effect) != dominant_bin(decline)
    assert dominant_bin(no_effect) != dominant_bin(error)
