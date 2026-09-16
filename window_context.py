"""
Window ranking for "which one did you mean" -- the piece that was
missing from actions.py's focus_or_launch_app, which used to take
windows[0] (whatever EnumWindows happened to yield first) with no
notion of which monitor you're looking at, which window you used most
recently, or any word you actually said to disambiguate. Deliberately
narrow in scope: this module does NOT know what an "app" is or how to
match a window to one (that stays in actions.py/apps_registry.py) --
it only takes an already-matched list of candidate hwnds and decides
which is the best guess, plus builds the metadata that decision needs
(which monitor a window is on, whether it's minimized, its recency).

Kept free of any apps_registry/APPS dependency on purpose, so it has
no import-time coupling to the registry and stays independently
testable: everything decision-shaped here (extract_hints, rank_windows,
CycleTracker) is a pure function/class over plain data, with the win32
calls confined to enrich_windows/foreground_monitor_key.
"""

import ctypes
import re
import time
from dataclasses import dataclass
from typing import Optional

import win32gui

import monitors
from virtual_desktop import is_window_on_current_desktop

# DWM cloaks a window that's on another virtual desktop, and also a
# suspended/ghost UWP window that's technically IsWindowVisible-true but
# not actually showing anything (confirmed live: Settings/Input
# Experience host windows sit cloaked while visible-by-the-API-only).
# Not a substitute for is_window_on_current_desktop -- that stays the
# fail-closed authority for the cross-desktop question -- this is a
# cheap *additional* signal for "is this actually a live window right
# now", used only to break ties, never to override the desktop check.
_DWM_CLOAKED = 14


def _is_cloaked(hwnd: int) -> bool:
    try:
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, _DWM_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return bool(value.value)
    except Exception:
        return False


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    monitor_key: Optional[str]
    z_index: int  # position in the input list -- see enrich_windows
    is_foreground: bool
    is_minimized: bool
    is_cloaked: bool
    on_current_desktop: bool


def enrich_windows(hwnds: "list[int]") -> "list[WindowInfo]":
    """
    Builds ranking metadata for an already-matched list of candidate
    hwnds (e.g. actions._find_app_windows' result for one app_key).
    z_index is just the input list's own order -- callers pass hwnds in
    EnumWindows order, which is Z-order (topmost/most-recently-active
    first) as an observed (if undocumented) Windows behavior; this is
    the same "cheap proxy for MRU" reasoning the codebase already uses
    for focus_vscode_repo's current-desktop-first tiebreak.

    Never raises on a single bad hwnd (stale/closed between enumeration
    and here) -- that window is just dropped, not fatal to the others.
    """
    result = []
    for z_index, hwnd in enumerate(hwnds):
        try:
            if not win32gui.IsWindow(hwnd):
                continue
            # GetWindowRect on a minimized window returns Windows' own
            # off-screen iconic sentinel (observed: (-32000, -32000,
            # ...)), which resolves to no monitor at all -- exactly the
            # windows this ranking cares most about placing correctly.
            # GetWindowPlacement's rcNormalPosition is the restored
            # (non-iconified) position regardless of current state, so
            # it still reflects the real monitor for a minimized window.
            rect = win32gui.GetWindowPlacement(hwnd)[4]
            result.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=win32gui.GetWindowText(hwnd),
                    monitor_key=monitors.monitor_key_for_rect(rect),
                    z_index=z_index,
                    is_foreground=(hwnd == win32gui.GetForegroundWindow()),
                    is_minimized=win32gui.IsIconic(hwnd),
                    is_cloaked=_is_cloaked(hwnd),
                    on_current_desktop=is_window_on_current_desktop(hwnd),
                )
            )
        except Exception:
            continue
    return result


def foreground_monitor_key() -> Optional[str]:
    """
    Which monitor the current foreground window is on -- used as the
    "current screen" bias in rank_windows. Fails closed to None (no
    monitor preference applied) on a 0/stale handle, same guard as
    actions.count_files_in_active_explorer_folder's documented crash fix.
    """
    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return None
    try:
        return monitors.monitor_key_for_rect(win32gui.GetWindowRect(fg_hwnd))
    except Exception:
        return None


# Words that carry no disambiguating information for "which window did
# you mean" -- verbs/fillers from how these commands are actually
# phrased, not a general-purpose stopword list. Deliberately small and
# specific rather than borrowed from an NLP library: the failure mode of
# a missing stopword is just "one harmless extra hint token that happens
# to match every window and gets dropped anyway" (see extract_hints'
# "matches everything" rule below), not a wrong answer.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "me",
        "please",
        "can",
        "you",
        "could",
        "would",
        "open",
        "bring",
        "pull",
        "switch",
        "go",
        "up",
        "to",
        "over",
        "on",
        "in",
        "window",
        "tab",
        "with",
        "that",
        "one",
        "for",
        "and",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> "list[str]":
    return _WORD_RE.findall(text.lower().replace("_", " ").replace("-", " "))


def extract_hints(text: str, extra_stopwords: "frozenset[str]" = frozenset()) -> "set[str]":
    """
    Pulls out whatever's left of an utterance after removing command
    verbs/fillers and (via extra_stopwords) the app's own name -- e.g.
    "bring up the chrome window with the football" with app_key "chrome"
    and its process-name stems as extra_stopwords leaves {"football"}.
    Pure and app-agnostic; the caller supplies extra_stopwords for
    whatever app is being disambiguated.

    Tokens shorter than 3 characters are dropped (too weak a signal,
    and this is where stray single letters from a messy transcript
    would otherwise land). Returns an empty set for a plain "open
    chrome" with no hint at all, which rank_windows treats as "no
    title-based preference" rather than an error.
    """
    stop = _STOPWORDS | extra_stopwords
    return {word for word in _words(text) if len(word) >= 3 and word not in stop}


def _score(window: WindowInfo, hint_tokens: "set[str]", preferred_monitor: Optional[str]) -> tuple:
    """
    Lexicographic score, high-to-low: a title hint match dominates
    everything (if you named it, that's the answer); then whether the
    window is actually visible right now (a minimized match loses to a
    visible one, since focusing "brings up" what you're already looking
    at in preference to un-hiding something else); then whether it's on
    the monitor you're currently working on; then recency (z-order).
    Returned as a tuple so higher is always simply "sorts first" via
    reverse=True in rank_windows -- no hand-tuned weighted sum to get
    wrong.
    """
    title_words = set(_words(window.title))
    hint_matches = len(hint_tokens & title_words) if hint_tokens else 0
    return (
        hint_matches,
        not window.is_minimized,
        window.monitor_key is not None and window.monitor_key == preferred_monitor,
        -window.z_index,
    )


def rank_windows(
    windows: "list[WindowInfo]",
    *,
    hint_tokens: "set[str]" = frozenset(),
    preferred_monitor: Optional[str] = None,
) -> "list[WindowInfo]":
    """
    Best guess first. A hint token that matches every candidate carries
    no disambiguating information, so it's dropped before scoring --
    without this, a hint mentioning the app's own generic word (missed
    by the caller's extra_stopwords) would just tie everything and
    silently fall through to the lower tiers anyway, but dropping it
    explicitly keeps the scoring honest about what actually discriminated.
    """
    if hint_tokens and windows:
        titles = [set(_words(w.title)) for w in windows]
        hint_tokens = {token for token in hint_tokens if not all(token in title for title in titles)}
    return sorted(windows, key=lambda w: _score(w, hint_tokens, preferred_monitor), reverse=True)


class CycleTracker:
    """
    Lets repeating the same request within a short window cycle to the
    next-best candidate ("bring up chrome" twice in a row raises a
    different Chrome window the second time) without ever asking a
    clarifying question out loud.

    The candidate list is frozen on the first request of a cycle and
    never re-ranked on a repeat -- deliberately: focusing a window
    changes its Z-order, so re-ranking between calls would put the
    window just raised back at the top and make "the other one" oscillate
    between the same two windows forever instead of advancing. `now` is
    a parameter rather than read internally so this is testable without
    real timing.
    """

    CYCLE_WINDOW_SECONDS = 12.0

    def __init__(self) -> None:
        self._key: Optional[tuple] = None
        self._hwnds: "tuple[int, ...]" = ()
        self._index = 0
        self._last_ts = 0.0

    def choose(self, key: tuple, ranked: "list[WindowInfo]", now: float) -> Optional[WindowInfo]:
        """
        `key` should capture everything that makes two requests "the
        same ask" (e.g. (action, app_key, frozenset(hint_tokens))) --
        a different key always starts a fresh cycle at index 0.
        Returns None if `ranked` is empty (nothing to choose from).
        """
        if not ranked:
            self._key = None
            return None

        by_hwnd = {w.hwnd: w for w in ranked}
        is_repeat = key == self._key and (now - self._last_ts) <= self.CYCLE_WINDOW_SECONDS and self._hwnds

        if is_repeat:
            # Prune hwnds that closed since the list was frozen, but
            # keep the frozen order and current position otherwise.
            surviving = [h for h in self._hwnds if h in by_hwnd]
            if surviving:
                self._index = (self._index + 1) % len(surviving)
                self._hwnds = tuple(surviving)
            else:
                is_repeat = False  # everything from the old cycle is gone -- start over below

        if not is_repeat:
            self._key = key
            self._hwnds = tuple(w.hwnd for w in ranked)
            self._index = 0

        self._last_ts = now
        chosen_hwnd = self._hwnds[self._index]
        return by_hwnd.get(chosen_hwnd, ranked[0])
