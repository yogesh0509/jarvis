"""
Phase 2 (+ Phase 5 expansion): local intent matcher.

Embeds a transcript and compares it against a small table of known
commands + paraphrase seeds. This is the fast path: near-instant,
offline, zero cost. Anything below the confidence threshold falls
back to the local LLM (llm_intent.py) -- parameterized/free-text
phrases (VS Code repo names, queries) always go there; app open/
minimize/desktop-switching phrases go there only until they've proven
common enough to promote into this fast path (see seed_promotion.py).

Labels for app-targeted intents are compound: "{action}:{app_or_dir}"
(e.g. "focus_or_launch_app:chrome", "switch_desktop:next"), matching
the same shape llm_intent.py produces so both paths dispatch through
actions.dispatch_llm_action() -- parsed in actions.py, not here.
"""

from dataclasses import dataclass

import numpy as np

from local_embedder import LocalEmbedder
from seed_promotion import load_learned_seeds

MATCH_THRESHOLD = 0.80

INTENTS: dict[str, list[str]] = {
    # Kept deliberately short per app -- the embedder already generalizes
    # "open"/"bring up"/"pull up"/"switch to" as near-synonyms, so a
    # handful of seeds covers most real phrasing. Anything that doesn't
    # land here falls to the LLM (see llm_intent.py); a phrase seen
    # repeatedly there can be promoted in by hand (see seed_promotion.py)
    # rather than every plausible paraphrase being pre-seeded, since each
    # extra seed here is extra embedding work at every startup.
    "focus_or_launch_app:chrome": [
        "open chrome",
        "open google chrome",
        "bring up chrome",
        "switch to chrome",
        "pull up chrome",
    ],
    "focus_or_launch_app:discord": [
        "open discord",
        "bring up discord",
        "switch to discord",
    ],
    "focus_or_launch_app:vscode": [
        "open vs code",
        "open vscode",
        "open visual studio code",
        "switch to vscode",
    ],
    "focus_or_launch_app:file_explorer": [
        "open file explorer",
        "open explorer",
    ],
    "focus_or_launch_app:settings": [
        "open settings",
        "open windows settings",
    ],
    "focus_or_launch_app:local_disk_d": [
        "open local disk d",
        "open my d drive",
        "open d drive",
    ],
    "focus_or_launch_app:terminal": [
        "open the terminal",
        "open terminal",
        "open windows terminal",
        "bring up the terminal",
    ],
    "focus_or_launch_app:spotify": [
        "open spotify",
        "bring up spotify",
        "switch to spotify",
    ],
    "minimize_app:terminal": [
        "minimize the terminal",
        "close the terminal",
    ],
    "minimize_app:chrome": [
        "minimize chrome",
        "close chrome",
        "hide chrome",
    ],
    "minimize_app:discord": [
        "minimize discord",
        "close discord",
    ],
    "minimize_app:vscode": [
        "minimize vs code",
        "close vs code",
    ],
    "minimize_app:file_explorer": [
        "minimize file explorer",
        "close file explorer",
    ],
    "switch_desktop:next": [
        "switch to the next desktop",
        "go to the next desktop",
        "next desktop",
    ],
    "switch_desktop:previous": [
        "switch to the previous desktop",
        "go to the previous desktop",
        "previous desktop",
    ],
    "snap_window:left": [
        "snap left",
        "snap this window to the left",
        "snap to the left",
    ],
    "snap_window:right": [
        "snap right",
        "snap this window to the right",
        "snap to the right",
    ],
    # System/media controls -- no app or direction of their own, so no
    # compound label; each is its own bare intent. maximize_app is
    # deliberately NOT seeded here at all (LLM-fallback only, for now)
    # -- one seed per registered app would be the same cost as
    # minimize_app's seeds for a much less frequently used action; not
    # worth paying at every startup unless it proves common enough to
    # promote (seed_promotion.py already recognizes the label if it does).
    "volume_up": ["turn the volume up", "volume up", "louder"],
    "volume_down": ["turn the volume down", "volume down", "quieter"],
    "toggle_mute": ["mute", "mute the volume", "unmute"],
    "media_play_pause": ["play", "pause", "play music", "pause the music"],
    "media_next_track": ["next track", "skip this track", "next song"],
    "media_previous_track": ["previous track", "go back a track", "previous song"],
    "lock_workstation": ["lock my computer", "lock the screen", "lock windows"],
    "describe_screen": ["what's on my screen", "what's on screen right now", "what am I looking at"],
    "now_playing": ["what's playing", "what's currently playing", "what song is this", "what's on spotify"],
    "move_to_monitor": ["move this to my laptop", "move this to my monitor", "move this to the other screen"],
    # Meta intents -- handled specially in main.py (they toggle the
    # listener's own state), not routed through actions.py's allowlist.
    # Dual meaning, resolved in main.py by context: said mid-session,
    # this closes the session (back to wake-word-armed); said via
    # push-to-talk with no session active, it pauses the whole thing.
    "disable_listening": [
        "stop listening",
        "disable listening",
        "go to sleep",
        "stand down",
        "that's enough for now",
        "power down",
        "we're done",
        "I'm done",
        "that's it",
    ],
    "enable_listening": [
        "start listening",
        "enable listening",
        "wake up",
        "come back online",
    ],
    # Dictation mode -- only meaningful within an open session (see
    # main.py's start_dictation handling); said via push-to-talk it's
    # declined with a clarifying message instead. A phrasing this list
    # doesn't cover still falls back to the LLM, which also knows about
    # start_dictation now (llm_intent.py) -- confirmed directly that an
    # unmatched dictation-start phrase used to reach an LLM with no
    # notion of dictation at all and get resolved as something unrelated.
    # Exiting dictation is deliberately NOT done via this matcher at all
    # once dictation is active -- main.py checks the raw transcript
    # against a small fixed phrase set instead, since fuzzy-matching an
    # exit condition against free-form dictated prose risks an
    # unexpected early exit.
    "start_dictation": [
        "take dictation",
        "start dictation",
        "begin dictation",
        "dictate this",
        "start dictating",
        "let's dictate",
        "i want to dictate something",
        "take a note",
    ],
}


@dataclass
class MatchResult:
    intent: str | None
    score: float
    closest_seed: str | None


class IntentMatcher:
    def __init__(self, intents: dict[str, list[str]] = INTENTS, threshold: float = MATCH_THRESHOLD):
        self.threshold = threshold
        self.model = LocalEmbedder()

        merged = {label: list(seeds) for label, seeds in intents.items()}
        for label, learned in load_learned_seeds().items():
            merged.setdefault(label, []).extend(s for s in learned if s not in merged.get(label, []))

        self._labels: list[str] = []
        self._seeds: list[str] = []
        for intent, seeds in merged.items():
            for seed in seeds:
                self._labels.append(intent)
                self._seeds.append(seed)

        self._seed_vectors = self.model.embed(self._seeds)  # already L2-normalized

    def match(self, text: str) -> MatchResult:
        vec = self.model.embed([text])[0]

        sims = self._seed_vectors @ vec
        best_idx = int(np.argmax(sims))
        best_score = float(sims[best_idx])

        if best_score >= self.threshold:
            return MatchResult(self._labels[best_idx], best_score, self._seeds[best_idx])
        return MatchResult(None, best_score, self._seeds[best_idx])


if __name__ == "__main__":
    print("Spinning up the pattern-recognition circuits...")
    matcher = IntentMatcher()
    print("Ready. Quick self-test first:\n")

    test_phrases = [
        "can you open chrome for me",
        "hey open up chrome for me",
        "what's the weather today",
        "switch to desktop 2",
        "yo pull up chrome real quick",
        "can you open discord",
        "close vs code",
        "go to the next desktop",
    ]
    for phrase in test_phrases:
        result = matcher.match(phrase)
        verdict = result.intent or "NO MATCH"
        print(f'  "{phrase}" -> {verdict}  (score={result.score:.3f}, closest="{result.closest_seed}")')

    print("\nType a phrase to test live (blank line to quit).")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            break
        if not text:
            break
        result = matcher.match(text)
        verdict = result.intent or "no match, sir -- that one's above my pay grade"
        print(f'  -> {verdict}  (score={result.score:.3f}, closest seed="{result.closest_seed}")')
