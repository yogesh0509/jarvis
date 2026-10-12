"""
Tests for audio_endpoint._clamp_scalar -- the one piece of decision
logic in that module kept COM-free, same reasoning as
test_output_monitor.py: no audio device is assumed present (CLAUDE.md),
so nothing that actually calls IAudioEndpointVolume is exercised here.
"""

from audio_endpoint import _clamp_scalar


def test_in_range_value_is_unchanged():
    assert _clamp_scalar(0.5) == 0.5


def test_above_range_clamps_to_one():
    assert _clamp_scalar(1.4) == 1.0


def test_below_range_clamps_to_zero():
    assert _clamp_scalar(-0.3) == 0.0


def test_boundary_values_pass_through():
    assert _clamp_scalar(0.0) == 0.0
    assert _clamp_scalar(1.0) == 1.0
