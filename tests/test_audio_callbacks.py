"""
Regression tests for the two sounddevice input callbacks.

sounddevice registers its callback with `error=_lib.paAbort`, so any
exception escaping a callback returns paAbort to PortAudio, which
**permanently stops the stream** -- no further callbacks, ever. In
practice that looked like "hey jarvis" simply doing nothing forever,
with nothing logged, until the input watchdog eventually reopened the
stream. So neither callback may *ever* let an exception escape,
whatever goes wrong inside it.
"""

import numpy as np
import pytest

import main


class _FakeWhisper:
    def transcribe(self, *args, **kwargs):
        return iter(()), None


class _FakeMatcher:
    _seeds = ["open chrome"]

    def match(self, text):
        raise AssertionError("not reached in this test")


class _FakeWakeWord:
    def predict(self, chunk):
        return {}

    def reset(self):
        pass


class _FakeVad:
    def __init__(self, rate):
        pass

    def speech_prob(self, frame):
        return 0.0

    def reset(self):
        pass


def _callbacks_from_a_live_main(monkeypatch):
    """
    Run the real main() far enough to capture the real callbacks, then
    bail out.

    Everything heavy is faked and sd.InputStream is replaced with a
    recorder, so no model loads and no audio device is touched. The
    sentinel raised from the second stream open stops main() once both
    callbacks exist. Faked rather than skipped deliberately -- a skipped
    test asserts nothing about the property this file exists to protect.
    """
    captured = {}

    class _StopHere(Exception):
        pass

    class FakeInputStream:
        def __init__(self, **kwargs):
            captured.setdefault("callbacks", []).append(kwargs["callback"])
            if len(captured["callbacks"]) == 2:
                raise _StopHere

        def start(self):
            pass

    monkeypatch.setattr(main.sd, "InputStream", FakeInputStream)
    monkeypatch.setattr(main, "_acquire_single_instance_lock", lambda: None)
    monkeypatch.setattr(main, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(main.hud, "init", lambda: None)
    monkeypatch.setattr(main, "speak", lambda *a, **k: None)
    monkeypatch.setattr(main.earcons, "play", lambda *a, **k: None)
    monkeypatch.setattr(main, "WhisperModel", lambda *a, **k: _FakeWhisper())
    monkeypatch.setattr(main, "IntentMatcher", _FakeMatcher)
    monkeypatch.setattr(main, "WakeWordModel", lambda **k: _FakeWakeWord())
    monkeypatch.setattr(main, "SileroVAD", _FakeVad)
    # Boot starts this before either InputStream call above -- without
    # this it's a real COM polling thread against whatever render
    # device this machine has, not the fake one this module promises.
    monkeypatch.setattr(main.output_monitor, "start", lambda: None)
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    with pytest.raises(_StopHere):
        main.main()
    assert len(captured["callbacks"]) == 2
    return captured["callbacks"]


@pytest.mark.parametrize("index", [0, 1])
def test_a_callback_that_blows_up_internally_does_not_raise(monkeypatch, index):
    """
    The property that actually matters. Whatever fails inside -- a HUD
    bug, a full queue, a bad frame -- the exception must not reach
    PortAudio, because PortAudio's answer to an exception is to kill the
    stream for the rest of the process's life.

    The failure is injected into hud.note with a truthy `status`, which
    is the one line *both* callbacks run unconditionally. An earlier
    version broke hud.feed_level instead and so passed vacuously for the
    push-to-talk callback, which only reaches feed_level while the key
    is actually held.
    """
    callbacks = _callbacks_from_a_live_main(monkeypatch)
    callback = callbacks[index]

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    reached = []
    monkeypatch.setattr(main.hud, "note", explode)
    monkeypatch.setattr(main, "_log_event", lambda *a, **k: reached.append(True))

    # Must simply return, not raise.
    callback(
        np.zeros((main.FRAME_SAMPLES, 1), dtype=np.float32),
        main.FRAME_SAMPLES,
        None,
        "input overflow",  # truthy status -> hud.note -> boom
    )
    assert reached, "the failure should have been recorded, not silently dropped"


@pytest.mark.parametrize("index", [0, 1])
def test_a_healthy_callback_records_no_error(monkeypatch, index):
    """Guards against the try/except turning every frame into a logged failure."""
    callbacks = _callbacks_from_a_live_main(monkeypatch)
    logged = []
    monkeypatch.setattr(main, "_log_event", lambda *a, **k: logged.append(a))
    callbacks[index](
        np.zeros((main.FRAME_SAMPLES, 1), dtype=np.float32), main.FRAME_SAMPLES, None, None
    )
    assert logged == []
