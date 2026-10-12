"""
Hand-run reader over logs/session.jsonl -- run by hand after a stretch
of real use, same precedent as _bench_llm.py and seed_promotion.py:
not part of the live pipeline, not wired into jarvisctl.py (that socket
has a 3s client read timeout; this is analysis, not control, and reads
the whole log file which can take longer than that on a long-running
install).

Answers exactly the questions this project's own decision log says are
blocked on data, not a guess:

- TRANSCRIPT_GATE_ENFORCED (main.py) is off because, at the sample size
  when it was written, no single avg_logprob threshold separated
  confirmed noise from a real, frequently-used command. Section 2 below
  is that exact check, re-run against however much log has accumulated
  since.
- WAKE_THRESHOLD was tuned, per its own comment, without ever having a
  record of what a real wake trigger actually scores. Section 3 is that
  record.
- The clipping diagnosis this project's DECISIONS.md records (a mic
  sitting at 100%, producing confident-but-wrong transcripts) was found
  by hand, once, from a snapshot of this same file. Section 1
  reproduces that analysis as a repeatable check, not a one-off.

Every section degrades gracefully on missing fields -- clipped_fraction
and status, in particular, don't exist on log lines written before the
code that adds them did, and a long-running log will always have a mix.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import SESSION_LOG_PATH, WAKE_THRESHOLD  # noqa: E402 -- see sys.path.insert above


def _load_events(path: Path) -> "list[dict]":
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line from a crash mid-write -- skip, don't abort the whole report
    except FileNotFoundError:
        pass
    return events


def _stats_line(values: "list[float]", unit: str = "") -> str:
    if not values:
        return "  (no data)"
    values = sorted(values)
    n = len(values)
    median = statistics.median(values)
    p90 = values[int(n * 0.9)] if n > 1 else values[0]
    return f"  n={n:<5} median={median:.3f}{unit}  p90={p90:.3f}{unit}  max={values[-1]:.3f}{unit}  min={values[0]:.3f}{unit}"


def report_clipping(events: "list[dict]") -> None:
    print("\n=== 1. Input clipping (peak vs. Whisper's own confidence) ===")
    transcripts = [e for e in events if e.get("kind") == "transcript"]
    if not transcripts:
        print("  no transcript events logged yet")
        return

    with_peak = [e for e in transcripts if isinstance(e.get("peak"), (int, float))]
    quiet = [e["avg_logprob"] for e in with_peak if e["peak"] < 0.3 and isinstance(e.get("avg_logprob"), (int, float))]
    loud = [e["avg_logprob"] for e in with_peak if e["peak"] >= 0.3 and isinstance(e.get("avg_logprob"), (int, float))]
    print(f"  peak <0.3  (quiet/near-silence):{_stats_line(quiet)[2:]}" if quiet else "  peak <0.3 : (no data)")
    print(f"  peak >=0.3 (loud enough to clip):{_stats_line(loud)[2:]}" if loud else "  peak >=0.3: (no data)")
    if quiet and loud and statistics.median(quiet) < statistics.median(loud):
        print(
            "  -> quieter transcripts score WORSE (lower avg_logprob) than loud ones here, "
            "consistent with a healthy mic level -- clipping is not the dominant failure mode right now."
        )
    elif loud and statistics.median(loud) < -0.5:
        print("  -> loud transcripts are still scoring notably low -- worth checking the mic level (see audio_endpoint.get_input_volume).")

    clipped_fractions = [e["clipped_fraction"] for e in transcripts if isinstance(e.get("clipped_fraction"), (int, float))]
    if clipped_fractions:
        heavily_clipped = sum(1 for c in clipped_fractions if c >= 0.10)
        print(f"\n  clipped_fraction across {len(clipped_fractions)} transcripts:{_stats_line(clipped_fractions)[2:]}")
        print(f"  {heavily_clipped}/{len(clipped_fractions)} ({heavily_clipped / len(clipped_fractions):.0%}) at or above the 0.10 shadow-gate threshold")
    else:
        print("\n  no clipped_fraction data yet (only present on transcripts logged after that field was added)")


def report_transcript_gate(events: "list[dict]") -> None:
    """
    A single resolved instance of a would-reject candidate's exact text
    is NOT itself proof the gate would reject a real command --
    checked directly against this exact log, not assumed: "oh" and
    "mobile key" both show up "resolved to a real action" (via_llm=True,
    a plausible-looking action attached), but that resolution is itself
    the LLM hallucinating a confident action from noise -- precisely
    the failure mode this gate exists to catch, not a counter-example
    to it. A `resolved` event records "the LLM produced *something*",
    never "the user actually meant this."

    The one thing this log genuinely can distinguish: the SAME text
    resolving to the SAME action *more than once*. A one-off hallucination
    landing on a plausible action by chance is far less likely to repeat
    identically than a real, recurring phrasing is. That repetition,
    not mere presence in a `resolved` event, is the real counter-evidence
    to look for -- and it's exactly how DECISIONS.md's own prior manual
    finding ("how many files", asked repeatedly) was actually reasoned
    about, not a new standard invented for this tool.
    """
    print("\n=== 2. Pre-LLM gate: would-reject vs. real dispatches (TRANSCRIPT_GATE_ENFORCED) ===")
    would_reject = [e for e in events if e.get("kind") == "transcript_would_reject"]
    if not would_reject:
        print("  no transcript_would_reject events logged yet (gate has never had a candidate to flag)")
        return

    # action(s) seen for each exact text, across every resolution ever
    # logged for it (fast-path or LLM) -- a text that always resolves
    # to the same action, more than once, is doing something a single
    # noise-hallucination is unlikely to do by chance.
    actions_by_text = defaultdict(Counter)
    for e in events:
        if e.get("kind") == "resolved" and isinstance(e.get("parsed"), dict):
            action = e["parsed"].get("action")
            if action not in (None, "none", "no_match"):
                actions_by_text[e.get("text", "").strip().lower()][action] += 1

    by_reason = Counter(e.get("reason") for e in would_reject)
    print(f"  {len(would_reject)} total, by reason:")
    for reason, count in by_reason.most_common():
        print(f"    {reason}: {count}")

    logprobs = [e["avg_logprob"] for e in would_reject if isinstance(e.get("avg_logprob"), (int, float))]
    if logprobs:
        print(f"\n  avg_logprob of flagged candidates:{_stats_line(logprobs)[2:]}")

    repeated = []
    for e in would_reject:
        text = e.get("text", "").strip().lower()
        counts = actions_by_text.get(text)
        if counts and counts.most_common(1)[0][1] >= 2:
            repeated.append((e, counts.most_common(1)[0]))

    if repeated:
        floor = min(e["avg_logprob"] for e, _ in repeated if isinstance(e.get("avg_logprob"), (int, float)))
        print(
            f"\n  WARNING: {len(repeated)} flagged candidate(s) share text that resolved to the SAME "
            f"action more than once elsewhere in this log -- real repeated intent, not a one-off "
            f"resolution, is the strong counter-evidence here. Lowest shared avg_logprob: {floor:.2f}."
        )
        for e, (action, count) in repeated[:5]:
            print(f'    "{e["text"]}"  (avg_logprob={e.get("avg_logprob")}, resolved to {action!r} x{count})')
    else:
        print(
            "\n  No flagged candidate's exact text resolved to the same action more than once "
            "elsewhere in this log -- no strong evidence yet that enforcing the gate would reject "
            "a real, recurring command. Still not sufficient on its own to flip "
            "TRANSCRIPT_GATE_ENFORCED (a different, unlogged phrasing of a real command could still "
            "score just as low, and a real command only ever said once wouldn't show up as a repeat "
            "either); it's the absence of a known counter-example, not proof there isn't one."
        )


def report_wake_scores(events: "list[dict]") -> None:
    print("\n=== 3. Wake-word trigger scores vs. WAKE_THRESHOLD ===")
    triggers = [e for e in events if e.get("kind") == "wake_triggered"]
    if not triggers:
        print(f"  no wake_triggered events logged yet (WAKE_THRESHOLD={WAKE_THRESHOLD} was tuned by ear, not from a record of real scores)")
        return
    scores = [e["score"] for e in triggers if isinstance(e.get("score"), (int, float))]
    print(f"  WAKE_THRESHOLD = {WAKE_THRESHOLD}")
    print(f"  real trigger scores:{_stats_line(scores)[2:]}")
    margin = min(scores) - WAKE_THRESHOLD if scores else None
    if margin is not None:
        print(f"  narrowest margin above threshold: {margin:+.3f}")


def report_llm_latency(events: "list[dict]") -> None:
    print("\n=== 4. LLM fallback latency, cold vs. warm ===")
    resolved = [e for e in events if e.get("kind") == "resolved" and e.get("via_llm")]
    if not resolved:
        print("  no LLM-resolved events logged yet")
        return
    cold = [e["llm_seconds"] for e in resolved if e.get("llm_cold") and isinstance(e.get("llm_seconds"), (int, float))]
    warm = [e["llm_seconds"] for e in resolved if not e.get("llm_cold") and isinstance(e.get("llm_seconds"), (int, float))]
    print(f"  cold:{_stats_line(cold, 's')[2:]}")
    print(f"  warm:{_stats_line(warm, 's')[2:]}")
    if cold and warm:
        print(f"  prewarm is saving roughly {statistics.median(cold) - statistics.median(warm):.1f}s per cold call it prevents")


def report_deaf_time(events: "list[dict]") -> None:
    """
    Deaf time is the *gated* part of each playback stretch. Before echo
    cancellation that was all of it (and older log lines only carry
    `seconds`, so they count in full); with echo_canceller.py covering,
    `gated_seconds` is just the convergence blip at the start of a song.
    """
    print("\n=== 5. Time spent deaf to system output (output gate) ===")
    stopped = [e for e in events if e.get("kind") == "output_playing_stopped" and isinstance(e.get("seconds"), (int, float))]
    if not stopped:
        print("  no output_playing_stopped events logged yet")
        return
    total_playing = sum(e["seconds"] for e in stopped)
    total_deaf = sum(
        e["gated_seconds"] if isinstance(e.get("gated_seconds"), (int, float)) else e["seconds"] for e in stopped
    )
    timestamps = [e["ts"] for e in events if isinstance(e.get("ts"), (int, float))]
    span = (max(timestamps) - min(timestamps)) if len(timestamps) > 1 else 0
    print(f"  {len(stopped)} playback stretches, {total_deaf:.0f}s deaf to hands-free wake in total")
    if total_playing > 0:
        print(f"  {total_deaf / total_playing:.0%} of {total_playing:.0f}s of playback")
    if span > 0:
        print(f"  {total_deaf / span:.1%} of the {span / 3600:.1f}h this log spans")

    # Echo cancellation's side of it -- only present once it shipped.
    over_audio = [
        e for e in events
        if e.get("kind") == "wake_triggered" and e.get("output_playing") and isinstance(e.get("score"), (int, float))
    ]
    if over_audio:
        print(f"  wake triggers over playing audio (wanted, or residual echo -- check what followed):"
              f"{_stats_line([e['score'] for e in over_audio])[2:]}")
    lost = Counter(e.get("reason") for e in events if e.get("kind") == "aec_coverage" and e.get("covering") is False)
    if lost:
        print("  echo cancellation lost mid-playback: " + ", ".join(f"{r} x{n}" for r, n in lost.most_common()))
    rejected = sum(e.get("frames", 0) for e in events if e.get("kind") == "echo_onset_rejected")
    if rejected:
        print(f"  {rejected} session frames VAD called speech but the echo floor refused "
              f"(residual echo -- or a quiet real command, if it ever coincides with one)")


def report_dispatch_outcomes(events: "list[dict]") -> None:
    """
    Not in the original plan for this tool -- added because Phase 1's
    actions.Outcome work means every "outcome" event now carries a real
    status field, and "how often do dispatches actually fail" is
    exactly the question that field exists to answer.
    """
    print("\n=== 6. Dispatch outcomes (actions.Outcome status, bonus section) ===")
    outcomes = [e for e in events if e.get("kind") == "outcome" and e.get("status") is not None]
    if not outcomes:
        print("  no status-carrying outcome events yet (only present after actions.Outcome shipped)")
        return
    by_status = Counter(e["status"] for e in outcomes)
    total = len(outcomes)
    for status, count in by_status.most_common():
        print(f"  {status}: {count}/{total} ({count / total:.0%})")

    timed = defaultdict(list)
    for e in outcomes:
        for stage, seconds in (e.get("timings") or {}).items():
            if isinstance(seconds, (int, float)):
                timed[stage].append(seconds)
    if timed:
        print("\n  per-stage timing, across all logged outcomes:")
        for stage in ("transcribe", "match", "llm", "action"):
            if stage in timed:
                print(f"    {stage:<11}{_stats_line(timed[stage], 's')[2:]}")


def main() -> None:
    events = _load_events(SESSION_LOG_PATH)
    print(f"logs/session.jsonl: {len(events)} events" + ("" if events else " (nothing logged yet)"))
    if not events:
        return

    report_clipping(events)
    report_transcript_gate(events)
    report_wake_scores(events)
    report_llm_latency(events)
    report_deaf_time(events)
    report_dispatch_outcomes(events)


if __name__ == "__main__":
    main()
