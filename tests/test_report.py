"""
Tests for tools/_report.py -- the hand-run reader over
logs/session.jsonl. Pure functions throughout (a list of dicts in,
stdout out), so these run the same no-audio/no-models/no-window-manager
way as the rest of tests/, even though the module under test lives
outside main.py's own extraction pattern.

_report.py isn't reached via conftest.py's project-root sys.path (that
covers the root only, not tools/), so this file does its own -- mirrors
_report.py's own sys.path.insert to reach the project root from tools/,
just the other direction.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import _report


# --- _load_events -------------------------------------------------------


def test_load_events_parses_valid_jsonl(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text('{"ts": 1.0, "kind": "boot"}\n{"ts": 2.0, "kind": "shutdown"}\n', encoding="utf-8")
    events = _report._load_events(log)
    assert [e["kind"] for e in events] == ["boot", "shutdown"]


def test_load_events_skips_a_torn_line_without_aborting(tmp_path):
    """A crash mid-write can leave a partial last line -- one bad line must not lose the rest."""
    log = tmp_path / "session.jsonl"
    log.write_text('{"ts": 1.0, "kind": "boot"}\n{"ts": 2.0, "ki', encoding="utf-8")
    events = _report._load_events(log)
    assert [e["kind"] for e in events] == ["boot"]


def test_load_events_on_a_missing_file_returns_empty_list(tmp_path):
    assert _report._load_events(tmp_path / "does_not_exist.jsonl") == []


def test_load_events_skips_blank_lines(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text('{"kind": "boot"}\n\n\n{"kind": "shutdown"}\n', encoding="utf-8")
    events = _report._load_events(log)
    assert len(events) == 2


# --- _stats_line ----------------------------------------------------------


def test_stats_line_empty_input():
    assert "no data" in _report._stats_line([])


def test_stats_line_reports_median_and_max():
    line = _report._stats_line([1.0, 2.0, 3.0, 4.0, 5.0])
    assert "median=3.000" in line
    assert "max=5.000" in line
    assert "min=1.000" in line
    assert "n=5" in line


def test_stats_line_appends_the_given_unit():
    line = _report._stats_line([1.0], unit="s")
    assert "1.000s" in line


# --- report_transcript_gate: the repeated-action counter-evidence check --


def test_flags_a_candidate_whose_text_repeatedly_resolved_to_the_same_action(capsys):
    events = [
        {"kind": "transcript_would_reject", "text": "how many files", "reason": "avg_logprob", "avg_logprob": -2.1},
        {"kind": "resolved", "text": "how many files", "parsed": {"action": "count_files_in_folder"}},
        {"kind": "resolved", "text": "how many files", "parsed": {"action": "count_files_in_folder"}},
    ]
    _report.report_transcript_gate(events)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "how many files" in out
    assert "count_files_in_folder" in out


def test_does_not_flag_a_one_off_resolution():
    """
    The exact regression this exists for: a *single* resolved instance
    is the LLM hallucinating a confident action from noise just as
    easily as it's a real command -- "resolved once" alone must not
    count as counter-evidence (checked live against this project's own
    log: "oh" and "mobile key" both resolved to a plausible-looking
    action exactly once, and neither was a real command).
    """
    events = [
        {"kind": "transcript_would_reject", "text": "oh", "reason": "avg_logprob", "avg_logprob": -1.2},
        {"kind": "resolved", "text": "oh", "parsed": {"action": "minimize_app"}},
    ]
    counts = _report.defaultdict(_report.Counter)
    for e in events:
        if e["kind"] == "resolved":
            counts[e["text"]][e["parsed"]["action"]] += 1
    # One occurrence only -- the >=2 threshold in report_transcript_gate
    # must exclude this from the "repeated" bucket.
    assert counts["oh"].most_common(1)[0][1] == 1


def test_does_not_flag_when_repeats_resolve_to_different_actions():
    """Two resolutions that disagree on the action aren't repeated intent either."""
    events = [
        {"kind": "transcript_would_reject", "text": "bdk", "reason": "avg_logprob", "avg_logprob": -2.0},
        {"kind": "resolved", "text": "bdk", "parsed": {"action": "focus_vscode_repo"}},
        {"kind": "resolved", "text": "bdk", "parsed": {"action": "minimize_app"}},
    ]
    counts = _report.defaultdict(_report.Counter)
    for e in events:
        if e["kind"] == "resolved":
            counts[e["text"]][e["parsed"]["action"]] += 1
    assert counts["bdk"].most_common(1)[0][1] == 1  # each action seen once, not >=2


def test_no_match_and_none_actions_are_excluded_from_the_legitimacy_check(capsys):
    """An abstain or a chat reply is not evidence of a real dispatchable command."""
    events = [
        {"kind": "transcript_would_reject", "text": "hmm", "reason": "avg_logprob", "avg_logprob": -1.9},
        {"kind": "resolved", "text": "hmm", "parsed": {"action": "no_match"}},
        {"kind": "resolved", "text": "hmm", "parsed": {"action": "none"}},
    ]
    _report.report_transcript_gate(events)
    out = capsys.readouterr().out
    assert "WARNING" not in out


def test_no_would_reject_events_reports_cleanly(capsys):
    _report.report_transcript_gate([{"kind": "boot"}])
    out = capsys.readouterr().out
    assert "no transcript_would_reject events" in out


# --- report_clipping ------------------------------------------------------


def test_clipping_report_buckets_by_peak(capsys):
    events = [
        {"kind": "transcript", "peak": 0.05, "avg_logprob": -2.0},
        {"kind": "transcript", "peak": 0.9, "avg_logprob": -0.2},
    ]
    _report.report_clipping(events)
    out = capsys.readouterr().out
    assert "peak <0.3" in out
    assert "peak >=0.3" in out


def test_clipping_report_handles_missing_clipped_fraction_gracefully(capsys):
    """Older log lines, written before clipped_fraction existed, must not crash the report."""
    events = [{"kind": "transcript", "peak": 0.5, "avg_logprob": -0.5}]
    _report.report_clipping(events)  # must not raise
    out = capsys.readouterr().out
    assert "no clipped_fraction data yet" in out


def test_clipping_report_on_no_transcripts_at_all(capsys):
    _report.report_clipping([{"kind": "boot"}])
    out = capsys.readouterr().out
    assert "no transcript events" in out


# --- report_wake_scores -----------------------------------------------


def test_wake_scores_reports_margin_above_threshold(capsys):
    events = [{"kind": "wake_triggered", "score": 0.7}, {"kind": "wake_triggered", "score": 0.9}]
    _report.report_wake_scores(events)
    out = capsys.readouterr().out
    assert "0.7" in out or "0.700" in out


# --- report_llm_latency -----------------------------------------------


def test_llm_latency_splits_cold_and_warm(capsys):
    events = [
        {"kind": "resolved", "via_llm": True, "llm_cold": True, "llm_seconds": 20.0},
        {"kind": "resolved", "via_llm": True, "llm_cold": False, "llm_seconds": 3.0},
        {"kind": "resolved", "via_llm": False, "llm_cold": False, "llm_seconds": 0.0},  # fast-path, excluded
    ]
    _report.report_llm_latency(events)
    out = capsys.readouterr().out
    assert "cold:" in out and "warm:" in out
    assert "n=1" in out  # exactly one of each, not the excluded fast-path row


# --- report_deaf_time ---------------------------------------------------


def test_deaf_time_sums_stopped_durations(capsys):
    events = [
        {"kind": "output_playing_stopped", "seconds": 100.0, "ts": 1000.0},
        {"kind": "output_playing_stopped", "seconds": 50.0, "ts": 2000.0},
        {"kind": "boot", "ts": 500.0},
    ]
    _report.report_deaf_time(events)
    out = capsys.readouterr().out
    assert "150s" in out


def test_deaf_time_counts_only_the_gated_part_once_echo_cancellation_logs_it(capsys):
    events = [
        {"kind": "output_playing_stopped", "seconds": 200.0, "gated_seconds": 1.5, "ts": 1000.0},
        {"kind": "output_playing_stopped", "seconds": 50.0, "ts": 2000.0},  # pre-AEC line: all of it
        {"kind": "wake_triggered", "score": 0.81, "output_playing": True, "aec": "covering"},
        {"kind": "aec_coverage", "covering": False, "reason": "residual_high"},
        {"kind": "echo_onset_rejected", "frames": 7},
    ]
    _report.report_deaf_time(events)
    out = capsys.readouterr().out
    assert "52s deaf" in out
    assert "of 250s of playback" in out
    assert "wake triggers over playing audio" in out
    assert "residual_high x1" in out
    assert "7 session frames" in out


# --- report_dispatch_outcomes -------------------------------------------


def test_dispatch_outcomes_aggregates_status_and_timings(capsys):
    events = [
        {"kind": "outcome", "status": "ok", "timings": {"transcribe": 0.5, "action": 0.1}},
        {"kind": "outcome", "status": "failed", "timings": {"transcribe": 0.6}},
    ]
    _report.report_dispatch_outcomes(events)
    out = capsys.readouterr().out
    assert "ok: 1/2" in out
    assert "failed: 1/2" in out
    assert "transcribe" in out


# --- main() end to end ----------------------------------------------------


def test_main_handles_an_empty_log_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(_report, "SESSION_LOG_PATH", tmp_path / "empty.jsonl")
    _report.main()  # must not raise


def test_main_runs_all_sections_against_a_realistic_mixed_log(tmp_path, monkeypatch, capsys):
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join(
            [
                '{"kind": "transcript", "peak": 0.9, "avg_logprob": -0.3, "clipped_fraction": 0.02}',
                '{"kind": "transcript_would_reject", "text": "bdk", "reason": "avg_logprob", "avg_logprob": -2.1}',
                '{"kind": "wake_triggered", "score": 0.8}',
                '{"kind": "resolved", "via_llm": true, "llm_cold": false, "llm_seconds": 4.0, "text": "open chrome", "parsed": {"action": "focus_or_launch_app"}}',
                '{"kind": "output_playing_stopped", "seconds": 30.0, "ts": 1000.0}',
                '{"kind": "outcome", "status": "ok", "timings": {"action": 0.2}}',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_report, "SESSION_LOG_PATH", log)
    _report.main()  # must not raise
    out = capsys.readouterr().out
    for heading in ("1. Input clipping", "2. Pre-LLM gate", "3. Wake-word", "4. LLM fallback", "5. Time spent deaf", "6. Dispatch outcomes"):
        assert heading in out
