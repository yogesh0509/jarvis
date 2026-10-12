"""
Tests for slots.py's pure lexical parsing -- no audio, no COM, no clock,
just strings in and numbers out. See slots.py's module docstring for why
this parses the raw transcript rather than adding LLM schema fields.
"""

from slots import Amount, parse_amount, parse_duration


# --- parse_amount ---------------------------------------------------


def test_relative_amount_with_digit():
    assert parse_amount("reduce the volume by 10") == Amount(10, "relative")


def test_relative_amount_with_word():
    assert parse_amount("reduce the volume by ten") == Amount(10, "relative")


def test_absolute_amount_with_to():
    assert parse_amount("set the volume to 30") == Amount(30, "absolute")


def test_absolute_amount_with_at():
    assert parse_amount("set the volume at 45") == Amount(45, "absolute")


def test_bare_number_defaults_to_absolute():
    # No "by"/"to" marker at all -- "volume 30" and "up 10" both read
    # more naturally as a target than a step in every phrasing tried.
    assert parse_amount("volume 30") == Amount(30, "absolute")


def test_compound_tens_and_units():
    assert parse_amount("turn it down by twenty five") == Amount(25, "relative")


def test_hyphenated_compound_tens_and_units():
    assert parse_amount("lower it by twenty-five") == Amount(25, "relative")


def test_misspelled_forty_is_accepted():
    # Whisper produces "fourty" often enough in practice to be worth
    # accepting rather than losing the whole utterance to a spelling
    # the user never controlled -- see slots.py's _TENS comment.
    assert parse_amount("set volume to fourty") == Amount(40, "absolute")


def test_tens_word_not_followed_by_unit_stands_alone():
    assert parse_amount("turn it up by twenty") == Amount(20, "relative")


def test_vague_amount_a_bit():
    result = parse_amount("turn it down a bit")
    assert result is not None
    assert result.mode == "relative"
    assert result.value > 0


def test_vague_amount_a_lot_is_bigger_than_a_bit():
    bit = parse_amount("turn it down a bit")
    lot = parse_amount("turn it down a lot")
    assert lot.value > bit.value


def test_no_amount_at_all_returns_none():
    # "turn it up" with no number and no vague-amount phrase -- the
    # caller falls back to its own default single-tap step.
    assert parse_amount("turn it up") is None


def test_empty_string_returns_none():
    assert parse_amount("") is None


def test_number_followed_by_time_unit_is_not_an_amount():
    # "ten minutes" is a duration, not a volume amount -- both parsers
    # see the same raw text and each has to decline what isn't theirs.
    assert parse_amount("set a timer for ten minutes") is None


def test_longest_vague_phrase_wins():
    # "quite a lot" must not be read as the shorter "a lot" by accident.
    result = parse_amount("turn it down quite a lot")
    assert result == Amount(20, "relative")


# --- parse_duration ---------------------------------------------------


def test_duration_digits_and_minutes():
    assert parse_duration("set a timer for ten minutes") == 600


def test_duration_digit_seconds():
    assert parse_duration("90 seconds") == 90


def test_duration_an_hour():
    assert parse_duration("remind me in an hour") == 3600


def test_duration_a_minute():
    assert parse_duration("wait a minute") == 60


def test_duration_half_an_hour():
    assert parse_duration("set a timer for half an hour") == 1800


def test_duration_hour_and_a_half():
    assert parse_duration("an hour and a half") == 5400


def test_duration_two_and_a_half_minutes():
    assert parse_duration("two and a half minutes") == 150


def test_duration_combined_units_add_up():
    assert parse_duration("one hour thirty minutes") == 5400


def test_duration_abbreviated_units():
    assert parse_duration("set a timer for 5 min") == 300
    assert parse_duration("set a timer for 2 hrs") == 7200


def test_no_duration_at_all_returns_none():
    assert parse_duration("open chrome") is None


def test_empty_string_duration_returns_none():
    assert parse_duration("") is None


def test_zero_duration_is_not_a_valid_timer():
    # "zero minutes" parses as a number but a non-positive total isn't a
    # real timer request -- the caller shouldn't have to special-case it.
    assert parse_duration("set a timer for zero minutes") is None


# --- format_duration (the inverse: seconds back into words) -------------

from slots import format_duration


def test_format_duration_single_units():
    assert format_duration(60) == "1 minute"
    assert format_duration(600) == "10 minutes"
    assert format_duration(3600) == "1 hour"


def test_format_duration_pluralizes_correctly():
    assert format_duration(120) == "2 minutes"
    assert format_duration(7200) == "2 hours"
    assert format_duration(1) == "1 second"
    assert format_duration(2) == "2 seconds"


def test_format_duration_combines_at_most_two_units():
    assert format_duration(3660) == "1 hour and 1 minute"
    assert format_duration(5400) == "1 hour and 30 minutes"


def test_format_duration_seconds_only_shown_with_no_larger_unit():
    assert format_duration(90) == "1 minute"  # seconds dropped once a minute is present
    assert format_duration(45) == "45 seconds"


def test_format_duration_zero_and_negative():
    assert format_duration(0) == "no time at all"
    assert format_duration(-5) == "no time at all"


def test_format_duration_round_trips_through_parse_duration():
    for seconds in (60, 90, 600, 1800, 3600, 5400):
        words = format_duration(seconds)
        assert parse_duration(words) is not None


# --- extract_reminder_label ---------------------------------------------

from slots import extract_reminder_label


def test_extract_reminder_label_after_to():
    assert extract_reminder_label("remind me in an hour to stretch") == "stretch"


def test_extract_reminder_label_after_about():
    assert extract_reminder_label("remind me about the meeting") == "the meeting"


def test_extract_reminder_label_strips_trailing_punctuation():
    assert extract_reminder_label("remind me in 5 minutes to check the oven.") == "check the oven"


def test_extract_reminder_label_no_marker_returns_empty():
    assert extract_reminder_label("set a timer for ten minutes") == ""


def test_extract_reminder_label_is_case_insensitive_for_the_marker():
    assert extract_reminder_label("remind me in an hour TO stretch") == "stretch"


def test_extract_reminder_label_prefers_to_over_about_when_both_present():
    # "to" is checked first -- matches the natural "in X to Y" order
    # this project's own phrasing and the plan it was built from use.
    assert extract_reminder_label("remind me to think about dinner") == "think about dinner"
