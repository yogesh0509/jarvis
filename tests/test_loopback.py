"""
Tests for loopback.py's pure helpers -- Framer and GapFiller, the two
pieces with logic worth pinning. The WASAPI stream itself needs a real
render device, which the test environment deliberately doesn't have.
"""

import numpy as np

import loopback
from loopback import FRAME_SAMPLES, Framer, GapFiller


def test_framer_emits_only_whole_frames_and_carries_the_rest():
    framer = Framer()
    frames = framer.push(np.ones(FRAME_SAMPLES * 2 + 50, dtype=np.float32))
    assert len(frames) == 2
    assert all(len(f) == FRAME_SAMPLES for f in frames)

    # The 50 left over must lead the next frame, not be dropped -- a
    # reference timeline that silently loses samples at every packet
    # boundary drifts against the mic for good.
    frames = framer.push(np.zeros(FRAME_SAMPLES - 50, dtype=np.float32))
    assert len(frames) == 1
    assert np.all(frames[0][:50] == 1.0)
    assert np.all(frames[0][50:] == 0.0)


def test_framer_preserves_sample_order_across_odd_sized_pushes():
    framer = Framer()
    source = np.arange(FRAME_SAMPLES * 5, dtype=np.float32)
    out = []
    for size in (7, 333, 1, 160, 299):
        start = sum(len(f) for f in out) + len(framer._carry)
        out.extend(framer.push(source[start:start + size]))
    assert np.array_equal(np.concatenate(out), source[: len(out) * FRAME_SAMPLES])


def test_framer_reset_discards_the_carry():
    framer = Framer()
    framer.push(np.ones(FRAME_SAMPLES - 1, dtype=np.float32))
    framer.reset()
    assert framer.push(np.zeros(FRAME_SAMPLES - 1, dtype=np.float32)) == []


def test_gap_filler_stays_quiet_while_packets_keep_arriving():
    gaps = GapFiller(fill_after_seconds=0.03)
    gaps.on_data(10.0)
    assert gaps.silence_due(10.02) == 0


def test_gap_filler_synthesizes_the_whole_gap_once_the_endpoint_goes_idle():
    gaps = GapFiller(sample_rate=16000, fill_after_seconds=0.03)
    gaps.on_data(10.0)
    assert gaps.silence_due(10.5) == 8000
    # Already accounted for -- the next poll only owes what's new.
    assert gaps.silence_due(10.51) == 0
    assert abs(gaps.silence_due(10.6) - 1600) <= 1


def test_gap_filler_does_not_drift_short_over_many_fills():
    # int() truncation per fill would shave a fraction of a sample each
    # time if the mark jumped to "now"; advancing by exactly what was
    # emitted keeps the total honest.
    gaps = GapFiller(sample_rate=16000, fill_after_seconds=0.03)
    gaps.on_data(0.0)
    total = 0
    now = 0.0
    for _ in range(1000):
        now += 0.0333
        total += gaps.silence_due(now)
    assert abs(total - now * 16000) <= 1


def test_gap_filler_first_poll_only_sets_the_mark():
    gaps = GapFiller()
    assert gaps.silence_due(5.0) == 0


def test_pending_reference_is_bounded():
    # While nothing consumes it (paused, or AEC switched off), the
    # reference must not grow without limit.
    assert loopback._frames.maxlen == loopback.MAX_PENDING_FRAMES
