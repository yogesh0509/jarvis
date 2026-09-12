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

MODEL_PATH = r"D:\jarvis\models\qwen2.5-1.5b-instruct-gguf\qwen2.5-1.5b-instruct-q4_k_m.gguf"

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "focus_or_launch_app",
                "minimize_app",
                "switch_desktop",
                "focus_vscode_repo",
                "count_app_windows",
                "count_files_in_folder",
                "none",
            ],
        },
        "app": {
            "type": "string",
            "enum": ["chrome", "discord", "vscode", "file_explorer", "settings", "local_disk_d", "none"],
        },
        "direction": {"type": "string", "enum": ["next", "previous", "none"]},
        "repo": {"type": "string"},
    },
    "required": ["action", "app", "direction", "repo"],
}

SYSTEM_PROMPT = """You turn a spoken command into a structured action. Respond with ONLY a JSON object, no other text.

Fields:
- action: one of focus_or_launch_app, minimize_app, switch_desktop, focus_vscode_repo, count_app_windows, count_files_in_folder, none
- app: the app involved (chrome, discord, vscode, file_explorer, settings, local_disk_d), or "none" if not applicable
- direction: for switch_desktop only, "next" or "previous", else "none"
- repo: for focus_vscode_repo only, the repository/folder name mentioned, else ""

If the command doesn't match any action, set action to "none".

Examples:
"open chrome" -> {"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": ""}
"can you minimize discord for me" -> {"action": "minimize_app", "app": "discord", "direction": "none", "repo": ""}
"go to the next desktop" -> {"action": "switch_desktop", "app": "none", "direction": "next", "repo": ""}
"switch to the bdk wallet window in vscode" -> {"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet"}
"open my d drive" -> {"action": "focus_or_launch_app", "app": "local_disk_d", "direction": "none", "repo": ""}
"what's the weather like" -> {"action": "none", "app": "none", "direction": "none", "repo": ""}
"""

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
    "what's the weather like today",  # should map to none
    "thank you",  # should map to none
]


def main():
    print("Loading model...")
    t0 = time.time()
    llm = Llama(model_path=MODEL_PATH, n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
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
        max_tokens=100,
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
            max_tokens=100,
        )
        elapsed = time.time() - t0
        total += elapsed
        content = result["choices"][0]["message"]["content"]
        print(f'{elapsed:5.2f}s  "{utterance}"')
        print(f"        -> {content}")

    print(f"\nAverage: {total / len(TEST_UTTERANCES):.2f}s per inference")


if __name__ == "__main__":
    main()
