"""
Tests for the reply phrase pools.

The interesting assertions are structural rather than about wording.
Every key actions.py and main.py reference has to exist (a typo'd key
returns an empty string, which is silence where a reply should be --
worse than a wrong phrase, because nothing is logged); every variant
with a placeholder has to be formattable with the arguments the call
site actually passes; and no variant may lead with a lowercase
placeholder, which was a real defect caught here -- "next desktop, sir."
is a fragment, not a sentence.
"""

import re

import pytest

import actions
import phrases

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_variant_is_formattable_with_its_own_placeholders():
    for key, options in phrases.PHRASES.items():
        for option in options:
            names = set(PLACEHOLDER.findall(option))
            option.format(**{name: "x" for name in names})


def test_placeholder_sets_are_consistent_within_a_pool():
    """
    A call site passes one fixed set of keyword arguments, so a pool
    where one variant wants {app} and another wants {repo} raises
    KeyError on whichever one happens to be picked -- an error that only
    shows up some of the time.
    """
    for key, options in phrases.PHRASES.items():
        sets = {frozenset(PLACEHOLDER.findall(option)) for option in options}
        assert len(sets) == 1, f"{key} has inconsistent placeholders: {sets}"


# Placeholders substituted with lowercase text (an app key, a repo
# name, "next"/"left"). A variant leading with one renders as a
# fragment. {count} is exempt -- "3 windows open, sir." is a sentence.
_TEXT_PLACEHOLDERS = {"app", "repo", "direction", "monitor"}


def test_no_variant_starts_with_a_lowercase_placeholder():
    """Caught for real: "{direction} desktop, sir." rendered as "next desktop, sir."."""
    for key, options in phrases.PHRASES.items():
        for option in options:
            leading = PLACEHOLDER.match(option)
            assert not (leading and leading.group(1) in _TEXT_PLACEHOLDERS), (
                f"{key}: {option!r} leads with a lowercase placeholder"
            )


def test_no_pool_is_empty_and_none_has_duplicates():
    for key, options in phrases.PHRASES.items():
        assert options, f"{key} is empty"
        assert len(set(options)) == len(options), f"{key} has duplicate variants"


def test_pick_never_repeats_itself_back_to_back():
    """
    The point of the module. Pool size matters much less than this --
    an immediate repeat is what actually gets noticed as robotic.
    """
    previous = None
    for _ in range(200):
        choice = phrases.pick("minimize_ok")
        assert choice != previous
        previous = choice


def test_pick_eventually_uses_every_variant():
    seen = {phrases.pick("minimize_ok") for _ in range(400)}
    assert seen == set(phrases.PHRASES["minimize_ok"])


def test_pick_with_a_single_variant_pool_is_stable():
    phrases.PHRASES["_single"] = ("only one",)
    try:
        assert phrases.pick("_single") == "only one"
        assert phrases.pick("_single") == "only one"
    finally:
        del phrases.PHRASES["_single"]
        phrases._last_choice.pop("_single", None)


def test_unknown_key_returns_empty_rather_than_raising():
    """A missing phrase should degrade to silence, never take down an action that already ran."""
    assert phrases.pick("no_such_key") == ""


def test_unknown_key_is_noted_not_just_silently_swallowed(monkeypatch):
    """
    The silence itself stays exactly as designed (see the test above) --
    but it used to be completely untraceable afterward, with no record
    anywhere that anything had gone wrong.
    """
    noted = []
    monkeypatch.setattr(phrases.hud, "note", lambda text: noted.append(text))
    phrases.pick("no_such_key")
    assert noted and "no_such_key" in noted[0]


def test_sir_is_not_on_every_single_line():
    """
    The original replies ended in "sir" 100% of the time, which reads as
    a template rather than a butler.

    Asserted across the corpus rather than per pool: a few pools
    legitimately want it on every variant (boot, shutdown, apologising
    for an error are exactly the formal moments where it belongs). What
    mattered was the overall density, not any individual pool.
    """
    everything = [o for options in phrases.PHRASES.values() for o in options]
    with_sir = [o for o in everything if "sir" in o.lower()]
    ratio = len(with_sir) / len(everything)
    assert 0.3 <= ratio <= 0.75, f"'sir' density is {ratio:.0%}"


def test_frequent_action_confirmations_are_not_all_sir():
    """The pools heard many times an hour are the ones where repetition grates."""
    for key in ("minimize_ok", "maximize_ok", "switch_desktop", "volume_up", "play_pause"):
        options = phrases.PHRASES[key]
        assert [o for o in options if "sir" not in o.lower()], f"every variant of {key} says 'sir'"


@pytest.mark.parametrize(
    "key, kwargs",
    [
        ("app_not_registered", {"app": "chrome"}),
        ("vscode_no_window", {"repo": "jarvis"}),
        ("vscode_switching", {"repo": "jarvis"}),
        ("switch_desktop", {"direction": "next"}),
        ("snap", {"direction": "left"}),
        ("count_many", {"count": 3}),
        ("screen_summary_known", {"app": "Chrome", "title": "New Tab", "monitor": "your monitor"}),
        ("screen_summary_unknown", {"title": "Some Window", "monitor": "your laptop screen"}),
        ("screen_other_summary", {"monitor": "your laptop screen", "summary": "two Chrome and VS Code"}),
        ("screen_named_summary", {"monitor": "your monitor", "summary": "Discord"}),
        ("screen_empty", {"monitor": "your laptop screen"}),
        ("move_monitor_ok", {"monitor": "your monitor"}),
        ("move_monitor_already_there", {"monitor": "your laptop screen"}),
        ("volume_at", {"percent": 40}),
        ("input_level_high", {"percent": 92}),
    ],
)
def test_call_sites_render_cleanly(key, kwargs):
    for _ in range(30):
        rendered = phrases.pick(key, **kwargs)
        assert "{" not in rendered and "}" not in rendered
        assert rendered[0].isupper() or rendered[0].isdigit() or rendered[0] == '"'


def test_actions_only_references_keys_that_exist():
    """
    A typo'd key is silent -- pick() returns "" and the action reports
    nothing at all. Nothing else in the system would catch that.
    """
    source = open(actions.__file__, encoding="utf-8").read()
    used = set(re.findall(r'phrases\.pick\(\s*"([^"]+)"', source))
    assert used, "expected actions.py to use the phrase pools"
    missing = used - set(phrases.PHRASES)
    assert not missing, f"actions.py references unknown phrase keys: {missing}"


def test_main_only_references_keys_that_exist():
    import main

    source = open(main.__file__, encoding="utf-8").read()
    used = set(re.findall(r'phrases\.pick\(\s*"([^"]+)"', source))
    assert used, "expected main.py to use the phrase pools"
    missing = used - set(phrases.PHRASES)
    assert not missing, f"main.py references unknown phrase keys: {missing}"


def test_tts_only_references_keys_that_exist():
    import tts

    source = open(tts.__file__, encoding="utf-8").read()
    used = set(re.findall(r'phrases\.pick\(\s*"([^"]+)"', source))
    assert used, "expected tts.py to use the phrase pools"
    missing = used - set(phrases.PHRASES)
    assert not missing, f"tts.py references unknown phrase keys: {missing}"


def test_every_phrase_pool_is_referenced_by_some_call_site():
    """
    The other half of the "typo'd key is silent" problem: a key nobody
    ever picks isn't broken, but it's dead weight at best and a sign
    something meant to use it doesn't -- confirmed for real with
    "audio_trouble", defined here since early on and picked from
    nowhere for just as long, while the one call site that should have
    used it hardcoded its own near-duplicate wording instead.
    """
    import main
    import timers
    import tts

    source = "".join(
        open(module.__file__, encoding="utf-8").read() for module in (actions, main, timers, tts)
    )
    referenced = set(re.findall(r'phrases\.pick\(\s*"([^"]+)"', source))
    unreferenced = set(phrases.PHRASES) - referenced
    assert not unreferenced, f"phrase pools defined but never picked anywhere: {unreferenced}"
