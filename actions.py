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
import os
import subprocess
import time

import psutil
import win32api
import win32com.client
import win32con
import win32gui
import win32process

import app_titles
import monitors
import phrases
import window_context
from apps_registry import APPS
from virtual_desktop import is_window_on_current_desktop


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


def focus_or_launch_app(app_key: str, hint_text: str = "") -> str:
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
        return phrases.pick("app_not_registered", app=app_key)

    windows = _find_app_windows(app_key, current_desktop_only=True)
    if windows:
        target = _choose_window(app_key, windows, hint_text) if len(windows) > 1 else windows[0]
        _force_foreground(target)
        return phrases.pick("focus_already_here")

    was_running_elsewhere = bool(_find_app_windows(app_key, current_desktop_only=False))

    subprocess.Popen(APPS[app_key]["launch_command"], shell=True)
    for _ in range(20):
        time.sleep(0.25)
        windows = _find_app_windows(app_key, current_desktop_only=True)
        if windows:
            _force_foreground(windows[0])
            if was_running_elsewhere:
                return phrases.pick("focus_other_desktop")
            return phrases.pick("focus_launched")
    return phrases.pick("focus_launched_no_window")


def minimize_app(app_key: str) -> str:
    if app_key not in APPS:
        return phrases.pick("app_not_registered", app=app_key)
    windows = _find_app_windows(app_key, current_desktop_only=True)
    if not windows:
        return phrases.pick("minimize_nothing")
    for hwnd in windows:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return phrases.pick("minimize_ok")


def maximize_app(app_key: str) -> str:
    """
    Same shape as minimize_app -- only ever acts on an already-open
    window (current desktop only), never launches a fresh one just to
    maximize it.
    """
    if app_key not in APPS:
        return phrases.pick("app_not_registered", app=app_key)
    windows = _find_app_windows(app_key, current_desktop_only=True)
    if not windows:
        return phrases.pick("maximize_nothing")
    for hwnd in windows:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return phrases.pick("maximize_ok")


def focus_vscode_repo(repo_name: str) -> str:
    """
    repo_name is free text (can't pre-register every repo), matched
    as a substring against open Code.exe window titles -- inherently
    safe regardless of the string: worst case is focusing the wrong
    window if some other title happens to match, never anything
    destructive. Unlike focus_or_launch_app, this *will* cross virtual
    desktops if that's where the match is -- there's no "launch fresh"
    fallback for an arbitrary repo the way there is for a registered
    app, and "go to X" implies actually navigating there.
    """
    normalized = repo_name.lower().replace("_", " ").replace("-", " ")
    found: list[int] = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        if _process_exe_name(hwnd) != "code.exe":
            return
        title = win32gui.GetWindowText(hwnd).lower().replace("_", " ").replace("-", " ")
        if normalized in title:
            found.append(hwnd)

    win32gui.EnumWindows(_enum, None)
    if not found:
        return phrases.pick("vscode_no_window", repo=repo_name)

    current_desktop = [hwnd for hwnd in found if is_window_on_current_desktop(hwnd)]
    target = current_desktop or found
    _force_foreground(target[0])
    return phrases.pick("vscode_switching", repo=repo_name)


def switch_desktop(direction: str) -> str:
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
    return phrases.pick("switch_desktop", direction=direction)


def snap_window(direction: str) -> str:
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
    return phrases.pick("snap", direction=direction)


def _tap_media_key(vk_code: int) -> None:
    win32api.keybd_event(vk_code, 0, 0, 0)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)


def volume_up() -> str:
    _tap_media_key(win32con.VK_VOLUME_UP)
    return phrases.pick("volume_up")


def volume_down() -> str:
    _tap_media_key(win32con.VK_VOLUME_DOWN)
    return phrases.pick("volume_down")


def toggle_mute() -> str:
    _tap_media_key(win32con.VK_VOLUME_MUTE)
    return phrases.pick("mute")


def media_play_pause() -> str:
    _tap_media_key(win32con.VK_MEDIA_PLAY_PAUSE)
    return phrases.pick("play_pause")


def media_next_track() -> str:
    _tap_media_key(win32con.VK_MEDIA_NEXT_TRACK)
    return phrases.pick("next_track")


def media_previous_track() -> str:
    _tap_media_key(win32con.VK_MEDIA_PREV_TRACK)
    return phrases.pick("prev_track")


def lock_workstation() -> str:
    """
    user32!LockWorkStation -- the documented, direct API for this
    (used by Win+L itself), rather than simulating that key combo.
    """
    ctypes.windll.user32.LockWorkStation()
    return phrases.pick("lock")


def count_app_windows(app_key: str) -> str:
    """Counts across all desktops -- "how many X are open" is naturally system-wide, not scoped to the current desktop."""
    if app_key not in APPS:
        return phrases.pick("app_not_registered", app=app_key)
    count = len(_find_app_windows(app_key, current_desktop_only=False))
    if count == 0:
        return phrases.pick("count_none")
    if count == 1:
        return phrases.pick("count_one")
    return phrases.pick("count_many", count=count)


def now_playing() -> str:
    """
    "What's playing" -- reads Spotify's own window title, which only
    ever carries "Artist - Track" while something is actually playing
    (see app_titles.describe_spotify) -- there's no way to tell "paused"
    apart from "nothing queued" from the title alone, so both degrade to
    the same honest "can't tell" reply rather than guessing. Checked
    system-wide (current_desktop_only=False), same reasoning as
    count_app_windows -- "what's playing" is naturally not scoped to
    whichever desktop happens to be active.
    """
    if "spotify" not in APPS:
        return phrases.pick("now_playing_no_spotify")
    windows = _find_app_windows("spotify", current_desktop_only=False)
    if not windows:
        return phrases.pick("now_playing_not_open")
    for hwnd in windows:
        track = app_titles.describe_spotify(win32gui.GetWindowText(hwnd))
        if track:
            return phrases.pick("now_playing_track", track=track)
    return phrases.pick("now_playing_unknown")


def count_files_in_active_explorer_folder() -> str:
    """
    "This folder" = whatever the currently-focused File Explorer window
    is showing, read via Shell.Application COM automation. Read-only --
    just os.scandir on a path Explorer itself already has open.
    """
    fg_hwnd = win32gui.GetForegroundWindow()
    # GetForegroundWindow can return 0, or a handle that's gone stale
    # between this call and the next -- GetClassName throws on either
    # (crashed the listener thread once with "Invalid window handle").
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return phrases.pick("explorer_not_focused")
    if win32gui.GetClassName(fg_hwnd) != "CabinetWClass":
        return phrases.pick("explorer_not_focused")
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        for window in shell.Windows():
            if window.HWND == fg_hwnd:
                path = window.Document.Folder.Self.Path
                count = sum(1 for entry in os.scandir(path) if entry.is_file())
                return phrases.pick("folder_count", count=count)
    except Exception:
        pass
    return phrases.pick("folder_unreadable")


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


def _describe_focused_window(fg_hwnd: int, current_monitor: "str | None", monitors_list: "list") -> str:
    if not fg_hwnd or not win32gui.IsWindow(fg_hwnd):
        return phrases.pick("screen_unknown")
    title = win32gui.GetWindowText(fg_hwnd)
    if not title:
        return phrases.pick("screen_unknown")

    app_key = next((key for key in APPS if _matches_app(fg_hwnd, key)), None)
    descriptor = app_titles.describe_title(app_key, title) or title
    monitor_label = _MONITOR_SPOKEN_NAMES.get(current_monitor, "your screen")

    if app_key:
        base = phrases.pick("screen_summary_known", app=_spoken_app_name(app_key), title=descriptor, monitor=monitor_label)
    else:
        base = phrases.pick("screen_summary_unknown", title=descriptor, monitor=monitor_label)

    other_summary = _summarize_other_monitor(current_monitor, monitors_list)
    return f"{base} {other_summary}" if other_summary else base


def _describe_named_screen(monitor_key: str) -> str:
    entries = _describe_screen_entries(monitor_key)
    label = _MONITOR_SPOKEN_NAMES.get(monitor_key, "that screen")
    if not entries:
        return phrases.pick("screen_empty", monitor=label)
    summary = _phrase_app_counts(_count_by_app(entries))
    return phrases.pick("screen_named_summary", monitor=label, summary=summary)


def describe_screen(hint_text: str = "") -> str:
    """
    "What's on my screen" -- read-only, same class as
    count_files_in_active_explorer_folder/count_app_windows.

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
        return phrases.pick("screen_unnamed")
    if reason == "unnamed":
        return phrases.pick("screen_unnamed")
    if reason == "ambiguous":
        return phrases.pick("screen_ambiguous")
    if reason == "single":
        return phrases.pick("screen_single")

    if named_key is not None:
        return _describe_named_screen(named_key)

    fg_hwnd = win32gui.GetForegroundWindow()
    return _describe_focused_window(fg_hwnd, current_monitor, monitors_list)


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


def move_window_to_monitor(hint_text: str = "") -> str:
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
        return phrases.pick("move_monitor_no_window")

    current_key = window_context.foreground_monitor_key()
    target_key, reason = monitors.resolve_monitor_mention(hint_text, monitors_list, current_key)

    if reason in ("numbered", "unnamed"):
        return phrases.pick("move_monitor_unnamed")
    if reason == "ambiguous" or (reason == "none" and target_key is None):
        return phrases.pick("move_monitor_unclear")
    if reason == "single":
        return phrases.pick("move_monitor_single")

    by_key = {m.key: m for m in monitors_list}
    target_monitor = by_key.get(target_key)
    if target_monitor is None:
        return phrases.pick("move_monitor_unnamed")

    if target_key == current_key:
        return phrases.pick("move_monitor_already_there", monitor=_MONITOR_SPOKEN_NAMES.get(target_key, "that screen"))

    if win32gui.IsIconic(fg_hwnd):
        return phrases.pick("move_monitor_minimized")

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
        return phrases.pick("move_monitor_failed")

    # Final postcondition -- checked against GetWindowRect (the window's
    # actual current bounds), not rcNormalPosition, since a maximized
    # window's normal-position rect is meaningless for "which monitor is
    # it showing on right now". Never claims success without this: fail
    # closed, matching the rest of this project's correctness-adjacent
    # checks.
    if not _poll_until(lambda: monitors.monitor_key_for_rect(win32gui.GetWindowRect(fg_hwnd)) == target_key):
        return phrases.pick("move_monitor_failed")

    return phrases.pick("move_monitor_ok", monitor=_MONITOR_SPOKEN_NAMES.get(target_key, "that screen"))


ACTIONS = {
    "focus_or_launch_app": focus_or_launch_app,
    "minimize_app": minimize_app,
    "maximize_app": maximize_app,
    "focus_vscode_repo": focus_vscode_repo,
    "switch_desktop": switch_desktop,
    "snap_window": snap_window,
    "count_app_windows": count_app_windows,
    "count_files_in_folder": count_files_in_active_explorer_folder,
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
)


def dispatch_llm_action(parsed: dict, text: str = "") -> str:
    """
    Takes an already-validated dict from llm_intent.infer() (action,
    app, direction, repo -- app/direction/repo may be "none"/"" when
    not applicable) and calls the matching ACTIONS entry with the
    right argument. The single place that knows each action's calling
    convention, so main.py doesn't need its own if/elif per action.

    text is the raw utterance that produced `parsed` -- optional (default
    "" so every existing single-argument call site keeps working
    unchanged) and used by three actions as a free-text hint, never
    anything schema-carried: focus_or_launch_app (which window to raise
    when several of the same app are open), describe_screen (which
    screen to describe, if one was named), and move_to_monitor (which
    screen to send the focused window to). All three stay in
    _NO_PARAM_ACTIONS for parse_fast_path_intent's sake (their fast-path
    label carries no parameter -- see that function), but need their own
    branch here since the *Python* call takes an argument even though
    the *schema* doesn't; _NO_PARAM_ACTIONS itself still means "zero
    Python args" for everything else in it.
    """
    action = parsed["action"]
    if action == "focus_or_launch_app":
        return ACTIONS[action](parsed["app"], text)
    if action in ("describe_screen", "move_to_monitor"):
        return ACTIONS[action](text)
    if action in ("minimize_app", "maximize_app", "count_app_windows"):
        return ACTIONS[action](parsed["app"])
    if action in ("switch_desktop", "snap_window"):
        return ACTIONS[action](parsed["direction"])
    if action == "focus_vscode_repo":
        return ACTIONS[action](parsed["repo"])
    if action == "count_files_in_folder" or action in _NO_PARAM_ACTIONS:
        return ACTIONS[action]()
    return phrases.pick("unknown_action")


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
