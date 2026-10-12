"""
Lexical slot parsing -- pulling numbers, amounts and durations straight
out of the raw transcript.

Why here and not in the LLM schema
----------------------------------
`llm_intent.SCHEMA` carries exactly five fields (action/app/direction/
repo/reply) and every one of them costs prompt tokens on every cold
call -- prefill runs at ~50 tok/s on this project's reference hardware,
so a new enum field is real latency, paid forever, on every utterance
whether or not it uses the field.

`describe_screen` and `move_to_monitor` already established the
alternative: the parameter is read from the *raw utterance text* that
`dispatch_llm_action` passes through as `hint_text`, never from a schema
field. That costs zero prompt tokens and -- the part that actually
matters -- works identically on the fast embedding path and the LLM
fallback, because both hand the same raw text down. See DECISIONS.md's
"Screen and window context" for the original reasoning.

This module is the shared, pure implementation of that pattern for
*numbers*, so volume amounts and timer durations don't each grow their
own half-correct word-to-number parser. Everything here is a pure
function of a string: no win32, no COM, no audio, no clock. That is
deliberate -- it's what lets the tests run with no device present, the
same constraint `monitors.resolve_monitor_mention` is written under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Spelled-out numbers. Whisper transcribes small numbers as words far
# more often than as digits ("reduce the volume by ten"), and both forms
# show up in logs/session.jsonl, so both have to work. Capped at the
# range that's actually plausible in speech for the things this parses
# -- a volume percentage or a timer length -- rather than trying to be a
# general numeric parser.
_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    # Whisper produces both spellings; "fourty" is wrong but it is what
    # comes back often enough to be worth accepting rather than losing
    # the whole utterance to a spelling the user never controlled.
    "fourty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# "a"/"an" read as 1 only directly in front of a unit noun ("an hour",
# "a minute") -- never on their own, or "turn it up a bit" would parse
# as an amount of 1 rather than the vague-small case below.
_ARTICLES = {"a", "an"}

# Vague amounts, mapped to concrete steps. These exist because the log
# has real utterances like "reduce it quite a lot" and "turn it down a
# bit" -- refusing them and saying "by how much, sir?" would be a
# clarifying question, and this project's established stance (see
# window_context.CycleTracker) is to make a sensible choice rather than
# ask one out loud.
_VAGUE_AMOUNTS = {
    "a bit": 5,
    "a little": 5,
    "a touch": 5,
    "slightly": 5,
    "a lot": 20,
    "a load": 20,
    "loads": 20,
    "way": 20,
    "right": 20,
    "quite a lot": 20,
    "a fair bit": 15,
    "a fair amount": 15,
}

_SECONDS_PER_UNIT = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
}

# An absolute target ("set it to 30") rather than a relative step ("turn
# it down by 10"). Checked as whole words against the token immediately
# before the number, so "to" inside "to" of an infinitive can't trip it.
_ABSOLUTE_MARKERS = {"to", "at"}
_RELATIVE_MARKERS = {"by"}


@dataclass(frozen=True)
class Amount:
    """
    A parsed quantity and whether it was meant as a step or a target.

    `mode` is "relative" for "by 10" and "absolute" for "to 30". The
    distinction matters: with master volume at 70, "turn it down by 10"
    means 60 while "turn it down to 10" means 10, and getting that
    backwards is a loud, immediate, obviously-wrong result.
    """

    value: int
    mode: str  # "relative" | "absolute"


def _words(text: str) -> list[str]:
    """
    Lowercased word tokens, punctuation stripped. Hyphens become spaces
    first so "twenty-five" tokenizes the same as "twenty five" -- Whisper
    emits both for the same utterance.
    """
    return re.findall(r"[a-z0-9]+", text.lower().replace("-", " "))


def _number_at(tokens: list[str], i: int) -> tuple[Optional[int], int]:
    """
    Read one number starting at `tokens[i]`.

    Returns `(value, tokens_consumed)`, or `(None, 0)` if there isn't a
    number here. Handles digits ("30"), units ("ten"), tens ("twenty"),
    and the two-token compound form ("twenty five") -- the compound is
    why this returns a consumed count rather than just a value.
    """
    if i >= len(tokens):
        return None, 0

    token = tokens[i]

    if token.isdigit():
        return int(token), 1

    if token in _UNITS:
        return _UNITS[token], 1

    if token in _TENS:
        base = _TENS[token]
        # "twenty five" -- only a *unit* may follow a tens word, so
        # "twenty twenty" doesn't silently become 40.
        if i + 1 < len(tokens) and tokens[i + 1] in _UNITS and _UNITS[tokens[i + 1]] < 10:
            return base + _UNITS[tokens[i + 1]], 2
        return base, 1

    if token in ("hundred", "hundreds"):
        return 100, 1

    return None, 0


def parse_amount(text: str) -> Optional[Amount]:
    """
    Pull a volume-style amount out of an utterance.

    "turn it down by 10"     -> Amount(10, "relative")
    "set the volume to 30"   -> Amount(30, "absolute")
    "reduce the volume by 5" -> Amount(5,  "relative")
    "turn it down a bit"     -> Amount(5,  "relative")
    "turn it up"             -> None        (caller uses its default step)

    Returns None rather than a zero or a guess when there's no amount at
    all, so the caller can tell "no number was said" apart from "the
    number said was 0" -- those mean different things for volume.
    """
    tokens = _words(text)
    if not tokens:
        return None

    for i, token in enumerate(tokens):
        value, consumed = _number_at(tokens, i)
        if value is None:
            continue

        # A number immediately followed by a time unit is a duration,
        # not a volume amount ("set a timer for ten minutes"). Bail
        # rather than reading 10 as a volume step -- both parsers see
        # the same raw text, so each has to decline what isn't theirs.
        if i + consumed < len(tokens) and tokens[i + consumed] in _SECONDS_PER_UNIT:
            return None

        previous = tokens[i - 1] if i > 0 else ""
        if previous in _ABSOLUTE_MARKERS:
            mode = "absolute"
        elif previous in _RELATIVE_MARKERS:
            mode = "relative"
        else:
            # A bare number with no marker ("volume 30", "up 10") reads
            # as a target far more naturally than as a step in every
            # phrasing tried, so absolute is the better default here.
            mode = "absolute"

        return Amount(value, mode)

    # No digits or number words -- fall back to the vague forms. Longest
    # phrase first so "quite a lot" doesn't match on "a lot" and lose its
    # own (identical, here, but that's luck) mapping.
    normalized = " ".join(tokens)
    for phrase in sorted(_VAGUE_AMOUNTS, key=len, reverse=True):
        if phrase in normalized:
            return Amount(_VAGUE_AMOUNTS[phrase], "relative")

    return None


def parse_duration(text: str) -> Optional[int]:
    """
    Pull a duration out of an utterance, in whole seconds.

    "set a timer for ten minutes" -> 600
    "remind me in an hour"        -> 3600
    "90 seconds"                  -> 90
    "an hour and a half"          -> 5400
    "half an hour"                -> 1800
    "two and a half minutes"      -> 150

    Multiple units add up ("one hour thirty minutes" -> 5400), which is
    also how the "and a half" forms are handled rather than as a special
    case.
    """
    tokens = _words(text)
    if not tokens:
        return None

    total = 0
    found = False
    i = 0

    while i < len(tokens):
        token = tokens[i]

        # "half an hour" / "half a minute" -- the fraction leads, so it
        # can't be picked up by the number reader below.
        if token == "half" and i + 1 < len(tokens):
            rest = tokens[i + 1 :]
            offset = 1 if rest and rest[0] in _ARTICLES else 0
            if offset < len(rest) and rest[offset] in _SECONDS_PER_UNIT:
                total += _SECONDS_PER_UNIT[rest[offset]] // 2
                found = True
                i += 1 + offset + 1
                continue

        # "an hour", "a minute" -- an article standing in for 1,
        # optionally extended by a trailing "and a half" ("an hour and
        # a half" -> 1.5 hours). This is a distinct construction from
        # the number-leading "two and a half minutes" case below: here
        # the unit word comes *before* "and a half", not after, so it
        # needs its own suffix check rather than falling into that one.
        if token in _ARTICLES and i + 1 < len(tokens) and tokens[i + 1] in _SECONDS_PER_UNIT:
            unit = _SECONDS_PER_UNIT[tokens[i + 1]]
            after = i + 2
            half = tokens[after : after + 3] == ["and", "a", "half"]
            total += unit + (unit // 2 if half else 0)
            found = True
            i = after + 3 if half else after
            continue

        value, consumed = _number_at(tokens, i)
        if value is not None:
            after = i + consumed

            # "two and a half minutes" -- the unit sits past the
            # fraction, so read ahead before deciding this number isn't
            # a duration at all.
            half = False
            if tokens[after : after + 3] == ["and", "a", "half"]:
                half = True
                after += 3

            if after < len(tokens) and tokens[after] in _SECONDS_PER_UNIT:
                unit = _SECONDS_PER_UNIT[tokens[after]]
                total += value * unit + (unit // 2 if half else 0)
                found = True
                i = after + 1
                continue

        i += 1

    if not found or total <= 0:
        return None
    return total


def format_duration(seconds: int) -> str:
    """
    The inverse of parse_duration -- a whole number of seconds back into
    words, for speaking a duration back rather than parsing one out of
    speech ("timer set for {format_duration(...)}, sir"). Coarsest unit
    first, at most two units (an hour-plus reminder reads naturally as
    "an hour and ten minutes", not "1 hour, 10 minutes, and 0 seconds"),
    and singular/plural handled since "1 minutes" is the kind of thing
    that makes a reply sound generated rather than spoken.
    """
    seconds = max(0, int(round(seconds)))
    if seconds == 0:
        return "no time at all"

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    def _unit(value, singular):
        return f"{value} {singular}" if value == 1 else f"{value} {singular}s"

    parts = []
    if hours:
        parts.append(_unit(hours, "hour"))
    if minutes:
        parts.append(_unit(minutes, "minute"))
    if not parts and secs:
        parts.append(_unit(secs, "second"))

    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} and {parts[1]}"


def extract_reminder_label(text: str) -> str:
    """
    The trailing clause after "to" or "about" in a reminder phrasing --
    "remind me in an hour to stretch" -> "stretch", "set a timer for ten
    minutes" -> "" (no marker, a plain unlabeled timer). Whole-word
    match (spaces on both sides), not a substring check, so nothing
    inside another word matches by accident.

    Deliberately narrow, like every other parser in this module: handles
    the natural "in DURATION to LABEL" word order cleanly (the order
    this project's own real phrasing and the plan this was built from
    both use), not every possible ordering a reminder could be phrased
    in -- "remind me to call mom in ten minutes" would pull in "in ten
    minutes" as part of the label too, since the duration clause follows
    "to" rather than leading it. Harmless in practice (the label ends up
    slightly more verbose, not wrong), and parse_duration still finds
    the duration correctly regardless of where in the text it sits.
    """
    lowered = text.lower()
    for marker in (" to ", " about "):
        idx = lowered.find(marker)
        if idx != -1:
            return text[idx + len(marker) :].strip().rstrip(".,!?")
    return ""
