"""
Phase 5: local LLM intent fallback for phrases the embedding matcher
(intent_matcher.py) can't confidently place. Qwen2.5-1.5B-Instruct,
GGUF Q4_K_M, via llama-cpp-python -- benchmarked standalone first
(16/17 correct on a realistic test set) before wiring in.

Latency note (corrects an earlier claim in this docstring and in
DECISIONS.md's decision log): "~2.5-2.8s steady-state per call after the
one-time ~6s first-load cost" was measured by a benchmark that warmed
the model up before timing anything, so it only ever measured a call
with the KV cache already holding the system prompt's prefix.
Constructing the Llama object really is only ~1.5-1.8s -- but the FIRST
real inference in a process (or after unload()) also has to prefill the
~600-1300 token system prompt itself, measured at ~50 tokens/sec on
this project's hardware, i.e. an additional ~13-27s on top of that
~1.5s, not included in the old figure at all. See prewarm()/main.py's
_start_llm_prewarm for how that cost is now paid in the background
instead of inline on a real command.

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
or dispatched, only ever spoken (see main.py). A third outcome,
"no_match", lets the model abstain outright instead of being forced to
pick a real action or produce a chat reply -- see validate_parsed and
the prompt's own rule for when to use it.

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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from apps_registry import CURATED_APP_KEYS

if TYPE_CHECKING:
    from llama_cpp import Llama
    from llama_cpp.llama_grammar import LlamaGrammar

MODEL_PATH = Path(__file__).parent / "models" / "qwen2.5-1.5b-instruct-gguf" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"

# The LLM only ever sees/selects from this curated subset, not the full
# apps_registry.APPS (which balloons to 30-40+ entries once
# app_discovery.py's Start Menu scan runs -- see apps_registry.py).
# Two independent, both-measured reasons:
#   1. Prompt cost: interpolating the full discovered list into both the
#      prompt prose and the GBNF enum cost ~190 of this prompt's ~1300
#      tokens -- at ~50 tok/s prefill, close to 4 seconds of every
#      single cold call, just to list apps most machines will never be
#      asked about by voice.
#   2. Accuracy: constrained decoding *forces* a pick from whatever's in
#      the enum. A model handed "idle_python_3_12_64_bit" and
#      "blackmagic_raw_speed_test" as valid choices for a garbled
#      "maximize vs code"/"open the terminal" doesn't refuse -- it picks
#      the closest-sounding discovered name anyway (confirmed directly
#      in llm_usage_log.jsonl). The "don't guess" rule in the prompt
#      below can't prevent this: the model can't compare against an
#      option it was never shown in the first place.
# Fast-path matching (intent_matcher.py) and dispatch (actions.py) still
# use the full APPS -- an app registered only via discovery still works
# fine by name/seed phrase; only the LLM fallback's reach is narrower.
# Add an app to apps_registry.py's _CURATED_APPS by hand if voice
# command via the LLM fallback specifically is wanted for it.
LLM_APP_KEYS = list(CURATED_APP_KEYS)
_ACTION_NAMES = [
    "focus_or_launch_app",
    "minimize_app",
    "maximize_app",
    "switch_desktop",
    "snap_window",
    "focus_vscode_repo",
    "count_app_windows",
    "count_files_in_folder",
    "describe_screen",
    "now_playing",
    "move_to_monitor",
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
    "describe_screen",
    "now_playing",
    "move_to_monitor",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [*_ACTION_NAMES, "none", "no_match"]},
        "app": {"type": "string", "enum": [*LLM_APP_KEYS, "none"]},
        "direction": {"type": "string", "enum": ["next", "previous", "left", "right", "none"]},
        "repo": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "app", "direction", "repo", "reply"],
}

# Compressed from an earlier, more verbose version (measured at 1292
# tokens with the real tokenizer) down to roughly half that -- prefill
# is linear in prompt tokens at a fixed ~50 tok/s on this project's
# hardware, so every token here is directly ~20ms of cold-call latency.
# Three specific cuts, each validated against _bench_llm.py before
# shipping (see that file):
#   - the 40-entry discovered-app list dropped to LLM_APP_KEYS' 7
#     curated names (see that constant's comment) -- both the biggest
#     token cost and a real accuracy problem, not just a cost.
#   - 14 few-shot examples trimmed to the ones that each demonstrate
#     something no other example does (a real action, direction-only,
#     free-text repo extraction, the "don't guess" decline, a non-empty
#     chat reply, the Context-message pronoun case, and the new
#     "no_match" abstain) -- and written in compact JSON (no spaces)
#     rather than the schema's more readable spaced form, which measured
#     as a real decode-time saving too: the model tends to mirror the
#     example's own formatting.
#   - prose generally tightened to keep only what the grammar doesn't
#     already enforce structurally.
#   - the free-text-repo example's repo name is a fictional placeholder
#     ("payments_api"), not a real one from this machine -- it used to be
#     the user's actual "bdk_wallet". Confirmed live as a real failure
#     mode, not a hypothetical one: with the one concrete repo name the
#     model had ever seen being a real, frequently-used one, a bare "bdk"
#     or other low-information utterance ("for what") reliably collapsed
#     onto exactly that example rather than genuinely extracting a repo
#     name from the utterance (focus_vscode_repo, repo="bdk_wallet",
#     dispatched from single-word/noise input more than once -- see
#     session.jsonl). A name with zero resemblance to anything the user
#     would plausibly say removes it as an attractor without losing the
#     example's actual teaching point (that repo is free text pulled from
#     the utterance, not a closed enum).
SYSTEM_PROMPT_TEMPLATE = f"""You turn a spoken command into a structured action, or handle small talk yourself. Reply with ONLY a JSON object, no other text, no extra whitespace.

Fields:
- action: {", ".join(_ACTION_NAMES)}, none, or no_match
- app: the app involved ({", ".join(LLM_APP_KEYS)}), or none
- direction: next/previous (switch_desktop), left/right (snap_window -- screen-half snap, not a virtual desktop), else none
- repo: focus_vscode_repo's repo/folder name only, else ""
- reply: only when action is none -- a short (1-2 sentence) spoken reply, in character as a formal, deferential British assistant (always "sir", think Jarvis from Iron Man). Empty "" otherwise.

Rules:
- "close" means minimize, never closing/quitting. {", ".join(_NO_PARAM_ACTION_NAMES)} and start_dictation take no app/direction/repo.
- focus_vscode_repo only for a specifically named repo/project; bare "open vscode" is focus_or_launch_app.
- No internet/live data (weather, news, search) -- say so honestly, but you do get the current date/time below, so answer those directly.
- A "Context" message may give the last app touched and/or what's currently focused -- use it for "it"/"that"/"this". No Context and such a word used -> action none, ask which app.
- A specific app is clearly meant but it's not in the app list -> action MUST be "none" (never any app action, even with app "none"), say in "reply" you don't have it registered. Never guess the closest-sounding one.
- move_to_monitor sends the focused window to a different physical screen; snap_window only splits the current screen in half.
- Garbled, fragmentary, or not something a person would actually say (a transcriber's best guess at noise) -> action no_match, empty "reply". Say nothing rather than guess.

Examples:
"open chrome"->{{"action":"focus_or_launch_app","app":"chrome","direction":"none","repo":"","reply":""}}
"maximize chrome"->{{"action":"maximize_app","app":"chrome","direction":"none","repo":"","reply":""}}
"go to the next desktop"->{{"action":"switch_desktop","app":"none","direction":"next","repo":"","reply":""}}
"switch to the payments api window in vscode"->{{"action":"focus_vscode_repo","app":"vscode","direction":"none","repo":"payments_api","reply":""}}
"how many files are in this folder"->{{"action":"count_files_in_folder","app":"none","direction":"none","repo":"","reply":""}}
"what's on my screen right now"->{{"action":"describe_screen","app":"none","direction":"none","repo":"","reply":""}}
"what's playing right now"->{{"action":"now_playing","app":"none","direction":"none","repo":"","reply":""}}
"open photoshop"->{{"action":"none","app":"none","direction":"none","repo":"","reply":"I don't have Photoshop registered, sir."}}
"what's the weather like"->{{"action":"none","app":"none","direction":"none","repo":"","reply":"I'm afraid I've no access to live weather data, sir -- fully offline, I'm blind to anything outside this machine."}}
"shu programming bitcoin bitshaka"->{{"action":"no_match","app":"none","direction":"none","repo":"","reply":""}}
(context: app is "chrome") "minimize it"->{{"action":"minimize_app","app":"chrome","direction":"none","repo":"","reply":""}}
"""


def _system_prompt() -> str:
    # Built per call, not baked into the constant above, so "what time
    # is it"/"what's today's date" get a real answer instead of the
    # model either declining or hallucinating one -- this is the only
    # context about the outside world it's ever given.
    now = datetime.datetime.now()
    return SYSTEM_PROMPT_TEMPLATE + f"\nCurrent date/time: {now.strftime('%A, %B %d, %Y, %I:%M %p')}."


def _build_messages(
    text: str, context_app: "Optional[str]" = None, screen_context: "Optional[str]" = None
) -> list:
    """
    Split out for testability (see _system_prompt's docstring rationale
    -- same idea).

    context_app is main.py's memory of the last app an action actually
    touched (set after a successful dispatch; see state["last_app"]) --
    lets "minimize it"/"close that" resolve without naming the app
    again, one turn of pronoun memory, not a full conversation history.

    screen_context (actions.llm_screen_context()) is a terse description
    of the currently-focused window -- only passed when main.py's
    deictic gate decided the utterance actually needs it ("minimize
    this", "what's this page about"), since it costs real prefill tokens
    on every call it's included in. Both, either, or neither may be
    given; when either is present they merge into ONE system message,
    not two -- see below for why that matters.

    Both fold into the SAME second system message rather than each
    getting their own -- this is deliberate and load-bearing, not just
    tidiness: llama-cpp-python reuses the longest-common-prefix of the
    KV cache across calls (see llm_intent.py's module docstring), and
    that prefix is the first system message (_system_prompt()) plus
    however many messages come before it change. Keeping context to
    exactly one optional message after the stable prefix, always in the
    same position, is what keeps that prefix reuse intact -- a third
    message here would still work correctly, it just multiplies the
    number of distinct prefixes ever seen, diluting how often any one of
    them is still warm.
    """
    messages = [{"role": "system", "content": _system_prompt()}]
    parts = []
    if context_app:
        parts.append(f'app is "{context_app}"')
    if screen_context:
        parts.append(f"you're currently looking at {screen_context}")
    if parts:
        messages.append({"role": "system", "content": "Context: " + "; ".join(parts) + "."})
    messages.append({"role": "user", "content": text})
    return messages


_llm: "Optional[Llama]" = None
_grammar: "Optional[LlamaGrammar]" = None

# Serializes every actual llama.cpp call (infer() and prewarm() both go
# through _run_inference below) -- llama-cpp-python's Llama object isn't
# safe to drive from two threads at once, and this project now has two
# callers: the listener/push-to-talk threads via infer(), and the
# background prewarm thread started by main.py's _start_llm_prewarm. A
# dedicated lock rather than reusing main.py's _processing_lock is
# deliberate: _processing_lock also guards transcription, so a prewarm
# holding *that* lock for its ~13-27s duration would stall a
# push-to-talk press before it even got to transcribe the audio. With a
# separate lock here, transcription proceeds immediately and only the
# LLM step (if the fast path misses) waits on whatever prewarm is doing.
_lock = threading.RLock()

# How many real llama.cpp calls (infer() + prewarm(), combined) have
# gone through _run_inference since the model was last (re)loaded.
# call_count() == 0 right before a call is what main.py checks to know
# whether that call is about to pay the cold prefill cost -- see
# main.py's llm_cold.
_call_count = 0

# "cold" (never prewarmed since load) / "warming" (a prewarm is running
# right now) / "warm" (a prewarm completed) / "failed" (the last prewarm
# attempt raised). Purely observational -- infer() works regardless of
# this value, prewarming is a latency optimization, not a precondition.
_prewarm_state = "cold"

# The utterance prewarm() actually runs through the real pipeline to
# populate the KV cache -- any well-formed command works equally well
# for this purpose since what's being warmed is the *system prompt's*
# prefix, not anything about this specific text.
PREWARM_UTTERANCE = "open chrome"


def _get_llm() -> "Llama":
    # Importing llama_cpp itself (not just loading the model weights)
    # loads a compiled native backend -- deferred here, same as the
    # weights, so `import llm_intent` at main.py's startup stays cheap
    # for the common case where a session never needs the LLM fallback.
    # Still true with prewarming added: prewarm() only ever runs from a
    # background daemon thread main.py starts well after boot completes
    # (see _start_llm_prewarm), never from this module's own import, so
    # time-to-armed is unaffected either way.
    global _llm, _grammar
    if _llm is None:
        from llama_cpp import Llama
        from llama_cpp.llama_grammar import LlamaGrammar

        _llm = Llama(model_path=str(MODEL_PATH), n_ctx=2048, n_threads=4, n_threads_batch=4, verbose=False)
        _grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)
    return _llm


def is_loaded() -> bool:
    return _llm is not None


def call_count() -> int:
    return _call_count


def prewarm_state() -> str:
    return _prewarm_state


def unload() -> None:
    """
    Drops the loaded model, its GBNF grammar, and the call/prewarm
    bookkeeping, so the next call reconstructs from scratch.

    Deliberately does NOT take _lock. An in-flight infer()/prewarm()
    already holds its own local reference to the (still-live) Llama
    object via _get_llm()'s return value -- nilling the globals here
    can't crash it out from under itself, it only means the *next*
    _get_llm() call builds a fresh one. Taking _lock here instead would
    block this call for the full duration of any prewarm in progress
    (up to ~13-27s); this is called from inside main.py's
    _processing_lock (deep sleep, pause, and shutdown all go through
    it), so that would stall whatever real event triggered the unload
    for as long as the prewarm takes. The rare edge case this accepts --
    unload() landing mid-prewarm -- is harmless: the in-flight prewarm
    still completes safely against the old object (its result is simply
    thrown away), and the next real call pays a cold cost again rather
    than reusing work that's being discarded anyway.
    """
    global _llm, _grammar, _call_count, _prewarm_state
    _llm = None
    _grammar = None
    _call_count = 0
    _prewarm_state = "cold"


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
    if action == "no_match":
        # A deliberate abstain (see the prompt's own "no_match" rule) --
        # the model deciding this doesn't read like a real command
        # rather than being structurally forced to emit some action or
        # write a chat reply. "reply" is force-emptied regardless of
        # what the model actually wrote: an abstain must never become
        # speech (main.py treats this the same as the pre-LLM transcript
        # gate's rejection -- silent, not spoken) -- fail closed here
        # rather than trust the model to have left it blank on its own.
        return {"action": "no_match", "app": "none", "direction": "none", "repo": "", "reply": ""}
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
    if app not in (*LLM_APP_KEYS, "none"):
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


def _run_inference(
    text: str, context_app: "Optional[str]" = None, screen_context: "Optional[str]" = None
) -> Optional[dict]:
    """
    The actual grammar-constrained call, shared by infer() and
    prewarm() -- factored out so prewarm can run a real inference
    (populating the KV cache with the system prompt's prefix) through
    the exact same path a live command would use, rather than a
    hand-approximated substitute that could drift out of sync with it.
    Caller must hold _lock.
    """
    global _call_count
    llm = _get_llm()
    try:
        result = llm.create_chat_completion(
            messages=_build_messages(text, context_app, screen_context),
            grammar=_grammar,
            temperature=0.0,
            max_tokens=220,  # enough headroom for a short spoken "reply" plus the JSON scaffolding around it
        )
        _call_count += 1
        parsed = json.loads(result["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return None

    return validate_parsed(parsed)


def infer(
    text: str, context_app: "Optional[str]" = None, screen_context: "Optional[str]" = None
) -> Optional[dict]:
    """
    Returns a validated action dict (action "none"/"no_match" counts as
    valid -- see validate_parsed), or None if the LLM produced something
    invalid. context_app, screen_context: see _build_messages.

    Serialized against any in-progress prewarm() via _lock -- if one is
    running, this simply waits for it (getting the benefit of whatever
    prefill it already did) rather than racing it on the same Llama
    object from two threads.
    """
    with _lock:
        return _run_inference(text, context_app, screen_context)


def prewarm() -> bool:
    """
    Runs one real inference (PREWARM_UTTERANCE, discarded) purely to
    populate the KV cache with the system prompt's prefix before a real
    command needs it. This is NOT the same as loading the model:
    constructing the Llama object (inside _get_llm, called from here
    too) is only ~1.5-1.8s -- the ~13-27s cost this project measured is
    prompt *prefill*, paid by whichever call is first to actually run
    one, cold-model or not. See main.py's _start_llm_prewarm for when
    this gets called (a background daemon thread, never inline on a
    real command).

    Never propagates an exception -- meant to be called from a thread
    with nothing to catch one, where a raised exception would simply
    vanish and leave _prewarm_state stuck at "warming" forever (which
    would in turn disable every future prewarm attempt, since callers
    treat "warming" as "already in progress, don't start another").
    Returns whether the model ended up warm.
    """
    global _prewarm_state
    if _prewarm_state in ("warming", "warm"):
        return _prewarm_state == "warm"
    _prewarm_state = "warming"
    try:
        with _lock:
            _run_inference(PREWARM_UTTERANCE)
        _prewarm_state = "warm"
        return True
    except Exception:
        _prewarm_state = "failed"
        return False
