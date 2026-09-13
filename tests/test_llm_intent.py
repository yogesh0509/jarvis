"""
Tests for llm_intent.validate_parsed and _build_messages -- the
re-validation and prompt-assembly layers that run regardless of what
the grammar-constrained LLM claims to have produced. Deliberately does
not import the real Llama/GGUF model (that's llm_intent's _get_llm(),
lazy-loaded and untouched here).
"""

from llm_intent import _NO_PARAM_ACTION_NAMES, _build_messages, validate_parsed


def test_accepts_well_formed_focus_action():
    parsed = {"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": ""}
    assert validate_parsed(parsed) == parsed


def test_accepts_start_dictation():
    # A meta intent, not a real dispatchable action -- main.py checks
    # only parsed["action"] for this one, so app/direction/repo just
    # need to not cause validate_parsed to decline it outright.
    parsed = validate_parsed({"action": "start_dictation", "app": "none", "direction": "none", "repo": ""})
    assert parsed is not None
    assert parsed["action"] == "start_dictation"


def test_start_dictation_tolerates_stray_values_in_unused_fields():
    parsed = validate_parsed({"action": "start_dictation", "app": "chrome", "direction": "next", "repo": "x"})
    assert parsed is not None
    assert parsed["action"] == "start_dictation"


def test_accepts_well_formed_switch_desktop():
    parsed = {"action": "switch_desktop", "app": "none", "direction": "next", "repo": ""}
    assert validate_parsed(parsed) == parsed


def test_accepts_well_formed_focus_vscode_repo():
    parsed = {"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet"}
    assert validate_parsed(parsed) == parsed


def test_rejects_unknown_action():
    assert validate_parsed({"action": "delete_system32", "app": "none", "direction": "none", "repo": ""}) is None


def test_rejects_unregistered_app():
    assert validate_parsed({"action": "focus_or_launch_app", "app": "notepad", "direction": "none", "repo": ""}) is None


def test_rejects_bad_direction():
    assert validate_parsed({"action": "switch_desktop", "app": "none", "direction": "sideways", "repo": ""}) is None


def test_rejects_non_string_repo():
    assert validate_parsed({"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": 123}) is None


def test_declines_app_action_with_app_none():
    # A real action paired with a missing required parameter -- usually
    # the model latching onto an action-shaped word in background
    # chatter that isn't actually a command. Must decline, not dispatch
    # something guaranteed to fail downstream.
    for action in ("focus_or_launch_app", "minimize_app", "maximize_app", "count_app_windows"):
        assert validate_parsed({"action": action, "app": "none", "direction": "none", "repo": ""}) is None


def test_declines_switch_desktop_with_direction_none():
    assert validate_parsed({"action": "switch_desktop", "app": "none", "direction": "none", "repo": ""}) is None


def test_declines_switch_desktop_with_left_or_right():
    # switch_desktop only understands next/previous -- left/right are
    # snap_window's vocabulary, not this action's.
    for bad_direction in ("left", "right"):
        assert validate_parsed({"action": "switch_desktop", "app": "none", "direction": bad_direction, "repo": ""}) is None


def test_accepts_well_formed_snap_window():
    for direction in ("left", "right"):
        parsed = {"action": "snap_window", "app": "none", "direction": direction, "repo": ""}
        assert validate_parsed(parsed) == parsed


def test_declines_snap_window_with_next_or_previous():
    for bad_direction in ("next", "previous", "none"):
        assert validate_parsed({"action": "snap_window", "app": "none", "direction": bad_direction, "repo": ""}) is None


def test_accepts_no_param_actions():
    for action in _NO_PARAM_ACTION_NAMES:
        parsed = {"action": action, "app": "none", "direction": "none", "repo": ""}
        assert validate_parsed(parsed) == parsed


def test_no_param_actions_tolerate_stray_values_in_unused_fields():
    # Deliberately lenient: dispatch_llm_action calls these with no
    # arguments regardless, so a stray value the model left in an
    # unused field can't cause anything to actually go wrong -- and
    # declining over it would just be an unforced refusal.
    parsed = validate_parsed({"action": "volume_up", "app": "chrome", "direction": "next", "repo": "x"})
    assert parsed is not None
    assert parsed["action"] == "volume_up"


def test_declines_focus_vscode_repo_with_blank_repo():
    assert validate_parsed({"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "   "}) is None


def test_action_none_is_a_valid_chat_outcome_not_a_decline():
    # action "none" is the small-talk path (main.py speaks "reply"
    # directly) -- distinct from a genuinely malformed response, which
    # still collapses to None below.
    parsed = validate_parsed(
        {"action": "none", "app": "none", "direction": "none", "repo": "", "reply": "All systems nominal, sir."}
    )
    assert parsed == {
        "action": "none",
        "app": "none",
        "direction": "none",
        "repo": "",
        "reply": "All systems nominal, sir.",
    }


def test_action_none_tolerates_missing_or_non_string_reply():
    assert validate_parsed({"action": "none", "app": "none", "direction": "none", "repo": ""})["reply"] == ""
    assert (
        validate_parsed({"action": "none", "app": "none", "direction": "none", "repo": "", "reply": 123})["reply"]
        == ""
    )


def test_rejects_missing_fields_gracefully():
    assert validate_parsed({}) is None
    assert validate_parsed({"action": "focus_or_launch_app"}) is None


def test_build_messages_without_context_is_system_then_user():
    messages = _build_messages("open chrome")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1] == {"role": "user", "content": "open chrome"}


def test_build_messages_with_context_inserts_a_context_system_message():
    messages = _build_messages("minimize it", context_app="chrome")
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert 'app is "chrome"' in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "minimize it"}


def test_build_messages_omits_context_message_when_falsy():
    assert len(_build_messages("open chrome", context_app=None)) == 2
    assert len(_build_messages("open chrome", context_app="")) == 2
