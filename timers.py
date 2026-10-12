"""
Timers and reminders -- "set a timer for ten minutes", "remind me in an
hour to stretch", "cancel the timer", "how much time is left".

A single background polling thread, the same shape as
output_monitor.py's, rather than one threading.Timer per active timer
(the approach main.py's push-to-talk hold-threshold confirmation
uses) -- that shape fits push-to-talk's "exactly one thing, briefly
pending" case; this one naturally wants a live countdown pushed to the
HUD every second regardless of how many timers are running or when
they're due, which one thread doing one tick loop gives for free.

Firing goes through tts.speak() and earcons.play() -- both already
handle their own mic-muting (tts._begin_speaking/_end_speaking) the
same way every other spoken reply and cue in this codebase does, so a
timer going off doesn't get transcribed as a fresh command the way an
untracked sound would (see earcons.py's own module docstring for the
bug class this avoids).

Persisted through memory.py so a timer survives the quit-on-sleep
cycle (main.py's own restart-on-wake, not just an ordinary pause): set
a timer, put the machine to sleep, and the restored process picks up
exactly where the durable record says it should, including firing
immediately (with a note it's late) if it was actually due while the
process was down.

Deliberately a leaf module, like window_context.py/monitors.py: no
import of main.py or actions.py. actions.py's own timer-shaped actions
are thin wrappers that call into this module and translate its plain
dict/None returns into actions.Outcome + phrases.pick() text, the same
relationship actions.py already has with monitors.resolve_monitor_mention.
"""

import threading
import time
import uuid

import earcons
import hud
import memory
import phrases
import slots
import tts

POLL_SECONDS = 1.0

_lock = threading.Lock()
_active: "list[dict]" = []  # in-memory mirror of memory.get("timers"), kept sorted by fires_at
_thread: "threading.Thread | None" = None
_stop = threading.Event()


def _sorted_by_fire_time(records: "list[dict]") -> "list[dict]":
    return sorted(records, key=lambda t: t["fires_at"])


def _persist_locked() -> None:
    """Caller must hold _lock. Writes the current _active list through to memory.json."""
    memory.update("timers", lambda _current: list(_active))


def set_timer(seconds: int, label: str = "") -> dict:
    """
    Schedules a new timer `seconds` from now and returns its record --
    {id, label, set_at, fires_at, duration_seconds}. label is "" for a
    plain timer ("set a timer for ten minutes") or free text for a
    reminder ("remind me in an hour to stretch" -> label="stretch").
    """
    now = time.time()
    record = {
        "id": uuid.uuid4().hex[:8],
        "label": label,
        "set_at": now,
        "fires_at": now + seconds,
        "duration_seconds": seconds,
    }
    with _lock:
        _active.append(record)
        _active[:] = _sorted_by_fire_time(_active)
        _persist_locked()
    _ensure_running()
    _push_countdown()
    return dict(record)


def cancel_most_recent(label: str = "") -> "dict | None":
    """
    Cancels a timer and returns its record, or None if there was
    nothing to cancel. With a label, matches the most recently set
    timer whose own label contains it (a substring match, same
    tolerance window_context.extract_hints/fuzzy_match use elsewhere in
    this codebase for spoken text); with no label, cancels whichever
    timer was set most recently -- the one a bare "cancel the timer"
    said right after setting it almost always means, and this project's
    own established habit (window_context.CycleTracker) is to make a
    sensible choice rather than ask a clarifying question out loud.
    """
    with _lock:
        if not _active:
            return None
        if label:
            matches = [t for t in _active if label.lower() in t["label"].lower()]
            candidates = matches or _active
        else:
            candidates = _active
        target = max(candidates, key=lambda t: t["set_at"])
        _active.remove(target)
        _persist_locked()
    _push_countdown()
    return dict(target)


def list_active() -> "list[dict]":
    """All currently active timers, soonest-to-fire first."""
    with _lock:
        return [dict(t) for t in _active]


def _fire_due_locked(now: float) -> "list[dict]":
    """Caller must hold _lock. Removes and returns every timer whose fires_at has passed."""
    due = [t for t in _active if t["fires_at"] <= now]
    for t in due:
        _active.remove(t)
    if due:
        _persist_locked()
    return due


def _speak_firing(record: dict, overdue: bool) -> None:
    label = record.get("label") or ""
    if label:
        text = phrases.pick("timer_done_labeled", label=label)
    else:
        text = phrases.pick("timer_done_unlabeled")
    if overdue:
        # Restored from a sleep/restart cycle after its own fire time
        # already passed -- said once, so a late notification doesn't
        # silently read as an on-time one.
        text = phrases.pick("timer_overdue_prefix") + " " + text
    earcons.play("timer_done")
    tts.speak(text)


def _push_countdown() -> None:
    """
    Pushed to the HUD every tick regardless of whether anything changed
    -- see hud.set_timer_countdown's own docstring for why this is a
    push, not something the render loop pulls.
    """
    with _lock:
        soonest = _active[0] if _active else None
        count = len(_active)
    if soonest is None:
        hud.set_timer_countdown(None)
        return
    remaining = max(0, round(soonest["fires_at"] - time.time()))
    text = slots.format_duration(remaining)
    if count > 1:
        text += f" (+{count - 1})"
    hud.set_timer_countdown(text)


def _poll_loop() -> None:
    while not _stop.is_set():
        now = time.time()
        with _lock:
            due = _fire_due_locked(now)
        for record in due:
            try:
                _speak_firing(record, overdue=False)
            except Exception:
                pass  # a firing must never take this thread down -- the next tick still has to run
        _push_countdown()
        _stop.wait(POLL_SECONDS)


def _ensure_running() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, name="timers", daemon=True)
    _thread.start()


def restore() -> None:
    """
    Called once at boot. Re-arms every timer memory.json still has on
    record -- a quit-on-sleep/restart-on-wake cycle (main.py's own
    scheduled tasks, see scheduler/README.md) must not silently drop a
    timer that was running when the machine went down. One already due
    by the time this runs fires immediately, marked overdue rather than
    silently dropped or silently treated as on-time.
    """
    with _lock:
        _active[:] = _sorted_by_fire_time(memory.get("timers", []))
        due = _fire_due_locked(time.time())
    for record in due:
        try:
            _speak_firing(record, overdue=True)
        except Exception:
            pass
    if _active:
        _ensure_running()
    _push_countdown()


def stop() -> None:
    """For tests and a clean shutdown -- idempotent, safe to call even if never started."""
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
