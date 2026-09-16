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


# --- pre-LLM transcript gate (_should_reject_transcript) ---------------

_CLEAN_QUALITY = {"no_speech_prob": 0.02, "avg_logprob": -0.30, "compression_ratio": 1.30}


def test_should_reject_transcript_accepts_clean_confident_speech():
    assert main._should_reject_transcript("open chrome", _CLEAN_QUALITY) is None


def test_should_reject_transcript_rejects_empty():
    assert main._should_reject_transcript("", _CLEAN_QUALITY) == "empty"
    assert main._should_reject_transcript("   ", _CLEAN_QUALITY) == "empty"
    # Punctuation-only normalizes down to an empty string too (see
    # _normalize_transcript's strip set) -- caught by the same check.
    assert main._should_reject_transcript("...", _CLEAN_QUALITY) == "empty"


def test_should_reject_transcript_rejects_high_no_speech_prob():
    quality = {**_CLEAN_QUALITY, "no_speech_prob": 0.75}
    assert main._should_reject_transcript("some words here", quality) == "no_speech_prob"


def test_should_reject_transcript_rejects_low_avg_logprob():
    quality = {**_CLEAN_QUALITY, "avg_logprob": -1.4}
    assert main._should_reject_transcript("some words here", quality) == "avg_logprob"


def test_should_reject_transcript_rejects_high_compression_ratio():
    quality = {**_CLEAN_QUALITY, "compression_ratio": 3.1}
    assert main._should_reject_transcript("some words here", quality) == "compression_ratio"


def test_should_reject_transcript_denylist_is_exact_match_not_substring():
    # The failure mode this guards against is real: a naive substring
    # check would eat a legitimate command just because it happens to
    # start with a filler phrase.
    assert main._should_reject_transcript("thank you", _CLEAN_QUALITY) == "denylist"
    assert main._should_reject_transcript("Thank you.", _CLEAN_QUALITY) == "denylist"
    assert main._should_reject_transcript("thank you, now open chrome", _CLEAN_QUALITY) is None


def test_should_reject_transcript_wake_word_echo():
    assert main._should_reject_transcript("hey jarvis", _CLEAN_QUALITY) == "wake_echo"
    assert main._should_reject_transcript("jarvis", _CLEAN_QUALITY) == "wake_echo"


def test_should_reject_transcript_never_rejects_legitimate_short_commands():
    # The single most important property of this gate: it must not make
    # Jarvis deaf to its own shortest, most common fast-path commands.
    # None of these should ever be rejected on acoustic/text grounds
    # alone when the transcription was clean and confident.
    for phrase in ("mute", "louder", "play", "pause", "lock", "next", "back", "power down"):
        assert main._should_reject_transcript(phrase, _CLEAN_QUALITY) is None


def test_should_reject_transcript_catches_real_logged_hallucinations_when_acoustically_bad():
    # Real transcripts from llm_usage_log.jsonl that were all confidently
    # (mis)dispatched -- this is what the acoustic signal is meant to
    # catch when Whisper's own confidence was actually low for them (the
    # gate does not claim text alone identifies these; see this
    # function's docstring for why the acoustic check comes first).
    bad_quality = {"no_speech_prob": 0.7, "avg_logprob": -1.3, "compression_ratio": 1.2}
    for phrase in ("or been grown for me", "hey job is", "follow down", "shu programming bitcoin bitshaka"):
        assert main._should_reject_transcript(phrase, bad_quality) is not None


def test_normalize_transcript_lowercases_strips_punctuation_and_collapses_whitespace():
    assert main._normalize_transcript("  Thank You!  ") == "thank you"
    assert main._normalize_transcript("Hey Jarvis.") == "hey jarvis"


def test_gate_is_disabled_by_default():
    # Ships observe-only -- see TRANSCRIPT_GATE_ENFORCED's own comment.
    # This pins that default so a future change to it is deliberate, not
    # accidental.
    assert main.TRANSCRIPT_GATE_ENFORCED is False


def test_format_stats_includes_rejected_label_only_when_nonzero():
    stats = {"total": 1, "rejected": 1, "recent": deque([{"ts": 0, "text": "hey job is", "category": "rejected", "note": "avg_logprob"}])}
    output = main._format_stats(stats)
    assert "rejected as noise/misheard: 1" in output

    stats_without = {"total": 1, "fast_path": 1, "recent": deque()}
    assert "rejected" not in main._format_stats(stats_without)


# --- interrogative-question guard ---------------------------------------


def test_looks_interrogative_true_for_question_openers():
    for phrase in (
        "is chrome open", "is my computer locked", "are you there",
        "what's on my screen", "which desktop is this", "who is this",
        "where is my file explorer", "why is discord minimized",
        "how many chrome windows are open", "does chrome have windows open",
    ):
        assert main._looks_interrogative(phrase) is True, phrase


def test_looks_interrogative_false_for_commands_including_polite_ones():
    # Must NOT trigger on "can"/"could"/"would" -- those are the
    # matcher's own seeded polite-request phrasing and must keep
    # dispatching normally (see _INTERROGATIVE_OPENERS' comment).
    for phrase in (
        "open chrome", "minimize discord", "lock my computer", "mute",
        "can you open chrome for me", "can you minimize discord for me",
        "could you mute that", "would you lock my computer",
        "take dictation", "snap this window to the left",
    ):
        assert main._looks_interrogative(phrase) is False, phrase


def test_is_dangerous_interrogative_match_excludes_query_shaped_actions():
    # These actions exist specifically to answer a question -- must
    # never be declined just because they were phrased as one.
    for action in ("describe_screen", "count_app_windows", "count_files_in_folder", "none", "no_match", "start_dictation"):
        assert main._is_dangerous_interrogative_match(action, "what's on my screen") is False


def test_is_dangerous_interrogative_match_flags_side_effecting_actions():
    for action in ("focus_or_launch_app", "minimize_app", "maximize_app", "lock_workstation", "toggle_mute", "switch_desktop"):
        assert main._is_dangerous_interrogative_match(action, "is chrome open") is True


def test_is_dangerous_interrogative_match_false_for_non_interrogative_text():
    assert main._is_dangerous_interrogative_match("lock_workstation", "lock my computer") is False


# --- "move" vs switch_desktop guard --------------------------------------


def test_looks_like_a_move_request():
    assert main._looks_like_a_move_request("move this to the next desktop") is True
    assert main._looks_like_a_move_request("move this window to my laptop") is True
    assert main._looks_like_a_move_request("go to the next desktop") is False
    assert main._looks_like_a_move_request("switch to the previous desktop") is False


# --- deictic gate for LLM screen context ---------------------------------


def test_looks_deictic_true_for_genuine_deictic_commands():
    for phrase in (
        "minimize this", "move that to the other screen",
        "what's this page about", "close the window I'm looking at",
        "what's on the current screen",
    ):
        assert main._looks_deictic(phrase) is True, phrase


def test_looks_deictic_false_for_ordinary_commands_that_reach_the_llm():
    # These are the commands that actually take the LLM fallback path in
    # practice -- the gate must not add cost to any of them.
    for phrase in (
        "open chrome", "minimize discord", "go to the next desktop",
        "how many chrome windows are open", "mute", "lock my computer",
        "take dictation for me",
    ):
        assert main._looks_deictic(phrase) is False, phrase
