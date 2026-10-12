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
    # Without this, boot calls the real morning_briefing(), which
    # enumerates real windows via win32gui/virtual_desktop's real COM
    # manager -- exactly the "real audio device/model/window manager"
    # class of dependency this module's own docstring says a test here
    # must never touch (see test_state_machine.py's `live` fixture,
    # which needed the identical fix for the identical reason).
    monkeypatch.setattr(main, "morning_briefing", lambda: "")
    monkeypatch.setattr(main, "WhisperModel", lambda *a, **k: _FakeWhisper())
    monkeypatch.setattr(main, "IntentMatcher", _FakeMatcher)
    monkeypatch.setattr(main, "WakeWordModel", lambda **k: _FakeWakeWord())
    monkeypatch.setattr(main, "SileroVAD", _FakeVad)
    # Boot starts this before either InputStream call above -- without
    # this it's a real COM polling thread against whatever render
    # device this machine has, not the fake one this module promises.
    monkeypatch.setattr(main.output_monitor, "start", lambda: None)
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    # Same again for echo cancellation: the real start() loads livekit's
    # native library and opens a WASAPI loopback stream on a COM thread.
    # Left unstarted, every echo_canceller entry point is a no-op.
    monkeypatch.setattr(main.echo_canceller, "start", lambda: None)
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


def _main_audio_q(callback):
    # audio_q is a local of main(); reach it through the callback's closure.
    return next(
        cell.cell_contents
        for cell in callback.__closure__
        if hasattr(cell.cell_contents, "put") and hasattr(cell.cell_contents, "get_nowait")
    )


def test_a_failing_echo_canceller_still_queues_the_raw_frame(monkeypatch):
    # echo_canceller.process() never raises by contract. If it ever did,
    # the main callback must neither let it reach PortAudio nor lose the
    # frame with it -- raw audio is exactly what the listener got before
    # echo cancellation existed.
    callbacks = _callbacks_from_a_live_main(monkeypatch)
    logged = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **k: logged.append(kind))

    def explode(block):
        raise RuntimeError("aec boom")

    monkeypatch.setattr(main.echo_canceller, "process", explode)
    raw = np.full((main.FRAME_SAMPLES, 1), 0.125, dtype=np.float32)
    callbacks[0](raw, main.FRAME_SAMPLES, None, None)

    assert logged == ["aec_process_error"]
    assert np.array_equal(_main_audio_q(callbacks[0]).get_nowait(), raw[:, 0])


def test_the_main_callback_queues_what_the_echo_canceller_returns(monkeypatch):
    callbacks = _callbacks_from_a_live_main(monkeypatch)
    cleaned = np.full(main.FRAME_SAMPLES, 0.25, dtype=np.float32)
    monkeypatch.setattr(main.echo_canceller, "process", lambda block: cleaned)
    callbacks[0](np.zeros((main.FRAME_SAMPLES, 1), dtype=np.float32), main.FRAME_SAMPLES, None, None)
    assert _main_audio_q(callbacks[0]).get_nowait() is cleaned


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
