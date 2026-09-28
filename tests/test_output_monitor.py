"""
Tests for output_monitor.EdgeDebouncer -- the one piece of decision
logic in output_monitor.py, kept COM-free and testable in isolation for
exactly the reason its own docstring gives. No audio device, no COM, no
background thread -- just a state machine fed synthetic peak sequences.
"""

from output_monitor import EdgeDebouncer

THRESHOLD = 0.01


def test_a_single_loud_poll_does_not_immediately_declare_playing():
    # Same reasoning as main.py's SPEECH_ONSET_FRAMES: one reading is
    # not evidence, real output sustains across polls.
    gate = EdgeDebouncer(on_polls=3, off_polls=20)
    assert gate.update(1.0, THRESHOLD) is False


def test_sustained_loud_polls_declare_playing():
    gate = EdgeDebouncer(on_polls=3, off_polls=20)
    for _ in range(2):
        gate.update(1.0, THRESHOLD)
    assert gate.playing is False  # not yet -- one short of on_polls
    assert gate.update(1.0, THRESHOLD) is True


def test_a_broken_run_of_loud_polls_never_declares_playing():
    # Interleaving a quiet poll resets the run -- an intermittent spike
    # should not count any more than one loud poll alone does.
    gate = EdgeDebouncer(on_polls=3, off_polls=20)
    gate.update(1.0, THRESHOLD)
    gate.update(1.0, THRESHOLD)
    gate.update(0.0, THRESHOLD)  # breaks the run
    gate.update(1.0, THRESHOLD)
    assert gate.playing is False


def test_playing_holds_through_a_brief_quiet_passage():
    # The asymmetric-debounce case this exists for: a quiet beat or the
    # gap between songs must not immediately reopen the gate, or every
    # song with any dynamic range at all would flap it open and shut.
    gate = EdgeDebouncer(on_polls=3, off_polls=20)
    for _ in range(3):
        gate.update(1.0, THRESHOLD)
    assert gate.playing is True

    for _ in range(19):  # one short of off_polls
        gate.update(0.0, THRESHOLD)
    assert gate.playing is True

    assert gate.update(0.0, THRESHOLD) is False


def test_playing_clears_after_sustained_silence():
    gate = EdgeDebouncer(on_polls=3, off_polls=5)
    for _ in range(3):
        gate.update(1.0, THRESHOLD)
    assert gate.playing is True

    for _ in range(5):
        gate.update(0.0, THRESHOLD)
    assert gate.playing is False


def test_peak_exactly_at_threshold_does_not_count_as_output():
    # Strictly-greater-than, matching main.py's INPUT_SILENCE_FLOOR
    # comparison style -- a boundary reading is not evidence either way,
    # and treating "== threshold" as playing would mean a threshold of
    # 0 (a plausible fully-quiet reading) always counted as playing.
    gate = EdgeDebouncer(on_polls=1, off_polls=1)
    assert gate.update(THRESHOLD, THRESHOLD) is False


def test_on_and_off_poll_counts_are_never_zero():
    # round(ON_SECONDS / POLL_SECONDS) could floor to 0 for a small
    # enough ON_SECONDS -- max(1, ...) in the constructor is what keeps
    # that from silently becoming "any single poll declares playing".
    gate = EdgeDebouncer(on_polls=0, off_polls=0)
    assert gate.on_polls == 1
    assert gate.off_polls == 1


def test_the_release_debounce_outlasts_the_engage_debounce():
    # The asymmetry is the point -- see output_monitor.py's own comment:
    # engaging fast matters (catch loud audio before it can false-
    # trigger a wake word), releasing slow matters more (bridge an
    # ordinary quiet beat without flapping).
    import output_monitor

    assert output_monitor.OFF_SECONDS > output_monitor.ON_SECONDS
