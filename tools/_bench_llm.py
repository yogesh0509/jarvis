"""
Standalone benchmark for the local LLM intent fallback (llm_intent.py,
wired into main.py). Kept as a re-validation tool, not part of the live
pipeline -- run by hand after changing the model, prompt, or schema to
confirm latency/accuracy before trusting a change.

Two things this version fixes over the original: it used to call
warmup() *before* timing anything, so every timed call was a KV-prefix
hit -- that produced the "~2.5-2.8s steady-state" figure once recorded
in llm_intent.py's docstring and DECISIONS.md's decision log, and never
measured what production actually pays on its first real call. This
version times the COLD first call as its own, clearly separate number,
and reports prompt/completion token counts (from the API's own "usage"
field) so a prefill regression can be told apart from a decode-length
regression. It also used to just print raw JSON for a human to eyeball
-- it now checks each utterance against an expected action and prints a
pass/fail count.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import time

from llama_cpp import Llama
from llama_cpp.llama_grammar import LlamaGrammar

from llm_intent import MODEL_PATH, SCHEMA, _system_prompt

SYSTEM_PROMPT = _system_prompt()
# ^ Imported straight from llm_intent.py rather than duplicated here --
# this file drifted out of sync with the live schema/prompt once
# already (missing the "reply" field entirely) before this. Recomputed
# per run (not a fixed string) since _system_prompt() bakes in the
# current date/time now, same as the live path.

# (utterance, expected_action) -- expected_action is None for anything
# that should map to a chat reply ("none") or an abstain ("no_match");
# those are checked by action name only, not by exact reply text (an
# in-character reply is deliberately non-deterministic in wording).
TEST_UTTERANCES = [
    ("open chrome", "focus_or_launch_app"),  # baseline -- embedding matcher already handles this
    ("hey can you pull up chrome for me real quick", "focus_or_launch_app"),  # paraphrase
    ("minimize chrome please", "minimize_app"),
    ("can you open discord", "focus_or_launch_app"),
    ("close vs code", "minimize_app"),  # "close" means minimize per user's own vocabulary
    ("open up visual studio code", "focus_or_launch_app"),
    ("pull up the file explorer", "focus_or_launch_app"),
    ("open settings", "focus_or_launch_app"),
    ("open my d drive", "focus_or_launch_app"),
    ("switch to the next desktop", "switch_desktop"),
    ("go back to the previous desktop", "switch_desktop"),
    ("can you go to the bdk wallet repo in vscode", "focus_vscode_repo"),
    ("switch over to the hashstack window in vs code", "focus_vscode_repo"),
    ("how many chrome windows do i have open right now", "count_app_windows"),
    ("how many files are in this folder", "count_files_in_folder"),
    ("what's the weather like today", "none"),  # should map to none + a spoken reply
    ("what time is it", "none"),  # should map to none + the actual current time
    ("thank you", "none"),  # should map to none + a short acknowledgement
    ("tell me a joke", "none"),  # should map to none + something in character
    # --- regression coverage for behavior the prompt shrink dropped an
    # explicit few-shot example for, but is still supposed to work off
    # the enum + rules alone ---
    ("snap this window to the right", "snap_window"),
    ("maximize discord", "maximize_app"),
    ("skip this track", "media_next_track"),
    ("mute", "toggle_mute"),
    ("take dictation for me", "start_dictation"),
    ("open notepad", "none"),  # not registered -- must decline, never guess
    # --- the new abstain path, using the real hallucinated transcripts
    # from logs/llm_usage_log.jsonl that used to get confidently (mis)dispatched ---
    ("or been grown for me", "no_match"),
    ("hey job is", "no_match"),
    ("follow down", "no_match"),
]


def _time_call(llm, grammar, utterance: str):
    t0 = time.perf_counter()
    result = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": utterance},
        ],
        grammar=grammar,
        temperature=0.0,
        max_tokens=220,  # matches llm_intent.infer() -- enough for a short spoken reply plus JSON scaffolding
    )
    elapsed = time.perf_counter() - t0
    return elapsed, result


def main():
    print("Loading model (this is NOT the number that matters for cold latency -- see below)...")
    t0 = time.perf_counter()
    llm = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
    print(f"  Llama(...) construction: {time.perf_counter() - t0:.2f}s")

    grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)

    prompt_chars = len(SYSTEM_PROMPT)
    print(f"\nSystem prompt: {prompt_chars} chars")

    print("\n--- COLD call (this is the number production actually pays on first use) ---")
    cold_elapsed, cold_result = _time_call(llm, grammar, TEST_UTTERANCES[0][0])
    usage = cold_result.get("usage", {})
    print(f"  {cold_elapsed:.2f}s  prompt_tokens={usage.get('prompt_tokens')}  completion_tokens={usage.get('completion_tokens')}")
    print(f"        -> {cold_result['choices'][0]['message']['content']}")

    print("\n--- warm calls (KV prefix already resident -- this is the *repeat* cost, not the first) ---")
    total = 0.0
    passed = 0
    failed = []
    for utterance, expected_action in TEST_UTTERANCES[1:]:
        elapsed, result = _time_call(llm, grammar, utterance)
        total += elapsed
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        try:
            action = json.loads(content).get("action")
        except json.JSONDecodeError:
            action = "<invalid JSON>"
        ok = action == expected_action
        passed += ok
        if not ok:
            failed.append((utterance, expected_action, action))
        mark = "OK  " if ok else "FAIL"
        print(
            f"{mark}  {elapsed:5.2f}s  prompt={usage.get('prompt_tokens')!s:>4} "
            f'completion={usage.get("completion_tokens")!s:>3}  "{utterance}"'
        )
        print(f"          -> {content}")

    n_warm = len(TEST_UTTERANCES) - 1
    print(f"\nWarm average: {total / n_warm:.2f}s per inference over {n_warm} calls")
    print(f"Accuracy (warm calls only, action-name match): {passed}/{n_warm}")
    if failed:
        print("\nFailed:")
        for utterance, expected_action, action in failed:
            print(f'  "{utterance}" -> expected action={expected_action!r}, got {action!r}')


if __name__ == "__main__":
    main()
