"""
Tests for echo_canceller.py -- the coverage state machine, the echo
floor check, and Canceller's fail-closed handling, against a fake APM
and a fake reference. livekit is never imported here (the real module
loads it lazily, only in _LivekitApm), and nothing touches audio or COM.
"""

from collections import deque

import numpy as np
import pytest

import echo_canceller as ec
from echo_canceller import APM_FRAME_SAMPLES, CoverageTracker, clears_echo_floor

BLOCK = APM_FRAME_SAMPLES * 8  # main.py's 80ms FRAME_SAMPLES


class _FakeApm:
    """Cancels perfectly unless told otherwise: capture() returns `gain` x input."""

    def __init__(self, gain=0.0):
        self.gain = gain
        self.reversed = 0
        self.closed = False

    def reverse(self, frame):
        self.reversed += 1

    def capture(self, frame):
        return (frame * self.gain).astype(np.int16)

    def close(self):
        self.closed = True


class _RaisingApm(_FakeApm):
    def capture(self, frame):
        raise RuntimeError("native failure")


class _FakeReference:
    def __init__(self):
        self.frames = deque()
        self.gen = 1
        self.healthy = True

    def feed(self, amplitude, count=8):
        for _ in range(count):
            self.frames.append(np.full(APM_FRAME_SAMPLES, amplitude, dtype=np.float32))

    def pop_frame(self):
        return self.frames.popleft() if self.frames else None

    def pending(self):
        return len(self.frames)

    def drop_oldest(self, count):
        for _ in range(count):
            self.frames.popleft()

    def generation(self):
        return self.gen

    def is_healthy(self):
        return self.healthy

    def endpoint_name(self):
        return "fake speakers"


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _fixed_aligner(hold_back_ms=0.0):
    # No hold-back by default, and never re-measures: the constant
    # synthetic frames below aren't audio GCC-PHAT can say anything
    # meaningful about. DelayAligner has its own tests further down.
    return ec.DelayAligner(initial_hold_back_ms=hold_back_ms, min_strength=float("inf"))


def _canceller(apm_cls=_FakeApm, aligner=None):
    reference, clock = _FakeReference(), _Clock()
    canceller = ec.Canceller(apm_cls, reference, clock=clock, aligner=aligner or _fixed_aligner())
    canceller.open()
    return canceller, reference, clock


def _run(canceller, reference, clock, seconds, reference_amplitude=0.1, mic_amplitude=0.1):
    out = None
    for _ in range(round(seconds / 0.08)):
        if reference_amplitude:
            reference.feed(reference_amplitude)
        out = canceller.process(np.full(BLOCK, mic_amplitude, dtype=np.float32))
        clock.now += 0.08
    return out


# --- CoverageTracker ----------------------------------------------------


def test_tracker_is_idle_before_any_reference():
    tracker = CoverageTracker()
    tracker.update(0.0, False, -90.0)
    assert tracker.state(0.0) == "idle"


def test_tracker_converges_before_it_covers():
    tracker = CoverageTracker(converge_seconds=1.5, residual_max_dbfs=-50.0, min_blocks=5)
    t = 0.0
    for _ in range(10):  # 0.8s of quiet residual -- quiet, but not converged yet
        tracker.update(t, True, -70.0)
        t += 0.08
    assert tracker.state(t) == "converging"
    for _ in range(10):
        tracker.update(t, True, -70.0)
        t += 0.08
    assert tracker.state(t) == "covering"


def test_tracker_reports_residual_high_when_cancellation_is_poor():
    tracker = CoverageTracker(converge_seconds=0.0, residual_max_dbfs=-50.0, min_blocks=5)
    t = 0.0
    for _ in range(20):
        tracker.update(t, True, -35.0)  # barely cancelled at all
        t += 0.08
    assert tracker.state(t) == "residual_high"


def test_user_talking_over_the_music_does_not_flip_coverage():
    # The low percentile is the point: the user's own speech is loud by
    # design and must not read as "the canceller stopped working"
    # halfway through the very command it's there to let through.
    tracker = CoverageTracker(converge_seconds=0.0, residual_max_dbfs=-50.0, window_seconds=4.0, min_blocks=5)
    t = 0.0
    for _ in range(50):  # 4s of well-cancelled music
        tracker.update(t, True, -68.0)
        t += 0.08
    for _ in range(25):  # then 2s of the user talking over it
        tracker.update(t, True, -30.0)
        t += 0.08
    assert tracker.state(t) == "covering"


def test_tracker_hysteresis_holds_coverage_through_a_small_rise():
    tracker = CoverageTracker(converge_seconds=0.0, residual_max_dbfs=-50.0, hysteresis_db=6.0, min_blocks=5)
    t = 0.0
    for _ in range(20):
        tracker.update(t, True, -60.0)
        t += 0.08
    assert tracker.state(t) == "covering"
    for _ in range(60):  # a louder passage: above the entry bar, inside the hysteresis
        tracker.update(t, True, -47.0)
        t += 0.08
    assert tracker.state(t) == "covering"
    for _ in range(60):  # well past it
        tracker.update(t, True, -40.0)
        t += 0.08
    assert tracker.state(t) == "residual_high"


def test_tracker_goes_idle_once_the_reference_stops():
    tracker = CoverageTracker(converge_seconds=0.0, min_blocks=1, idle_seconds=2.0)
    tracker.update(0.0, True, -70.0)
    assert tracker.state(0.0) == "covering"
    assert tracker.state(2.5) == "idle"


def test_tracker_reset_requires_reconvergence():
    tracker = CoverageTracker(converge_seconds=1.0, min_blocks=1)
    t = 0.0
    for _ in range(20):
        tracker.update(t, True, -70.0)
        t += 0.08
    assert tracker.state(t) == "covering"
    tracker.reset()
    tracker.update(t, True, -70.0)
    assert tracker.state(t) == "converging"


# --- echo floor ------------------------------------------------------------


def test_no_floor_means_no_extra_requirement():
    assert clears_echo_floor(np.zeros(BLOCK, dtype=np.float32), None) is True


def test_quiet_residual_does_not_clear_the_floor_but_a_near_voice_does():
    floor = -60.0
    residual = np.full(BLOCK, 10 ** (-58 / 20), dtype=np.float32)  # ~-58 dBFS
    voice = np.full(BLOCK, 10 ** (-30 / 20), dtype=np.float32)  # ~-30 dBFS
    assert clears_echo_floor(residual, floor, margin_db=12.0) is False
    assert clears_echo_floor(voice, floor, margin_db=12.0) is True


# --- Canceller -------------------------------------------------------------


def test_raw_audio_passes_through_untouched_while_nothing_plays():
    # Wake word, VAD and Whisper were tuned on the raw signal; with
    # nothing playing they must keep getting exactly that.
    canceller, reference, clock = _canceller()
    block = np.full(BLOCK, 0.1, dtype=np.float32)
    out = canceller.process(block)
    assert out is block


def test_cleaned_audio_is_returned_while_the_reference_is_active():
    canceller, reference, clock = _canceller()
    out = _run(canceller, reference, clock, seconds=0.5)
    assert np.all(out == 0.0)  # the fake APM cancels perfectly


def test_covers_once_converged_with_low_residual():
    canceller, reference, clock = _canceller()
    _run(canceller, reference, clock, seconds=ec.AEC_CONVERGE_SECONDS + 0.5)
    assert canceller.coverage() == (True, "covering")


def test_poor_cancellation_never_covers():
    canceller, reference, clock = _canceller(lambda: _FakeApm(gain=1.0))
    _run(canceller, reference, clock, seconds=ec.AEC_CONVERGE_SECONDS + 1.0)
    assert canceller.coverage() == (False, "residual_high")


def test_an_apm_failure_returns_raw_audio_and_fails_closed():
    canceller, reference, clock = _canceller(_RaisingApm)
    reference.feed(0.1)
    block = np.full(BLOCK, 0.1, dtype=np.float32)
    assert canceller.process(block) is block
    assert canceller.coverage() == (False, "unhealthy")
    assert "native failure" in canceller.stats()["error"]


def test_an_unhealthy_reference_fails_closed():
    canceller, reference, clock = _canceller()
    _run(canceller, reference, clock, seconds=ec.AEC_CONVERGE_SECONDS + 0.5)
    reference.healthy = False
    assert canceller.coverage() == (False, "unhealthy")


def test_an_odd_sized_block_is_returned_raw_rather_than_mangled():
    canceller, reference, clock = _canceller()
    block = np.full(APM_FRAME_SAMPLES * 3 + 7, 0.1, dtype=np.float32)
    assert canceller.process(block) is block
    assert canceller.coverage()[0] is False


def test_a_new_output_device_restarts_convergence_with_a_fresh_apm():
    created = []

    def factory():
        apm = _FakeApm()
        created.append(apm)
        return apm

    canceller, reference, clock = _canceller(factory)
    _run(canceller, reference, clock, seconds=ec.AEC_CONVERGE_SECONDS + 0.5)
    assert canceller.coverage()[0] is True

    reference.gen += 1  # loopback reopened on a different endpoint
    _run(canceller, reference, clock, seconds=0.16)

    assert canceller.coverage() == (False, "converging")
    assert len(created) == 2 and created[0].closed


def test_a_reference_queue_that_ran_deep_is_trimmed_all_the_way_back():
    # Back to the hold-back, not to hold-back + slack: every frame left
    # queued makes the reference that much later than its own echo. The
    # spike's first laptop-speaker runs had it arriving ~125ms *after*
    # the echo this way, and AEC3 managed 6dB.
    canceller, reference, clock = _canceller()
    reference.feed(0.1, count=8 + ec.REFERENCE_SLACK_FRAMES + 40)
    canceller.process(np.zeros(BLOCK, dtype=np.float32))
    assert reference.pending() == 0


def test_ordinary_reader_jitter_does_not_trigger_a_trim():
    canceller, reference, clock = _canceller()
    reference.feed(0.1, count=8 + ec.REFERENCE_SLACK_FRAMES)
    canceller.process(np.zeros(BLOCK, dtype=np.float32))
    assert reference.pending() == ec.REFERENCE_SLACK_FRAMES


def test_the_reference_is_held_back_when_the_echo_comes_late():
    # A 220ms hold-back -> 22 frames. Nothing reaches the canceller until
    # the queue is deeper than that; from then on it's a fixed offset,
    # one frame per frame.
    apm = _FakeApm()
    canceller, reference, clock = _canceller(lambda: apm, aligner=_fixed_aligner(220))
    reference.feed(0.1, count=22)
    canceller.process(np.zeros(BLOCK, dtype=np.float32))
    assert apm.reversed == 0

    reference.feed(0.1, count=8)
    canceller.process(np.zeros(BLOCK, dtype=np.float32))
    assert apm.reversed == 8
    assert reference.pending() == 22


def test_disabled_means_raw_audio_and_reason_off():
    canceller, reference, clock = _canceller()
    canceller.set_enabled(False)
    reference.feed(0.1)
    block = np.full(BLOCK, 0.1, dtype=np.float32)
    assert canceller.process(block) is block
    assert canceller.coverage() == (False, "off")


def test_echo_floor_is_only_reported_while_the_reference_is_active():
    canceller, reference, clock = _canceller(lambda: _FakeApm(gain=0.001))
    assert canceller.echo_floor_dbfs() is None
    _run(canceller, reference, clock, seconds=1.0)
    assert canceller.echo_floor_dbfs() is not None
    clock.now += ec.IDLE_PASSTHROUGH_SECONDS + 1
    assert canceller.echo_floor_dbfs() is None


# --- DelayAligner / GCC-PHAT ----------------------------------------------


def _noise(seconds, seed=0):
    return np.random.default_rng(seed).standard_normal(int(seconds * ec.SAMPLE_RATE)).astype(np.float32) * 0.1


def test_gcc_phat_finds_a_known_delay():
    reference = _noise(2.0)
    lag = int(0.25 * ec.SAMPLE_RATE)
    mic = np.concatenate([np.zeros(lag, dtype=np.float32), reference[:-lag]]) * 0.3
    lag_ms, strength = ec.gcc_phat_lag_ms(mic, reference)
    assert abs(lag_ms - 250.0) < 1.0
    assert strength > ec.DELAY_MIN_STRENGTH


def test_gcc_phat_reports_no_clear_peak_for_unrelated_audio():
    _lag_ms, strength = ec.gcc_phat_lag_ms(_noise(2.0, seed=1), _noise(2.0, seed=2))
    assert strength < ec.DELAY_MIN_STRENGTH


def _feed_aligner(aligner, mic, reference):
    for start in range(0, len(mic), APM_FRAME_SAMPLES):
        aligner.record(mic[start:start + APM_FRAME_SAMPLES], reference[start:start + APM_FRAME_SAMPLES], True)


def _delayed(reference, ms):
    lag = int(ms / 1000 * ec.SAMPLE_RATE)
    return np.concatenate([np.zeros(lag, dtype=np.float32), reference[:-lag]])


def test_a_short_echo_path_needs_no_hold_back():
    # The laptop-speaker case: the echo lands ~75ms after the reference
    # with nothing held back -- already where AEC3 wants it.
    aligner = ec.DelayAligner()
    reference = _noise(ec.DELAY_WINDOW_SECONDS)
    _feed_aligner(aligner, _delayed(reference, 75), reference)
    assert aligner.maybe_update() is False
    assert aligner.hold_back_ms == 0.0
    assert abs(aligner.last_residual_ms - 75) < 1.0


def test_a_late_echo_is_held_back_to_the_target_residual():
    # A Bluetooth-speaker-shaped case: echo 330ms after the reference.
    # Hold back 250 so AEC3 is left the target 80.
    aligner = ec.DelayAligner()
    reference = _noise(ec.DELAY_WINDOW_SECONDS)
    _feed_aligner(aligner, _delayed(reference, 330), reference)
    assert aligner.maybe_update() is True
    assert abs(aligner.hold_back_ms - 250) < 2.0
    assert aligner.backlog_frames == 25


def test_an_echo_that_beats_its_reference_shrinks_the_hold_back():
    # Held back too far: the echo now arrives *before* the reference
    # AEC3 is given (negative residual). Back off by the full error.
    aligner = ec.DelayAligner(initial_hold_back_ms=300)
    reference = _noise(ec.DELAY_WINDOW_SECONDS)
    lag = int(0.1 * ec.SAMPLE_RATE)
    mic = np.concatenate([reference[lag:], np.zeros(lag, dtype=np.float32)])  # mic leads by 100ms
    _feed_aligner(aligner, mic, reference)
    assert aligner.maybe_update() is True
    assert abs(aligner.hold_back_ms - 120) < 2.0  # 300 + (-100 - 80)


def test_aligner_ignores_a_window_with_no_clear_echo():
    # Headphones, or the user talking: no echo to measure, so the
    # hold-back it already has stands.
    aligner = ec.DelayAligner(initial_hold_back_ms=200)
    _feed_aligner(aligner, _noise(ec.DELAY_WINDOW_SECONDS, seed=3), _noise(ec.DELAY_WINDOW_SECONDS, seed=4))
    assert aligner.maybe_update() is False
    assert aligner.hold_back_ms == 200


def test_aligner_does_not_chase_jitter():
    aligner = ec.DelayAligner(initial_hold_back_ms=200)
    reference = _noise(ec.DELAY_WINDOW_SECONDS)
    _feed_aligner(aligner, _delayed(reference, 95), reference)  # 15ms off target
    assert aligner.maybe_update() is False
    assert aligner.hold_back_ms == 200

def test_aligner_waits_for_a_full_window_of_active_reference():
    aligner = ec.DelayAligner()
    reference = _noise(ec.DELAY_WINDOW_SECONDS / 2)
    _feed_aligner(aligner, reference, reference)
    assert aligner.maybe_update() is False


def test_realignment_restarts_convergence():
    class _OneShotAligner(ec.DelayAligner):
        def maybe_update(self):
            return True

    canceller, reference, clock = _canceller(aligner=_OneShotAligner())
    _run(canceller, reference, clock, seconds=0.5)
    assert canceller.coverage() == (False, "converging")


# --- the module-level instance, when it never started -------------------


@pytest.fixture
def no_canceller(monkeypatch):
    monkeypatch.setattr(ec, "_canceller", None)


def test_without_a_canceller_everything_is_a_safe_no_op(no_canceller):
    block = np.full(BLOCK, 0.1, dtype=np.float32)
    assert ec.process(block) is block
    assert ec.coverage() == (False, "unavailable")
    assert ec.above_echo_floor(block) is True
    assert ec.is_enabled() is False
    assert ec.stats() == {"state": "unavailable"}


def test_start_reports_a_failed_apm_instead_of_raising(monkeypatch, no_canceller):
    def broken():
        raise ImportError("no livekit here")

    started = []
    monkeypatch.setattr(ec, "_LivekitApm", broken)
    monkeypatch.setattr(ec.loopback, "start", lambda: started.append(True))

    reason = ec.start()

    assert "no livekit here" in reason
    assert ec.coverage() == (False, "unavailable")
    assert not started  # no point capturing a reference nothing will use
