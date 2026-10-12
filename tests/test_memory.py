"""
Tests for memory.py's locked get()/update() store. No real file I/O
against the project's own logs/memory.json -- every test redirects
MEMORY_PATH to a pytest tmp_path and resets the in-memory cache, the
same two things a real process restart would give for free.
"""

import pytest

import memory


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(memory, "_cache", None)


def test_get_on_a_missing_file_returns_the_default():
    assert memory.get("timers") == []
    assert memory.get("preferences") == {}
    assert memory.get("last_layout") is None


def test_get_on_an_unknown_key_returns_the_given_default():
    assert memory.get("something_new", "fallback") == "fallback"


def test_update_persists_across_a_simulated_restart(tmp_path, monkeypatch):
    memory.update("notes", lambda current: current + ["buy milk"])

    # Simulate a fresh process: drop the in-memory cache, keep the file.
    monkeypatch.setattr(memory, "_cache", None)

    assert memory.get("notes") == ["buy milk"]


def test_update_receives_the_current_value_and_returns_the_new_one():
    memory.update("preferences", lambda current: {**current, "honorific": "sir"})
    result = memory.update("preferences", lambda current: {**current, "wit": 1})
    assert result == {"honorific": "sir", "wit": 1}
    assert memory.get("preferences") == {"honorific": "sir", "wit": 1}


def test_get_returns_a_copy_not_the_live_object():
    memory.update("timers", lambda current: current + [{"id": "a"}])
    fetched = memory.get("timers")
    fetched.append({"id": "b"})  # mutating the returned list must not affect the store
    assert memory.get("timers") == [{"id": "a"}]


def test_a_corrupt_file_degrades_to_defaults_rather_than_raising(tmp_path):
    memory.MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    memory.MEMORY_PATH.write_text("not valid json {{{", encoding="utf-8")
    assert memory.get("timers") == []


def test_a_key_added_to_defaults_after_an_older_file_was_written_still_comes_back(monkeypatch):
    """
    An on-disk file from before some new _DEFAULTS key existed must not
    make a feature reading that new key hit a bare dict.get() miss --
    _load_from_disk merges over the defaults rather than trusting the
    file wholesale.
    """
    memory.update("notes", lambda current: current + ["existing note"])
    monkeypatch.setattr(memory, "_cache", None)
    monkeypatch.setitem(memory._DEFAULTS, "a_future_key", "default_value")

    assert memory.get("a_future_key") == "default_value"
    assert memory.get("notes") == ["existing note"]


def test_non_dict_json_on_disk_degrades_to_defaults(tmp_path):
    memory.MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    memory.MEMORY_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    assert memory.get("timers") == []


def test_concurrent_updates_to_different_keys_do_not_clobber_each_other():
    """
    The exact race this module's own docstring exists to close: two
    independent read-modify-write cycles on different keys must both
    survive, not just whichever one saved last.
    """
    memory.update("notes", lambda current: current + ["note one"])
    memory.update("preferences", lambda current: {**current, "wit": 2})
    memory.update("notes", lambda current: current + ["note two"])

    assert memory.get("notes") == ["note one", "note two"]
    assert memory.get("preferences") == {"wit": 2}
