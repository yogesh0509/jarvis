"""
Tests for actions.py's pure dispatch/parsing logic -- parse_fast_path_intent
and dispatch_llm_action's routing. The individual ACTIONS functions
(focus_or_launch_app etc.) do real window/process/OS work and aren't
covered here; dispatch_llm_action is tested against a monkeypatched
ACTIONS table so no real window is touched.
"""

import pytest

import actions
import phrases


def test_parse_fast_path_intent_app_action():
    assert actions.parse_fast_path_intent("focus_or_launch_app:chrome") == {
        "action": "focus_or_launch_app",
        "app": "chrome",
        "direction": "none",
        "repo": "",
    }


def test_parse_fast_path_intent_minimize():
    assert actions.parse_fast_path_intent("minimize_app:discord") == {
        "action": "minimize_app",
        "app": "discord",
        "direction": "none",
        "repo": "",
    }


def test_parse_fast_path_intent_switch_desktop():
    assert actions.parse_fast_path_intent("switch_desktop:next") == {
        "action": "switch_desktop",
        "app": "none",
        "direction": "next",
        "repo": "",
    }


def test_parse_fast_path_intent_maximize():
    assert actions.parse_fast_path_intent("maximize_app:vscode") == {
        "action": "maximize_app",
        "app": "vscode",
        "direction": "none",
        "repo": "",
    }


def test_parse_fast_path_intent_snap_window():
    assert actions.parse_fast_path_intent("snap_window:left") == {
        "action": "snap_window",
        "app": "none",
        "direction": "left",
        "repo": "",
    }


def test_parse_fast_path_intent_no_param_actions():
    for label in actions._NO_PARAM_ACTIONS:
        assert actions.parse_fast_path_intent(label) == {
            "action": label,
            "app": "none",
            "direction": "none",
            "repo": "",
        }


def test_parse_fast_path_intent_non_compound_label_returns_none():
    # Meta intents like "disable_listening" have no ":" -- handled
    # separately in main.py, never routed through here.
    assert actions.parse_fast_path_intent("disable_listening") is None


def test_parse_fast_path_intent_unknown_compound_action_returns_none():
    # An action prefix this function doesn't recognize (count_app_windows
    # and friends aren't produced as compound fast-path labels today).
    assert actions.parse_fast_path_intent("count_app_windows:chrome") is None


def test_dispatch_llm_action_routes_app_actions(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "minimize_app", lambda app: calls.append(("minimize", app)) or "ok")
    result = actions.dispatch_llm_action({"action": "minimize_app", "app": "chrome", "direction": "none", "repo": ""})
    assert result == actions.Outcome("ok", "ok")
    assert calls == [("minimize", "chrome")]


def test_dispatch_llm_action_routes_focus_or_launch_app_with_hint_text(monkeypatch):
    # focus_or_launch_app is the one action dispatch_llm_action calls
    # with two arguments -- the app key, and the raw utterance text as
    # an optional window-selection hint (see actions.focus_or_launch_app's
    # hint_text parameter). The `text` argument defaults to "" so every
    # call site that doesn't have one (or predates this) keeps working.
    calls = []
    monkeypatch.setitem(
        actions.ACTIONS, "focus_or_launch_app", lambda app, hint_text: calls.append((app, hint_text)) or "ok"
    )
    result = actions.dispatch_llm_action(
        {"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": ""},
        "bring up the chrome with the football stream",
    )
    assert result == actions.Outcome("ok", "ok")
    assert calls == [("chrome", "bring up the chrome with the football stream")]


def test_dispatch_llm_action_defaults_hint_text_to_empty_string(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "focus_or_launch_app", lambda app, hint_text: calls.append(hint_text) or "ok")
    actions.dispatch_llm_action({"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": ""})
    assert calls == [""]


def test_dispatch_llm_action_routes_switch_desktop(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "switch_desktop", lambda direction: calls.append(direction) or "ok")
    result = actions.dispatch_llm_action({"action": "switch_desktop", "app": "none", "direction": "next", "repo": ""})
    assert result == actions.Outcome("ok", "ok")
    assert calls == ["next"]


def test_dispatch_llm_action_routes_focus_vscode_repo(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "focus_vscode_repo", lambda repo: calls.append(repo) or "ok")
    result = actions.dispatch_llm_action({"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet"})
    assert result == actions.Outcome("ok", "ok")
    assert calls == ["bdk_wallet"]


def test_count_files_in_folder_is_no_longer_an_action():
    assert "count_files_in_folder" not in actions.ACTIONS
    result = actions.dispatch_llm_action({"action": "count_files_in_folder", "app": "none", "direction": "none", "repo": ""})
    assert result.status == "failed"


def test_dispatch_llm_action_routes_maximize_app(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "maximize_app", lambda app: calls.append(app) or "ok")
    result = actions.dispatch_llm_action({"action": "maximize_app", "app": "vscode", "direction": "none", "repo": ""})
    assert result == actions.Outcome("ok", "ok")
    assert calls == ["vscode"]


def test_dispatch_llm_action_routes_snap_window(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "snap_window", lambda direction: calls.append(direction) or "ok")
    result = actions.dispatch_llm_action({"action": "snap_window", "app": "none", "direction": "right", "repo": ""})
    assert result == actions.Outcome("ok", "ok")
    assert calls == ["right"]


# Actions special-cased in dispatch_llm_action to receive `text` as a
# free-text hint even though they stay in _NO_PARAM_ACTIONS for
# parse_fast_path_intent's sake -- see that function's own docstring.
_TEXT_HINT_ACTIONS = (
    "describe_screen",
    "move_to_monitor",
    "volume_up",
    "volume_down",
    "set_timer",
    "cancel_timer",
    "system_status",
)


def test_dispatch_llm_action_routes_no_param_actions_with_no_args(monkeypatch):
    for label in actions._NO_PARAM_ACTIONS:
        if label in _TEXT_HINT_ACTIONS:
            continue
        calls = []
        monkeypatch.setitem(actions.ACTIONS, label, lambda calls=calls: calls.append(True) or "ok")
        result = actions.dispatch_llm_action({"action": label, "app": "none", "direction": "none", "repo": ""})
        assert result == actions.Outcome("ok", "ok")
        assert calls == [True]


@pytest.mark.parametrize("label", _TEXT_HINT_ACTIONS)
def test_dispatch_llm_action_routes_text_hint_actions_with_hint_text(monkeypatch, label):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, label, lambda hint_text: calls.append(hint_text) or "ok")
    result = actions.dispatch_llm_action(
        {"action": label, "app": "none", "direction": "none", "repo": ""},
        "what's on my laptop screen",
    )
    assert result == actions.Outcome("ok", "ok")
    assert calls == ["what's on my laptop screen"]


@pytest.mark.parametrize("label", _TEXT_HINT_ACTIONS)
def test_dispatch_llm_action_defaults_text_hint_actions_to_empty_string(monkeypatch, label):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, label, lambda hint_text: calls.append(hint_text) or "ok")
    actions.dispatch_llm_action({"action": label, "app": "none", "direction": "none", "repo": ""})
    assert calls == [""]


def test_dispatch_llm_action_unknown_action_declines_gracefully():
    # Asserted against the phrase pool rather than one exact substring:
    # replies are drawn from phrases.py variants now, so pinning a
    # specific wording here would break on every phrasing tweak while
    # testing nothing about the actual behaviour (declining, not raising
    # and not dispatching something unrelated).
    result = actions.dispatch_llm_action({"action": "delete_everything", "app": "none", "direction": "none", "repo": ""})
    assert result.text in phrases.PHRASES["unknown_action"]
    # An action key dispatch_llm_action doesn't even recognize is a
    # real failure to service the request, not a soft "nothing to do".
    assert result.status == "failed"


def test_dispatch_llm_action_normalizes_a_bare_outcome_return_unchanged(monkeypatch):
    # A real, already-converted action returns Outcome directly --
    # dispatch_llm_action must pass it through as-is, not re-wrap it
    # into Outcome(Outcome(...), "ok").
    monkeypatch.setitem(
        actions.ACTIONS, "maximize_app", lambda app: actions.Outcome("already maximised", "no_effect")
    )
    result = actions.dispatch_llm_action({"action": "maximize_app", "app": "vscode", "direction": "none", "repo": ""})
    assert result == actions.Outcome("already maximised", "no_effect")


# --- _best_fuzzy_repo_match (focus_vscode_repo's fallback) --------------
#
# Pure matching logic, kept separate from the real EnumWindows call so
# it's testable without a window manager (CLAUDE.md's stated test
# constraint). See fuzzy_match.py's own docstring for why this stays
# narrowly scoped to real, already-open window titles rather than a
# general word-repair pass.


def test_fuzzy_repo_match_finds_a_misheard_repo_name():
    candidates = [(111, "main.py - bitcoin wallet - visual studio code")]
    assert actions._best_fuzzy_repo_match("bit coin wallet", candidates) == 111


def test_fuzzy_repo_match_prefers_the_closer_of_two_open_repos():
    candidates = [
        (111, "readme.md - payments api - visual studio code"),
        (222, "main.py - bitcoin wallet - visual studio code"),
    ]
    assert actions._best_fuzzy_repo_match("bit coin wallet", candidates) == 222


def test_fuzzy_repo_match_returns_none_when_nothing_is_close():
    candidates = [(111, "main.py - jarvis - visual studio code")]
    assert actions._best_fuzzy_repo_match("completely unrelated phrase", candidates) is None


def test_fuzzy_repo_match_returns_none_with_no_open_windows():
    assert actions._best_fuzzy_repo_match("bitcoin wallet", []) is None


def test_fuzzy_repo_match_handles_single_word_repo_names():
    candidates = [(111, "main.py - jarvis - visual studio code")]
    assert actions._best_fuzzy_repo_match("jarviss", candidates) == 111


# --- system_status: topic detection and drive parsing -------------------
#
# Pure lexical logic, kept separate from the real psutil calls the same
# way _best_fuzzy_repo_match is kept separate from the real EnumWindows
# call -- testable without touching the real battery/memory/disk state
# this machine happens to have.


def test_detect_status_topics_battery_only():
    assert actions._detect_status_topics("how's my battery") == ["battery"]


def test_detect_status_topics_memory_only():
    assert actions._detect_status_topics("how much ram am i using") == ["memory"]


def test_detect_status_topics_disk_only():
    assert actions._detect_status_topics("is my disk full") == ["disk"]


def test_detect_status_topics_multiple_at_once():
    result = actions._detect_status_topics("battery and memory please")
    assert result == ["battery", "memory"]


def test_detect_status_topics_no_recognized_word_reports_all_three():
    # A query action erring toward saying more, not less, rather than
    # guessing wrong or declining outright.
    assert actions._detect_status_topics("how am I doing") == ["battery", "memory", "disk"]


def test_disk_fact_uses_the_system_drive_by_default(monkeypatch):
    monkeypatch.setattr(actions.os, "environ", {"SystemDrive": "C:"})
    seen = []
    monkeypatch.setattr(
        actions.psutil,
        "disk_usage",
        lambda drive: seen.append(drive) or actions.psutil._ntuples.sdiskusage(100, 50, 50, 50.0),
    )
    actions._disk_fact("is my disk full")
    assert seen == ["C:/"]


def test_disk_fact_uses_a_named_drive_letter(monkeypatch):
    seen = []
    monkeypatch.setattr(
        actions.psutil,
        "disk_usage",
        lambda drive: seen.append(drive) or actions.psutil._ntuples.sdiskusage(100, 20, 80, 20.0),
    )
    actions._disk_fact("how much space is on the d drive")
    assert seen == ["D:/"]


def test_disk_fact_on_a_nonexistent_drive_degrades_honestly(monkeypatch):
    def _raise(drive):
        raise OSError("no such drive")

    monkeypatch.setattr(actions.psutil, "disk_usage", _raise)
    result = actions._disk_fact("how much space is on the z drive")
    assert "z" in result.lower() or "Z" in result


def test_battery_fact_reports_plugged_in(monkeypatch):
    monkeypatch.setattr(
        actions.psutil,
        "sensors_battery",
        lambda: actions.psutil._ntuples.sbattery(87, 0, True),
    )
    assert "87" in actions._battery_fact()
    assert "plugged in" in actions._battery_fact()


def test_battery_fact_reports_unplugged(monkeypatch):
    monkeypatch.setattr(
        actions.psutil,
        "sensors_battery",
        lambda: actions.psutil._ntuples.sbattery(42, 1000, False),
    )
    assert "unplugged" in actions._battery_fact()


def test_battery_fact_returns_none_when_no_battery_present(monkeypatch):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    assert actions._battery_fact() is None


def test_system_status_falls_back_honestly_with_no_battery_and_only_battery_asked(monkeypatch):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    result = actions.system_status("how's my battery")
    assert result.status == "ok"
    assert result.text in phrases.PHRASES["system_status_no_battery"]


def test_system_status_reports_other_topics_even_if_battery_is_missing(monkeypatch):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(
        actions.psutil,
        "virtual_memory",
        lambda: actions.psutil._ntuples.svmem(100, 20, 50.0, 80, 20),
    )
    result = actions.system_status("battery and memory")
    assert "memory" in result.text.lower()


# --- proactive briefing: morning_briefing / save_current_layout ---------


def test_time_of_day_bucket_boundaries():
    assert actions._time_of_day_bucket(5) == "morning"
    assert actions._time_of_day_bucket(11) == "morning"
    assert actions._time_of_day_bucket(12) == "afternoon"
    assert actions._time_of_day_bucket(16) == "afternoon"
    assert actions._time_of_day_bucket(17) == "evening"
    assert actions._time_of_day_bucket(21) == "evening"
    assert actions._time_of_day_bucket(22) == "night"
    assert actions._time_of_day_bucket(4) == "night"
    assert actions._time_of_day_bucket(0) == "night"


_ALL_BRIEFING_GREETINGS = (
    phrases.PHRASES["briefing_greeting_morning"]
    + phrases.PHRASES["briefing_greeting_afternoon"]
    + phrases.PHRASES["briefing_greeting_evening"]
    + phrases.PHRASES["briefing_greeting_night"]
)


@pytest.fixture
def _isolated_memory(tmp_path, monkeypatch):
    """
    Same isolation test_memory.py's own autouse fixture gives every test
    in that file -- scoped locally here instead of file-wide, since
    morning_briefing/save_current_layout are the only things in this
    file that touch memory.json at all.
    """
    monkeypatch.setattr(actions.memory, "MEMORY_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(actions.memory, "_cache", None)


def test_morning_briefing_is_just_a_greeting_with_no_battery_and_no_windows(monkeypatch, _isolated_memory):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(actions, "_describe_screen_entries", lambda monitor_key: [])

    result = actions.morning_briefing()

    # Real wall-clock hour picks the bucket -- not frozen (see
    # _time_of_day_bucket's own tests for the boundary logic itself),
    # so this only asserts a real greeting came back, from whichever
    # pool the hour this test happens to run in lands on.
    assert result in _ALL_BRIEFING_GREETINGS


def test_morning_briefing_includes_the_battery_fact(monkeypatch, _isolated_memory):
    monkeypatch.setattr(
        actions.psutil,
        "sensors_battery",
        lambda: actions.psutil._ntuples.sbattery(82, 0, True),
    )
    monkeypatch.setattr(actions, "_describe_screen_entries", lambda monitor_key: [])

    result = actions.morning_briefing()

    assert "82" in result
    assert "plugged in" in result


def test_morning_briefing_reports_current_layout_with_no_prior_snapshot(monkeypatch, _isolated_memory):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(
        actions,
        "_describe_screen_entries",
        lambda monitor_key: [(None, "Widget"), (None, "Widget"), (None, "Gadget")],
    )

    result = actions.morning_briefing()

    expected = {t.format(summary="two Widget and Gadget") for t in phrases.PHRASES["briefing_layout_current"]}
    assert any(result.endswith(text) for text in expected)


def test_morning_briefing_reports_unchanged_when_the_layout_matches_the_last_snapshot(monkeypatch, _isolated_memory):
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(
        actions,
        "_describe_screen_entries",
        lambda monitor_key: [(None, "Widget"), (None, "Gadget")],
    )
    actions.memory.update("last_layout", lambda _current: "Widget and Gadget")

    result = actions.morning_briefing()

    expected = {t.format(summary="Widget and Gadget") for t in phrases.PHRASES["briefing_layout_unchanged"]}
    assert any(result.endswith(text) for text in expected)


def test_morning_briefing_never_invents_notes_or_preferences(monkeypatch, _isolated_memory):
    """
    memory.json also holds "notes" and "preferences" -- nothing in this
    project writes to either yet (see morning_briefing's own docstring),
    so this confirms the briefing doesn't quietly start reporting them
    just because they happen to be non-empty in the store.
    """
    monkeypatch.setattr(actions.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(actions, "_describe_screen_entries", lambda monitor_key: [])
    actions.memory.update("notes", lambda _current: ["something"])
    actions.memory.update("preferences", lambda _current: {"honorific": "sir"})

    result = actions.morning_briefing()

    assert result in _ALL_BRIEFING_GREETINGS


def test_save_current_layout_writes_the_current_summary(monkeypatch, _isolated_memory):
    monkeypatch.setattr(
        actions,
        "_describe_screen_entries",
        lambda monitor_key: [(None, "Widget"), (None, "Widget")],
    )

    actions.save_current_layout()

    assert actions.memory.get("last_layout") == "two Widget"


# --- screenshots ----------------------------------------------------------


class _FakeImage:
    def __init__(self):
        self.saved_to = None

    def save(self, path):
        self.saved_to = path
        # A real Image.save() would fail on a directory that doesn't
        # exist -- _save_screenshot's own mkdir(parents=True) is what
        # this indirectly proves ran first.
        with open(path, "wb") as fh:
            fh.write(b"fake-png-bytes")


def test_save_screenshot_creates_the_directory_and_a_timestamped_file(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "SCREENSHOTS_DIR", tmp_path / "Screenshots")
    image = _FakeImage()

    path = actions._save_screenshot(image, "test")

    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path / "Screenshots"
    assert path.name.startswith("test_")
    assert path.suffix == ".png"


def test_save_screenshot_sanitizes_an_unsafe_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "SCREENSHOTS_DIR", tmp_path / "Screenshots")
    path = actions._save_screenshot(_FakeImage(), 'weird:/title"<>?')
    assert path is not None
    for bad_char in ':/"<>?':
        assert bad_char not in path.name


def test_save_screenshot_returns_none_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "SCREENSHOTS_DIR", tmp_path / "Screenshots")

    class _ExplodingImage:
        def save(self, path):
            raise OSError("disk full")

    assert actions._save_screenshot(_ExplodingImage(), "test") is None


def test_screenshot_full_grabs_all_screens_and_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "SCREENSHOTS_DIR", tmp_path / "Screenshots")
    calls = []
    monkeypatch.setattr(actions.ImageGrab, "grab", lambda **kw: calls.append(kw) or _FakeImage())

    result = actions.screenshot_full()

    assert result.status == "ok"
    assert calls == [{"all_screens": True}]


def test_screenshot_full_reports_failure_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "SCREENSHOTS_DIR", tmp_path / "Screenshots")
    monkeypatch.setattr(actions.ImageGrab, "grab", lambda **kw: (_ for _ in ()).throw(OSError("no display")))

    result = actions.screenshot_full()

    assert result.status == "failed"


def test_screenshot_window_with_no_focused_window_fails_honestly(monkeypatch):
    monkeypatch.setattr(actions.win32gui, "GetForegroundWindow", lambda: 0)
    result = actions.screenshot_window()
    assert result.status == "failed"


def test_screenshot_window_names_the_file_after_the_window(monkeypatch):
    monkeypatch.setattr(actions.win32gui, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(actions.win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(actions.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(actions.win32gui, "GetWindowText", lambda hwnd: "main.py - jarvis - Visual Studio Code")
    monkeypatch.setattr(actions, "_matches_app", lambda hwnd, key: key == "vscode")
    monkeypatch.setattr(actions.ImageGrab, "grab", lambda **kw: _FakeImage())
    prefixes = []
    monkeypatch.setattr(actions, "_save_screenshot", lambda image, prefix: prefixes.append(prefix) or actions.Path("x.png"))

    result = actions.screenshot_window()

    assert result.status == "ok"
    assert prefixes == [actions.app_titles.describe_title("vscode", "main.py - jarvis - Visual Studio Code")]


def test_screenshot_window_grabs_exactly_the_window_s_own_rect(monkeypatch):
    monkeypatch.setattr(actions.win32gui, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(actions.win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(actions.win32gui, "GetWindowRect", lambda hwnd: (10, 20, 300, 400))
    monkeypatch.setattr(actions.win32gui, "GetWindowText", lambda hwnd: "some window")
    monkeypatch.setattr(actions, "_matches_app", lambda hwnd, key: False)
    grabbed = []
    monkeypatch.setattr(actions.ImageGrab, "grab", lambda **kw: grabbed.append(kw) or _FakeImage())
    monkeypatch.setattr(actions, "_save_screenshot", lambda image, prefix: actions.Path("x.png"))

    actions.screenshot_window()

    assert grabbed == [{"bbox": (10, 20, 300, 400)}]
