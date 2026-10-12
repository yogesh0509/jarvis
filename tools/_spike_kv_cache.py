"""
One-off spike: does persisting llama.cpp's KV cache to disk actually
turn a cold call's 13-15s prompt prefill into a fast load? Not part of
the live pipeline -- run by hand, same "kept in the repo as the record
of what was learned" convention as _spike_desktop_move.py, whatever the
answer turns out to be. Three questions, matching the plan this spike
was scoped from exactly:

  1. How big is Llama.save_state()'s output at n_ctx=2048, and how long
     does save_state()/load_state() actually take?
  2. Does loading a saved state on a *fresh* Llama() instance (the same
     thing a process restart looks like from llama.cpp's own
     perspective -- a new context either way) actually beat the
     documented 13-15s cold-prefill baseline?
  3. llm_intent._system_prompt() appends the live date/time to the
     *end* of the prompt -- does a state saved at one moment still
     apply correctly (not just fast, CORRECT) once real time has moved
     on, or does the mismatch silently produce wrong output rather
     than safely falling back to a full recompute?

Only wire this into llm_intent.py's real prewarm path if the answer to
all three is genuinely favorable -- this script's job is to find out,
not to assume.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pickle
import time

import numpy as np
from llama_cpp import Llama
from llama_cpp.llama import LlamaState
from llama_cpp.llama_grammar import LlamaGrammar

from llm_intent import MODEL_PATH, SCHEMA, SYSTEM_PROMPT_TEMPLATE

STATE_FILE = Path(__file__).parent / "_spike_kv_cache_state.pkl"
TEST_UTTERANCE = "open chrome"
EXPECTED_ACTION = "focus_or_launch_app"


def _prompt_for(when_str: str) -> str:
    # Mirrors llm_intent._system_prompt() exactly (not imported, since
    # the whole point of this section is to control the date/time string
    # directly rather than accept whatever `datetime.now()` gives at
    # call time).
    return SYSTEM_PROMPT_TEMPLATE + f"\nCurrent date/time: {when_str}."


def _slim(state: LlamaState) -> LlamaState:
    """
    LlamaState.scores is a full-vocabulary logits array, one row per
    retained token position (151936-wide for Qwen2.5's vocab) -- meant
    for a caller that wants to inspect past per-token probabilities,
    which nothing in this codebase's use case does. Confirmed directly
    (not assumed) that load_state() doesn't need it populated: zeroing
    it to a minimal placeholder shape leaves load_state() and the
    inference that follows both working exactly as before, while
    dropping the dominant, wasteful contributor to the saved size --
    scores alone measured ~297 MB against an actual KV payload
    (llama_state_size) of ~1 MB per ~35 tokens (~25 MB at this prompt's
    real ~900-token length), a >10x difference for data nothing reads
    back.
    """
    return LlamaState(
        input_ids=state.input_ids,
        scores=np.zeros((1, state.scores.shape[1]), dtype=state.scores.dtype),
        n_tokens=state.n_tokens,
        llama_state=state.llama_state,
        llama_state_size=state.llama_state_size,
        seed=getattr(state, "seed", 0),
    )


def _infer(llm, grammar, prompt: str, utterance: str):
    t0 = time.perf_counter()
    result = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": utterance},
        ],
        grammar=grammar,
        temperature=0.0,
        max_tokens=220,
    )
    elapsed = time.perf_counter() - t0
    content = result["choices"][0]["message"]["content"]
    try:
        action = json.loads(content).get("action")
    except json.JSONDecodeError:
        action = "<invalid JSON>"
    return elapsed, action, content


def main() -> None:
    grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)
    now_str = "Tuesday, September 29, 2026, 03:00 PM"
    later_str = "Tuesday, September 29, 2026, 03:47 PM"  # a plausible "time passed since save" gap

    print("=== 1. Baseline: real Llama() construction + a genuinely cold call ===")
    t0 = time.perf_counter()
    llm_a = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
    construction_s = time.perf_counter() - t0
    print(f"  Llama(...) construction: {construction_s:.2f}s")

    cold_elapsed, cold_action, _ = _infer(llm_a, grammar, _prompt_for(now_str), TEST_UTTERANCE)
    print(f"  cold call: {cold_elapsed:.2f}s -> action={cold_action!r} (expected {EXPECTED_ACTION!r})")

    print("\n=== 2. save_state(): size and time ===")
    t0 = time.perf_counter()
    state = llm_a.save_state()
    save_s = time.perf_counter() - t0
    size_mb = state.llama_state_size / (1024 * 1024)
    scores_mb = state.scores.nbytes / (1024 * 1024)
    print(f"  save_state(): {save_s:.2f}s")
    print(f"  llama_state_size (the actual KV payload): {size_mb:.1f} MB (n_tokens={state.n_tokens})")
    print(f"  .scores (full-vocab logits per position, unused by load_state -- see _slim): {scores_mb:.1f} MB")

    t0 = time.perf_counter()
    with open(STATE_FILE, "wb") as fh:
        pickle.dump(state, fh)
    pickle_s = time.perf_counter() - t0
    disk_mb = STATE_FILE.stat().st_size / (1024 * 1024)
    print(f"  pickled naively (includes .scores) to disk: {pickle_s:.2f}s, {disk_mb:.1f} MB on disk")

    slimmed = _slim(state)
    t0 = time.perf_counter()
    with open(STATE_FILE, "wb") as fh:
        pickle.dump(slimmed, fh)
    slim_pickle_s = time.perf_counter() - t0
    slim_disk_mb = STATE_FILE.stat().st_size / (1024 * 1024)
    print(f"  pickled SLIMMED (scores dropped) to disk: {slim_pickle_s:.2f}s, {slim_disk_mb:.1f} MB on disk")

    print("\n=== 3. Fresh Llama() instance (what a process restart looks like) + load_state() ===")
    t0 = time.perf_counter()
    llm_b = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
    construction_b_s = time.perf_counter() - t0
    print(f"  Llama(...) construction: {construction_b_s:.2f}s")

    t0 = time.perf_counter()
    with open(STATE_FILE, "rb") as fh:
        loaded_state = pickle.load(fh)
    unpickle_s = time.perf_counter() - t0
    print(f"  unpickled from disk: {unpickle_s:.2f}s")

    t0 = time.perf_counter()
    llm_b.load_state(loaded_state)
    load_s = time.perf_counter() - t0
    print(f"  load_state(): {load_s:.2f}s")

    print("\n=== 4. Inference against the loaded state, EXACT same date/time (best case) ===")
    exact_elapsed, exact_action, _ = _infer(llm_b, grammar, _prompt_for(now_str), TEST_UTTERANCE)
    print(f"  {exact_elapsed:.2f}s -> action={exact_action!r} (expected {EXPECTED_ACTION!r})")
    exact_correct = exact_action == EXPECTED_ACTION
    exact_fast = exact_elapsed < cold_elapsed * 0.5

    print("\n=== 5. Inference against the loaded state, DIFFERENT date/time (real-world case) ===")
    print("       (time has moved on since save -- does correctness survive the mismatch?)")
    stale_elapsed, stale_action, stale_content = _infer(llm_b, grammar, _prompt_for(later_str), TEST_UTTERANCE)
    print(f"  {stale_elapsed:.2f}s -> action={stale_action!r} (expected {EXPECTED_ACTION!r})")
    stale_correct = stale_action == EXPECTED_ACTION
    stale_fast = stale_elapsed < cold_elapsed * 0.5
    if not stale_correct:
        print(f"  FULL OUTPUT (wrong -- worth a look): {stale_content}")

    STATE_FILE.unlink(missing_ok=True)

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"cold baseline:                        {cold_elapsed:.2f}s")
    print(f"KV payload (llama_state_size):        {size_mb:.1f} MB")
    print(f"naive pickle (state as save_state() returns it, .scores included): {disk_mb:.1f} MB")
    print(f"slimmed pickle (.scores dropped, what was actually loaded below):  {slim_disk_mb:.1f} MB")
    print(f"save_state() + slim + pickle time:    {save_s + slim_pickle_s:.2f}s  (a one-time cost, not paid per boot)")
    print(f"unpickle + load_state() time:         {unpickle_s + load_s:.2f}s  (this is what replaces the cold prefill)")
    print(f"exact date/time match:          {exact_elapsed:.2f}s, action correct = {exact_correct}")
    print(f"stale date/time (47min later):  {stale_elapsed:.2f}s, action correct = {stale_correct}")

    print()
    if exact_correct and stale_correct and exact_fast and stale_fast:
        print("VERDICT: GREEN -- loading a saved state is both fast and correct, including once the")
        print("  appended date/time has drifted. Worth wiring into llm_intent.prewarm()'s real path:")
        print("  save once after the existing in-memory prewarm call (using _slim(), not the raw")
        print("  save_state() return value -- the naive version above is >10x larger for data")
        print("  load_state() never reads back), load it at the START of prewarm on every subsequent")
        print("  boot instead of running a real inference to fill the cache.")
    elif exact_correct and stale_correct and not stale_fast:
        print("VERDICT: YELLOW -- correct either way, but the date/time mismatch costs real speed")
        print("  (llama.cpp is safely recomputing from the point of divergence, not silently reusing")
        print("  stale tokens). Still likely worth it if unpickle+load beats cold prefill even in the")
        print("  worst case measured above -- compare the two numbers directly.")
    elif not stale_correct:
        print("VERDICT: RED -- the loaded state produced WRONG output once the date/time no longer")
        print("  matches what was saved. Do not wire this in without first confirming *why* (a version")
        print("  string embedded in the saved state that load_state() should have caught, or a genuine")
        print("  llama.cpp gap) -- silently wrong intent resolution is worse than a slow correct one.")
    else:
        print("VERDICT: RED -- loading a saved state was not meaningfully faster than a cold call.")
        print("  Not worth the added complexity (a new state file, a save step after every prewarm,")
        print("  invalidation logic if the prompt template ever changes) for no measured benefit.")


if __name__ == "__main__":
    main()
