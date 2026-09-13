"""
Tests for tts.py's is_speaking()/interrupt() bookkeeping -- the
barge-in state machine. Deliberately doesn't load the real Piper voice
or open a real audio device (both are per-machine, gitignored
resources) -- covers the pure counting/id/threshold logic around
playback, not actual audio output.
"""

import pytest

import tts


@pytest.fixture(autouse=True)
def _reset_tts_state():
    tts._speaking_count = 0
    tts._next_utterance_id = 0
    tts._interrupted_up_to_id = 0
    tts._output_stream = None
    yield
    tts._speaking_count = 0
    tts._next_utterance_id = 0
    tts._interrupted_up_to_id = 0
    tts._output_stream = None


def test_is_speaking_reflects_the_in_flight_count():
    assert tts.is_speaking() is False
    tts._begin_speaking()
    assert tts.is_speaking() is True
    tts._begin_speaking()
    assert tts.is_speaking() is True  # still true with two overlapping calls in flight
    tts._end_speaking()
    assert tts.is_speaking() is True
    tts._end_speaking()
    assert tts.is_speaking() is False


def test_begin_speaking_returns_increasing_ids():
    first = tts._begin_speaking()
    second = tts._begin_speaking()
    assert second > first


def test_end_speaking_never_goes_negative():
    tts._end_speaking()
    tts._end_speaking()
    assert tts.is_speaking() is False


def test_interrupt_clears_speaking_state():
    tts._begin_speaking()
    tts._begin_speaking()
    assert tts.is_speaking() is True

    tts.interrupt()

    assert tts.is_speaking() is False


def test_interrupt_raises_the_threshold_to_the_latest_issued_id():
    utterance_id = tts._begin_speaking()
    tts.interrupt()
    assert tts._interrupted_up_to_id == utterance_id


def test_a_new_utterance_issued_after_an_interrupt_is_not_itself_interrupted():
    first_id = tts._begin_speaking()
    tts.interrupt()
    assert first_id <= tts._interrupted_up_to_id

    second_id = tts._begin_speaking()
    assert second_id > tts._interrupted_up_to_id


def test_a_backlog_of_already_queued_ids_all_count_as_interrupted():
    # The bug this guards: an earlier design recorded only a single
    # exact "this one id is interrupted" value. Several speak() calls
    # queued up close together (e.g. rapid press/release testing) each
    # got their own id, but only the *latest* interrupt() call's id was
    # remembered -- an older, still-queued call (blocked on
    # _playback_lock, not yet even started) would find a *later* id had
    # since overwritten the one meant for it, see no match, and play out
    # in full. Confirmed directly: this caused a growing backlog that
    # took many seconds to unwind instead of cutting off immediately.
    # A threshold has no such gap: every id issued so far is <= it.
    id_a = tts._begin_speaking()
    id_b = tts._begin_speaking()
    id_c = tts._begin_speaking()

    tts.interrupt()

    assert id_a <= tts._interrupted_up_to_id
    assert id_b <= tts._interrupted_up_to_id
    assert id_c <= tts._interrupted_up_to_id


def test_interrupt_never_touches_the_output_stream_itself():
    # interrupt() is pure bookkeeping only -- no stream I/O -- and must
    # stay that way: calling stream.abort()/.start() from whatever
    # thread calls interrupt() (rather than the thread actually
    # mid-write() on that same stream, in _speak_now) is exactly the
    # design this project tried and hit a real hang from -- confirmed
    # directly, a rapid interrupt-while-writing cycle left the stream
    # permanently wedged. See interrupt()'s docstring.
    calls = []

    class TripwireStream:
        def abort(self):
            calls.append("abort")

        def start(self):
            calls.append("start")

    tts._output_stream = TripwireStream()
    tts._begin_speaking()

    tts.interrupt()

    assert calls == []
    assert tts.is_speaking() is False


def test_interrupt_is_a_harmless_noop_when_nothing_is_speaking():
    tts.interrupt()
    assert tts.is_speaking() is False
