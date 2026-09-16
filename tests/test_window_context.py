"""
Tests for window_context's pure decision logic -- extract_hints,
rank_windows, and CycleTracker -- built entirely over synthetic
WindowInfo values, no real win32 calls (see enrich_windows, which is
deliberately the only win32-touching function here and is left
untested the same way actions.py's own window functions are: see
test_actions.py's docstring for that convention).
"""

from window_context import CycleTracker, WindowInfo, extract_hints, rank_windows


def _window(hwnd, title, *, monitor_key="monitor", z_index=0, minimized=False, foreground=False):
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        monitor_key=monitor_key,
        z_index=z_index,
        is_foreground=foreground,
        is_minimized=minimized,
        is_cloaked=False,
        on_current_desktop=True,
    )


# --- extract_hints -------------------------------------------------------


def test_extract_hints_strips_stopwords_and_short_tokens():
    # No extra_stopwords here -- "chrome" is deliberately NOT a base
    # stopword (extract_hints doesn't know what an app name is; the
    # caller supplies that via extra_stopwords, tested separately below).
    assert extract_hints("bring up") == set()
    assert extract_hints("open the file explorer window") == {"file", "explorer"}


def test_extract_hints_keeps_real_content_words():
    assert extract_hints("bring up the window with the football stream") == {"football", "stream"}


def test_extract_hints_uses_extra_stopwords_for_the_apps_own_name():
    # "chrome" alone isn't in the base stopword list (it's app-specific,
    # not a general filler word) -- the caller supplies it.
    assert extract_hints("open chrome", extra_stopwords=frozenset({"chrome"})) == set()
    assert extract_hints("the github chrome window", extra_stopwords=frozenset({"chrome"})) == {"github"}


def test_extract_hints_empty_for_no_text():
    assert extract_hints("") == set()


# --- rank_windows ---------------------------------------------------------


def test_rank_windows_hint_match_dominates_everything_else():
    hinted = _window(1, "GitHub - Google Chrome", monitor_key="laptop", z_index=5, minimized=True)
    unhinted = _window(2, "New Tab - Google Chrome", monitor_key="monitor", z_index=0, minimized=False)
    ranked = rank_windows([unhinted, hinted], hint_tokens={"github"}, preferred_monitor="monitor")
    assert ranked[0].hwnd == 1


def test_rank_windows_visible_beats_minimized_when_no_hint():
    minimized = _window(1, "Chrome", minimized=True, z_index=0)
    visible = _window(2, "Chrome", minimized=False, z_index=5)
    ranked = rank_windows([minimized, visible])
    assert ranked[0].hwnd == 2


def test_rank_windows_prefers_the_current_monitor():
    other_monitor = _window(1, "Chrome", monitor_key="laptop", z_index=0)
    current_monitor = _window(2, "Chrome", monitor_key="monitor", z_index=1)
    ranked = rank_windows([other_monitor, current_monitor], preferred_monitor="monitor")
    assert ranked[0].hwnd == 2


def test_rank_windows_falls_back_to_recency_as_the_final_tiebreak():
    older = _window(1, "Chrome", z_index=3)
    newer = _window(2, "Chrome", z_index=0)  # lower z_index = more recently active
    ranked = rank_windows([older, newer])
    assert ranked[0].hwnd == 2


def test_rank_windows_drops_a_hint_token_that_matches_every_candidate():
    # "chrome" matching every title carries no disambiguating information
    # -- must not let it accidentally tie-break in a way that hides the
    # real signal (monitor/recency) below it.
    a = _window(1, "chrome window one", monitor_key="laptop", z_index=0)
    b = _window(2, "chrome window two", monitor_key="monitor", z_index=1)
    ranked = rank_windows([a, b], hint_tokens={"chrome"}, preferred_monitor="monitor")
    assert ranked[0].hwnd == 2  # monitor preference still decides, not a false 3-way tie


def test_rank_windows_empty_list_returns_empty():
    assert rank_windows([]) == []


def test_rank_windows_single_window_is_trivially_first():
    only = _window(1, "Chrome")
    assert rank_windows([only])[0].hwnd == 1


# --- CycleTracker -----------------------------------------------------------


def test_cycle_tracker_first_call_returns_the_top_ranked_window():
    tracker = CycleTracker()
    ranked = [_window(1, "a"), _window(2, "b")]
    chosen = tracker.choose(("focus_or_launch_app", "chrome", frozenset()), ranked, now=0.0)
    assert chosen.hwnd == 1


def test_cycle_tracker_repeat_within_window_advances_to_next_candidate():
    tracker = CycleTracker()
    ranked = [_window(1, "a"), _window(2, "b"), _window(3, "c")]
    key = ("focus_or_launch_app", "chrome", frozenset())
    first = tracker.choose(key, ranked, now=0.0)
    second = tracker.choose(key, ranked, now=1.0)
    third = tracker.choose(key, ranked, now=2.0)
    fourth = tracker.choose(key, ranked, now=3.0)  # wraps back around
    assert [first.hwnd, second.hwnd, third.hwnd, fourth.hwnd] == [1, 2, 3, 1]


def test_cycle_tracker_does_not_reuse_a_stale_ranking_within_the_window():
    # The frozen hwnd order/index must survive even if the caller passes
    # a freshly re-ranked list on the repeat (e.g. because focusing the
    # first window changed its z_index) -- otherwise "the other one"
    # could oscillate between the same two windows forever instead of
    # advancing. Simulated here by reordering `ranked` between calls.
    tracker = CycleTracker()
    key = ("focus_or_launch_app", "chrome", frozenset())
    first_ranking = [_window(1, "a"), _window(2, "b")]
    first = tracker.choose(key, first_ranking, now=0.0)
    assert first.hwnd == 1

    reranked_after_focusing_1 = [_window(1, "a", z_index=0), _window(2, "b", z_index=1)]
    second = tracker.choose(key, reranked_after_focusing_1, now=1.0)
    assert second.hwnd == 2  # advanced, not re-picked as "top of the new ranking"


def test_cycle_tracker_different_key_starts_a_fresh_cycle():
    tracker = CycleTracker()
    ranked = [_window(1, "a"), _window(2, "b")]
    tracker.choose(("focus_or_launch_app", "chrome", frozenset()), ranked, now=0.0)
    tracker.choose(("focus_or_launch_app", "chrome", frozenset()), ranked, now=1.0)  # now at index 1

    other = tracker.choose(("focus_or_launch_app", "discord", frozenset()), ranked, now=1.5)
    assert other.hwnd == 1  # fresh cycle for a different app, not continuing discord's


def test_cycle_tracker_expires_after_the_window_elapses():
    tracker = CycleTracker()
    ranked = [_window(1, "a"), _window(2, "b")]
    key = ("focus_or_launch_app", "chrome", frozenset())
    tracker.choose(key, ranked, now=0.0)

    long_after = tracker.choose(key, ranked, now=0.0 + CycleTracker.CYCLE_WINDOW_SECONDS + 1)
    assert long_after.hwnd == 1  # started fresh again, not advanced


def test_cycle_tracker_prunes_a_closed_window_and_continues():
    tracker = CycleTracker()
    key = ("focus_or_launch_app", "chrome", frozenset())
    tracker.choose(key, [_window(1, "a"), _window(2, "b"), _window(3, "c")], now=0.0)
    # Window 2 has since closed -- no longer in the candidate list passed
    # on the repeat.
    still_open = tracker.choose(key, [_window(1, "a"), _window(3, "c")], now=1.0)
    assert still_open.hwnd == 3


def test_cycle_tracker_empty_ranked_list_returns_none():
    tracker = CycleTracker()
    assert tracker.choose(("focus_or_launch_app", "chrome", frozenset()), [], now=0.0) is None
