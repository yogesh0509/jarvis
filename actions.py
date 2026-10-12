"""
The execution layer. Every action the matcher/LLM can trigger lives
here as a plain Python function, referenced only through ACTIONS at
the bottom -- callers look up a key in this dict, they never execute
a generated string or shell command. App-targeted actions are further
bounded by apps_registry.APPS: an app_key must be a registered entry
there, never free text -- that's the actual security boundary for
"apps in general" (Phase 5), enforced here in code, not by trusting
the LLM's grammar alone (defense in depth: even a malformed/bypassed
grammar output still can't reference an unregistered app, since this
layer does its own `if app_key not in APPS` check).
"""

import ctypes
import datetime
import difflib
import os
import re
import subprocess
import time
from pathlib import Path

import psutil
import win32api
import win32con
import win32gui
import win32process
from pathvalidate import sanitize_filename
from PIL import ImageGrab

import app_titles
import audio_endpoint
import fuzzy_match
import memory
import monitors
import phrases
import slots
import timers
import window_context
from apps_registry import APPS
from virtual_desktop import is_window_on_current_desktop
from typing import NamedTuple


class Outcome(NamedTuple):
    """
    What an action actually did, not just what it says. Every action
    used to return a bare spoken string, which meant success and
    failure were indistinguishable to everything downstream: the same
    "understood" chirp, the same reply glyph, the same stats category
    for "Done, sir" and "That didn't take, sir" alike -- confirmed live
    as a real correctness gap, not a theoretical one, by checking how
    many times `hud.reply()`'s `ok=False` branch had ever actually
    fired in practice (zero; `hud.ERROR` was unreachable).

    Three states, not two, because the phrase pools already draw this
    distinction and always have -- this type just makes it visible
    outside the string itself:
      - "ok": it worked. ("Minimised, sir.")
      - "no_effect": understood, correctly, and there was genuinely
        nothing to do. ("Already up on this desktop, sir." /
        "Nothing to minimise, sir." / "It's already on your laptop
        screen, sir.") Not a failure -- the goal state may already
        hold -- but distinct from "ok" because something was *asked
        for* and nothing changed.
      - "failed": attempted and it did not work, or the request
        couldn't be serviced at all (an unregistered app, a folder
        that wouldn't read, a window that never appeared).

    `dispatch_llm_action` normalizes a bare `str` return to
    `Outcome(text, "ok")` -- real actions convert to returning this
    directly one at a time, and anything that hasn't yet (or a test's
    monkeypatched stand-in) keeps working unchanged either way.
    """

    text: str
    status: str = "ok"  # "ok" | "no_effect" | "failed"


def _process_exe_name(hwnd) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return ""


def _matches_app(hwnd, app_key: str) -> bool:
    entry = APPS[app_key]
    if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
        return False
    # Chrome_WidgetWin_1 is NOT unique to Google Chrome -- Electron apps
    # (VS Code, Discord, Slack, Teams, ...) use the same window class on
    # Windows, since Electron's native windowing is derived from Chromium's
    # own views toolkit. Window class alone can't tell them apart; the
    # owning process' actual executable name can. Learned this the hard
    # way -- with VS Code open, "open chrome" was bringing VS Code forward.
    if entry.get("window_class") and win32gui.GetClassName(hwnd) != entry["window_class"]:
        return False
    if _process_exe_name(hwnd) not in [p.lower() for p in entry["process_names"]]:
        return False
    title_contains = entry.get("title_contains")
    if title_contains and title_contains.lower() not in win32gui.GetWindowText(hwnd).lower():
        return False
    return True


def _find_app_windows(app_key: str, current_desktop_only: bool = True) -> list[int]:
    found: list[int] = []
    win32gui.EnumWindows(lambda hwnd, _: found.append(hwnd) if _matches_app(hwnd, app_key) else None, None)
    if current_desktop_only:
        found = [hwnd for hwnd in found if is_window_on_current_desktop(hwnd)]
    return found


def _force_foreground(hwnd: int) -> None:
    """
    SetForegroundWindow silently refuses to steal focus from an
    unrelated foreground process (Windows' "foreground lock") unless
    the calling process is the one that most recently handled real
    user input -- which our listener genuinely is (it just handled the
    voice/hotkey trigger). AttachThreadInput is the standard technique
    for this. (A synthetic Alt-key tap was tried here too as an extra
    "belt and suspenders" measure, but it fires on whatever window
    currently has real focus -- in VS Code, and plenty of other apps,
    a lone Alt press toggles menu-bar focus as a side effect, which
    caused Chrome to end up focused-but-behind VS Code visually. Not
    worth the side effect given AttachThreadInput alone should suffice
    here.)

    AttachThreadInput can itself legitimately fail -- confirmed
    directly: "(5, 'AttachThreadInput', 'Access is denied.')" attaching
    to a differently-privileged foreground window (an elevated process,
    the secure desktop, ...). That crashed the whole caller before this
    try/except existed -- caught by main.py's crash-recovery, but still
    a real command failing mid-flight over something that should just
    degrade. Never raises now: falls back to a plain SetForegroundWindow
    (works often enough on its own even without the attach dance), and
    failing that, BringWindowToTop -- worst case the window is merely
    visible (already ensured above via ShowWindow) rather than truly
    focused.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    current_thread = win32api.GetCurrentThreadId()

    attached_fg = attached_target = False
    try:
        try:
            if fg_thread and fg_thread != current_thread:
                win32process.AttachThreadInput(current_thread, fg_thread, True)
                attached_fg = True
            if target_thread != current_thread:
                win32process.AttachThreadInput(current_thread, target_thread, True)
                attached_target = True
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                win32process.AttachThreadInput(current_thread, fg_thread, False)
            if attached_target:
                win32process.AttachThreadInput(current_thread, target_thread, False)
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                pass


# One shared tracker across every focus_or_launch_app call -- see
# window_context.CycleTracker's docstring for why the candidate list is
# frozen per cycle rather than re-ranked on each repeat.
_cycle = window_context.CycleTracker()


def _app_hint_stopwords(app_key: str) -> "frozenset[str]":
    """
    Extra words to strip from a voice hint before matching it against
    window titles -- the app's own name and process-name stems carry no
    disambiguating information (every candidate window's title tends to
    contain them), e.g. "bring up the chrome window with the football"
    for app_key "chrome" should leave just {"football"}.
    """
    words = {app_key.replace("_", " ")}
    for name in APPS.get(app_key, {}).get("process_names", ()):
        words.add(os.path.splitext(name)[0].lower())
    stopwords: "set[str]" = set()
    for phrase in words:
        stopwords.update(phrase.split())
    return frozenset(stopwords)


def _choose_window(app_key: str, windows: "list[int]", hint_text: str) -> int:
    """
    Replaces the old windows[0] -- picks among several open windows of
    the same app using (in order of precedence) a title hint from the
    utterance, whether it's actually visible right now, the monitor
    currently in use, and recency -- see window_context.rank_windows.
    Repeating the same request shortly after cycles to the next-best
    candidate instead of asking a clarifying question out loud.
    """
    infos = window_context.enrich_windows(windows)
    if not infos:
        # Every candidate hwnd went stale between _find_app_windows'
        # enumeration and here -- fall back to the raw (now-unenriched)
        # first entry rather than crash; _force_foreground already
        # tolerates a hwnd that's since become invalid.
        return windows[0]

    hint_tokens = window_context.extract_hints(hint_text, _app_hint_stopwords(app_key))
    ranked = window_context.rank_windows(
        infos, hint_tokens=hint_tokens, preferred_monitor=window_context.foreground_monitor_key()
    )
    chosen = _cycle.choose((app_key, frozenset(hint_tokens)), ranked, time.time())
    return chosen.hwnd if chosen is not None else windows[0]


def focus_or_launch_app(app_key: str, hint_text: str = "") -> Outcome:
    """
    Only considers/launches on the *current* virtual desktop -- a
    matching window on a different desktop doesn't count as "already
    up"; a fresh instance is opened here instead of yanking the
    existing one across (SetForegroundWindow on a window on another
    desktop makes Windows switch your active desktop to follow it,
    which is not what "open X" should silently do to you).

    hint_text is the raw utterance (optional, defaults to "" for any
    caller that doesn't have one) -- used only to disambiguate which
    window to raise when several of the app are open; it plays no part
    in deciding *whether* to launch a fresh instance.
    """
    if app_key not in APPS:
        return Outcome(phrases.pick("app_not_registered", app=app_key), "failed")

    windows = _find_app_windows(app_key, current_desktop_only=True)
    if windows:
        target = _choose_window(app_key, windows, hint_text) if len(windows) > 1 else windows[0]
        _force_foreground(target)
        return Outcome(phrases.pick("focus_already_here"), "no_effect")

    was_running_elsewhere = bool(_find_app_windows(app_key, current_desktop_only=False))

    subprocess.Popen(APPS[app_key]["launch_command"], shell=True)
    for _ in range(20):
        time.sleep(0.25)
        windows = _find_app_windows(app_key, current_desktop_only=True)
        if windows:
            _force_foreground(windows[0])
            if was_running_elsewhere:
                return Outcome(phrases.pick("focus_other_desktop"), "ok")
            return Outcome(phrases.pick("focus_launched"), "ok")
    return Outcome(phrases.pick("focus_launched_no_window"), "failed")


def minimize_app(app_key: str) -> Outcome:
    if app_key not in APPS:
        return Outcome(phrases.pick("app_not_registered", app=app_key), "failed")
    windows = _find_app_windows(app_key, current_desktop_only=True)
    if not windows:
        return Outcome(phrases.pick("minimize_nothing"), "no_effect")
    for hwnd in windows:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return Outcome(phrases.pick("minimize_ok"), "ok")


def maximize_app(app_key: str) -> Outcome:
    """
    Same shape as minimize_app -- only ever acts on an already-open
    window (current desktop only), never launches a fresh one just to
    maximize it.
    """
    if app_key not in APPS:
        return Outcome(phrases.pick("app_not_registered", app=app_key), "failed")
    windows = _find_app_windows(app_key, current_desktop_only=True)
    if not windows:
        return Outcome(phrases.pick("maximize_nothing"), "no_effect")
    for hwnd in windows:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return Outcome(phrases.pick("maximize_ok"), "ok")


_REPO_FUZZY_CUTOFF = 0.72
# Looser than fuzzy_match.py's own 0.80 default -- justified by a much
# lower-severity failure mode. The candidate set here is only ever
# whatever VS Code windows are actually open (a handful, not "any
# English word"), so a wrong pick means focusing a different real,
# already-open repo -- never inventing content the way a broader
# whole-transcript repair risked (see fuzzy_match.py's module
# docstring for that test).


def _best_fuzzy_repo_match(normalized_query: str, candidates: "list[tuple[int, str]]"):
    """
    Pure matching logic behind the fallback below, kept separate from
    the EnumWindows call so it's testable without a real window manager
    (see CLAUDE.md: tests run with no window manager present).

    `candidates` is a list of (hwnd, normalized_title) pairs -- every
    open Code.exe window the exact substring check already failed to
    place. Scores each title's word n-grams (see fuzzy_match.ngrams)
    against the spoken phrase and returns the hwnd of the best-scoring
    one, or None if nothing clears _REPO_FUZZY_CUTOFF.
    """
    best_hwnd = None
    best_ratio = 0.0
    for hwnd, title in candidates:
        for phrase in fuzzy_match.ngrams(title.split()):
            ratio = difflib.SequenceMatcher(None, normalized_query, phrase).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_hwnd = hwnd
    if best_hwnd is not None and best_ratio >= _REPO_FUZZY_CUTOFF:
        return best_hwnd
    return None


def focus_vscode_repo(repo_name: str) -> Outcome:
    """
    repo_name is free text (can't pre-register every repo), matched
    as a substring against open Code.exe window titles -- inherently
    safe regardless of the string: worst case is focusing the wrong
    window if some other title happens to match, never anything
    destructive. Unlike focus_or_launch_app, this *will* cross virtual
    desktops if that's where the match is -- there's no "launch fresh"
    fallback for an arbitrary repo the way there is for a registered
    app, and "go to X" implies actually navigating there.

    Falls back to a fuzzy match (_best_fuzzy_repo_match) when no window
    title contains repo_name as an exact substring -- Whisper mishears
    repo names it was never primed on, and priming them was tried and
    reverted (see main.py's TRANSCRIBE_HOTWORDS comment for why: it
    caused real false dispatches from near-silent audio). Correcting
    after the fact, against real open windows only, doesn't share that
    failure mode.
    """
    normalized = repo_name.lower().replace("_", " ").replace("-", " ")
    found: list[int] = []
    all_code_windows: "list[tuple[int, str]]" = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        if _process_exe_name(hwnd) != "code.exe":
            return
        title = win32gui.GetWindowText(hwnd).lower().replace("_", " ").replace("-", " ")
        all_code_windows.append((hwnd, title))
        if normalized in title:
            found.append(hwnd)

    win32gui.EnumWindows(_enum, None)

    if not found:
        fuzzy_hwnd = _best_fuzzy_repo_match(normalized, all_code_windows)
        if fuzzy_hwnd is not None:
            found = [fuzzy_hwnd]

    if not found:
        return Outcome(phrases.pick("vscode_no_window", repo=repo_name), "failed")

    current_desktop = [hwnd for hwnd in found if is_window_on_current_desktop(hwnd)]
    target = current_desktop or found
    _force_foreground(target[0])
    return Outcome(phrases.pick("vscode_switching", repo=repo_name), "ok")


def switch_desktop(direction: str) -> Outcome:
    """
    Simulates Windows' own built-in Ctrl+Win+Right/Left shortcut via
    synthetic key events -- deliberately not using any undocumented
    COM interface for this (the class of API this project avoided for
    desktop detection too, see virtual_desktop.py). Absolute desktop
    numbers aren't supported -- no stable public API for that.
    """
    vk_arrow = win32con.VK_RIGHT if direction == "next" else win32con.VK_LEFT
    win32api.keybd_event(win32con.VK_LWIN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk_arrow, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_arrow, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_LWIN, 0, win32con.KEYEVENTF_KEYUP, 0)
    return Outcome(phrases.pick("switch_desktop", direction=direction), "ok")


def snap_window(direction: str) -> Outcome:
    """
    Simulates Windows' own built-in Win+Left/Right snap shortcut --
    same synthetic-key approach as switch_desktop, and for the same
    reason: a documented, built-in OS behavior, not anything bespoke.
    Always acts on whatever currently has focus, same as the real
    shortcut does -- no app parameter, nothing to target.
    """
    vk_arrow = win32con.VK_LEFT if direction == "left" else win32con.VK_RIGHT
    win32api.keybd_event(win32con.VK_LWIN, 0, 0, 0)
    win32api.keybd_event(vk_arrow, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_arrow, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_LWIN, 0, win32con.KEYEVENTF_KEYUP, 0)
    return Outcome(phrases.pick("snap", direction=direction), "ok")


def _tap_media_key(vk_code: int) -> None:
    win32api.keybd_event(vk_code, 0, 0, 0)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)


def _apply_volume_amount(hint_text: str, step_sign: int) -> "Outcome | None":
    """
    Shared by volume_up/volume_down. Returns the Outcome to use if the
    utterance carried a parseable amount, or None if it didn't -- the
    caller's cue to fall back to the plain single-tap behaviour, which
    stays completely unchanged for the common "turn it up"/"turn it
    down" case with no amount said.

    An *absolute* amount ("set the volume to 30") is applied exactly as
    given, regardless of step_sign -- confirmed live that this phrasing
    fast-path matches volume_up's own seeds (~0.83-0.85 cosine) purely
    because of surface similarity to "turn the volume up", not because
    it means "up"; direction only matters for the *relative* case ("by
    10"), where step_sign says which of volume_up/volume_down asked.
    """
    amount = slots.parse_amount(hint_text)
    if amount is None:
        return None

    if amount.mode == "absolute":
        result = audio_endpoint.set_output_volume(amount.value / 100.0)
    else:
        result = audio_endpoint.step_output_volume(step_sign * amount.value / 100.0)

    if result is None:
        return Outcome(phrases.pick("volume_unavailable"), "failed")
    return Outcome(phrases.pick("volume_at", percent=round(result * 100)), "ok")


def volume_up(hint_text: str = "") -> Outcome:
    outcome = _apply_volume_amount(hint_text, +1)
    if outcome is not None:
        return outcome
    _tap_media_key(win32con.VK_VOLUME_UP)
    return Outcome(phrases.pick("volume_up"), "ok")


def volume_down(hint_text: str = "") -> Outcome:
    outcome = _apply_volume_amount(hint_text, -1)
    if outcome is not None:
        return outcome
    _tap_media_key(win32con.VK_VOLUME_DOWN)
    return Outcome(phrases.pick("volume_down"), "ok")


def toggle_mute() -> Outcome:
    _tap_media_key(win32con.VK_VOLUME_MUTE)
    return Outcome(phrases.pick("mute"), "ok")


def media_play_pause() -> Outcome:
    _tap_media_key(win32con.VK_MEDIA_PLAY_PAUSE)
    return Outcome(phrases.pick("play_pause"), "ok")


def media_next_track() -> Outcome:
    _tap_media_key(win32con.VK_MEDIA_NEXT_TRACK)
    return Outcome(phrases.pick("next_track"), "ok")


def media_previous_track() -> Outcome:
    _tap_media_key(win32con.VK_MEDIA_PREV_TRACK)
    return Outcome(phrases.pick("prev_track"), "ok")


def lock_workstation() -> Outcome:
    """
    user32!LockWorkStation -- the documented, direct API for this
    (used by Win+L itself), rather than simulating that key combo.
    """
    ctypes.windll.user32.LockWorkStation()
    return Outcome(phrases.pick("lock"), "ok")


def set_timer(hint_text: str = "") -> Outcome:
    """
    "Set a timer for ten minutes" / "remind me in an hour to stretch" --
    duration and an optional label are both lexical hints pulled from
    the raw utterance (slots.parse_duration / slots.extract_reminder_
    label), the same zero-schema-cost pattern describe_screen's screen
    hint and volume_up/volume_down's amount already use. The actual
    scheduling lives in timers.py, a leaf module this one just calls
    into and translates the result out of, the same relationship this
    file already has with monitors.resolve_monitor_mention.
    """
    seconds = slots.parse_duration(hint_text)
    if seconds is None:
        return Outcome(phrases.pick("timer_no_duration"), "no_effect")
    label = slots.extract_reminder_label(hint_text)
    timers.set_timer(seconds, label)
    return Outcome(phrases.pick("timer_set", duration=slots.format_duration(seconds)), "ok")


def cancel_timer(hint_text: str = "") -> Outcome:
    """
    "Cancel the timer" / "cancel my stretch reminder" -- see
    timers.cancel_most_recent for how a bare cancel (no label) picks
    which timer, when more than one is running.
    """
    label = slots.extract_reminder_label(hint_text)
    cancelled = timers.cancel_most_recent(label)
    if cancelled is None:
        return Outcome(phrases.pick("timer_none_to_cancel"), "no_effect")
    return Outcome(phrases.pick("timer_cancelled"), "ok")


def list_timers() -> Outcome:
    """"How much time is left" / "what timers do I have" -- read-only, same class as now_playing/count_app_windows."""
    active = timers.list_active()
    if not active:
        return Outcome(phrases.pick("timer_list_none"), "ok")
    remaining = max(0, round(active[0]["fires_at"] - time.time()))
    duration = slots.format_duration(remaining)
    if len(active) == 1:
        return Outcome(phrases.pick("timer_list_one", duration=duration), "ok")
    return Outcome(phrases.pick("timer_list_many", count=len(active), duration=duration), "ok")


_BATTERY_WORDS = {"battery", "charge", "charged", "charging"}
_MEMORY_WORDS = {"memory", "ram"}
_DISK_WORDS = {"disk", "storage", "space", "drive"}


def _detect_status_topics(hint_text: str) -> "list[str]":
    """
    Which of battery/memory/disk was actually asked about, from the raw
    utterance -- another zero-schema-cost lexical hint, same pattern as
    monitors.resolve_monitor_mention's word sets. No recognized topic
    word at all ("how am I doing") reports all three rather than
    guessing wrong or declining -- a query action erring toward saying
    more, not less.
    """
    words = set(re.findall(r"[a-z]+", hint_text.lower()))
    topics = []
    if words & _BATTERY_WORDS:
        topics.append("battery")
    if words & _MEMORY_WORDS:
        topics.append("memory")
    if words & _DISK_WORDS:
        topics.append("disk")
    return topics or ["battery", "memory", "disk"]


def _format_bytes_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.0f} GB"


def _battery_fact() -> "str | None":
    battery = psutil.sensors_battery()
    if battery is None:
        return None  # desktops/some VMs report no battery at all -- honest omission, not a guess
    percent = round(battery.percent)
    if battery.power_plugged:
        return f"battery at {percent} percent, plugged in"
    return f"battery at {percent} percent, unplugged"


def _memory_fact() -> str:
    vmem = psutil.virtual_memory()
    return f"memory at {round(vmem.percent)} percent used"


def _disk_fact(hint_text: str) -> str:
    # Named drive letter in the utterance ("how much space on D") wins;
    # otherwise the system drive Windows itself is installed on --
    # os.environ is what Windows itself uses to answer "which drive is
    # the system on", not an assumption that it's always C.
    named = re.search(r"\b([a-z])\s*(?:drive|:)\b", hint_text.lower())
    drive = f"{named.group(1).upper()}:/" if named else os.environ.get("SystemDrive", "C:") + "/"
    try:
        usage = psutil.disk_usage(drive)
    except OSError:
        return f"couldn't read {drive}, sir -- that drive may not exist"
    return f"the {drive[:2]} drive at {round(usage.percent)} percent full, {_format_bytes_gb(usage.free)} free"


def system_status(hint_text: str = "") -> Outcome:
    """
    "How's my battery" / "how much memory am I using" / "is my disk
    full" -- one action covering all three (psutil, already a
    dependency, used today only for process names), disambiguated by
    which topic word is actually in the utterance -- see
    _detect_status_topics. Always "ok": a query that gets a true,
    honest answer has succeeded at being a query even when the number
    isn't good news (a nearly-full disk is still a real, correct
    answer) -- the one exception is no battery detected at all, which
    still reports whatever other topics were asked about rather than
    failing the whole request over one missing sensor.
    """
    topics = _detect_status_topics(hint_text)
    facts = []
    for topic in topics:
        if topic == "battery":
            fact = _battery_fact()
            if fact is not None:
                facts.append(fact)
            elif len(topics) == 1:
                return Outcome(phrases.pick("system_status_no_battery"), "ok")
        elif topic == "memory":
            facts.append(_memory_fact())
        elif topic == "disk":
            facts.append(_disk_fact(hint_text))
    if not facts:
        return Outcome(phrases.pick("system_status_no_battery"), "ok")
    return Outcome(phrases.pick("system_status", summary="; ".join(facts)), "ok")


# Windows' own default location for a screenshot -- PrintScreen, Snipping
# Tool, and Win+Shift+S all save here already, so a screenshot Jarvis
# takes shows up alongside every other one the user already knows to
# look for, rather than a project-internal folder nobody would think
# to check.
SCREENSHOTS_DIR = Path.home() / "Pictures" / "Screenshots"


def open_snipping_tool() -> Outcome:
    """
    "Take a screenshot" / "open snipping tool" -- Win+Shift+S, the same
    synthetic-built-in-shortcut precedent switch_desktop/snap_window
    already use, rather than launching Snipping Tool's own executable
    directly: this is the exact gesture that opens the region-select
    overlay, not just the app window. Always "ok" -- like every other
    synthetic-key action here, there's no OS feedback to verify against.
    """
    win32api.keybd_event(win32con.VK_LWIN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
    win32api.keybd_event(ord("S"), 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(ord("S"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_LWIN, 0, win32con.KEYEVENTF_KEYUP, 0)
    return Outcome(phrases.pick("snip_tool_opened"), "ok")


def _save_screenshot(image, prefix: str) -> "Path | None":
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(f"{prefix}_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png")
        path = SCREENSHOTS_DIR / filename
        image.save(path)
        return path
    except Exception:
        return None


def screenshot_full() -> Outcome:
    """
    "Take a screenshot of my screen" -- a silent full-screen grab (all
    monitors, PIL.ImageGrab already a dependency and unused elsewhere
    until now), saved with no UI at all, unlike open_snipping_tool's
    interactive overlay.
    """
    try:
        image = ImageGrab.grab(all_screens=True)
    except Exception:
        return Outcome(phrases.pick("screenshot_failed"), "failed")
    path = _save_screenshot(image, "screenshot")
    if path is None:
        return Outcome(phrases.pick("screenshot_failed"), "failed")
    return Outcome(phrases.pick("screenshot_saved"), "ok")


def screenshot_window() -> Outcome:
    """
    "Screenshot this window" -- just the focused window's own bounds
    (win32gui.GetWindowRect, the same live-bounds call move_window_to_
    monitor's own postcondition check uses), not the whole screen.
    Named after what's actually in it (app_titles.describe_title, the
    same per-app title cleanup describe_screen already uses) rather
    than a bare timestamp, so the file is identifiable at a glance
    later without having to open it first.
    """
    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return Outcome(phrases.pick("screenshot_no_window"), "failed")
    try:
        rect = win32gui.GetWindowRect(fg_hwnd)
        image = ImageGrab.grab(bbox=rect)
    except Exception:
        return Outcome(phrases.pick("screenshot_failed"), "failed")

    title = win32gui.GetWindowText(fg_hwnd)
    app_key = next((key for key in APPS if _matches_app(fg_hwnd, key)), None)
    descriptor = app_titles.describe_title(app_key, title) or title or "window"
    path = _save_screenshot(image, descriptor)
    if path is None:
        return Outcome(phrases.pick("screenshot_failed"), "failed")
    return Outcome(phrases.pick("screenshot_saved"), "ok")


def count_app_windows(app_key: str) -> Outcome:
    """Counts across all desktops -- "how many X are open" is naturally system-wide, not scoped to the current desktop."""
    if app_key not in APPS:
        return Outcome(phrases.pick("app_not_registered", app=app_key), "failed")
    count = len(_find_app_windows(app_key, current_desktop_only=False))
    if count == 0:
        return Outcome(phrases.pick("count_none"), "ok")
    if count == 1:
        return Outcome(phrases.pick("count_one"), "ok")
    return Outcome(phrases.pick("count_many", count=count), "ok")


def now_playing() -> Outcome:
    """
    "What's playing" -- reads Spotify's own window title, which only
    ever carries "Artist - Track" while something is actually playing
    (see app_titles.describe_spotify) -- there's no way to tell "paused"
    apart from "nothing queued" from the title alone, so both degrade to
    the same honest "can't tell" reply rather than guessing. Checked
    system-wide (current_desktop_only=False), same reasoning as
    count_app_windows -- "what's playing" is naturally not scoped to
    whichever desktop happens to be active.

    Every branch here is "ok" except the unregistered case: a query
    that gets a true, honest answer ("not open", "can't tell what's
    queued") has succeeded at being a query, even when the answer isn't
    exciting -- only "I don't know Spotify at all" is a real failure to
    service the request.
    """
    if "spotify" not in APPS:
        return Outcome(phrases.pick("now_playing_no_spotify"), "failed")
    windows = _find_app_windows("spotify", current_desktop_only=False)
    if not windows:
        return Outcome(phrases.pick("now_playing_not_open"), "ok")
    for hwnd in windows:
        track = app_titles.describe_spotify(win32gui.GetWindowText(hwnd))
        if track:
            return Outcome(phrases.pick("now_playing_track", track=track), "ok")
    return Outcome(phrases.pick("now_playing_unknown"), "ok")


_MONITOR_SPOKEN_NAMES = {
    "laptop": "your laptop screen",
    "monitor": "your monitor",
    "monitor_2": "your second monitor",
}

# Registry-key -> what to actually say. Falls back to a title-cased,
# underscore-stripped version of the key for anything not listed here
# (a discovered app, or a future curated one this map hasn't caught up
# with yet) -- see _spoken_app_name.
_APP_SPOKEN_NAMES = {
    "chrome": "Chrome",
    "discord": "Discord",
    "vscode": "VS Code",
    "file_explorer": "File Explorer",
    "settings": "Settings",
    "local_disk_d": "the D drive",
    "terminal": "the terminal",
    "spotify": "Spotify",
}


def _spoken_app_name(app_key: str) -> str:
    return _APP_SPOKEN_NAMES.get(app_key, app_key.replace("_", " ").title())


# Reverse of APPS: exe name -> candidate app keys. Built once at import
# (APPS is itself frozen at import -- see apps_registry.py) so
# describe_screen doesn't have to check every registered app against
# every window on the screen, an O(windows x apps) scan of psutil calls
# for what is usually a 1-candidate lookup.
def _build_exe_to_app_keys() -> "dict[str, list[str]]":
    index: "dict[str, list[str]]" = {}
    for key, entry in APPS.items():
        for proc in entry.get("process_names", ()):
            index.setdefault(proc.lower(), []).append(key)
    return index


_EXE_TO_APP_KEYS = _build_exe_to_app_keys()


def _resolve_app_key_for_window(hwnd) -> "str | None":
    exe = _process_exe_name(hwnd)
    for key in _EXE_TO_APP_KEYS.get(exe, ()):
        if _matches_app(hwnd, key):
            return key
    return None


def _enumerate_all_windows() -> "list[int]":
    """Every visible, titled top-level window -- the same first gate _matches_app applies, generalized to any app."""
    found: "list[int]" = []
    win32gui.EnumWindows(
        lambda hwnd, _: found.append(hwnd) if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) else None,
        None,
    )
    return found


# Shell/system windows that are visible+titled (so they'd otherwise show
# up in a screen summary) but aren't anything a person would call an
# "app". window_context's cloak filter catches most background shell
# noise (confirmed live: Windows Input Experience, Shell Experience
# Host), but Program Manager -- the desktop itself -- is neither cloaked
# nor a registered app, so it needs its own exclusion here.
_SHELL_WINDOW_CLASSES = {"Progman", "WorkerW"}


def _describe_screen_entries(monitor_key: "str | None") -> "list[tuple[str | None, str]]":
    """(app_key_or_None, cleaned_descriptor) for every real window on `monitor_key` (every monitor if None), current desktop only."""
    infos = window_context.enrich_windows(_enumerate_all_windows())
    entries: "list[tuple[str | None, str]]" = []
    for info in infos:
        if info.is_cloaked or not info.on_current_desktop:
            continue
        if monitor_key is not None and info.monitor_key != monitor_key:
            continue
        try:
            if win32gui.GetClassName(info.hwnd) in _SHELL_WINDOW_CLASSES:
                continue
        except Exception:
            continue
        app_key = _resolve_app_key_for_window(info.hwnd)
        descriptor = app_titles.describe_title(app_key, info.title) or info.title
        entries.append((app_key, descriptor))
    return entries


def _count_by_app(entries: "list[tuple[str | None, str]]") -> "list[tuple[str, int]]":
    """
    [(label, count), ...] in first-seen order. A registered app groups
    by its spoken name (so three Chrome windows become one "three
    Chrome" entry); an unregistered window is counted individually by
    its own descriptor, since there's no app identity to group it under.
    """
    counts: "dict[str, int]" = {}
    order: "list[str]" = []
    for app_key, descriptor in entries:
        label = _spoken_app_name(app_key) if app_key else descriptor
        if label not in counts:
            order.append(label)
        counts[label] = counts.get(label, 0) + 1
    return [(label, counts[label]) for label in order]


_SPOKEN_COUNTS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _phrase_app_counts(counts: "list[tuple[str, int]]") -> str:
    """[("Chrome", 2), ("VS Code", 1)] -> 'two Chrome and VS Code' -- natural comma+and join."""
    parts = []
    for label, count in counts:
        parts.append(label if count <= 1 else f"{_SPOKEN_COUNTS.get(count, str(count))} {label}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _summarize_other_monitor(current_monitor: "str | None", monitors_list: "list") -> str:
    """One short trailing clause about the other screen's contents, or '' when there isn't a single clear 'other' screen to mention."""
    others = [m for m in monitors_list if m.key != current_monitor]
    if len(others) != 1:
        return ""  # 0 (single-screen machine) or 2+ (which "other"?) -- keep the default reply simple either way
    other = others[0]
    entries = _describe_screen_entries(other.key)
    if not entries:
        return ""
    summary = _phrase_app_counts(_count_by_app(entries))
    label = _MONITOR_SPOKEN_NAMES.get(other.key, "your other screen")
    return phrases.pick("screen_other_summary", monitor=label, summary=summary)


def _describe_focused_window(fg_hwnd: int, current_monitor: "str | None", monitors_list: "list") -> Outcome:
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return Outcome(phrases.pick("screen_unknown"), "failed")
    title = win32gui.GetWindowText(fg_hwnd)
    if not title:
        return Outcome(phrases.pick("screen_unknown"), "failed")

    app_key = next((key for key in APPS if _matches_app(fg_hwnd, key)), None)
    descriptor = app_titles.describe_title(app_key, title) or title
    monitor_label = _MONITOR_SPOKEN_NAMES.get(current_monitor, "your screen")

    if app_key:
        base = phrases.pick("screen_summary_known", app=_spoken_app_name(app_key), title=descriptor, monitor=monitor_label)
    else:
        base = phrases.pick("screen_summary_unknown", title=descriptor, monitor=monitor_label)

    other_summary = _summarize_other_monitor(current_monitor, monitors_list)
    text = f"{base} {other_summary}" if other_summary else base
    return Outcome(text, "ok")


def _describe_named_screen(monitor_key: str) -> Outcome:
    entries = _describe_screen_entries(monitor_key)
    label = _MONITOR_SPOKEN_NAMES.get(monitor_key, "that screen")
    if not entries:
        return Outcome(phrases.pick("screen_empty", monitor=label), "ok")
    summary = _phrase_app_counts(_count_by_app(entries))
    return Outcome(phrases.pick("screen_named_summary", monitor=label, summary=summary), "ok")


def describe_screen(hint_text: str = "") -> Outcome:
    """
    "What's on my screen" -- read-only, same class as count_app_windows.

    With no screen named, describes the focused window plus a one-line
    summary of the other screen (see _describe_focused_window). With a
    screen named ("what's on my laptop screen", "what's on the
    monitor"), describes that screen's contents grouped by app instead
    (see _describe_named_screen) -- the focused window's own screen
    counts as "named" here too, so asking about the screen you're
    already looking at gets the grouped-by-app answer, not a repeat of
    the single-window one.

    hint_text is the raw utterance (optional, defaults to "" -- same
    convention as focus_or_launch_app's hint_text): used only to notice
    a named screen, never to change whether this runs at all.
    """
    monitors_list = monitors.list_monitors()
    current_monitor = window_context.foreground_monitor_key()

    named_key, reason = monitors.resolve_monitor_mention(hint_text, monitors_list, current_monitor)
    if reason == "numbered":
        return Outcome(phrases.pick("screen_unnamed"), "no_effect")
    if reason == "unnamed":
        return Outcome(phrases.pick("screen_unnamed"), "no_effect")
    if reason == "ambiguous":
        return Outcome(phrases.pick("screen_ambiguous"), "no_effect")
    if reason == "single":
        return Outcome(phrases.pick("screen_single"), "ok")

    if named_key is not None:
        return _describe_named_screen(named_key)

    fg_hwnd = win32gui.GetForegroundWindow()
    return _describe_focused_window(fg_hwnd, current_monitor, monitors_list)


def save_current_layout() -> None:
    """
    A one-line snapshot of what's open across every monitor, written to
    memory.json's "last_layout" key -- called once, at shutdown (see
    main.py's _shutdown), so the *next* boot's morning_briefing below
    can honestly say whether anything changed while Jarvis was down,
    rather than guessing. Shutdown, not boot, is the correct write
    point: it's the last true snapshot of the session that just ended,
    which is what the next boot needs to compare against. An unclean
    shutdown (crash, kill) just means the snapshot goes stale until the
    next clean one -- a known, low-stakes gap, not worth a second write
    path for.
    """
    entries = _describe_screen_entries(None)
    summary = _phrase_app_counts(_count_by_app(entries))
    memory.update("last_layout", lambda _current: summary)


def _time_of_day_bucket(hour: int) -> str:
    """
    Pure on purpose -- takes the hour rather than calling
    datetime.datetime.now() itself, so the four boundaries are directly
    testable without mocking the clock (same reasoning as slots.py's
    parsers staying pure). Boundaries match common usage, not sunrise/
    sunset: a 2am "good evening" would read as more broken than a 5am
    one landing in "night" instead of "morning". Returns a bare bucket
    name, not a phrase key -- morning_briefing below does the dispatch
    to phrases.pick with a literal string argument per branch, so
    test_phrases.py's "every pool gets picked somewhere" source scan
    (which only matches a quoted string directly inside the call) can
    actually see these keys being used.
    """
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def morning_briefing() -> str:
    """
    Composed once at boot (see main.py, replacing the old flat "boot"
    phrase) -- never a dispatchable action, so it isn't in ACTIONS/
    _NO_PARAM_ACTIONS and costs nothing in the LLM schema. Reuses
    system_status/describe_screen's own building blocks rather than
    re-deriving battery or window facts a second way.

    memory.json also defines "notes" and "preferences" keys, and
    neither is mentioned here -- nothing in this project writes to
    either one yet (both are placeholders for a feature that was never
    built), so there is nothing true to report; inventing content to
    fill them out would be a guess dressed up as a fact, which this
    project's own battery-sensor handling (_battery_fact's "honest
    omission, not a guess") already treats as the wrong move. "Still
    where you left them" is only ever said when the current layout
    genuinely matches the snapshot save_current_layout took at the
    *previous* shutdown -- a fresh boot with a changed desktop, or no
    prior snapshot at all, gets the plain current-state phrasing
    instead.
    """
    bucket = _time_of_day_bucket(datetime.datetime.now().hour)
    if bucket == "morning":
        sentences = [phrases.pick("briefing_greeting_morning")]
    elif bucket == "afternoon":
        sentences = [phrases.pick("briefing_greeting_afternoon")]
    elif bucket == "evening":
        sentences = [phrases.pick("briefing_greeting_evening")]
    else:
        sentences = [phrases.pick("briefing_greeting_night")]

    battery = _battery_fact()
    if battery is not None:
        sentences.append(f"{battery[0].upper()}{battery[1:]}.")

    entries = _describe_screen_entries(None)
    summary = _phrase_app_counts(_count_by_app(entries))
    if summary:
        previous_layout = memory.get("last_layout")
        if summary == previous_layout:
            sentences.append(phrases.pick("briefing_layout_unchanged", summary=summary))
        else:
            sentences.append(phrases.pick("briefing_layout_current", summary=summary))

    return " ".join(sentences)


def llm_screen_context() -> "str | None":
    """
    A short, terse description of the focused window for injection into
    the LLM fallback's Context message (see llm_intent._build_messages)
    -- plain text the model reads, never spoken, so no phrases.pick()
    here. Returns None when there's nothing useful to report (no usable
    foreground window), same guard as describe_screen's own.

    Deliberately minimal -- this costs real prefill tokens on every call
    it's included in (see main.py's deictic gate, which decides whether
    to call this at all), so it's one compact line, not a full
    describe_screen-style reply.
    """
    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return None
    title = win32gui.GetWindowText(fg_hwnd)
    if not title:
        return None
    app_key = next((key for key in APPS if _matches_app(fg_hwnd, key)), None)
    descriptor = app_titles.describe_title(app_key, title) or title
    monitor_key = window_context.foreground_monitor_key()
    monitor_label = _MONITOR_SPOKEN_NAMES.get(monitor_key, "the current screen")
    app_label = _spoken_app_name(app_key) if app_key else "an unregistered app"
    return f'{app_label} -- "{descriptor}" on {monitor_label}'


def move_window_to_monitor(hint_text: str = "") -> Outcome:
    """
    "Move this to my laptop screen" -- moves the FOREGROUND window to a
    named monitor (no app parameter, acts on whatever has focus, same
    precedent as snap_window). Target is resolved lexically from the
    raw utterance via monitors.resolve_monitor_mention, same mechanism
    as focus_or_launch_app's window hint and describe_screen's screen
    hint -- zero prompt-token cost, works identically on the fast path
    and the LLM path.

    Deterministic geometry (SetWindowPos into the target's work_rect via
    monitors.plan_move_rect), not a synthetic Win+Shift+Arrow chord --
    a deliberate deviation from switch_desktop/snap_window's "simulate
    the OS's own shortcut" precedent. Verified live on the reference
    machine: the two monitors are stacked *vertically* and overlap
    horizontally, so Left/Right is geometrically ambiguous and Windows
    publishes no Up/Down monitor-move shortcut -- a named target has to
    be addressed by geometry, there's no key combination that means "the
    laptop" regardless of physical arrangement. See README's decision
    log for the fuller justification.

    A minimized foreground window is declined rather than moved: its
    GetWindowPlacement rect can't be verified against a real monitor
    (the iconic sentinel maps to none), and this is a near-dead path in
    practice since a minimized window is essentially never foreground.
    A maximized window is restored, moved, and re-maximized -- calling
    SetWindowPos directly on a maximized window snaps it straight back
    to the *original* monitor, a well-known multi-monitor trap.

    Never claims success without checking: after moving, the window's
    actual landing monitor is re-read and compared against the target --
    only a match gets the success phrase, matching this project's
    fail-closed convention for anything correctness-adjacent.
    """
    monitors_list = monitors.list_monitors(force_refresh=True)
    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return Outcome(phrases.pick("move_monitor_no_window"), "failed")

    current_key = window_context.foreground_monitor_key()
    target_key, reason = monitors.resolve_monitor_mention(hint_text, monitors_list, current_key)

    if reason in ("numbered", "unnamed"):
        return Outcome(phrases.pick("move_monitor_unnamed"), "no_effect")
    if reason == "ambiguous" or (reason == "none" and target_key is None):
        return Outcome(phrases.pick("move_monitor_unclear"), "no_effect")
    if reason == "single":
        return Outcome(phrases.pick("move_monitor_single"), "no_effect")

    by_key = {m.key: m for m in monitors_list}
    target_monitor = by_key.get(target_key)
    if target_monitor is None:
        return Outcome(phrases.pick("move_monitor_unnamed"), "no_effect")

    if target_key == current_key:
        return Outcome(
            phrases.pick("move_monitor_already_there", monitor=_MONITOR_SPOKEN_NAMES.get(target_key, "that screen")),
            "no_effect",
        )

    if win32gui.IsIconic(fg_hwnd):
        return Outcome(phrases.pick("move_monitor_minimized"), "no_effect")

    original_placement = win32gui.GetWindowPlacement(fg_hwnd)
    is_maximized = original_placement[1] == win32con.SW_SHOWMAXIMIZED

    # ShowWindow/SetWindowPos only ever POST a request to the target
    # window's message queue -- they return before a cross-process
    # window has necessarily finished acting on it. Confirmed as a real
    # bug here: calling ShowWindow(SW_MAXIMIZE) immediately after
    # SetWindowPos, with no wait for the reposition to actually land,
    # let Windows decide which monitor to re-maximize onto based on
    # where the window *still* was -- so the whole sequence completed
    # "successfully" while the window silently stayed on the original
    # monitor. `_poll_until` below is the fix: wait for each step's
    # effect to actually be observable before trusting it and moving on
    # to the next one, with a short budget rather than a single
    # immediate check.
    def _poll_until(predicate, timeout=0.5, interval=0.02):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    try:
        source_rect = original_placement[4]  # rcNormalPosition -- correct even while maximized/minimized
        source_monitor = by_key.get(monitors.monitor_key_for_rect(source_rect))
        source_work = source_monitor.work_rect if source_monitor else source_rect
        new_rect = monitors.plan_move_rect(source_rect, source_work, target_monitor.work_rect)

        if is_maximized:
            win32gui.ShowWindow(fg_hwnd, win32con.SW_RESTORE)
            _poll_until(lambda: win32gui.GetWindowPlacement(fg_hwnd)[1] != win32con.SW_SHOWMAXIMIZED)

        win32gui.SetWindowPos(
            fg_hwnd,
            0,
            new_rect[0],
            new_rect[1],
            new_rect[2] - new_rect[0],
            new_rect[3] - new_rect[1],
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )
        _poll_until(lambda: monitors.monitor_key_for_rect(win32gui.GetWindowRect(fg_hwnd)) == target_key)

        if is_maximized:
            win32gui.ShowWindow(fg_hwnd, win32con.SW_MAXIMIZE)
            _poll_until(lambda: win32gui.GetWindowPlacement(fg_hwnd)[1] == win32con.SW_SHOWMAXIMIZED)
    except Exception:
        try:
            win32gui.SetWindowPlacement(fg_hwnd, original_placement)
        except Exception:
            pass
        return Outcome(phrases.pick("move_monitor_failed"), "failed")

    # Final postcondition -- checked against GetWindowRect (the window's
    # actual current bounds), not rcNormalPosition, since a maximized
    # window's normal-position rect is meaningless for "which monitor is
    # it showing on right now". Never claims success without this: fail
    # closed, matching the rest of this project's correctness-adjacent
    # checks.
    if not _poll_until(lambda: monitors.monitor_key_for_rect(win32gui.GetWindowRect(fg_hwnd)) == target_key):
        return Outcome(phrases.pick("move_monitor_failed"), "failed")

    return Outcome(phrases.pick("move_monitor_ok", monitor=_MONITOR_SPOKEN_NAMES.get(target_key, "that screen")), "ok")


ACTIONS = {
    "focus_or_launch_app": focus_or_launch_app,
    "minimize_app": minimize_app,
    "maximize_app": maximize_app,
    "focus_vscode_repo": focus_vscode_repo,
    "switch_desktop": switch_desktop,
    "snap_window": snap_window,
    "count_app_windows": count_app_windows,
    "describe_screen": describe_screen,
    "now_playing": now_playing,
    "move_to_monitor": move_window_to_monitor,
    "volume_up": volume_up,
    "volume_down": volume_down,
    "toggle_mute": toggle_mute,
    "media_play_pause": media_play_pause,
    "media_next_track": media_next_track,
    "media_previous_track": media_previous_track,
    "lock_workstation": lock_workstation,
    "set_timer": set_timer,
    "cancel_timer": cancel_timer,
    "list_timers": list_timers,
    "system_status": system_status,
    "open_snipping_tool": open_snipping_tool,
    "screenshot_full": screenshot_full,
    "screenshot_window": screenshot_window,
}

# Actions that take no parameter at all -- system/media controls that
# always act on the whole system or whatever currently has focus, never
# a specific registered app. Shared between dispatch_llm_action and
# parse_fast_path_intent so the two stay in sync automatically.
_NO_PARAM_ACTIONS = (
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
    "set_timer",
    "cancel_timer",
    "list_timers",
    "system_status",
    "open_snipping_tool",
    "screenshot_full",
    "screenshot_window",
)


def dispatch_llm_action(parsed: dict, text: str = "") -> Outcome:
    """
    Takes an already-validated dict from llm_intent.infer() (action,
    app, direction, repo -- app/direction/repo may be "none"/"" when
    not applicable) and calls the matching ACTIONS entry with the
    right argument. The single place that knows each action's calling
    convention, so main.py doesn't need its own if/elif per action.

    text is the raw utterance that produced `parsed` -- optional (default
    "" so every existing single-argument call site keeps working
    unchanged) and used by several actions as a free-text hint, never
    anything schema-carried: focus_or_launch_app (which window to raise
    when several of the same app are open), describe_screen (which
    screen to describe, if one was named), move_to_monitor (which screen
    to send the focused window to), volume_up/volume_down (how much to
    change by, or an exact target -- see slots.parse_amount), and
    set_timer/cancel_timer (a duration and an optional label -- see
    slots.parse_duration/extract_reminder_label). The no-param ones
    among these (describe_screen, move_to_monitor, volume_up,
    volume_down, set_timer, cancel_timer) stay in _NO_PARAM_ACTIONS for
    parse_fast_path_intent's sake (their fast-path label carries no
    parameter -- see that function), but need their own branch here
    since the *Python* call takes an argument even though the *schema*
    doesn't; _NO_PARAM_ACTIONS itself still means "zero Python args" for
    everything else in it.

    Always returns an Outcome. Every real action in ACTIONS returns one
    directly now, but a bare `str` (from a not-yet-converted action, or
    a test's monkeypatched stand-in -- see test_actions.py, which mocks
    ACTIONS entries with plain lambdas returning "ok") is normalized to
    Outcome(text, "ok") here rather than assumed away, so neither needs
    to change in lockstep with the other.
    """
    action = parsed["action"]
    if action == "focus_or_launch_app":
        result = ACTIONS[action](parsed["app"], text)
    elif action in (
        "describe_screen",
        "move_to_monitor",
        "volume_up",
        "volume_down",
        "set_timer",
        "cancel_timer",
        "system_status",
    ):
        result = ACTIONS[action](text)
    elif action in ("minimize_app", "maximize_app", "count_app_windows"):
        result = ACTIONS[action](parsed["app"])
    elif action in ("switch_desktop", "snap_window"):
        result = ACTIONS[action](parsed["direction"])
    elif action == "focus_vscode_repo":
        result = ACTIONS[action](parsed["repo"])
    elif action in _NO_PARAM_ACTIONS:
        result = ACTIONS[action]()
    else:
        return Outcome(phrases.pick("unknown_action"), "failed")
    return result if isinstance(result, Outcome) else Outcome(result, "ok")


def parse_fast_path_intent(label: str) -> dict | None:
    """
    Converts a compound embedding-matcher label ("focus_or_launch_app:chrome",
    "switch_desktop:next") -- or, for a no-parameter action, a bare
    label ("volume_up") -- into the same dict shape llm_intent.infer()
    produces, so both paths dispatch through dispatch_llm_action()
    uniformly. Returns None for labels that aren't one of these shapes
    (meta intents like disable_listening, handled separately in main.py).
    """
    if label in _NO_PARAM_ACTIONS:
        return {"action": label, "app": "none", "direction": "none", "repo": ""}
    if ":" not in label:
        return None
    action, param = label.split(":", 1)
    if action in ("focus_or_launch_app", "minimize_app", "maximize_app"):
        return {"action": action, "app": param, "direction": "none", "repo": ""}
    if action in ("switch_desktop", "snap_window"):
        return {"action": action, "app": "none", "direction": param, "repo": ""}
    return None
