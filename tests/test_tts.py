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
    tts._pending_stale_ends = 0
    tts._output_stream = None
    yield
    tts._speaking_count = 0
    tts._next_utterance_id = 0
    tts._interrupted_up_to_id = 0
    tts._pending_stale_ends = 0
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


def test_a_reply_issued_after_an_interrupt_keeps_its_own_speaking_count():
    """
    interrupt() used to zero _speaking_count outright -- but every call
    it cut short still runs _end_speaking() in its own finally, and those
    stale decrements then landed on a count that by then belonged to a
    *newer* utterance. is_speaking() read False while audio was genuinely
    playing, which is precisely the hole the count exists to close: the
    always-on session listener hearing its own reply back through the mic
    and acting on it as a fresh command.
    """
    tts._begin_speaking()  # an earcon, in flight
    tts._begin_speaking()  # the spoken reply behind it, in flight
    assert tts.is_speaking() is True

    tts.interrupt()  # push-to-talk barge-in
    assert tts.is_speaking() is False

    # The reply for the command that barge-in was making room for.
    tts._begin_speaking()
    assert tts.is_speaking() is True

    # Only now do the two interrupted workers reach their own finally.
    tts._end_speaking()
    tts._end_speaking()
    assert tts.is_speaking() is True, "a stale finally must not un-mute the listener mid-reply"

    tts._end_speaking()  # the new reply genuinely finishes
    assert tts.is_speaking() is False


def test_stale_end_accounting_nets_out_whatever_order_the_finallys_run_in():
    # Nothing orders an interrupted worker's finally against a newer
    # speak() -- both are daemon threads. The accounting has to hold
    # either way round, so interleave them the other way here.
    tts._begin_speaking()
    tts._begin_speaking()
    tts.interrupt()

    tts._end_speaking()  # one stale finally lands first...
    tts._begin_speaking()  # ...then a new utterance starts
    assert tts.is_speaking() is True

    tts._end_speaking()  # the second stale finally
    assert tts.is_speaking() is True

    tts._end_speaking()  # the new utterance finishes
    assert tts.is_speaking() is False


def test_interrupt_is_a_harmless_noop_when_nothing_is_speaking():
    tts.interrupt()
    assert tts.is_speaking() is False
