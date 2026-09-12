"""
Phase 5: auto-promotes frequently-seen LLM-fallback utterances into
the fast embedding-matcher path, so a phrasing that started out
needing the ~2.5-3s LLM detour becomes instant once it's proven to be
something the user actually says regularly. Checked once at startup
("is 24h+ elapsed since the last run"), not a persistent background
timer -- simplest thing that satisfies "updates roughly once a day".

Scope: only the bounded-enum actions (focus_or_launch_app, minimize_app,
switch_desktop) get logged/promoted here. Their compound labels
("focus_or_launch_app:chrome", "switch_desktop:next", ...) are a small
fixed set that makes sense as embedding-matcher seed buckets. Free-text
actions (focus_vscode_repo, count_files_in_folder) are never logged --
there's no fixed bucket to promote an arbitrary repo name or a query
phrasing into.

Promotion requires the same (text, label) pair to have been seen at
least PROMOTION_THRESHOLD times, not just once -- guards against a
single LLM misclassification becoming a permanent bad fast-path seed.

Not run automatically at startup -- embedding a bigger seed list adds
measurable startup time, and the user would rather trigger this by
hand when they know a new phrasing has become common. Run it directly
(`python seed_promotion.py`) to check the log and promote anything
that qualifies, ignoring the 24h gate.
"""

import json
import time
from collections import Counter
from pathlib import Path

USAGE_LOG_PATH = Path(__file__).parent / "llm_usage_log.jsonl"
LEARNED_SEEDS_PATH = Path(__file__).parent / "learned_seeds.json"
PROMOTION_STATE_PATH = Path(__file__).parent / "seed_promotion_state.json"

PROMOTION_INTERVAL_SECONDS = 24 * 60 * 60
PROMOTION_THRESHOLD = 2


def compound_label(action: str, app: str, direction: str) -> str | None:
    if action in ("focus_or_launch_app", "minimize_app"):
        return f"{action}:{app}"
    if action == "switch_desktop":
        return f"{action}:{direction}"
    return None


def log_llm_resolution(text: str, parsed: dict) -> None:
    label = compound_label(parsed.get("action", ""), parsed.get("app", "none"), parsed.get("direction", "none"))
    if label is None:
        return
    entry = {"text": text.strip().lower(), "label": label, "ts": time.time()}
    with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_learned_seeds() -> dict[str, list[str]]:
    if not LEARNED_SEEDS_PATH.exists():
        return {}
    try:
        return json.loads(LEARNED_SEEDS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_state() -> dict:
    if not PROMOTION_STATE_PATH.exists():
        return {"last_run": 0}
    try:
        return json.loads(PROMOTION_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_run": 0}


def _save_state(state: dict) -> None:
    PROMOTION_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def maybe_run_promotion(existing_seeds: dict[str, list[str]], force: bool = False) -> list[str]:
    """
    Runs the promotion pass if 24h+ have passed since the last one, or
    always when `force=True` (the manual `python seed_promotion.py`
    entry point below uses this). `existing_seeds` (the static INTENTS
    table) is checked so a phrase already covered isn't promoted again.
    Returns human-readable descriptions of what got promoted -- empty
    if nothing ran or nothing qualified.
    """
    state = _load_state()
    if not force and time.time() - state.get("last_run", 0) < PROMOTION_INTERVAL_SECONDS:
        return []

    if not USAGE_LOG_PATH.exists():
        _save_state({"last_run": time.time()})
        return []

    counts: Counter = Counter()
    with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts[(entry["text"], entry["label"])] += 1

    learned = load_learned_seeds()
    promoted = []
    for (text, label), count in counts.items():
        if count < PROMOTION_THRESHOLD:
            continue
        already_known = text in existing_seeds.get(label, []) or text in learned.get(label, [])
        if already_known:
            continue
        learned.setdefault(label, []).append(text)
        promoted.append(f'"{text}" -> {label} (seen {count} times)')

    if promoted:
        LEARNED_SEEDS_PATH.write_text(json.dumps(learned, indent=2), encoding="utf-8")

    _save_state({"last_run": time.time()})
    return promoted


if __name__ == "__main__":
    from intent_matcher import INTENTS

    print("Checking the LLM usage log for phrases to promote...")
    promoted = maybe_run_promotion(INTENTS, force=True)
    if promoted:
        print(f"Promoted {len(promoted)} phrase(s) into the fast path:")
        for line in promoted:
            print(f"  {line}")
        print("Restart Jarvis for these to take effect.")
    else:
        print("Nothing new to promote yet.")
