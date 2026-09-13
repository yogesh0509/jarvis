"""
Tests for seed_promotion.py: compound_label, the learned-seeds label
allowlist (the fix for a bad/hallucinated label becoming a permanent
fast-path seed), and the promotion threshold/gate itself.
"""

import json

import seed_promotion as sp


def test_compound_label_for_focus_or_launch_app():
    assert sp.compound_label("focus_or_launch_app", "chrome", "none") == "focus_or_launch_app:chrome"


def test_compound_label_for_minimize_app():
    assert sp.compound_label("minimize_app", "discord", "none") == "minimize_app:discord"


def test_compound_label_for_switch_desktop():
    assert sp.compound_label("switch_desktop", "none", "previous") == "switch_desktop:previous"


def test_compound_label_for_maximize_app():
    assert sp.compound_label("maximize_app", "vscode", "none") == "maximize_app:vscode"


def test_compound_label_for_snap_window():
    assert sp.compound_label("snap_window", "none", "left") == "snap_window:left"


def test_compound_label_for_free_text_actions_is_none():
    # focus_vscode_repo/count_files_in_folder have no fixed bucket to
    # promote an arbitrary repo name or query phrasing into.
    assert sp.compound_label("focus_vscode_repo", "vscode", "none") is None
    assert sp.compound_label("count_files_in_folder", "none", "none") is None


def test_compound_label_for_no_param_actions_is_none():
    # volume_up and friends aren't compound at all (no app/direction of
    # their own) -- not promotable through this path, they're bare
    # fast-path seeds from the start (see intent_matcher.py).
    assert sp.compound_label("volume_up", "none", "none") is None
    assert sp.compound_label("lock_workstation", "none", "none") is None


def test_valid_labels_cover_every_registered_app():
    from apps_registry import APPS

    for app in APPS:
        assert f"focus_or_launch_app:{app}" in sp._VALID_LABELS
        assert f"minimize_app:{app}" in sp._VALID_LABELS
        assert f"maximize_app:{app}" in sp._VALID_LABELS
    assert "switch_desktop:next" in sp._VALID_LABELS
    assert "switch_desktop:previous" in sp._VALID_LABELS
    assert "snap_window:left" in sp._VALID_LABELS
    assert "snap_window:right" in sp._VALID_LABELS


def test_load_learned_seeds_drops_labels_outside_the_allowlist(tmp_path, monkeypatch):
    bad_file = tmp_path / "learned_seeds.json"
    bad_file.write_text(
        json.dumps(
            {
                "focus_or_launch_app:vscode": ["legit phrase"],
                "focus_or_launch_app:not_a_real_app": ["sneaky bad seed"],
                "totally_made_up_label": ["should be dropped entirely"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", bad_file)

    loaded = sp.load_learned_seeds()

    assert loaded == {"focus_or_launch_app:vscode": ["legit phrase"]}


def test_load_learned_seeds_drops_non_string_seeds(tmp_path, monkeypatch):
    bad_file = tmp_path / "learned_seeds.json"
    bad_file.write_text(
        json.dumps({"minimize_app:chrome": ["fine one", 123, None, "another fine one"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", bad_file)

    loaded = sp.load_learned_seeds()

    assert loaded == {"minimize_app:chrome": ["fine one", "another fine one"]}


def test_load_learned_seeds_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", tmp_path / "does_not_exist.json")
    assert sp.load_learned_seeds() == {}


def test_load_learned_seeds_corrupt_json_returns_empty(tmp_path, monkeypatch):
    bad_file = tmp_path / "learned_seeds.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", bad_file)
    assert sp.load_learned_seeds() == {}


def test_promotion_requires_threshold_not_just_one_sighting(tmp_path, monkeypatch):
    log_file = tmp_path / "llm_usage_log.jsonl"
    state_file = tmp_path / "seed_promotion_state.json"
    seeds_file = tmp_path / "learned_seeds.json"
    monkeypatch.setattr(sp, "USAGE_LOG_PATH", log_file)
    monkeypatch.setattr(sp, "PROMOTION_STATE_PATH", state_file)
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", seeds_file)

    # Two near-duplicate hallucinations of the same real phrase, seen
    # once each -- exactly the scenario that (at the old threshold of 2)
    # nearly promoted a bad seed in this project's actual usage log.
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"text": "some hallucinated phrase", "label": "focus_or_launch_app:vscode"}) + "\n")

    promoted = sp.maybe_run_promotion({}, force=True)
    assert promoted == []
    assert not seeds_file.exists()


def test_promotion_fires_once_threshold_is_met(tmp_path, monkeypatch):
    log_file = tmp_path / "llm_usage_log.jsonl"
    state_file = tmp_path / "seed_promotion_state.json"
    seeds_file = tmp_path / "learned_seeds.json"
    monkeypatch.setattr(sp, "USAGE_LOG_PATH", log_file)
    monkeypatch.setattr(sp, "PROMOTION_STATE_PATH", state_file)
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", seeds_file)

    entry = json.dumps({"text": "pull up chrome please", "label": "focus_or_launch_app:chrome"})
    with open(log_file, "w", encoding="utf-8") as f:
        for _ in range(sp.PROMOTION_THRESHOLD):
            f.write(entry + "\n")

    promoted = sp.maybe_run_promotion({}, force=True)
    assert len(promoted) == 1
    saved = json.loads(seeds_file.read_text(encoding="utf-8"))
    assert saved == {"focus_or_launch_app:chrome": ["pull up chrome please"]}


def test_promotion_skips_phrase_already_in_existing_seeds(tmp_path, monkeypatch):
    log_file = tmp_path / "llm_usage_log.jsonl"
    state_file = tmp_path / "seed_promotion_state.json"
    seeds_file = tmp_path / "learned_seeds.json"
    monkeypatch.setattr(sp, "USAGE_LOG_PATH", log_file)
    monkeypatch.setattr(sp, "PROMOTION_STATE_PATH", state_file)
    monkeypatch.setattr(sp, "LEARNED_SEEDS_PATH", seeds_file)

    entry = json.dumps({"text": "open chrome", "label": "focus_or_launch_app:chrome"})
    with open(log_file, "w", encoding="utf-8") as f:
        for _ in range(sp.PROMOTION_THRESHOLD):
            f.write(entry + "\n")

    existing = {"focus_or_launch_app:chrome": ["open chrome"]}
    promoted = sp.maybe_run_promotion(existing, force=True)
    assert promoted == []
