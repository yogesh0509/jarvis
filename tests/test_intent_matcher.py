"""
Light integration coverage for the fast embedding-matcher path -- uses
the real local BGE model (fully local, no network, loads in a couple
seconds), same as the module's own __main__ self-test. Not mocked,
because the whole point of this path is the actual embedding
similarity behavior, not just the plumbing around it.
"""

import pytest

from intent_matcher import IntentMatcher


@pytest.fixture(scope="module")
def matcher():
    return IntentMatcher()


@pytest.mark.parametrize(
    "phrase,expected_intent",
    [
        ("open chrome", "focus_or_launch_app:chrome"),
        ("can you open chrome for me", "focus_or_launch_app:chrome"),
        ("bring up discord", "focus_or_launch_app:discord"),
        ("close vs code", "minimize_app:vscode"),
        ("go to the next desktop", "switch_desktop:next"),
        ("stop listening", "disable_listening"),
        ("start listening", "enable_listening"),
        ("take dictation", "start_dictation"),
        ("start dictation", "start_dictation"),
        ("open the terminal", "focus_or_launch_app:terminal"),
        ("snap left", "snap_window:left"),
        ("snap to the right", "snap_window:right"),
        ("turn the volume up", "volume_up"),
        ("turn the volume down", "volume_down"),
        ("mute", "toggle_mute"),
        ("next track", "media_next_track"),
        ("lock my computer", "lock_workstation"),
        ("what's on my screen", "describe_screen"),
        ("what am I looking at right now", "describe_screen"),
    ],
)
def test_known_phrases_match_expected_intent(matcher, phrase, expected_intent):
    result = matcher.match(phrase)
    assert result.intent == expected_intent


def test_unrelated_phrase_does_not_match():
    matcher_ = IntentMatcher()
    result = matcher_.match("what's the weather like today")
    assert result.intent is None


def test_desktop_number_phrase_still_false_positives_on_next_desktop():
    # Documents the known limitation main.py's number-guard exists to
    # catch (see main.py's _has_number check on the resolved action) --
    # if the embedding matcher ever stops false-positiving on this, the
    # guard becomes dead code but harmless; if this test starts failing
    # because the match *changed* to something else, that's worth a look.
    matcher_ = IntentMatcher()
    result = matcher_.match("switch to desktop 2")
    assert result.intent == "switch_desktop:next"
    assert result.score >= 0.80
