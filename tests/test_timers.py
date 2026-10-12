"""
Tests for timers.py's scheduling logic. tts.speak/earcons.play are
mocked throughout -- both are real, working calls that would try to
synthesize and play real audio through a real device, exactly the kind
of thing CLAUDE.md's "no audio device" test constraint exists to keep
out of this suite. memory.py's own persistence is real (redirected to
a tmp_path, same isolation test_memory.py uses), not mocked, since
persisting across a restart is the actual behavior worth proving.
"""

import time

import pytest

import memory
import timers


@pytest.fixture(autouse=True)
def _isolate_timers(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(memory, "_cache", None)
    timers.stop()
    monkeypatch.setattr(timers, "_active", [])
    yield
    timers.stop()


@pytest.fixture(autouse=True)
def _mock_speech(monkeypatch):
    spoken = []
    played = []
    monkeypatch.setattr(timers.tts, "speak", lambda text: spoken.append(text))
    monkeypatch.setattr(timers.earcons, "play", lambda name: played.append(name))
    return {"spoken": spoken, "played": played}


# --- set_timer / cancel_most_recent / list_active -----------------------


def test_set_timer_returns_a_record_with_the_right_shape():
    record = timers.set_timer(600, label="")
    assert record["duration_seconds"] == 600
    assert record["label"] == ""
    assert record["fires_at"] > time.time()
    assert "id" in record and "set_at" in record


def test_set_timer_persists_immediately(monkeypatch):
    timers.set_timer(600, label="stretch")
    monkeypatch.setattr(memory, "_cache", None)  # simulate a fresh process

    persisted = memory.get("timers")
    assert len(persisted) == 1
    assert persisted[0]["label"] == "stretch"


def test_list_active_is_sorted_soonest_first():
    timers.set_timer(600)
    timers.set_timer(60)
    timers.set_timer(300)

    active = timers.list_active()
    assert [t["duration_seconds"] for t in active] == [60, 300, 600]


def test_cancel_most_recent_with_no_timers_returns_none():
    assert timers.cancel_most_recent() is None


def test_cancel_most_recent_with_no_label_cancels_the_one_set_last():
    timers.set_timer(600)
    time.sleep(0.01)
    second = timers.set_timer(60)

    cancelled = timers.cancel_most_recent()

    assert cancelled["id"] == second["id"]
    assert [t["duration_seconds"] for t in timers.list_active()] == [600]


def test_cancel_most_recent_with_a_matching_label():
    timers.set_timer(600, label="stretch")
    reminder = timers.set_timer(300, label="check the oven")

    cancelled = timers.cancel_most_recent(label="oven")

    assert cancelled["id"] == reminder["id"]
    assert len(timers.list_active()) == 1


def test_cancel_most_recent_with_a_label_matching_nothing_falls_back_to_most_recent():
    timers.set_timer(600, label="stretch")
    time.sleep(0.01)
    second = timers.set_timer(60, label="")

    cancelled = timers.cancel_most_recent(label="completely unrelated")

    assert cancelled["id"] == second["id"]


def test_cancel_removes_from_persisted_memory_too(monkeypatch):
    timers.set_timer(600)
    timers.cancel_most_recent()
    monkeypatch.setattr(memory, "_cache", None)

    assert memory.get("timers") == []


# --- firing, via the real polling thread ---------------------------------


def test_a_short_timer_actually_fires(_mock_speech):
    timers.set_timer(0, label="")  # fires on the very next poll tick

    for _ in range(50):  # up to ~5s of polling headroom
        if _mock_speech["spoken"]:
            break
        time.sleep(0.1)

    assert _mock_speech["played"] == ["timer_done"]
    assert len(_mock_speech["spoken"]) == 1
    assert timers.list_active() == []


def test_firing_speaks_the_label_when_one_was_given(_mock_speech, monkeypatch):
    monkeypatch.setattr(timers.phrases, "pick", lambda key, **kw: f"{key}:{kw}")
    timers.set_timer(0, label="stretch")

    for _ in range(50):
        if _mock_speech["spoken"]:
            break
        time.sleep(0.1)

    assert "timer_done_labeled" in _mock_speech["spoken"][0]
    assert "stretch" in _mock_speech["spoken"][0]


def test_firing_removes_the_timer_from_persisted_memory(monkeypatch):
    timers.set_timer(0)
    for _ in range(50):
        if not timers.list_active():
            break
        time.sleep(0.1)

    monkeypatch.setattr(memory, "_cache", None)
    assert memory.get("timers") == []


def test_a_firing_that_raises_does_not_kill_the_poll_loop(monkeypatch, _mock_speech):
    """A single bad firing must never take the whole timer subsystem down with it."""
    calls = []

    def _boom(text):
        calls.append(text)
        raise RuntimeError("simulated tts failure")

    monkeypatch.setattr(timers.tts, "speak", _boom)
    timers.set_timer(0, label="first")
    for _ in range(50):
        if calls:
            break
        time.sleep(0.1)
    assert calls  # the first (failing) firing was attempted

    # A second timer set afterward must still fire normally -- the loop survived.
    monkeypatch.setattr(timers.tts, "speak", lambda text: _mock_speech["spoken"].append(text))
    timers.set_timer(0, label="second")
    for _ in range(50):
        if _mock_speech["spoken"]:
            break
        time.sleep(0.1)
    assert _mock_speech["spoken"]


# --- restore() -----------------------------------------------------------


def test_restore_rearms_a_still_pending_timer(monkeypatch):
    future_record = {
        "id": "abc123",
        "label": "",
        "set_at": time.time(),
        "fires_at": time.time() + 300,
        "duration_seconds": 300,
    }
    monkeypatch.setattr(memory, "MEMORY_PATH", memory.MEMORY_PATH)  # keep the isolated path from the fixture
    memory.update("timers", lambda _current: [future_record])

    timers.restore()

    assert len(timers.list_active()) == 1
    assert timers.list_active()[0]["id"] == "abc123"


def test_restore_fires_an_overdue_timer_immediately(_mock_speech):
    overdue_record = {
        "id": "overdue1",
        "label": "",
        "set_at": time.time() - 600,
        "fires_at": time.time() - 5,  # already due
        "duration_seconds": 600,
    }
    memory.update("timers", lambda _current: [overdue_record])

    timers.restore()

    assert _mock_speech["played"] == ["timer_done"]
    assert timers.list_active() == []


def test_restore_marks_an_overdue_timer_as_overdue_in_the_reply(monkeypatch, _mock_speech):
    monkeypatch.setattr(timers.phrases, "pick", lambda key, **kw: f"[{key}]")
    overdue_record = {
        "id": "overdue2",
        "label": "",
        "set_at": time.time() - 600,
        "fires_at": time.time() - 5,
        "duration_seconds": 600,
    }
    memory.update("timers", lambda _current: [overdue_record])

    timers.restore()

    assert "[timer_overdue_prefix]" in _mock_speech["spoken"][0]


def test_restore_with_no_memory_at_all_does_not_raise():
    timers.restore()  # must not raise
    assert timers.list_active() == []
