"""
Phase 5: local LLM intent fallback for phrases the embedding matcher
(intent_matcher.py) can't confidently place. Qwen2.5-1.5B-Instruct,
GGUF Q4_K_M, via llama-cpp-python -- benchmarked standalone first
(16/17 correct on a realistic test set, ~2.5-2.8s steady-state per
call after the one-time ~6s first-load cost) before wiring in.

Only ever selects/parameterizes a fixed action schema via
grammar-constrained decoding (GBNF, built from a JSON schema) -- it is
structurally impossible for this to emit anything outside the schema,
the same allowlist-in-code security principle as everywhere else in
this project, enforced one layer earlier through constrained
generation rather than a Python dict lookup alone. Every field is
still re-validated in infer() below regardless -- defense in depth,
never trust a single layer, especially not an LLM's.

Model file fetched by hand into
models/qwen2.5-1.5b-instruct-gguf/, same huggingface.co workaround as
everywhere else in this project. Installed via a prebuilt wheel
(--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu)
rather than pip's default sdist build -- the sdist bundles llama.cpp's
full source tree including a deeply-nested Svelte web UI that exceeds
Windows' 260-char MAX_PATH during extraction.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from apps_registry import APPS

if TYPE_CHECKING:
    from llama_cpp import Llama
    from llama_cpp.llama_grammar import LlamaGrammar

MODEL_PATH = Path(__file__).parent / "models" / "qwen2.5-1.5b-instruct-gguf" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"

_APP_KEYS = list(APPS.keys())
_ACTION_NAMES = [
    "focus_or_launch_app",
    "minimize_app",
    "switch_desktop",
    "focus_vscode_repo",
    "count_app_windows",
    "count_files_in_folder",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [*_ACTION_NAMES, "none"]},
        "app": {"type": "string", "enum": [*_APP_KEYS, "none"]},
        "direction": {"type": "string", "enum": ["next", "previous", "none"]},
        "repo": {"type": "string"},
    },
    "required": ["action", "app", "direction", "repo"],
}

SYSTEM_PROMPT = f"""You turn a spoken command into a structured action. Respond with ONLY a JSON object, no other text.

Fields:
- action: one of {", ".join(_ACTION_NAMES)}, none
- app: the app involved ({", ".join(_APP_KEYS)}), or "none" if not applicable
- direction: for switch_desktop only, "next" or "previous", else "none"
- repo: for focus_vscode_repo only, the repository/folder name mentioned, else ""

Rules:
- "close" means minimize, never actually closing/quitting anything.
- Only use focus_vscode_repo when a specific repo/folder/project name is mentioned. Just "open vscode" or "open visual studio code" with no specific name is focus_or_launch_app with app "vscode".
- If the command doesn't match any action, set action to "none".

Examples:
"open chrome" -> {{"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": ""}}
"can you minimize discord for me" -> {{"action": "minimize_app", "app": "discord", "direction": "none", "repo": ""}}
"open vs code" -> {{"action": "focus_or_launch_app", "app": "vscode", "direction": "none", "repo": ""}}
"go to the next desktop" -> {{"action": "switch_desktop", "app": "none", "direction": "next", "repo": ""}}
"switch to the bdk wallet window in vscode" -> {{"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet"}}
"open my d drive" -> {{"action": "focus_or_launch_app", "app": "local_disk_d", "direction": "none", "repo": ""}}
"how many chrome windows are open" -> {{"action": "count_app_windows", "app": "chrome", "direction": "none", "repo": ""}}
"how many files are in this folder" -> {{"action": "count_files_in_folder", "app": "none", "direction": "none", "repo": ""}}
"what's the weather like" -> {{"action": "none", "app": "none", "direction": "none", "repo": ""}}
"""

_llm: "Optional[Llama]" = None
_grammar: "Optional[LlamaGrammar]" = None


def _get_llm() -> "Llama":
    # Importing llama_cpp itself (not just loading the model weights)
    # loads a compiled native backend -- deferred here, same as the
    # weights, so `import llm_intent` at main.py's startup stays cheap
    # for the common case where a session never needs the LLM fallback.
    global _llm, _grammar
    if _llm is None:
        from llama_cpp import Llama
        from llama_cpp.llama_grammar import LlamaGrammar

        _llm = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
        _grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)
    return _llm


def is_loaded() -> bool:
    return _llm is not None


def unload() -> None:
    global _llm, _grammar
    _llm = None
    _grammar = None


def infer(text: str) -> Optional[dict]:
    """
    Returns a validated action dict, or None if the LLM said "none" or
    produced something invalid. Every field is re-checked against the
    real enums here even though the grammar should already guarantee
    this structurally -- never trust a single layer blindly.
    """
    llm = _get_llm()
    try:
        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            grammar=_grammar,
            temperature=0.0,
            max_tokens=100,
        )
        parsed = json.loads(result["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return None

    action = parsed.get("action")
    if action not in _ACTION_NAMES:
        return None

    app = parsed.get("app")
    if app not in (*_APP_KEYS, "none"):
        return None

    direction = parsed.get("direction")
    if direction not in ("next", "previous", "none"):
        return None

    repo = parsed.get("repo", "")
    if not isinstance(repo, str):
        return None

    # The grammar allows app/direction/repo "none"/"" regardless of
    # action, since one JSON schema covers every action -- but a real
    # action still needs its own required parameter. An action paired
    # with a missing parameter isn't a valid inference (usually the
    # model latching onto an action-shaped word in background chatter
    # that isn't actually a command) -- decline rather than dispatch
    # something guaranteed to fail with "I don't have 'none' registered".
    if action in ("focus_or_launch_app", "minimize_app", "count_app_windows") and app == "none":
        return None
    if action == "switch_desktop" and direction == "none":
        return None
    if action == "focus_vscode_repo" and not repo.strip():
        return None

    return {"action": action, "app": app, "direction": direction, "repo": repo}
