"""
Tests for fuzzy_match.py's pure difflib-based scoring -- no audio, no
window manager, no COM. See that module's own docstring for why its
scope stopped at "generic scoring primitive" rather than a
whole-transcript word-repair pass: the broader idea was tested against
real English words and failed (ordinary words scored higher against
unrelated vocabulary than the real target correction did).
"""

from fuzzy_match import best_match, ngrams


# --- best_match -------------------------------------------------------


def test_exact_match_scores_perfectly():
    result = best_match("chrome", ["chrome", "discord", "spotify"])
    assert result == ("chrome", 1.0)


def test_close_candidate_above_cutoff_is_returned():
    result = best_match("bng", ["bring", "spotify", "discord"], cutoff=0.70)
    assert result is not None
    candidate, ratio = result
    assert candidate == "bring"
    assert ratio >= 0.70


def test_no_candidate_clears_cutoff_returns_none():
    assert best_match("xyz", ["chrome", "discord", "spotify"], cutoff=0.80) is None


def test_empty_candidates_returns_none():
    assert best_match("anything", [], cutoff=0.5) is None


def test_ties_keep_the_first_candidate_seen():
    # Both score identically against "ab" -- first one wins, not an
    # arbitrary/unstable pick.
    result = best_match("ab", ["ac", "ad"], cutoff=0.0)
    assert result[0] == "ac"


def test_picks_the_single_best_among_several_candidates():
    result = best_match("bitcoin wallet", ["jarvis", "payments api", "bitcoin wallet"], cutoff=0.5)
    assert result[0] == "bitcoin wallet"
    assert result[1] == 1.0


# --- ngrams -------------------------------------------------------------


def test_ngrams_single_word():
    assert ngrams(["jarvis"]) == ["jarvis"]


def test_ngrams_covers_all_lengths_up_to_max_n():
    result = ngrams(["a", "b", "c"], max_n=2)
    assert result == ["a", "b", "c", "a b", "b c"]


def test_ngrams_respects_max_n():
    result = ngrams(["a", "b", "c", "d"], max_n=1)
    assert result == ["a", "b", "c", "d"]


def test_ngrams_empty_input():
    assert ngrams([]) == []


def test_ngrams_extracts_relevant_substring_from_a_longer_title():
    # The real motivating case: a title with a repo name buried inside
    # surrounding text should still produce that name as one candidate.
    words = "main.py - jarvis backend - Visual Studio Code".lower().split()
    result = ngrams(words, max_n=2)
    assert "jarvis" in result
    assert "jarvis backend" in result
