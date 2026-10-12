"""
Pure difflib-based near-match scoring -- no vocabulary, no cutoff
policy baked in here, just "given a query and some candidates, which
one is closest and how close is it". stdlib only, no new dependency.

Scoped narrowly on purpose. The original idea for this module was
broader: a whole-transcript word-repair pass correcting any word close
to a known command vocabulary ("bng up chrome" -> "bring up chrome")
before handing text to the matcher/LLM. Tested against real English
words before shipping it, per this project's own stated method of
deciding from data rather than a guess -- and it failed that test:
at the one cutoff that catches "bng"/"bring" (ratio 0.75), ordinary
conversational words score *higher* against unrelated vocabulary
entries than the real target does ("love"/"move" 0.75, "sleepy"/"sleep"
0.91, "man"/"many" 0.86, "star"/"start" 0.89) -- there is no threshold
that keeps the wanted correction and excludes those. Plain per-word
edit-distance can't tell "a genuine mistranscription" apart from "a
real, different, shorter word that happens to be nearby" without more
context than a single isolated word carries.

What stays safe: actions.focus_vscode_repo's fallback match against
already-open VS Code window titles. Same scoring, much narrower
domain -- the candidate set is only ever whatever repos are actually
open (a handful, not "any English word"), and the failure mode of a
wrong pick is low-severity (focusing a different real, already-open
window) rather than corrupting arbitrary conversational text. See that
function for the one place this is actually wired in.
"""

import difflib


def best_match(query: str, candidates, cutoff: float = 0.80):
    """
    Returns (candidate, ratio) for whichever of `candidates` is closest
    to `query` by difflib.SequenceMatcher.ratio(), or None if nothing
    clears `cutoff` (or there are no candidates at all). Ties keep the
    first candidate seen.
    """
    best = None
    best_ratio = 0.0
    for candidate in candidates:
        ratio = difflib.SequenceMatcher(None, query, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = candidate
    if best is not None and best_ratio >= cutoff:
        return best, best_ratio
    return None


def ngrams(words, max_n: int = 4):
    """
    Every contiguous run of 1..max_n words from `words`, each joined
    back into a single space-separated string.

    Why: a title like "main.py - jarvis backend - Visual Studio Code"
    scores badly against the spoken phrase "jarvis" if compared as one
    whole string -- the surrounding words dilute the ratio. Scoring
    "jarvis" as one candidate n-gram among several (single words, pairs,
    triples...) lets a multi-word title's *relevant* substring compete
    on its own terms instead of being swamped by everything around it.
    """
    result = []
    count = len(words)
    for n in range(1, max_n + 1):
        for i in range(count - n + 1):
            result.append(" ".join(words[i : i + n]))
    return result
