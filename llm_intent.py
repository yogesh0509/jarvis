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

Small-talk fallback: the schema also carries a free-text "reply" field,
filled in only when action is "none" -- rather than a second LLM call
for anything conversational (doubling latency on top of an already-slow
path), one grammar-constrained call does both jobs: pick a real action
when there is one, or write a short in-character reply when there
isn't. A JSON string field is unconstrained free text under GBNF, so
this doesn't weaken the allowlist at all -- "reply" is never executed
or dispatched, only ever spoken (see main.py).

Model file fetched by hand into
models/qwen2.5-1.5b-instruct-gguf/, same huggingface.co workaround as
everywhere else in this project. Installed via a prebuilt wheel
(--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu)
rather than pip's default sdist build -- the sdist bundles llama.cpp's
full source tree including a deeply-nested Svelte web UI that exceeds
Windows' 260-char MAX_PATH during extraction.
"""

import datetime
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
    "maximize_app",
    "switch_desktop",
    "snap_window",
    "focus_vscode_repo",
    "count_app_windows",
    "count_files_in_folder",
    "volume_up",
    "volume_down",
    "toggle_mute",
    "media_play_pause",
    "media_next_track",
    "media_previous_track",
    "lock_workstation",
    # Not a real dispatchable action (nothing in actions.py handles it) --
    # a meta intent main.py intercepts itself, same as it already does
    # for the embedding matcher's disable_listening/enable_listening.
    # Included here so a dictation-start phrasing the fast path's small
    # seed list misses still gets recognized, instead of falling through
    # to the LLM with no notion of dictation at all and guessing
    # something unrelated (confirmed directly: this happened before it
    # was added here).
    "start_dictation",
]

# Actions with no parameter at all -- system/media controls that always
# act on the whole system or whatever currently has focus. Shared with
# actions.py's own _NO_PARAM_ACTIONS (kept separate, not imported,
# since llm_intent shouldn't need to import actions.py -- these two
# lists just need to stay in sync by hand when either changes).
_NO_PARAM_ACTION_NAMES = (
    "volume_up",
    "volume_down",
    "toggle_mute",
    "media_play_pause",
    "media_next_track",
    "media_previous_track",
    "lock_workstation",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [*_ACTION_NAMES, "none"]},
        "app": {"type": "string", "enum": [*_APP_KEYS, "none"]},
        "direction": {"type": "string", "enum": ["next", "previous", "left", "right", "none"]},
        "repo": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "app", "direction", "repo", "reply"],
}

SYSTEM_PROMPT_TEMPLATE = f"""You turn a spoken command into a structured action, and handle small talk yourself. Respond with ONLY a JSON object, no other text.

Fields:
- action: one of {", ".join(_ACTION_NAMES)}, none
- app: the app involved ({", ".join(_APP_KEYS)}), or "none" if not applicable
- direction: "next"/"previous" for switch_desktop, "left"/"right" for snap_window (screen-half snap, not a virtual desktop), else "none"
- repo: for focus_vscode_repo only, the repository/folder name mentioned, else ""
- reply: ONLY when action is "none" -- a short (1-2 sentence) spoken reply, in character as a formal, deferential British assistant (always "sir", think Jarvis from Iron Man), for anything that isn't a command. Empty "" otherwise.

Rules:
- "close" means minimize, never closing/quitting. {", ".join(_NO_PARAM_ACTION_NAMES)} and start_dictation take no app/direction/repo (always "none"/"").
- focus_vscode_repo only when a specific repo/project name is named; bare "open vscode" is focus_or_launch_app.
- start_dictation is for typing out whatever the user says next verbatim ("take dictation", "start dictating", "let's dictate something") -- not the same as any of the other actions.
- No match -> action "none", answer it yourself in "reply" (don't just say you can't help). No internet/live data (weather, news, search) -- say so honestly, but you're given the current date/time below, so answer those directly.
- A "Context" message names the app a pronoun ("it"/"that") refers to -- use it. No Context message and a pronoun used -> you don't know what it means; action "none", ask which app in "reply".
- If an app/action clearly involves a specific program but NONE of the app choices are actually that program, do NOT pick the closest-sounding one anyway -- action "none", say in "reply" you don't have that app registered. Guessing a wrong app is worse than admitting you don't have it.

Examples:
"open chrome" -> {{"action": "focus_or_launch_app", "app": "chrome", "direction": "none", "repo": "", "reply": ""}}
"can you minimize discord for me" -> {{"action": "minimize_app", "app": "discord", "direction": "none", "repo": "", "reply": ""}}
"maximize chrome" -> {{"action": "maximize_app", "app": "chrome", "direction": "none", "repo": "", "reply": ""}}
"go to the next desktop" -> {{"action": "switch_desktop", "app": "none", "direction": "next", "repo": "", "reply": ""}}
"snap this window to the left" -> {{"action": "snap_window", "app": "none", "direction": "left", "repo": "", "reply": ""}}
"switch to the bdk wallet window in vscode" -> {{"action": "focus_vscode_repo", "app": "vscode", "direction": "none", "repo": "bdk_wallet", "reply": ""}}
"how many chrome windows are open" -> {{"action": "count_app_windows", "app": "chrome", "direction": "none", "repo": "", "reply": ""}}
"how many files are in this folder" -> {{"action": "count_files_in_folder", "app": "none", "direction": "none", "repo": "", "reply": ""}}
"skip this track" -> {{"action": "media_next_track", "app": "none", "direction": "none", "repo": "", "reply": ""}}
"start dictating for me" -> {{"action": "start_dictation", "app": "none", "direction": "none", "repo": "", "reply": ""}}
"what's the weather like" -> {{"action": "none", "app": "none", "direction": "none", "repo": "", "reply": "I'm afraid I've no access to live weather data, sir -- fully offline, I'm blind to anything outside this machine."}}
"what time is it" -> {{"action": "none", "app": "none", "direction": "none", "repo": "", "reply": "It's half past two in the afternoon, sir."}}
"open photoshop" -> {{"action": "none", "app": "none", "direction": "none", "repo": "", "reply": "I don't have Photoshop registered, sir."}}
(context: app is "chrome") "minimize it" -> {{"action": "minimize_app", "app": "chrome", "direction": "none", "repo": "", "reply": ""}}
"""


def _system_prompt() -> str:
    # Built per call, not baked into the constant above, so "what time
    # is it"/"what's today's date" get a real answer instead of the
    # model either declining or hallucinating one -- this is the only
    # context about the outside world it's ever given.
    now = datetime.datetime.now()
    return SYSTEM_PROMPT_TEMPLATE + f"\nCurrent date/time: {now.strftime('%A, %B %d, %Y, %I:%M %p')}."


def _build_messages(text: str, context_app: "Optional[str]" = None) -> list:
    """
    Split out for testability (see _system_prompt's docstring rationale
    -- same idea). context_app is main.py's memory of the last app an
    action actually touched (set after a successful dispatch; see
    state["last_app"]) -- lets "minimize it"/"close that" resolve
    without naming the app again, one turn of pronoun memory, not a
    full conversation history.
    """
    messages = [{"role": "system", "content": _system_prompt()}]
    if context_app:
        messages.append(
            {
                "role": "system",
                "content": f'Context: app is "{context_app}".',
            }
        )
    messages.append({"role": "user", "content": text})
    return messages


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


def validate_parsed(parsed: dict) -> Optional[dict]:
    """
    Re-checks every field against the real enums, even though the
    grammar should already guarantee this structurally -- never trust a
    single layer blindly, especially not an LLM's, even a
    grammar-constrained one. Split out from infer() so this logic is
    testable on its own, without loading the actual GGUF model.

    action "none" is a valid, distinct outcome now -- not collapsed to
    None the way a genuinely malformed response is -- so the caller can
    tell "nothing to do, but here's a chat reply" apart from "couldn't
    make sense of this at all". dispatch_llm_action (actions.py) never
    sees this shape; main.py handles action "none" itself, speaking
    "reply" directly instead of dispatching.
    """
    action = parsed.get("action")
    if action == "none":
        reply = parsed.get("reply", "")
        return {
            "action": "none",
            "app": "none",
            "direction": "none",
            "repo": "",
            "reply": reply if isinstance(reply, str) else "",
        }

    if action not in _ACTION_NAMES:
        return None

    app = parsed.get("app")
    if app not in (*_APP_KEYS, "none"):
        return None

    direction = parsed.get("direction")
    if direction not in ("next", "previous", "left", "right", "none"):
        return None

    repo = parsed.get("repo", "")
    if not isinstance(repo, str):
        return None

    # The grammar allows app/direction/repo "none"/"" regardless of
    # action, since one JSON schema covers every action -- but a real
    # action still needs its own required parameter (or, for the
    # no-parameter system/media controls, needs *no* parameter at all).
    # A mismatch here isn't a valid inference (usually the model
    # latching onto an action-shaped word in background chatter that
    # isn't actually a command) -- decline rather than dispatch
    # something guaranteed to fail, or silently ignore a parameter that
    # was never asked for.
    if action in ("focus_or_launch_app", "minimize_app", "maximize_app", "count_app_windows") and app == "none":
        return None
    if action == "switch_desktop" and direction not in ("next", "previous"):
        return None
    if action == "snap_window" and direction not in ("left", "right"):
        return None
    if action == "focus_vscode_repo" and not repo.strip():
        return None
    # No corresponding check for _NO_PARAM_ACTION_NAMES (volume_up and
    # friends) or start_dictation -- deliberately lenient here, unlike
    # the checks above: dispatch_llm_action calls the former with no
    # arguments regardless of what ended up in app/direction/repo, and
    # main.py only ever reads parsed["action"] for the latter, so a
    # stray non-"none" value the model left in an unused field can't
    # cause anything to actually go wrong. Declining over it would just
    # be an unforced "I'm not sure how to do that" for an otherwise-fine
    # command.

    return {"action": action, "app": app, "direction": direction, "repo": repo}


def infer(text: str, context_app: "Optional[str]" = None) -> Optional[dict]:
    """
    Returns a validated action dict (action "none" with a "reply" field
    counts as valid -- see validate_parsed), or None if the LLM
    produced something invalid. context_app: see _build_messages.
    """
    llm = _get_llm()
    try:
        result = llm.create_chat_completion(
            messages=_build_messages(text, context_app),
            grammar=_grammar,
            temperature=0.0,
            max_tokens=220,  # enough headroom for a short spoken "reply" plus the JSON scaffolding around it
        )
        parsed = json.loads(result["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return None

    return validate_parsed(parsed)
