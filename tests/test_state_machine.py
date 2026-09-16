"""
Tests for the listener's own state machine -- the thing USER_GUIDE.md
documents as a state diagram (wake-word-armed awake/deep-sleep,
conversation open, paused) and `jarvis status` reports by name.

Two halves, for two different reasons:

- `_status_line` is a module-level pure function precisely so the state
  *naming* is testable without starting the app, in the same spirit as
  `_format_stats` and `_should_reject_transcript`. Every name it can
  print should correspond to a state the process can actually be in, and
  every state it can be in should have a name -- dictation used to have
  none, and reported as a plain "Conversation open" despite being the one
  state where a closing phrase does not close the conversation.

- `_set_enabled` is a closure inside `main()`, so it's reached the way
  `test_audio_callbacks.py` reaches the audio callbacks: run the real
  `main()` far enough with everything heavy faked, capture the frame, and
  bail. Worth the small amount of reflection -- pause/resume had no
  coverage at all, which is how a pause landing mid-conversation came to
  leave the machine in "session_listening", so that the next `jarvis
  resume` reopened a conversation nobody opened.

No audio device, no models, no window manager -- same constraint as the
rest of tests/.
"""

import queue
import sys
import threading

import numpy as np
import pytest

import main


# --- _status_line: the names `jarvis status` can print -----------------


def test_paused_outranks_every_other_state():
    # Pause is the privacy control: whatever else is true, if the mic is
    # off that is the headline, not a stale conversation or sleep state.
    line = main._status_line(
        enabled=False, mode="session_recording", whisper_loaded=True, dictating=True, llm_state="warm"
    )
    assert line.startswith("Paused (mic off)")


def test_the_four_documented_names_are_all_reachable():
    names = {
        main._status_line(enabled=False, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm"),
        main._status_line(enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm"),
        main._status_line(enabled=True, mode="wake", whisper_loaded=False, dictating=False, llm_state="cold"),
        main._status_line(
            enabled=True, mode="session_listening", whisper_loaded=True, dictating=False, llm_state="warm"
        ),
    }
    assert {n.split(" · LLM")[0] for n in names} == {
        "Paused (mic off)",
        "Wake-word armed (awake)",
        "Wake-word armed (deep sleep)",
        "Conversation open",
    }


def test_dictation_is_reported_distinctly_from_an_ordinary_conversation():
    # It is NOT an ordinary conversation: while dictating, "power down"
    # is typed out rather than closing anything, so a status that calls
    # the two the same hides the difference that matters.
    dictating = main._status_line(
        enabled=True, mode="session_listening", whisper_loaded=True, dictating=True, llm_state="warm"
    )
    ordinary = main._status_line(
        enabled=True, mode="session_listening", whisper_loaded=True, dictating=False, llm_state="warm"
    )
    assert dictating != ordinary
    assert "Dictation" in dictating
    assert "Dictation" not in ordinary


def test_both_session_modes_read_as_one_conversation():
    listening = main._status_line(
        enabled=True, mode="session_listening", whisper_loaded=True, dictating=False, llm_state="warm"
    )
    recording = main._status_line(
        enabled=True, mode="session_recording", whisper_loaded=True, dictating=False, llm_state="warm"
    )
    assert listening == recording == "Conversation open · LLM warm"


def test_the_llm_state_is_passed_through_verbatim():
    # Including "failed", which prewarm_state() really can return and the
    # guide's warm/warming/cold list did not mention.
    for llm_state in ("cold", "warming", "warm", "failed"):
        line = main._status_line(
            enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state=llm_state
        )
        assert line.endswith(f"LLM {llm_state}")


def test_a_stalled_listener_is_called_out_and_a_busy_one_is_not():
    busy = main._status_line(
        enabled=True,
        mode="wake",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        seconds_since_listener_beat=main.LISTENER_STALL_SECONDS - 1,
    )
    assert "stalled" not in busy

    dead = main._status_line(
        enabled=True,
        mode="wake",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        seconds_since_listener_beat=main.LISTENER_STALL_SECONDS + 1,
    )
    assert "listener stalled" in dead


def test_the_stall_threshold_clears_the_worst_legitimate_block():
    # The listener holds its heartbeat for the duration of a transcribe +
    # LLM fallback, documented at up to ~30s. A threshold under that
    # would cry wolf on every slow-but-healthy command.
    assert main.LISTENER_STALL_SECONDS > 30


# --- _set_enabled: pause/resume against the real closure ---------------


class _FakeStream:
    """Records what was done to it, so a closed stream is provable."""

    def __init__(self, **kwargs):
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


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


# Background work _set_enabled starts. Joined at fixture teardown so a
# thread can never outlive the monkeypatching and reach for the real
# Whisper weights -- with the fakes in place they all finish instantly.
_BACKGROUND_THREAD_NAMES = ("resume-reload", "pause-unload")


@pytest.fixture
def live():
    """
    main()'s own locals, captured mid-construction.

    Everything heavy is faked and the run is stopped at
    keyboard.on_press_key -- late enough that _set_enabled and the state
    dict exist, early enough that no listener thread is ever started. The
    frame's f_locals is a snapshot, so `state` and the closures are read
    out of it once; `state` is a real dict and stays live, while the
    streams are tracked on the fakes rather than by re-reading main()'s
    rebound `stream` local.

    The patches stay up for the body of the test, not just the capture:
    _set_enabled spawns background threads that load models, and undoing
    the fakes first sends those straight at the real weights.
    """
    captured = {}

    class _StopHere(Exception):
        pass

    streams = []

    def _fake_input_stream(**kwargs):
        s = _FakeStream(**kwargs)
        streams.append(s)
        return s

    def _capture_and_stop(key, fn):
        captured["locals"] = sys._getframe(1).f_locals
        raise _StopHere

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main.sd, "InputStream", _fake_input_stream)
        mp.setattr(main.keyboard, "on_press_key", _capture_and_stop)
        mp.setattr(main, "_acquire_single_instance_lock", lambda: None)
        mp.setattr(main, "_log_event", lambda *a, **k: None)
        mp.setattr(main.hud, "init", lambda: None)
        mp.setattr(main, "speak", lambda *a, **k: None)
        mp.setattr(main.earcons, "play", lambda *a, **k: None)
        mp.setattr(main, "WhisperModel", lambda *a, **k: _FakeWhisper())
        mp.setattr(main, "IntentMatcher", _FakeMatcher)
        mp.setattr(main, "WakeWordModel", lambda **k: _FakeWakeWord())
        mp.setattr(main, "SileroVAD", _FakeVad)
        # Keeps _start_llm_prewarm on its early return, so no background
        # thread ever touches the real GGUF model either.
        mp.setattr(main.llm_intent, "prewarm_state", lambda: "warm")
        with pytest.raises(_StopHere):
            main.main()

        local = captured["locals"]
        yield {
            "state": local["state"],
            "set_enabled": local["_set_enabled"],
            "drain": local["_drain_audio_queue"],
            "audio_q": local["audio_q"],
            "callback": local["audio_callback"],
            "lock": local["_processing_lock"],
            "streams": streams,
        }

        for thread in threading.enumerate():
            if thread.name in _BACKGROUND_THREAD_NAMES:
                thread.join(timeout=10)


def test_pause_shuts_the_main_stream_and_resets_the_session_machine(live):
    state = live["state"]
    state["mode"] = "session_recording"
    state["dictating"] = True

    live["set_enabled"](False)

    assert state["enabled"] is False
    # The reset that stops a later resume waking into a conversation
    # nobody opened.
    assert state["mode"] == "wake"
    assert state["dictating"] is False
    # Stream 0 is the main listener; stream 1 is push-to-talk's, which is
    # documented to keep working while paused and must NOT be touched.
    assert live["streams"][0].closed is True
    assert live["streams"][1].closed is False


def test_pause_shuts_the_mic_even_while_the_processing_lock_is_held(live):
    # `jarvis pause` is the privacy control, and the unload it triggers
    # needs _processing_lock -- which an in-flight LLM fallback can hold
    # for up to 30s. Done inline, that made the mic close wait out the
    # model (and blew jarvisctl.py's own 3s timeout, so a pause that had
    # already been asked for reported failure). Standing on the lock here
    # is the LLM call; the mic must still shut.
    held, release = threading.Event(), threading.Event()

    def _hog_the_lock():
        with live["lock"]:
            held.set()
            release.wait(10)

    hog = threading.Thread(target=_hog_the_lock, daemon=True)
    hog.start()
    assert held.wait(10), "could not acquire the processing lock to stand on"

    try:
        live["set_enabled"](False)
        assert live["state"]["enabled"] is False
        assert live["streams"][0].closed is True
    finally:
        release.set()
        hog.join(timeout=10)


def test_resume_comes_back_wake_armed_not_mid_conversation(live):
    state = live["state"]
    state["mode"] = "session_listening"
    state["dictating"] = True

    live["set_enabled"](False)
    live["set_enabled"](True)

    assert state["enabled"] is True
    assert state["mode"] == "wake"
    assert state["dictating"] is False
    # A fresh stream, not the closed one handed back.
    assert live["streams"][-1].started is True


def test_resume_rearms_the_watchdog_and_the_idle_timer_before_reopening(live):
    # Both are compared against wall-clock time. Left stamped from before
    # the pause, the 5s input watchdog tears down the brand-new stream on
    # its first idle tick -- reintroducing the Windows cold-open glitch
    # that keeping one stream open for the process's life exists to avoid.
    state = live["state"]
    live["set_enabled"](False)
    state["main_last_chunk"] = 0.0
    state["last_activity"] = 0.0

    live["set_enabled"](True)

    assert state["main_last_chunk"] > 0.0
    assert state["last_activity"] > 0.0


def test_resume_asks_the_listener_to_reset_the_acoustic_models(live):
    # openWakeWord builds confidence over a rolling window and Silero
    # carries recurrent state; the audio either side of a pause is
    # discontinuous. Flagged rather than reset inline because both objects
    # are driven by the listener thread.
    state = live["state"]
    live["set_enabled"](False)
    state["reset_acoustic"] = False

    live["set_enabled"](True)

    assert state["reset_acoustic"] is True


def test_paused_implies_wake_mode_however_it_was_entered(live):
    # The invariant the pause/resume race used to break. Whatever the
    # listener was doing when the pause landed, "not enabled" must mean
    # "mode is wake" -- otherwise resume silently reopens a conversation.
    state = live["state"]
    for mode in ("wake", "session_listening", "session_recording"):
        state["mode"] = mode
        live["set_enabled"](False)
        assert state["mode"] == "wake", f"pause from {mode} left mode {state['mode']!r}"
        live["set_enabled"](True)


def test_wake_mode_implies_dictation_is_off(live):
    # The other standing invariant: dictation must never survive into a
    # state where nothing is routing utterances to the keyboard, or the
    # next conversation starts silently typing.
    state = live["state"]
    for enabled in (False, True):
        state["dictating"] = True
        live["set_enabled"](enabled)
        if state["mode"] == "wake":
            assert state["dictating"] is False


# --- the audio-queue drain --------------------------------------------


def test_drain_empties_the_queue_and_reports_how_much_it_dropped(live):
    audio_q, drain = live["audio_q"], live["drain"]
    while not audio_q.empty():
        audio_q.get_nowait()

    for _ in range(7):
        audio_q.put(object())

    assert drain() == 7
    assert audio_q.empty()


def test_drain_on_an_already_empty_queue_is_a_harmless_noop(live):
    audio_q, drain = live["audio_q"], live["drain"]
    while not audio_q.empty():
        audio_q.get_nowait()
    assert drain() == 0


def test_pause_and_resume_both_drop_whatever_was_captured_meanwhile(live):
    # Frames banked while something else was blocking are stale by the
    # time anyone looks at them, and on the paths that end without
    # speaking they get drained with the is_speaking() mute already
    # lifted -- which is how Jarvis came to record its own "One moment,
    # sir" as the next command.
    audio_q = live["audio_q"]

    for _ in range(3):
        audio_q.put(object())
    live["set_enabled"](False)
    assert audio_q.empty()

    for _ in range(3):
        audio_q.put(object())
    live["set_enabled"](True)
    assert audio_q.empty()


def test_the_drained_queue_is_the_one_the_audio_callback_actually_feeds(live):
    # Guards against the fixture quietly capturing some other local: a
    # drain of the wrong queue would pass every test above while fixing
    # nothing at all in the real pipeline.
    audio_q, drain = live["audio_q"], live["drain"]
    assert isinstance(audio_q, queue.Queue)
    drain()

    frame = np.zeros((main.FRAME_SAMPLES, 1), dtype=np.float32)
    live["callback"](frame, main.FRAME_SAMPLES, None, None)

    assert audio_q.qsize() == 1
    assert drain() == 1
