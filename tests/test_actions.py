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
    assert result == "ok"
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
    assert result == "ok"
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
    assert result == "ok"
    assert calls == ["next"]


def test_dispatch_llm_action_routes_focus_vscode_repo(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "focus_vscode_repo", lambda repo: calls.append(repo) or "ok")
    result = actions.dispatch_llm_action({"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet"})
    assert result == "ok"
    assert calls == ["bdk_wallet"]


def test_dispatch_llm_action_routes_count_files_with_no_args(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "count_files_in_folder", lambda: calls.append(True) or "ok")
    result = actions.dispatch_llm_action({"action": "count_files_in_folder", "app": "none", "direction": "none", "repo": ""})
    assert result == "ok"
    assert calls == [True]


def test_dispatch_llm_action_routes_maximize_app(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "maximize_app", lambda app: calls.append(app) or "ok")
    result = actions.dispatch_llm_action({"action": "maximize_app", "app": "vscode", "direction": "none", "repo": ""})
    assert result == "ok"
    assert calls == ["vscode"]


def test_dispatch_llm_action_routes_snap_window(monkeypatch):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, "snap_window", lambda direction: calls.append(direction) or "ok")
    result = actions.dispatch_llm_action({"action": "snap_window", "app": "none", "direction": "right", "repo": ""})
    assert result == "ok"
    assert calls == ["right"]


# Actions special-cased in dispatch_llm_action to receive `text` as a
# free-text hint even though they stay in _NO_PARAM_ACTIONS for
# parse_fast_path_intent's sake -- see that function's own docstring.
_TEXT_HINT_ACTIONS = ("describe_screen", "move_to_monitor")


def test_dispatch_llm_action_routes_no_param_actions_with_no_args(monkeypatch):
    for label in actions._NO_PARAM_ACTIONS:
        if label in _TEXT_HINT_ACTIONS:
            continue
        calls = []
        monkeypatch.setitem(actions.ACTIONS, label, lambda calls=calls: calls.append(True) or "ok")
        result = actions.dispatch_llm_action({"action": label, "app": "none", "direction": "none", "repo": ""})
        assert result == "ok"
        assert calls == [True]


@pytest.mark.parametrize("label", _TEXT_HINT_ACTIONS)
def test_dispatch_llm_action_routes_text_hint_actions_with_hint_text(monkeypatch, label):
    calls = []
    monkeypatch.setitem(actions.ACTIONS, label, lambda hint_text: calls.append(hint_text) or "ok")
    result = actions.dispatch_llm_action(
        {"action": label, "app": "none", "direction": "none", "repo": ""},
        "what's on my laptop screen",
    )
    assert result == "ok"
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
    assert result in phrases.PHRASES["unknown_action"]
