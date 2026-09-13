"""
Tests for main.py's module-level pure helpers -- everything else in
main.py lives inside nested closures in main() (audio streams, the
listener thread, etc.) and isn't unit-testable without actually
starting the app against real hardware.
"""

from collections import deque

import main


def test_recognizes_exact_stop_phrases():
    for phrase in ("stop dictation", "end dictation", "finish dictation", "done dictating", "stop dictating"):
        assert main._is_dictation_stop_phrase(phrase) is True


def test_is_case_insensitive_and_tolerates_trailing_punctuation():
    assert main._is_dictation_stop_phrase("Stop Dictation") is True
    assert main._is_dictation_stop_phrase("stop dictation.") is True
    assert main._is_dictation_stop_phrase("stop dictation!") is True
    assert main._is_dictation_stop_phrase("  stop dictation  ") is True


def test_ordinary_dictated_prose_is_not_a_stop_phrase():
    # The whole point of a fixed phrase set instead of fuzzy matching --
    # dictated text that happens to be similar shouldn't end dictation.
    assert main._is_dictation_stop_phrase("please stop and think about dictation policy") is False
    assert main._is_dictation_stop_phrase("the meeting notes are done") is False
    assert main._is_dictation_stop_phrase("") is False


def test_format_stats_with_no_data_yet():
    assert main._format_stats({"total": 0, "recent": deque()}) == "No utterances processed yet, sir."


def test_format_stats_reports_total_and_only_nonzero_categories():
    stats = {"total": 3, "fast_path": 2, "no_match": 1, "recent": deque()}
    output = main._format_stats(stats)
    assert "3 utterance(s) processed" in output
    assert "fast-path matches: 2" in output
    assert "no match at all: 1" in output
    # Categories with no entries shouldn't clutter the output with zeros.
    assert "LLM chat replies" not in output
    assert "dictated" not in output


def test_format_stats_includes_recent_entries_with_timestamp_and_note():
    stats = {
        "total": 1,
        "fast_path": 1,
        "recent": deque([{"ts": 0, "text": "open chrome", "category": "fast_path", "note": "focus_or_launch_app:chrome"}]),
    }
    output = main._format_stats(stats)
    assert '"open chrome" -> fast_path' in output
    assert "focus_or_launch_app:chrome" in output


def test_format_stats_omits_empty_note_parens():
    stats = {"total": 1, "meta": 1, "recent": deque([{"ts": 0, "text": "power down", "category": "meta", "note": ""}])}
    output = main._format_stats(stats)
    assert '"power down" -> meta' in output
    assert "()" not in output
