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

import os
import subprocess
import time

import psutil
import win32api
import win32com.client
import win32con
import win32gui
import win32process

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
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    current_thread = win32api.GetCurrentThreadId()

    attached_fg = attached_target = False
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


def focus_or_launch_app(app_key: str) -> str:
    """
    Only considers/launches on the *current* virtual desktop -- a
    matching window on a different desktop doesn't count as "already
    up"; a fresh instance is opened here instead of yanking the
    existing one across (SetForegroundWindow on a window on another
    desktop makes Windows switch your active desktop to follow it,
    which is not what "open X" should silently do to you).
    """
    if app_key not in APPS:
        return f"I don't have \"{app_key}\" registered, sir."

    windows = _find_app_windows(app_key, current_desktop_only=True)
    if windows:
        _force_foreground(windows[0])
        return "Already up on this desktop, sir -- bringing it forward."

    was_running_elsewhere = bool(_find_app_windows(app_key, current_desktop_only=False))

    subprocess.Popen(APPS[app_key]["launch_command"], shell=True)
    for _ in range(20):
        time.sleep(0.25)
        windows = _find_app_windows(app_key, current_desktop_only=True)
        if windows:
            _force_foreground(windows[0])
            if was_running_elsewhere:
                return "That's open on another desktop, sir -- opened a fresh one here instead."
            return "Wasn't running. Fired it up for you."
    return "Told it to open, sir, but I haven't spotted its window yet."


def minimize_app(app_key: str) -> str:
    if app_key not in APPS:
        return f"I don't have \"{app_key}\" registered, sir."
    windows = _find_app_windows(app_key, current_desktop_only=True)
    if not windows:
        return "That's not open on this desktop, sir -- nothing to minimize."
    for hwnd in windows:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return "Done, sir."


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
        return f"I don't see a VS Code window open for {repo_name}, sir."

    current_desktop = [hwnd for hwnd in found if is_window_on_current_desktop(hwnd)]
    target = current_desktop or found
    _force_foreground(target[0])
    return f"Switching to {repo_name}, sir."


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
    return f"Switching to the {direction} desktop, sir."


def count_app_windows(app_key: str) -> str:
    """Counts across all desktops -- "how many X are open" is naturally system-wide, not scoped to the current desktop."""
    if app_key not in APPS:
        return f"I don't have \"{app_key}\" registered, sir."
    count = len(_find_app_windows(app_key, current_desktop_only=False))
    if count == 0:
        return "None open right now, sir."
    if count == 1:
        return "One window open, sir."
    return f"{count} windows open, sir."


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
        return "No File Explorer window is focused right now, sir."
    if win32gui.GetClassName(fg_hwnd) != "CabinetWClass":
        return "No File Explorer window is focused right now, sir."
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        for window in shell.Windows():
            if window.HWND == fg_hwnd:
                path = window.Document.Folder.Self.Path
                count = sum(1 for entry in os.scandir(path) if entry.is_file())
                return f"{count} files in that folder, sir."
    except Exception:
        pass
    return "Couldn't read that folder, sir."


ACTIONS = {
    "focus_or_launch_app": focus_or_launch_app,
    "minimize_app": minimize_app,
    "focus_vscode_repo": focus_vscode_repo,
    "switch_desktop": switch_desktop,
    "count_app_windows": count_app_windows,
    "count_files_in_folder": count_files_in_active_explorer_folder,
}


def dispatch_llm_action(parsed: dict) -> str:
    """
    Takes an already-validated dict from llm_intent.infer() (action,
    app, direction, repo -- app/direction/repo may be "none"/"" when
    not applicable) and calls the matching ACTIONS entry with the
    right argument. The single place that knows each action's calling
    convention, so main.py doesn't need its own if/elif per action.
    """
    action = parsed["action"]
    if action in ("focus_or_launch_app", "minimize_app", "count_app_windows"):
        return ACTIONS[action](parsed["app"])
    if action == "switch_desktop":
        return ACTIONS[action](parsed["direction"])
    if action == "focus_vscode_repo":
        return ACTIONS[action](parsed["repo"])
    if action == "count_files_in_folder":
        return ACTIONS[action]()
    return "I understood that as something to do, sir, but I'm not sure how to actually do it yet."


def parse_fast_path_intent(label: str) -> dict | None:
    """
    Converts a compound embedding-matcher label ("focus_or_launch_app:chrome",
    "switch_desktop:next") into the same dict shape llm_intent.infer()
    produces, so both paths dispatch through dispatch_llm_action()
    uniformly. Returns None for labels that aren't compound (meta
    intents like disable_listening, handled separately in main.py).
    """
    if ":" not in label:
        return None
    action, param = label.split(":", 1)
    if action in ("focus_or_launch_app", "minimize_app"):
        return {"action": action, "app": param, "direction": "none", "repo": ""}
    if action == "switch_desktop":
        return {"action": action, "app": "none", "direction": param, "repo": ""}
    return None
