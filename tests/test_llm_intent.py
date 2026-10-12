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


def test_no_match_is_a_valid_abstain_outcome():
    # The model's own abstain (see llm_intent.py's "no_match" prompt
    # rule) -- distinct from both a real action and the "none" chat
    # path. main.py treats this as silent (no speech), unlike "none".
    parsed = validate_parsed(
        {"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": ""}
    )
    assert parsed == {"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": ""}


def test_no_match_forces_reply_empty_even_if_model_wrote_one():
    # Fail closed: an abstain must never become speech, so "reply" is
    # force-emptied regardless of what the model actually put there --
    # never trust the model to have left it blank on its own.
    parsed = validate_parsed(
        {"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": "I don't know, sir."}
    )
    assert parsed["reply"] == ""


def test_no_match_tolerates_stray_values_in_unused_fields():
    parsed = validate_parsed({"action": "no_match", "app": "chrome", "direction": "next", "repo": "x", "reply": "x"})
    assert parsed == {"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": ""}


def test_llm_app_keys_matches_the_schema_enum():
    # Prompt/grammar drift is exactly how the model ends up being asked
    # to pick from a list it can't actually see (or vice versa) --
    # LLM_APP_KEYS must be the single source of truth for both.
    from llm_intent import LLM_APP_KEYS, SCHEMA

    assert SCHEMA["properties"]["app"]["enum"] == [*LLM_APP_KEYS, "none"]


def test_llm_app_keys_names_all_appear_in_the_prompt_text():
    from llm_intent import LLM_APP_KEYS, SYSTEM_PROMPT_TEMPLATE

    for app_key in LLM_APP_KEYS:
        assert app_key in SYSTEM_PROMPT_TEMPLATE


def test_system_prompt_template_stays_within_a_shrink_budget():
    # A char budget, not a token budget -- this test has no model to
    # tokenize with. The prompt was cut from a measured 1292 tokens
    # (~4830 chars) down to ~750-800 tokens (~3000-3200 chars) with the
    # real tokenizer (see the prompt-shrink comment above
    # SYSTEM_PROMPT_TEMPLATE) -- this just catches it silently creeping
    # back up over time. Generous headroom since char count is only a
    # proxy for token count.
    #
    # Raised twice since the original 3600, both recorded here rather
    # than silently changed:
    #
    # 3600 -> 4000 (tools/_spike_kv_cache.py, Phase 2): every
    # prefill-token argument that motivated the original tight budget
    # was specifically about *cold-call* latency, and a cold call is no
    # longer paid on every restart -- llm_intent.prewarm() now loads a
    # persisted KV cache in well under a second instead, falling back to
    # full prefill only once per install (or once after this exact
    # template changes, which invalidates that cache by design -- see
    # KV_CACHE_PATH's own comment).
    #
    # 4000 -> 4400 (Phase 3, adding set_timer/system_status/the three
    # screenshot actions): checked directly rather than assumed --
    # "screenshot this window" without a dedicated JSON example (a rule
    # line alone wasn't enough) had the model pick "snap_window" instead
    # and claim it "can't take screenshots directly," the same failure
    # mode LLM_APP_KEYS' own comment already documents ("the model can't
    # compare against an option it was never shown in the first time").
    # Adding one compact example fixed it, verified against the real
    # model (6/6 on a held-out phrasing set), and cost the size below.
    #
    # Still a real ceiling, not removed outright: a *warm* call still
    # pays some decode-time cost proportional to prompt length, and an
    # unbounded prompt is still worth catching before it creeps -- 4400
    # leaves room to breathe while staying well under the original
    # 4830-char pre-shrink size.
    from llm_intent import SYSTEM_PROMPT_TEMPLATE

    assert len(SYSTEM_PROMPT_TEMPLATE) < 4400


def test_prompt_repo_example_is_not_a_real_user_repo_name_regression():
    # Confirmed live as a real failure mode: with "bdk wallet"/"bdk_wallet"
    # as the only concrete focus_vscode_repo example the model had ever
    # seen, bare noise ("bdk", "for what") reliably collapsed onto that
    # exact example instead of genuinely extracting a repo name from the
    # utterance -- see DECISIONS.md's "Mid-session wake-word swallow, and
    # the 'bdk wallet' false dispatches". The example now uses a fictional
    # placeholder; this pins that so a future edit doesn't quietly bring a
    # real repo name back into the one example the model gets to imitate.
    from llm_intent import SYSTEM_PROMPT_TEMPLATE

    assert "bdk" not in SYSTEM_PROMPT_TEMPLATE.lower()


def test_rejects_no_match_missing_from_action_enum_regression():
    # "no_match" must be accepted as its own outcome, never treated as
    # an unrecognized/malformed action (that would silently collapse it
    # to a decline that looks identical to a real parse failure, losing
    # the distinction main.py relies on -- see its "llm_abstain" stat).
    assert validate_parsed({"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": ""}) is not None


def test_build_messages_with_screen_context_only():
    messages = _build_messages("what's this page about", screen_context='Chrome -- "github.com" on your monitor')
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert "you're currently looking at" in messages[1]["content"]
    assert 'Chrome -- "github.com" on your monitor' in messages[1]["content"]


def test_build_messages_merges_context_app_and_screen_context_into_one_message():
    # Load-bearing for KV-prefix reuse (see _build_messages' own
    # docstring) -- both must land in the SAME second system message,
    # never a third one.
    messages = _build_messages("move this to the other screen", context_app="chrome", screen_context="Chrome -- on your monitor")
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert 'app is "chrome"' in messages[1]["content"]
    assert "Chrome -- on your monitor" in messages[1]["content"]


def test_build_messages_screen_context_alone_omits_context_app_phrasing():
    messages = _build_messages("what's this", screen_context="Chrome -- on your monitor")
    assert 'app is' not in messages[1]["content"]


def test_build_messages_with_neither_is_unchanged():
    # Exact backward-compatibility check -- the pre-existing
    # context_app-only test above already covers this shape, but this
    # pins that adding screen_context as a parameter didn't change the
    # two-message format when it's not supplied.
    assert len(_build_messages("open chrome")) == 2
