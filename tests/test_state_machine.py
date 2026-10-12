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
import types
import sys
import threading
import time

import numpy as np
import pytest

import actions
import hud
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


def test_speaking_is_called_out_because_it_pauses_the_wake_word():
    # "Armed" and "actually listening right now" are different claims:
    # the listener skips wake-word detection entirely while Jarvis is
    # talking. Normally a second or two -- but tts.py's speaking count
    # is the one gate that can leak and stay up forever, and when it
    # does this line is what separates "the wake word is broken" from
    # "something never said it had stopped talking".
    line = main._status_line(
        enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm", speaking=True
    )
    assert line.startswith("Wake-word armed (awake)")
    assert "speaking" in line


def test_speaking_is_not_reported_while_paused():
    # Paused already means the mic is off; a leftover speaking flag on
    # top of that is noise, not information -- same reasoning as the
    # mic-signal line, which is also suppressed while paused.
    line = main._status_line(
        enabled=False, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm", speaking=True
    )
    assert "speaking" not in line


def test_a_quiet_listener_says_nothing_about_speaking():
    line = main._status_line(
        enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm", speaking=False
    )
    assert "speaking" not in line


def test_echo_cancellation_state_is_reported_when_given():
    # "Will hey jarvis work over the music right now" -- the reason names
    # come straight from echo_canceller.Canceller.coverage.
    line = main._status_line(
        enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm", aec="residual_high"
    )
    assert f"{main.DOT} AEC residual high" in line
    bare = main._status_line(enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm")
    assert "AEC" not in bare


def test_int16_conversion_saturates_instead_of_wrapping():
    # The wake path is the only int16 conversion in the pipeline, and
    # astype(np.int16) wraps rather than saturates: without the clip, a
    # float32 sample of 1.05 (which WASAPI/APO gain can hand back on a
    # hot mic) becomes -31131 -- a near-full-scale sample of the
    # OPPOSITE polarity, landing in exactly the loudest part of the
    # waveform. This pins the clip that prevents it; the assertion is
    # written against the same expression main.py uses.
    hot = np.array([0.5, 1.0, 1.05, 1.2, -1.3], dtype=np.float32)

    wrapped = (hot * 32767).astype(np.int16)
    assert wrapped[2] < 0, "precondition: unclipped conversion really does wrap the sign"

    clipped = (np.clip(hot, -1.0, 1.0) * 32767).astype(np.int16)
    assert clipped.min() >= -32767
    assert clipped.max() <= 32767
    assert (clipped[hot > 0] > 0).all(), "no positive sample may come back negative"


def test_near_miss_band_sits_below_the_wake_threshold():
    # The band only makes sense as "loud enough to be a real attempt,
    # not loud enough to fire" -- if the floor ever crossed the
    # threshold the window would log nothing at all, silently undoing
    # the one measurement this exists to capture.
    assert 0.0 < main.WAKE_NEAR_MISS_FLOOR < main.WAKE_THRESHOLD


def test_gate_stuck_threshold_outlasts_an_ordinary_spoken_reply():
    # Jarvis's own replies are the most common reason this gate is up,
    # and they run a few seconds at most -- the threshold has to clear
    # them comfortably or every single reply would log a "stuck" line.
    assert main.LISTENER_GATE_STUCK_SECONDS >= 30.0


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


def test_a_deaf_mic_is_called_out_even_though_everything_else_is_healthy():
    # The case this exists for: stream running, callbacks on schedule,
    # listener beating, models loaded -- and the capture device handing
    # over nothing. Every other signal in the process says "fine", so
    # the status line is the only thing that can say otherwise.
    deaf = main._status_line(
        enabled=True,
        mode="wake",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        input_silent=True,
    )
    assert "NO MIC SIGNAL" in deaf
    assert "Wake-word armed" in deaf  # still true, just no longer the whole truth


def test_a_deaf_mic_is_reported_during_a_conversation_too():
    # Orthogonal to the state name, not folded into it -- a conversation
    # can be open and deaf at the same time.
    line = main._status_line(
        enabled=True,
        mode="session_listening",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        input_silent=True,
    )
    assert "Conversation open" in line and "NO MIC SIGNAL" in line


def test_a_paused_mic_is_not_reported_as_a_fault():
    # Silence from a deliberately-closed stream is the correct outcome,
    # not something to alarm about.
    line = main._status_line(
        enabled=False,
        mode="wake",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        input_silent=True,
    )
    assert "NO MIC SIGNAL" not in line
    assert "Paused" in line


def test_a_hud_render_error_is_surfaced():
    """
    hud.last_render_error() existed all along -- a render-thread
    exception is swallowed by design, since a drawing bug must never
    take the listener down -- but nothing ever actually called it from
    here, so it was surfaced nowhere at all. This is the regression test
    for actually wiring it in.
    """
    line = main._status_line(
        enabled=True,
        mode="wake",
        whisper_loaded=True,
        dictating=False,
        llm_state="warm",
        render_error="Traceback (most recent call last):\n  ...\nValueError: bad tuple shape",
    )
    assert "HUD render error" in line
    assert "ValueError: bad tuple shape" in line
    # The full traceback has no place in a one-line status reply.
    assert "Traceback (most recent call last)" not in line


def test_no_render_error_by_default():
    line = main._status_line(enabled=True, mode="wake", whisper_loaded=True, dictating=False, llm_state="warm")
    assert "render error" not in line


def test_the_silence_floor_sits_above_one_16_bit_lsb():
    # The wedged-driver signature is a constant 1/32768 on every frame.
    # A floor at or below that would read it as signal and never fire.
    assert main.INPUT_SILENCE_FLOOR > 1.0 / 32768


def test_the_silence_window_outlasts_an_ordinary_quiet_stretch():
    # Long enough that a genuinely quiet room never trips it; short
    # enough to catch a dead device inside one sitting.
    assert main.INPUT_SILENCE_SECONDS >= 60
    assert main.INPUT_SILENCE_SECONDS > main.INPUT_WATCHDOG_SECONDS


def test_the_stall_threshold_clears_the_worst_legitimate_block():
    # The listener holds its heartbeat for the duration of a transcribe +
    # LLM fallback, documented at up to ~30s. A threshold under that
    # would cry wolf on every slow-but-healthy command.
    assert main.LISTENER_STALL_SECONDS > 30


# --- _set_enabled: pause/resume against the real closure ---------------


class _FakeStream:
    """
    Records what was done to it, so a closed stream is provable.

    The tiny sleeps in start()/stop() are deliberate, not incidental --
    real sd.InputStream() construction and .stop()/.close() cost real
    time against PortAudio, which is exactly the window
    test_concurrent_reopen_and_pause_resume_never_leaves_two_streams_open
    needs widened to reliably exercise _stream_lock rather than getting
    lucky on GIL scheduling. Negligible against every other test's
    runtime (a handful of pause/resume calls each).
    """

    def __init__(self, **kwargs):
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        time.sleep(0.001)
        self.started = True

    def stop(self):
        time.sleep(0.001)
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
        # Without this, main()'s boot sequence calls the real
        # morning_briefing(), which enumerates real windows via win32gui
        # and virtual_desktop's real COM manager -- exactly the kind of
        # thing the module docstring says tests must stay mockable
        # against (confirmed directly: this polluted
        # virtual_desktop._thread_local.manager for every test in this
        # process, breaking test_virtual_desktop.py's isolation
        # assertion when the suite ran in full).
        mp.setattr(main, "morning_briefing", lambda: "")
        mp.setattr(main, "WhisperModel", lambda *a, **k: _FakeWhisper())
        mp.setattr(main, "IntentMatcher", _FakeMatcher)
        mp.setattr(main, "WakeWordModel", lambda **k: _FakeWakeWord())
        mp.setattr(main, "SileroVAD", _FakeVad)
        # Keeps _start_llm_prewarm on its early return, so no background
        # thread ever touches the real GGUF model either.
        mp.setattr(main.llm_intent, "prewarm_state", lambda: "warm")
        # Without this, main()'s boot sequence starts a *real*
        # output-monitor thread doing real COM calls against whatever
        # render device this machine happens to have -- exactly the kind
        # of thing the module docstring says tests must stay mockable
        # against. Every test using this fixture gets it fixed at False
        # unless it overrides output_monitor.is_output_playing itself.
        mp.setattr(main.output_monitor, "start", lambda: None)
        mp.setattr(main.output_monitor, "stop", lambda: None)
        mp.setattr(main.output_monitor, "is_output_playing", lambda: False)
        # Same reasoning for echo cancellation: the real start() loads
        # livekit's native library and opens a WASAPI loopback stream.
        # Left unstarted, echo_canceller reports (False, "unavailable")
        # -- i.e. the output gate behaves exactly as it did before echo
        # cancellation existed -- unless a test overrides coverage().
        mp.setattr(main.echo_canceller, "start", lambda: None)
        mp.setattr(main.echo_canceller, "stop", lambda: None)
        with pytest.raises(_StopHere):
            main.main()

        local = captured["locals"]
        yield {
            "state": local["state"],
            "set_enabled": local["_set_enabled"],
            "drain": local["_drain_audio_queue"],
            "audio_q": local["audio_q"],
            "callback": local["audio_callback"],
            "check_signal": local["_check_input_signal"],
            "check_output_gate": local["_check_output_gate"],
            "is_session_speech": local["_is_session_speech"],
            "reopen": local["_reopen_input_stream"],
            "lock": local["_processing_lock"],
            "hotwords": local["TRANSCRIBE_HOTWORDS"],
            "record_timing": local["_record_timing"],
            "record_stat": local["_record_stat"],
            "on_press": local["on_press"],
            "on_release": local["on_release"],
            "handle_transcript": local["_handle_transcript"],
            "matcher": local["matcher"],
            "confirm_hotkey_hold": local["_confirm_hotkey_hold"],
            "hotkey_pre_buffer": local["hotkey_pre_buffer"],
            "streams": streams,
        }

        for thread in threading.enumerate():
            if thread.name in _BACKGROUND_THREAD_NAMES:
                thread.join(timeout=10)


def test_hotwords_no_longer_carries_repo_name_hallucination_bait(live):
    # Confirmed live as the actual source of false dispatches, not a
    # hypothetical one: llm_usage_log.jsonl showed this tail coming back
    # as literal transcribed text from near-silent audio ("shu
    # programming bitcoin bitshaka", "how many files folder windows bdk
    # wallet bdk wallet"), and twice as an actual dispatch (bare "bdk" ->
    # focus_vscode_repo, repo="bdk_wallet"). See DECISIONS.md's
    # "Mid-session wake-word swallow, and the 'bdk wallet' false
    # dispatches". Pinned here so it can't quietly creep back in.
    hotwords = live["hotwords"].lower()
    for bait in ("bdk", "hashstack", "djed", "bitshala", "bitshaka", "bitcoin", "shu programming"):
        assert bait not in hotwords
    # The removal shouldn't have taken real command vocabulary with it.
    for real in ("chrome", "how many"):
        assert real in hotwords


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


# --- the silence latch: a stream that runs but hears nothing -----------


def _frame(peak):
    """One callback-shaped frame whose loudest sample is `peak`."""
    frame = np.zeros((main.FRAME_SAMPLES, 1), dtype=np.float32)
    frame[0, 0] = peak
    return frame


def test_real_audio_keeps_the_signal_timestamp_fresh(live):
    state = live["state"]
    state["main_signal_at"] = 0.0

    # One loud frame is deliberately not enough -- see INPUT_SIGNAL_FRAMES
    # below -- so sustain it across a full onset run, same debounce as
    # SPEECH_ONSET_FRAMES.
    for _ in range(main.INPUT_SIGNAL_FRAMES):
        live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)

    assert state["main_signal_at"] > 0.0


def test_a_single_loud_frame_does_not_confirm_signal_on_its_own(live):
    # The regression this guards against: the reopen a dead latch
    # triggers hands back exactly one glitchy chunk from the freshly
    # opened device (the same cold-open artifact tts.py and the
    # push-to-talk stream already work around), and that used to read as
    # "signal is back" and clear the latch 0.19s after it was set.
    state = live["state"]
    state["main_signal_at"] = 0.0

    live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)

    assert state["main_signal_at"] == 0.0


def test_a_broken_run_of_loud_frames_never_confirms_signal(live):
    # Interleaving silence resets the run -- a device that occasionally
    # spikes without ever sustaining real audio should not count either.
    state = live["state"]
    state["main_signal_at"] = 0.0

    for _ in range(main.INPUT_SIGNAL_FRAMES - 1):
        live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)
    live["callback"](_frame(0.0), main.FRAME_SAMPLES, None, None)  # breaks the run
    live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)

    assert state["main_signal_at"] == 0.0


def test_a_wedged_driver_s_constant_one_lsb_does_not_count_as_signal(live):
    # The exact thing the Intel SST capture pipeline delivered after a
    # bad resume: 1/32768, every frame, forever. Callbacks keep arriving,
    # so main_last_chunk stays fresh and the 5s watchdog stays quiet --
    # this is the only tracker that must notice.
    state = live["state"]
    state["main_signal_at"] = 0.0

    for _ in range(50):
        live["callback"](_frame(1.0 / 32768), main.FRAME_SAMPLES, None, None)

    assert state["main_signal_at"] == 0.0


_ONE_LSB = 1.0 / 32768


def _go_frozen(live, frames=40):
    """
    Simulate a wedged converter: the same below-floor value on every
    single frame, which is exactly what the Intel SST array did after a
    bad resume (max peak == mean peak == 0.0000305, one distinct value).
    """
    for _ in range(frames):
        live["callback"](_frame(_ONE_LSB), main.FRAME_SAMPLES, None, None)
    live["state"]["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS + 5)


def _go_quiet_but_alive(live, frames=40):
    """
    Simulate an ordinary quiet room on the same hardware: still under the
    floor, but dithering across 1-3 LSB rather than repeating one value.
    Measured directly -- this is 97% of frames in a normal quiet room.
    """
    for i in range(frames):
        live["callback"](_frame(_ONE_LSB * (1 + i % 3)), main.FRAME_SAMPLES, None, None)
    live["state"]["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS + 5)


def test_an_ordinary_quiet_room_never_latches(live):
    # The bug this whole second condition exists for. Amplitude alone
    # cannot tell a dead device from a quiet room on this hardware --
    # 97% of frames in a normal quiet room sit under the floor -- so a
    # floor-only latch fired every two minutes and reopened a perfectly
    # healthy stream each time.
    state, check = live["state"], live["check_signal"]
    before = len(live["streams"])

    _go_quiet_but_alive(live)
    for _ in range(10):
        check()

    assert state["input_silent"] is False
    assert len(live["streams"]) == before, "a healthy quiet stream must not be reopened"


def test_the_latch_sets_only_after_the_full_silence_window(live):
    state, check = live["state"], live["check_signal"]

    _go_frozen(live)
    state["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS - 5)
    check()
    assert state["input_silent"] is False

    state["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS + 5)
    check()
    assert state["input_silent"] is True


def test_the_latch_reopens_the_stream_exactly_once(live):
    # Reopening does fix the recoverable version of this. A wedged driver
    # survives it, so hammering the device every 20ms afterwards would
    # achieve nothing except burying the log line that matters.
    check = live["check_signal"]
    before = len(live["streams"])

    _go_frozen(live)
    check()
    opened_once = len(live["streams"])
    for _ in range(20):
        check()

    assert opened_once == before + 1
    assert len(live["streams"]) == opened_once


def test_the_reopen_does_not_clear_the_latch_it_just_set(live):
    # The regression that got caught live rather than here: the reopen
    # used to stamp main_signal_at, so the check immediately after it
    # measured ~0s of silence and unlatched. Against a genuinely dead
    # mic that produced input_silent -> input_signal_returned 0.2s apart,
    # on a 120s loop, forever -- an alarm that clears itself is worse
    # than no alarm, because the log looks like the problem went away.
    state, check = live["state"], live["check_signal"]

    _go_frozen(live)
    check()
    assert state["input_silent"] is True

    for _ in range(10):
        check()
        assert state["input_silent"] is True, "latch cleared without any real audio"


def test_signal_coming_back_clears_the_latch(live):
    state, check = live["state"], live["check_signal"]
    _go_frozen(live)
    check()
    assert state["input_silent"] is True

    for _ in range(main.INPUT_SIGNAL_FRAMES):
        live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)
    check()

    assert state["input_silent"] is False


# --- the persistent HUD indicator for a wedge, not just the flag ------
#
# Historically NO MIC SIGNAL reached `jarvis status` only -- for 50
# minutes straight in the real incident this project has on record --
# while the band kept showing ordinary ARMED red the whole time. These
# pin the fix: the flag alone isn't the point, the room lights actually
# changing is.


def test_a_latched_wedge_flashes_error_and_sets_the_indicator(live):
    state, check = live["state"], live["check_signal"]
    hud.set_input_blocked(None)
    hud.end_flash()
    try:
        _go_frozen(live)
        check()
        assert state["input_silent"] is True
        assert hud._hud._effective_state() == hud.ERROR
        assert hud._hud._input_blocked_reason == "NO MIC SIGNAL"
    finally:
        hud.end_flash()
        hud.set_input_blocked(None)


def test_signal_returning_clears_both_the_flash_and_the_indicator(live):
    state, check = live["state"], live["check_signal"]
    hud.set_input_blocked(None)
    hud.end_flash()
    try:
        _go_frozen(live)
        check()
        assert hud._hud._input_blocked_reason == "NO MIC SIGNAL"

        for _ in range(main.INPUT_SIGNAL_FRAMES):
            live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)
        check()

        assert state["input_silent"] is False
        assert hud._hud._effective_state() != hud.ERROR
        assert hud._hud._input_blocked_reason is None
    finally:
        hud.end_flash()
        hud.set_input_blocked(None)


def test_recovery_does_not_claim_listening_again_while_still_gated(live):
    """
    Confirmed live as a real overlap, not a hypothetical one:
    KNOWN_LIMITATIONS.md's actual wedge incident records push-to-talk
    being tried *during* it. If the hotkey is still held the instant the
    mic itself recovers, the indicator must not flip to "nothing wrong"
    a beat before the hold actually ends.
    """
    state, check = live["state"], live["check_signal"]
    hud.set_input_blocked(None)
    hud.end_flash()
    try:
        _go_frozen(live)
        check()
        state["hotkey_active"] = True

        for _ in range(main.INPUT_SIGNAL_FRAMES):
            live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)
        check()

        assert state["input_silent"] is False
        # Flash still clears -- the wedge itself is genuinely over --
        # but the persistent reason is left for the routine gating
        # check to re-assert on its own very next pass, not silently
        # dropped to "nothing blocking" while the hotkey is still down.
        assert hud._hud._input_blocked_reason is not None
    finally:
        state["hotkey_active"] = False
        hud.end_flash()
        hud.set_input_blocked(None)


def test_real_audio_resets_the_frozen_value_tracking(live):
    # Otherwise a stretch of genuinely frozen frames before some real
    # speech would leave lo/hi equal and armed, and the next quiet
    # stretch would latch on evidence that had already been answered.
    state = live["state"]
    _go_frozen(live)
    assert state["quiet_peak_lo"] == state["quiet_peak_hi"]

    for _ in range(main.INPUT_SIGNAL_FRAMES):
        live["callback"](_frame(0.5), main.FRAME_SAMPLES, None, None)

    assert state["quiet_peak_lo"] is None and state["quiet_peak_hi"] is None


def test_a_paused_jarvis_never_latches(live):
    # The stream is deliberately closed while paused, so of course it is
    # silent. Latching here would mean every pause longer than the window
    # reported a hardware fault.
    state, check = live["state"], live["check_signal"]
    live["set_enabled"](False)
    state["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS * 10)

    check()

    assert state["input_silent"] is False


def test_resume_rearms_the_silence_tracker_before_reopening(live):
    # Without this, the first chunk drained after any pause longer than
    # the window would latch "no mic signal" instantly -- the same class
    # of bug that made resume trip the 5s watchdog on a brand-new stream.
    state = live["state"]
    live["set_enabled"](False)
    state["main_signal_at"] = time.time() - (main.INPUT_SILENCE_SECONDS * 10)
    state["input_silent"] = True

    live["set_enabled"](True)

    assert state["input_silent"] is False
    assert time.time() - state["main_signal_at"] < 5


def test_resume_clears_a_wedge_flash_and_indicator_left_over_from_before_the_pause(live):
    """
    _check_input_signal's own recovery path (which also clears these)
    never runs for this transition -- pausing and resuming resets
    input_silent directly. Without its own clearing, a wedge that was
    still latched (and therefore still ERROR-flashing) right before a
    pause would leave that flash stuck open-ended through the resume,
    with nothing left in the system that would ever call end_flash()
    again.
    """
    state = live["state"]
    hud.set_input_blocked(None)
    hud.end_flash()
    try:
        live["set_enabled"](False)
        state["input_silent"] = True
        hud.begin_flash(hud.ERROR)
        hud.set_input_blocked("NO MIC SIGNAL")

        live["set_enabled"](True)

        assert hud._hud._effective_state() != hud.ERROR
        assert hud._hud._input_blocked_reason is None
    finally:
        hud.end_flash()
        hud.set_input_blocked(None)


def test_signal_confirmation_requires_more_than_one_frame():
    # The cold-open glitch this exists to survive is transient by
    # definition -- one frame, maybe two. If this were 1, the fix would
    # be a no-op.
    assert main.INPUT_SIGNAL_FRAMES > 1


# --- the output gate: system audio, not just Jarvis's own voice --------


def test_output_gate_reflects_the_current_reading(live, monkeypatch):
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    assert live["check_output_gate"]() is True

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    assert live["check_output_gate"]() is False


def test_output_was_playing_latches_true_while_output_plays(live, monkeypatch):
    state, check = live["state"], live["check_output_gate"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)

    check()

    assert state["output_was_playing"] is True


def test_the_falling_edge_drains_the_stale_backlog(live, monkeypatch):
    # The regression this specifically guards against: unlike
    # is_speaking() (seconds), this gate can hold for the length of a
    # whole playlist -- without the drain, audio_q would burst-process
    # minutes of stale frames the instant output stops, at replay speed,
    # as if freshly captured.
    state, check, audio_q = live["state"], live["check_output_gate"], live["audio_q"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    check()
    for _ in range(5):
        audio_q.put(object())  # frames that arrived while "playing" gated them out

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    check()

    assert state["output_was_playing"] is False
    assert audio_q.empty()


def test_no_drain_fires_when_output_was_never_playing(live, monkeypatch):
    # A drain on every single quiet chunk (rather than only on the
    # transition) would needlessly throw away real queued audio during
    # perfectly ordinary silence.
    audio_q, check = live["audio_q"], live["check_output_gate"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    audio_q.put(object())

    check()

    assert audio_q.qsize() == 1


# --- echo cancellation: the gate only holds while it isn't covering -----


def _coverage(monkeypatch, covering, reason):
    monkeypatch.setattr(main.echo_canceller, "coverage", lambda: (covering, reason))


def test_output_playing_with_echo_cancellation_covering_is_not_gated(live, monkeypatch):
    # The whole point of echo_canceller.py: music playing, cancellation
    # demonstrably working -> frames go on to the wake word, no drain.
    state, check, audio_q = live["state"], live["check_output_gate"], live["audio_q"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    _coverage(monkeypatch, True, "covering")
    audio_q.put(object())

    assert check() is False
    assert state["output_was_playing"] is True
    assert audio_q.qsize() == 1


@pytest.mark.parametrize("reason", ["unavailable", "off", "converging", "residual_high", "unhealthy", "idle"])
def test_anything_short_of_covering_gates_exactly_as_before(live, monkeypatch, reason):
    # Fail closed: every non-covering state, including `jarvis aec off`,
    # is the old unconditional gate.
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    _coverage(monkeypatch, False, reason)
    assert live["check_output_gate"]() is True


def test_a_coverage_query_that_raises_gates_rather_than_lets_audio_through(live, monkeypatch):
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    monkeypatch.setattr(main.echo_canceller, "coverage", explode)
    assert live["check_output_gate"]() is True


def test_losing_and_regaining_coverage_mid_playback(live, monkeypatch):
    state, check, audio_q = live["state"], live["check_output_gate"], live["audio_q"]
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)

    _coverage(monkeypatch, True, "covering")
    assert check() is False
    _coverage(monkeypatch, False, "residual_high")
    assert check() is True
    for _ in range(5):
        audio_q.put(object())
    state["reset_acoustic"] = False
    _coverage(monkeypatch, True, "covering")
    assert check() is False

    # Regaining coverage is a gate falling edge like any other: drain the
    # backlog and restart the acoustic models, so the audio either side
    # of the gap isn't stitched together.
    assert audio_q.empty()
    assert state["reset_acoustic"] is True
    coverage_events = [kw["covering"] for kind, kw in events if kind == "aec_coverage"]
    assert coverage_events == [False, True]


def test_playback_stop_reports_how_much_of_it_was_deaf(live, monkeypatch):
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    check = live["check_output_gate"]

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    _coverage(monkeypatch, True, "covering")
    check()
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    check()

    stopped = [kw for kind, kw in events if kind == "output_playing_stopped"]
    assert stopped and stopped[0]["gated_seconds"] == 0.0


def test_covered_playback_stopping_resets_the_acoustic_models(live, monkeypatch):
    # The regression: with coverage, Jarvis's own reply never gates, so
    # the gate-release reset never fired and Silero carried the reply's
    # residual into the next listen -- calling the silence after it
    # speech, ~1s after every reply.
    state, check, audio_q = live["state"], live["check_output_gate"], live["audio_q"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    _coverage(monkeypatch, True, "covering")
    check()
    state["reset_acoustic"] = False
    audio_q.put(object())

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    check()

    assert state["reset_acoustic"] is True
    assert audio_q.qsize() == 1  # never held back, so nothing stale to drain


def test_no_acoustic_reset_when_output_was_never_playing(live, monkeypatch):
    state, check = live["state"], live["check_output_gate"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    state["reset_acoustic"] = False

    check()

    assert state["reset_acoustic"] is False


# Loud enough to clear the silence floor, so the echo-floor tests below
# exercise the echo floor and nothing else.
_SPEECH_LEVEL_CHUNK = np.full(main.FRAME_SAMPLES, 0.05, dtype=np.float32)


def test_session_speech_needs_to_clear_the_echo_floor_only_while_output_plays(live, monkeypatch):
    state, is_speech = live["state"], live["is_session_speech"]
    chunk = _SPEECH_LEVEL_CHUNK
    monkeypatch.setattr(main.echo_canceller, "above_echo_floor", lambda frame: False)

    state["output_was_playing"] = False
    assert is_speech(chunk, 0.9) is True  # nothing playing: VAD alone, as ever

    state["output_was_playing"] = True
    assert is_speech(chunk, 0.9) is False  # residual echo, not the user

    monkeypatch.setattr(main.echo_canceller, "above_echo_floor", lambda frame: True)
    assert is_speech(chunk, 0.9) is True
    assert is_speech(chunk, 0.0) is False  # the floor never overrides VAD saying no


def test_echo_floor_rejections_are_logged_but_rate_limited(live, monkeypatch):
    state, is_speech = live["state"], live["is_session_speech"]
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(main.echo_canceller, "above_echo_floor", lambda frame: False)
    state["output_was_playing"] = True
    chunk = _SPEECH_LEVEL_CHUNK

    for _ in range(50):
        is_speech(chunk, 0.9)

    rejected = [kw for kind, kw in events if kind == "echo_onset_rejected"]
    assert len(rejected) == 1
    assert state["echo_rejected_frames"] == 49  # banked for the next flush


def test_a_failing_echo_floor_check_refuses_the_frame(live, monkeypatch):
    state, is_speech = live["state"], live["is_session_speech"]

    def explode(frame):
        raise RuntimeError("boom")

    monkeypatch.setattr(main.echo_canceller, "above_echo_floor", explode)
    state["output_was_playing"] = True
    assert is_speech(_SPEECH_LEVEL_CHUNK, 0.9) is False


# --- the silence floor: digital silence is never session speech --------


_ONE_LSB = 1.0 / 32768


@pytest.mark.parametrize("output_playing", [False, True])
def test_digital_silence_is_never_session_speech_however_sure_silero_is(live, monkeypatch, output_playing):
    # The phantom recordings this exists for: raw peak 0.0 or 1-2 LSB,
    # every frame of them scored as speech by a Silero whose state still
    # held the residual of Jarvis's own reply.
    state, is_speech = live["state"], live["is_session_speech"]
    monkeypatch.setattr(main.echo_canceller, "above_echo_floor", lambda frame: True)
    state["output_was_playing"] = output_playing
    one_lsb = np.zeros(main.FRAME_SAMPLES, dtype=np.float32)
    one_lsb[::7] = _ONE_LSB
    one_lsb[3::7] = -_ONE_LSB

    assert is_speech(np.zeros(main.FRAME_SAMPLES, dtype=np.float32), 0.9) is False
    assert is_speech(one_lsb, 0.9) is False


def test_a_frame_just_above_the_silence_floor_is_left_to_the_vad(live):
    state, is_speech = live["state"], live["is_session_speech"]
    state["output_was_playing"] = False
    chunk = np.zeros(main.FRAME_SAMPLES, dtype=np.float32)
    chunk[0] = main.INPUT_SILENCE_FLOOR * 2

    assert is_speech(chunk, 0.9) is True
    assert is_speech(chunk, 0.0) is False


def test_silent_frame_refusals_are_logged_but_rate_limited(live, monkeypatch):
    state, is_speech = live["state"], live["is_session_speech"]
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    state["output_was_playing"] = False
    chunk = np.zeros(main.FRAME_SAMPLES, dtype=np.float32)

    for _ in range(50):
        is_speech(chunk, 0.9)
    is_speech(chunk, 0.0)  # Silero saying no is not a refusal worth counting

    rejected = [kw for kind, kw in events if kind == "silent_vad_rejected"]
    assert len(rejected) == 1
    assert state["silent_rejected_frames"] == 49  # banked for the next flush


# --- social intents and the acoustic gate on their fast-path seeds ------
#
# _handle_transcript here uses the *real* matcher (loaded for real as
# part of live's own boot sequence, the same as every other test in this
# file that reaches this far) -- these seeds are real enough to match
# for real, which is the point: this is the one class of fast-path
# intent that also has to survive an acoustic check, and that check has
# to see a real quality dict, not a mocked match result.

_CLEAN_QUALITY = {"no_speech_prob": 0.02, "avg_logprob": -0.30, "compression_ratio": 1.30, "clipped_fraction": 0.0}
_NOISE_QUALITY = {"no_speech_prob": 0.02, "avg_logprob": -1.40, "compression_ratio": 1.30, "clipped_fraction": 0.0}


def _stub_match(live, monkeypatch, intent, score, closest_seed):
    """
    live's own matcher is a deliberate trap (_FakeMatcher.match raises
    "not reached in this test") -- nothing before this exercised
    _handle_transcript at all. Overriding just the match() method on
    that same live instance, per test, is what lets these be the first.
    """
    result = types.SimpleNamespace(intent=intent, score=score, closest_seed=closest_seed)
    monkeypatch.setattr(live["matcher"], "match", lambda text: result)


def test_a_confident_thank_you_gets_a_reply(live, monkeypatch):
    spoken = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    _stub_match(live, monkeypatch, "social_thanks", 1.0, "thank you")

    should_close = live["handle_transcript"]("thank you", True, _CLEAN_QUALITY, via_session=True)

    assert should_close is False
    assert spoken and spoken[0] in main.phrases.PHRASES["social_thanks_reply"]


def test_a_low_confidence_thank_you_is_rejected_silently(live, monkeypatch):
    """
    The exact scenario _ACOUSTICALLY_GATED_SEEDS exists for: "thank you"
    is a confirmed near-silence Whisper hallucination (it's in
    _HALLUCINATION_DENYLIST for that reason), so a cosine match on it
    alone must not be trusted the way every other fast-path intent's
    match is.
    """
    spoken = []
    events = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    _stub_match(live, monkeypatch, "social_thanks", 0.95, "thank you")

    should_close = live["handle_transcript"]("thank you", False, _NOISE_QUALITY, via_session=True)

    assert should_close is False
    assert spoken == [], "a rejected candidate must produce no speech at all, not a quieter reply"
    # "transcript" is logged unconditionally before the gate ever runs
    # (see _handle_transcript_inner) -- the rejection event is somewhere
    # in the log, not necessarily first.
    rejected_events = [kw for kind, kw in events if kind == "social_intent_rejected"]
    assert rejected_events, "expected a social_intent_rejected event"
    assert rejected_events[0]["intent"] == "social_thanks"


def test_a_confident_okay_gets_acknowledged(live, monkeypatch):
    spoken = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    _stub_match(live, monkeypatch, "social_ack", 1.0, "okay")

    live["handle_transcript"]("okay", True, _CLEAN_QUALITY, via_session=True)

    assert spoken and spoken[0] in main.phrases.PHRASES["social_ack_reply"]


def test_a_confident_bye_closes_the_session_like_disable_listening(live, monkeypatch):
    """
    "bye" is folded into disable_listening's own seeds (see
    intent_matcher.py), not a separate farewell intent -- ending the
    conversation is what "bye" said to an assistant most naturally
    means, the same thing this intent already does.
    """
    spoken = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    _stub_match(live, monkeypatch, "disable_listening", 1.0, "bye")

    should_close = live["handle_transcript"]("bye", True, _CLEAN_QUALITY, via_session=True)

    assert should_close is True
    assert spoken and spoken[0] in main.phrases.PHRASES["session_close"]


def test_a_low_confidence_bye_does_not_close_a_real_session(live, monkeypatch):
    """The highest-stakes version of the gate: a hallucinated "bye" must not silently end a real conversation."""
    spoken = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    _stub_match(live, monkeypatch, "disable_listening", 0.95, "bye")

    should_close = live["handle_transcript"]("bye", False, _NOISE_QUALITY, via_session=True)

    assert should_close is False
    assert spoken == []


def test_the_gate_does_not_touch_an_ungated_fast_path_intent(live, monkeypatch):
    """
    Confirms the gate is narrowly scoped to _ACOUSTICALLY_GATED_SEEDS,
    not applied broadly -- an ordinary fast-path match on noise-quality
    audio must keep working exactly as it did before this existed.
    dispatch_llm_action is mocked here specifically because the real
    "mute" action taps a real volume key -- unlike the social intents
    above, this one really would dispatch, and a test must not actually
    change this machine's mute state to prove it.
    """
    spoken = []
    dispatched = []
    monkeypatch.setattr(main, "speak", lambda text: spoken.append(text))
    monkeypatch.setattr(main, "dispatch_llm_action", lambda parsed, text: dispatched.append(parsed) or actions.Outcome("Muted.", "ok"))
    _stub_match(live, monkeypatch, "toggle_mute", 0.9, "mute")

    live["handle_transcript"]("mute", True, _NOISE_QUALITY, via_session=True)

    assert dispatched and dispatched[0]["action"] == "toggle_mute"


# --- push-to-talk hold threshold ----------------------------------------
#
# Diagnosed directly from logs/session.jsonl: 215 of 216 logged
# push-to-talk presses produced no speech at all. Requiring
# HOTKEY_HOLD_THRESHOLD_SECONDS of hold before arming capture fixes
# that without costing the leading audio of a deliberate press -- see
# hotkey_pre_buffer's own comment in main.py.


def test_hotkey_pre_buffer_always_covers_the_full_hold_threshold():
    """
    HOTKEY_PRE_BUFFER_FRAMES exists only to guarantee this: whatever was
    said between press and confirmation must still be sitting in
    hotkey_pre_buffer when _confirm_hotkey_hold reads it. If this ever
    regresses (someone raises HOTKEY_HOLD_THRESHOLD_SECONDS again
    without this derived constant keeping pace), the start of every
    deliberate press gets silently clipped -- exactly the bug
    PRE_BUFFER_FRAMES was originally written to prevent, just
    reintroduced on the push-to-talk path instead.
    """
    buffered_seconds = main.HOTKEY_PRE_BUFFER_FRAMES * main.FRAME_SECONDS
    assert buffered_seconds > main.HOTKEY_HOLD_THRESHOLD_SECONDS


def test_a_tap_never_arms_capture_or_logs_anything(live, monkeypatch):
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    on_press, on_release, state = live["on_press"], live["on_release"], live["state"]

    on_press(None)
    on_release(None)  # released immediately -- well under the threshold

    assert state["hotkey_active"] is False
    assert events == [], "a tap must leave no trace at all, not a quieter one"


def test_a_tap_does_not_barge_in_on_a_reply(live, monkeypatch):
    """
    Confirmed as the actual risk, not a hypothetical one: on_press used
    to call interrupt_speech() unconditionally and instantly, so any
    stray tap while Jarvis was mid-reply would cut it off.
    """
    interrupted = []
    monkeypatch.setattr(main, "is_speaking", lambda: True)
    monkeypatch.setattr(main, "interrupt_speech", lambda: interrupted.append(True))
    on_press, on_release = live["on_press"], live["on_release"]

    on_press(None)
    on_release(None)

    assert interrupted == []


def test_a_confirmed_hold_arms_capture_and_seeds_from_the_pre_buffer(live):
    state, on_press = live["state"], live["on_press"]
    confirm, pre_buffer = live["confirm_hotkey_hold"], live["hotkey_pre_buffer"]
    # What would have accumulated in the pre-buffer during the hold-
    # confirmation window, if real audio had been arriving.
    pre_buffer.append(np.zeros(main.FRAME_SAMPLES, dtype=np.float32))
    pre_buffer.append(np.ones(main.FRAME_SAMPLES, dtype=np.float32))

    on_press(None)
    confirm()  # called directly -- see the real-timer test below for the wiring itself

    assert state["hotkey_active"] is True
    assert len(state["hotkey_frames"]) == 2
    assert len(pre_buffer) == 0, "the pre-buffer must be drained once its contents are handed off"


def test_a_confirmed_hold_barges_in_on_a_reply(live, monkeypatch):
    interrupted = []
    monkeypatch.setattr(main, "is_speaking", lambda: True)
    monkeypatch.setattr(main, "interrupt_speech", lambda: interrupted.append(True))
    on_press, confirm = live["on_press"], live["confirm_hotkey_hold"]

    on_press(None)
    confirm()

    assert interrupted == [True]


def test_confirming_after_release_is_a_no_op(live):
    """
    The race this project's own stream-lock incident is the precedent
    for: on_release runs on keyboard's callback thread, confirmation
    runs on the Timer's own thread, and a release landing before
    confirmation actually runs must leave nothing armed.
    """
    state, on_press, on_release = live["state"], live["on_press"], live["on_release"]
    confirm = live["confirm_hotkey_hold"]

    on_press(None)
    on_release(None)  # released before the (never-fired, in this test) timer would confirm
    confirm()  # simulates the timer firing anyway, racing behind the release

    assert state["hotkey_active"] is False


def test_os_auto_repeat_does_not_restart_the_hold_window(live):
    """
    Held keys fire repeated native press events on Windows -- a second
    on_press while already down must not push the confirmation window
    back out indefinitely, or a long hold could never arm at all.
    """
    state, on_press = live["state"], live["on_press"]
    on_press(None)
    first_pressed_at = state["hotkey_pressed_at"]

    on_press(None)  # simulated OS auto-repeat, key never actually released

    assert state["hotkey_pressed_at"] == first_pressed_at


def test_the_real_timer_confirms_a_hold_without_a_direct_call(live, monkeypatch):
    """
    The one test that exercises the actual threading.Timer wiring
    end-to-end rather than calling _confirm_hotkey_hold directly --
    everything above tests the logic; this catches a wiring mistake
    (wrong function passed to Timer, wrong attribute name) that a
    direct call would never see.
    """
    monkeypatch.setattr(main, "HOTKEY_HOLD_THRESHOLD_SECONDS", 0.01)
    state, on_press = live["state"], live["on_press"]

    on_press(None)
    for _ in range(50):  # up to ~0.5s -- generous headroom over the 0.01s threshold
        if state["hotkey_active"]:
            break
        time.sleep(0.01)

    assert state["hotkey_active"] is True


def test_the_real_timer_is_cancelled_by_a_release_before_it_fires(live, monkeypatch):
    monkeypatch.setattr(main, "HOTKEY_HOLD_THRESHOLD_SECONDS", 0.05)
    state, on_press, on_release = live["state"], live["on_press"], live["on_release"]

    on_press(None)
    on_release(None)  # released well before the 0.05s threshold
    time.sleep(0.15)  # past where the timer would have fired, had it not been cancelled

    assert state["hotkey_active"] is False


def test_output_gate_does_not_touch_hotkey_processing(live, monkeypatch):
    # Push-to-talk must keep working no matter what else is true --
    # same override this project already documents for is_speaking()
    # and pause. _check_output_gate only ever informs the session
    # listener's own skip condition (see its call site); it has no
    # opinion on hotkey_active at all.
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    live["state"]["hotkey_active"] = True

    # Calling the gate must not itself raise or touch hotkey state --
    # the two are wired together only at listener_loop's own combined
    # skip condition, not inside this closure.
    live["check_output_gate"]()

    assert live["state"]["hotkey_active"] is True


# --- _stream_lock: the race between two threads, not just two states --


def test_concurrent_reopen_and_pause_resume_never_leaves_two_streams_open(live):
    """
    Regression test for a real, previously-unsynchronized race: nothing
    guarded `stream` (main.py's shared, reassigned InputStream variable)
    against being touched by two threads at once --
    _reopen_input_stream runs on the listener thread (the 5s watchdog,
    and now the output-silence latch too), while _set_enabled
    (pause/resume) runs on the control-socket thread. Without a lock,
    a resume could see `stream is None` set by a reopen already in
    flight and open a *second* concurrent capture stream on the same
    device -- the first orphaned, never stopped, callback still
    attached. Confirmed this reliably reproduces without _stream_lock by
    temporarily removing it while writing this test; with it, the
    invariant below holds no matter how the two threads interleave.
    """
    reopen, set_enabled, streams = live["reopen"], live["set_enabled"], live["streams"]
    stop = threading.Event()
    errors = []

    def hammer_reopen():
        try:
            while not stop.is_set():
                reopen()
        except Exception as exc:  # pragma: no cover -- failure path only
            errors.append(exc)

    def hammer_pause_resume():
        try:
            while not stop.is_set():
                set_enabled(False)
                set_enabled(True)
        except Exception as exc:  # pragma: no cover -- failure path only
            errors.append(exc)

    t1 = threading.Thread(target=hammer_reopen, daemon=True)
    t2 = threading.Thread(target=hammer_pause_resume, daemon=True)
    t1.start()
    t2.start()
    time.sleep(0.5)
    stop.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"a stream operation raised under contention: {errors}"
    # Index 1 is hotkey_stream -- created once at boot and, by design,
    # never touched by reopen/pause/resume (push-to-talk keeps working
    # regardless of pause state; see _set_enabled's own docstring). It
    # stays started-and-not-closed for the process's whole life, same
    # assumption test_pause_shuts_the_main_stream_and_resets_the_session_machine
    # already relies on -- excluded here so it isn't mistaken for a
    # second concurrently-open *main* stream.
    open_count = sum(1 for i, s in enumerate(streams) if i != 1 and s.started and not s.closed)
    assert open_count <= 1, f"{open_count} main streams left open concurrently -- _stream_lock isn't serializing access"


def test_output_gate_transitions_are_logged_structurally(live, monkeypatch):
    # Not just a HUD line -- this project's own stated rule
    # (CLAUDE.md: "Structured event logging goes to logs/session.jsonl
    # regardless of what the HUD chooses to display") applies here just
    # as much as it does to wake_triggered/input_silent: a HUD-only
    # trace leaves nobody able to reconstruct "why didn't hey jarvis
    # work just now" after the fact.
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    check = live["check_output_gate"]

    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    check()
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: False)
    check()

    kinds = [k for k, _ in events]
    assert kinds == ["output_playing_started", "output_playing_stopped"]
    assert "seconds" in events[1][1]


# --- durable per-stage timings -----------------------------------------
#
# _TIMED_STAGES used to live only in an in-memory rolling deque (see
# state["stats"]["timings"]), which `jarvis stats` reads but which dies
# at every restart -- 40 `shutdown` records in the real log meant 40
# discarded sessions' worth of where-the-time-went data. current_timings
# is the durable half: a per-utterance snapshot, logged once into that
# utterance's own "outcome" event.


def test_record_timing_populates_the_current_utterance_snapshot(live):
    state, record_timing = live["state"], live["record_timing"]
    state["current_timings"] = {}

    record_timing("transcribe", 0.42)
    record_timing("match", 0.01)

    assert state["current_timings"] == {"transcribe": 0.42, "match": 0.01}
    # The rolling per-stage view jarvis stats reads from keeps working
    # unchanged alongside it.
    assert list(state["stats"]["timings"]["transcribe"]) == [0.42]


def test_record_stat_logs_the_current_utterance_timings(live, monkeypatch):
    state, record_stat = live["state"], live["record_stat"]
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    state["current_timings"] = {"transcribe": 0.4567, "action": 0.001}

    record_stat("fast_path", "open chrome", "focus_or_launch_app:chrome")

    kind, payload = events[-1]
    assert kind == "outcome"
    # Rounded for log readability, not truncated to uselessness --
    # fast-path stages can be single-digit milliseconds.
    assert payload["timings"] == {"transcribe": 0.457, "action": 0.001}


def test_record_stat_logs_an_empty_timings_dict_when_nothing_was_timed(live, monkeypatch):
    """
    A meta/off-duty/no-match exit never calls record_timing at all --
    the field should still be present and well-formed (an empty dict),
    not missing or raising.
    """
    state, record_stat = live["state"], live["record_stat"]
    events = []
    monkeypatch.setattr(main, "_log_event", lambda kind, **kw: events.append((kind, kw)))
    state["current_timings"] = {}

    record_stat("off_duty", "hey jarvis")

    kind, payload = events[-1]
    assert payload["timings"] == {}


def test_pause_clears_stale_output_gate_bookkeeping(live, monkeypatch):
    # A pause spanning a stretch of music would otherwise resume still
    # believing output was playing, and fire a spurious drain plus a
    # "System audio stopped" line on the first chunk after resume.
    state = live["state"]
    monkeypatch.setattr(main.output_monitor, "is_output_playing", lambda: True)
    live["check_output_gate"]()
    assert state["output_was_playing"] is True

    live["set_enabled"](False)
    live["set_enabled"](True)

    assert state["output_was_playing"] is False


def test_the_output_gate_is_evaluated_on_both_listener_paths():
    # Regression for a real gap found live: _check_output_gate used to be
    # called only after a chunk was successfully dequeued, so with an
    # empty queue -- a wedged capture device, or a paused Jarvis -- the
    # gate silently froze at whatever it last saw, and a tone played in
    # that state produced no transition at all. What the speakers are
    # doing has nothing to do with whether the mic is delivering
    # anything, so both of listener_loop's paths have to evaluate it.
    #
    # Checked against the source because listener_loop is a closure this
    # fixture deliberately never runs (see the module docstring) -- the
    # two call sites are the property worth pinning, not the wording
    # around them.
    import inspect

    src = inspect.getsource(main.main)
    assert src.count("_check_output_gate()") >= 2, (
        "both the queue-empty path and the chunk path must evaluate the output gate"
    )


# --- crash containment: the listener thread must never just vanish ----


def test_everything_before_the_main_try_is_individually_contained():
    """
    listener_loop's big try/except only starts at the mode-dispatch
    block. Every step between the queue read and that try -- the input
    signal check, the deep-sleep unload, the output gate -- therefore
    runs uncontained, and an exception in any of them kills the listener
    thread outright.

    That is not hypothetical. The output gate was first added here
    without its own try, and a live run died ~5 seconds after boot on
    the gate's falling edge: thread gone, process still up, `jarvis
    status` still reporting "Wake-word armed", nothing listening at all
    -- the precise failure the main except block's own comment says must
    never happen again.
    """
    import inspect

    src = inspect.getsource(main.main)
    body = src.split("except queue.Empty:")[1].split("            try:\n                if state[\"mode\"] == \"wake\":")[0]
    for call in ("_check_input_signal()", "_check_output_gate()", "_unload_heavy_models()"):
        assert call in body, f"{call} moved -- this guard needs updating"
    # Each of the three is preceded by its own `try:` somewhere in the
    # uncontained region; the region must contain at least that many.
    assert body.count("try:") >= 3, (
        "a step between the queue read and the main try is no longer individually "
        "contained -- an exception in it would kill the listener thread"
    )


def test_a_dying_thread_is_logged_rather_than_vanishing():
    # The net that would have caught the above immediately. Python's
    # default hook prints to stderr, which under `jarvis start` goes to a
    # terminal nobody reads.
    logged = []

    class _Args:
        thread = type("T", (), {"name": "listener"})()
        exc_type = ValueError
        exc_value = ValueError("boom")
        exc_traceback = None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "_log_event", lambda kind, **kw: logged.append((kind, kw)))
        mp.setattr(main.hud, "line", lambda *a, **k: None)
        main._log_dead_thread(_Args())

    assert logged and logged[0][0] == "thread_died"
    assert logged[0][1]["thread"] == "listener"
    assert logged[0][1]["exc_type"] == "ValueError"
