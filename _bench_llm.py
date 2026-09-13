"""
Standalone benchmark for the local LLM intent fallback (llm_intent.py,
wired into main.py). Kept as a re-validation tool, not part of the live
pipeline -- run by hand after changing the model, prompt, or schema to
confirm latency/accuracy before trusting a change. Originally used to
validate the approach before wiring it in at all (16/17 correct,
~2.5-2.8s steady-state per call).
"""

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

TEST_UTTERANCES = [
    "open chrome",  # baseline -- embedding matcher already handles this
    "hey can you pull up chrome for me real quick",  # paraphrase
    "minimize chrome please",  # new action
    "can you open discord",  # new app
    "close vs code",  # "close" means minimize per user's own vocabulary
    "open up visual studio code",
    "pull up the file explorer",
    "open settings",
    "open my d drive",
    "switch to the next desktop",
    "go back to the previous desktop",
    "can you go to the bdk wallet repo in vscode",
    "switch over to the hashstack window in vs code",
    "how many chrome windows do i have open right now",
    "how many files are in this folder",
    "what's the weather like today",  # should map to none + a spoken reply
    "what time is it",  # should map to none + the actual current time
    "thank you",  # should map to none + a short acknowledgement
    "tell me a joke",  # should map to none + something in character
]


def main():
    print("Loading model...")
    t0 = time.time()
    llm = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
    print(f"  loaded in {time.time() - t0:.1f}s")

    grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)

    print("\nWarmup call...")
    t0 = time.time()
    llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "open chrome"},
        ],
        grammar=grammar,
        temperature=0.0,
        max_tokens=220,  # matches llm_intent.infer() -- enough for a short spoken reply plus JSON scaffolding
    )
    print(f"  warmup done in {time.time() - t0:.2f}s")

    print("\n--- benchmark ---")
    total = 0.0
    for utterance in TEST_UTTERANCES:
        t0 = time.time()
        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": utterance},
            ],
            grammar=grammar,
            temperature=0.0,
            max_tokens=220,  # matches llm_intent.infer() -- enough for a short spoken reply plus JSON scaffolding
        )
        elapsed = time.time() - t0
        total += elapsed
        content = result["choices"][0]["message"]["content"]
        print(f'{elapsed:5.2f}s  "{utterance}"')
        print(f"        -> {content}")

    print(f"\nAverage: {total / len(TEST_UTTERANCES):.2f}s per inference")


if __name__ == "__main__":
    main()
