"""
Tests for monitors._classify and the point/rect lookup helpers -- the
pure decision logic, kept separate from the win32/ctypes calls in
list_monitors() so this runs with no real display hardware assumptions
(see CLAUDE.md: tests run with no window manager present).
"""

import monitors

# A verified-live two-screen layout: an external monitor at (0,0) as
# Windows' primary, and a laptop panel below it -- deliberately NOT a
# side-by-side layout, since that's what this project's actual reference
# machine looks like and it's the case that breaks a left/right or
# primary-is-laptop assumption.
_EXTERNAL = (r"\\.\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1080), True)
_LAPTOP = (r"\\.\DISPLAY2", (697, 1080, 2233, 1944), (697, 1080, 2233, 1944), False)
_TECH = {r"\\.\DISPLAY1": 5, r"\\.\DISPLAY2": 0x80000000}  # HDMI, INTERNAL


def test_classify_identifies_internal_panel_even_when_it_is_not_primary():
    result = monitors._classify([_EXTERNAL, _LAPTOP], _TECH)
    by_device = {m.device: m for m in result}
    assert by_device[_LAPTOP[0]].key == "laptop"
    assert by_device[_LAPTOP[0]].is_internal is True
    assert by_device[_EXTERNAL[0]].key == "monitor"
    assert by_device[_EXTERNAL[0]].is_internal is False
    # The primary flag is preserved as data but must not drive naming --
    # the external is primary here and still isn't called "laptop".
    assert by_device[_EXTERNAL[0]].is_primary is True


def test_classify_falls_back_to_neutral_names_when_technology_query_failed():
    # Empty dict simulates QueryDisplayConfig failing entirely (VM, RDP,
    # an unusual driver) -- must fail closed to neutral names, never
    # guess which one is the laptop.
    result = monitors._classify([_EXTERNAL, _LAPTOP], {})
    assert [m.key for m in result] == ["screen_1", "screen_2"]
    assert all(not m.is_internal for m in result)


def test_classify_falls_back_when_technology_data_is_incomplete():
    # Only one of the two devices resolved -- still not enough to trust,
    # same fail-closed fallback as a total failure.
    partial = {_EXTERNAL[0]: 5}
    result = monitors._classify([_EXTERNAL, _LAPTOP], partial)
    assert [m.key for m in result] == ["screen_1", "screen_2"]


def test_classify_numbers_a_second_external_monitor():
    second_external = (r"\\.\DISPLAY3", (1920, 0, 3840, 1080), (1920, 0, 3840, 1080), False)
    tech = {**_TECH, second_external[0]: 10}  # DisplayPort
    result = monitors._classify([_EXTERNAL, _LAPTOP, second_external], tech)
    keys = {m.device: m.key for m in result}
    assert keys[_EXTERNAL[0]] == "monitor"
    assert keys[second_external[0]] == "monitor_2"
    assert keys[_LAPTOP[0]] == "laptop"


def test_monitor_key_for_point_and_rect(monkeypatch):
    classified = monitors._classify([_EXTERNAL, _LAPTOP], _TECH)
    monkeypatch.setattr(monitors, "list_monitors", lambda **kw: classified)

    assert monitors.monitor_key_for_point(100, 100) == "monitor"
    assert monitors.monitor_key_for_point(1000, 1500) == "laptop"
    assert monitors.monitor_key_for_point(99999, 99999) is None
    assert monitors.monitor_key_for_rect((0, 0, 100, 100)) == "monitor"


# --- resolve_monitor_mention -----------------------------------------------

_TWO = monitors._classify([_EXTERNAL, _LAPTOP], _TECH)  # [monitor(DISPLAY1), laptop(DISPLAY2)]
_THIRD = (r"\.\DISPLAY3", (1920, 0, 3840, 1080), (1920, 0, 3840, 1080), False)
_THREE = monitors._classify([_EXTERNAL, _LAPTOP, _THIRD], {**_TECH, _THIRD[0]: 10})


def test_resolve_monitor_mention_no_screen_word_returns_none_reason():
    assert monitors.resolve_monitor_mention("open chrome", _TWO) == (None, "none")


def test_resolve_monitor_mention_laptop_words():
    for phrase in ("my laptop screen", "the laptop", "the internal display", "my notebook screen"):
        key, reason = monitors.resolve_monitor_mention(phrase, _TWO)
        assert (key, reason) == ("laptop", "ok"), phrase


def test_resolve_monitor_mention_external_words():
    for phrase in ("my monitor", "the external display", "the big screen"):
        key, reason = monitors.resolve_monitor_mention(phrase, _TWO)
        assert (key, reason) == ("monitor", "ok"), phrase


def test_resolve_monitor_mention_laptop_wins_when_both_words_present():
    # "put this on the laptop monitor" -- laptop must win over monitor.
    assert monitors.resolve_monitor_mention("the laptop monitor", _TWO) == ("laptop", "ok")


def test_resolve_monitor_mention_this_screen_uses_current_key():
    assert monitors.resolve_monitor_mention("this screen", _TWO, current_key="laptop") == ("laptop", "ok")
    assert monitors.resolve_monitor_mention("the current screen", _TWO, current_key="monitor") == ("monitor", "ok")


def test_resolve_monitor_mention_other_screen_with_two_monitors():
    assert monitors.resolve_monitor_mention("the other screen", _TWO, current_key="laptop") == ("monitor", "ok")
    assert monitors.resolve_monitor_mention("my other monitor", _TWO, current_key="monitor") == ("laptop", "ok")


def test_resolve_monitor_mention_other_screen_with_one_monitor_is_single():
    assert monitors.resolve_monitor_mention("the other screen", [_TWO[0]], current_key="monitor") == (None, "single")


def test_resolve_monitor_mention_other_screen_with_three_monitors_is_ambiguous():
    assert monitors.resolve_monitor_mention("the other screen", _THREE, current_key="monitor") == (None, "ambiguous")


def test_resolve_monitor_mention_numbered_screen_declined():
    for phrase in ("screen 2", "the second monitor", "monitor one", "screen number 3"):
        assert monitors.resolve_monitor_mention(phrase, _TWO) == (None, "numbered")


def test_resolve_monitor_mention_unnamed_when_screen_n_fallback():
    # QueryDisplayConfig failed -- every monitor falls back to screen_N,
    # so "laptop"/"monitor" cannot resolve to anything real.
    fallback = monitors._classify([_EXTERNAL, _LAPTOP], {})
    assert monitors.resolve_monitor_mention("my laptop screen", fallback) == (None, "unnamed")
    assert monitors.resolve_monitor_mention("the monitor", fallback) == (None, "unnamed")
    # But the purely geometric "other screen" still resolves -- the
    # valuable degraded-mode property this design buys.
    assert monitors.resolve_monitor_mention("the other screen", fallback, current_key="screen_1") == ("screen_2", "ok")


# --- plan_move_rect ---------------------------------------------------------


def test_plan_move_rect_shrinks_proportionally_external_to_laptop():
    # 1920x1080 -> 1536x864 (0.8x on both axes, matching the real
    # reference-machine ratio) -- a window at 50% width/height of the
    # source should land at 50% width/height of the target too.
    source_work = (0, 0, 1920, 1080)
    target_work = (0, 0, 1536, 864)
    window_rect = (0, 0, 960, 540)  # top-left quadrant of the source
    result = monitors.plan_move_rect(window_rect, source_work, target_work)
    assert result == (0, 0, 768, 432)  # same top-left quadrant, scaled


def test_plan_move_rect_grows_proportionally_laptop_to_external():
    source_work = (0, 0, 1536, 864)
    target_work = (0, 0, 1920, 1080)
    window_rect = (0, 0, 768, 432)  # top-left quadrant
    result = monitors.plan_move_rect(window_rect, source_work, target_work)
    assert result == (0, 0, 960, 540)


def test_plan_move_rect_preserves_relative_offset():
    source_work = (0, 0, 1000, 1000)
    target_work = (0, 0, 2000, 2000)
    window_rect = (500, 500, 750, 750)  # bottom-right quadrant, 25% size
    result = monitors.plan_move_rect(window_rect, source_work, target_work)
    assert result == (1000, 1000, 1500, 1500)


def test_plan_move_rect_clamps_a_window_larger_than_the_target():
    source_work = (0, 0, 3000, 2000)
    target_work = (0, 0, 1000, 1000)
    window_rect = (0, 0, 3000, 2000)  # fills the whole (huge) source
    result = monitors.plan_move_rect(window_rect, source_work, target_work)
    l, t, r, b = result
    assert r - l <= 1000 and b - t <= 1000
    assert l >= 0 and t >= 0 and r <= 1000 and b <= 1000


def test_plan_move_rect_enforces_a_minimum_size():
    source_work = (0, 0, 10000, 10000)
    target_work = (0, 0, 1000, 1000)
    window_rect = (0, 0, 10, 10)  # tiny sliver relative to its own source
    result = monitors.plan_move_rect(window_rect, source_work, target_work, min_size=(320, 200))
    l, t, r, b = result
    assert r - l >= 320
    assert b - t >= 200


def test_plan_move_rect_identity_when_source_equals_target():
    work = (0, 0, 1920, 1080)
    window_rect = (100, 200, 900, 700)
    assert monitors.plan_move_rect(window_rect, work, work) == window_rect


def test_plan_move_rect_handles_degenerate_zero_extent_source():
    # A zero-width/height source work rect must not raise (division by
    # zero) -- falls back to a safe inset placement in the target.
    result = monitors.plan_move_rect((0, 0, 100, 100), (0, 0, 0, 0), (0, 0, 1000, 1000))
    l, t, r, b = result
    assert 0 <= l < r <= 1000
    assert 0 <= t < b <= 1000


def test_plan_move_rect_handles_degenerate_zero_extent_target():
    result = monitors.plan_move_rect((0, 0, 100, 100), (0, 0, 1000, 1000), (0, 0, 0, 0))
    l, t, r, b = result
    assert r > l and b > t  # still a valid, non-empty rect despite a degenerate target


def test_plan_move_rect_respects_a_nontrivial_work_rect_offset():
    # Neither monitor on the reference machine has a non-trivial work
    # rect right now (taskbar auto-hidden -- work_rect == rect), so this
    # exercises the offset math synthetically: a 48px taskbar reserved
    # at the top of the target.
    source_work = (0, 0, 1920, 1080)
    target_work = (0, 48, 1536, 864)  # top 48px reserved
    window_rect = (0, 0, 960, 540)  # flush against the source's top-left
    result = monitors.plan_move_rect(window_rect, source_work, target_work)
    assert result[1] >= 48  # must not land above the reserved taskbar area
