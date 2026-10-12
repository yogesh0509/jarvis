"""
A small, locked, persisted key-value store -- the one place this
project's assistant-facing features (timers, notes, preferences, last
window layout) keep state that needs to survive a restart.

Lives at logs/memory.json, alongside this project's other gitignored,
locally-regenerable runtime state (learned_seeds.json,
seed_promotion_state.json, llm_kv_cache.pkl) -- not a model file, not a
log line, but the same category of "derived, safe to delete,
regenerates itself" (a missing or corrupt file just means starting from
an empty memory, never a crash).

Why a shared module rather than each feature reading/writing its own
file directly: timers fire from their own background thread
(timers.py) while a voice command mutating a *different* key (a note,
say) can run on the listener thread at the same moment. A naive
"read the whole file, modify one key, write the whole file back"
pattern -- done independently by two callers -- loses whichever write
lands first. This keeps one in-memory copy behind one lock and writes
through on every mutation, so two callers touching different keys at
the same moment can never clobber each other.
"""

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable

MEMORY_PATH = Path(__file__).parent / "logs" / "memory.json"

# Every key this project's features currently use, present from the
# start so a caller can always do memory.get("timers") and get a real
# (empty) value back rather than needing its own "or []" on every call
# site. Add a key here when a new feature needs one -- see timers.py
# and main.py's proactive-briefing code for the two current consumers.
_DEFAULTS = {
    "timers": [],
    "notes": [],
    "preferences": {},
    "last_layout": None,
}

_lock = threading.Lock()
_cache: "dict | None" = None  # loaded lazily; None means "not read from disk yet this process"


def _load_from_disk() -> dict:
    try:
        with open(MEMORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return dict(_DEFAULTS)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)
    # Merged over the defaults, not replaced by the file wholesale --
    # a key added to _DEFAULTS after some existing memory.json was
    # already written must still come back present, not KeyError the
    # first feature that reads it on an upgraded install.
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def _save_to_disk(data: dict) -> None:
    MEMORY_PATH.parent.mkdir(exist_ok=True)
    tmp_path = MEMORY_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    # Atomic on the same filesystem (Windows included, since Python
    # 3.3) -- a crash mid-write leaves the old file or nothing, never a
    # half-written one that the next load would fail to parse.
    tmp_path.replace(MEMORY_PATH)


def _ensure_loaded() -> None:
    global _cache
    if _cache is None:
        _cache = _load_from_disk()


def get(key: str, default: Any = None) -> Any:
    """
    A deep copy of the current value for `key` -- never the live
    object -- so a caller mutating what it got back (appending to a
    list, say) can't silently corrupt the in-memory cache without going
    through update() below, which is the only path that actually
    persists a change.
    """
    with _lock:
        _ensure_loaded()
        value = _cache.get(key, default)
        return copy.deepcopy(value)


def update(key: str, mutator: Callable[[Any], Any]) -> Any:
    """
    Reads the current value for `key`, applies `mutator` to it, stores
    and persists the result, all under one lock acquisition -- the
    read-modify-write is atomic with respect to every other caller of
    get()/update(), which is what actually prevents the lost-write race
    this module's docstring describes. Returns the new value.

    `mutator` receives the current value (already the stored default if
    the key was never set) and returns the new one -- e.g.
    `memory.update("timers", lambda current: current + [new_timer])`
    rather than a caller doing its own get()-then-update() pair, which
    would reopen exactly the race window a single update() call closes.
    """
    with _lock:
        _ensure_loaded()
        current = _cache.get(key, _DEFAULTS.get(key))
        new_value = mutator(current)
        _cache[key] = new_value
        _save_to_disk(_cache)
        return copy.deepcopy(new_value)
