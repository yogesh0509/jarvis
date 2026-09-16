"""
Phase 5: the registered-apps table. This IS the security boundary for
"open/minimize apps in general" -- the LLM's grammar restricts its
`app` field to exactly these keys, so it can never reference an app
that isn't explicitly registered here, no matter how a request is
phrased.

Each entry:
- process_names: exe name(s) to match against open windows (psutil).
- window_class: required window class, if the process name alone is
  ambiguous (explorer.exe is also the desktop/taskbar process).
- title_contains: optional substring the window title must contain
  (used to pick out one specific explorer window, e.g. the D: drive,
  from among all explorer.exe/CabinetWClass windows).
- launch_command: what to run if no matching window is found.

_CURATED_APPS below is the hand-maintained set -- add an entry here by
hand whenever an app needs precise disambiguation (an Electron app
sharing Chrome_WidgetWin_1 with others, a specific Explorer window by
title, ...). The public APPS is that table PLUS whatever
app_discovery.discover_apps() finds by scanning the Start Menu at
import time (Phase 6) -- curated entries always win on any collision,
by construction (see discover_apps' docstring); this file is still the
one place that defines the actual allowlist, discovery is just what
populates more of it automatically. Restart Jarvis to pick up a newly
installed app; nothing here changes mid-session.
"""

from app_discovery import discover_apps

_CURATED_APPS = {
    "chrome": {
        "process_names": ["chrome.exe"],
        "window_class": "Chrome_WidgetWin_1",
        "launch_command": 'start "" chrome.exe',
    },
    "discord": {
        "process_names": ["Discord.exe"],
        "window_class": "Chrome_WidgetWin_1",  # Discord is Electron too
        "launch_command": r'start "" "%LOCALAPPDATA%\Discord\Update.exe" --processStart Discord.exe',
    },
    "vscode": {
        "process_names": ["Code.exe"],
        "window_class": "Chrome_WidgetWin_1",  # VS Code is Electron too
        "launch_command": r'start "" "%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"',
    },
    "file_explorer": {
        "process_names": ["explorer.exe"],
        "window_class": "CabinetWClass",  # explorer.exe is also the desktop/taskbar -- this class is specifically file-browser windows
        "launch_command": "explorer.exe",
    },
    "settings": {
        "process_names": ["SystemSettings.exe"],
        "window_class": None,
        "launch_command": "start ms-settings:",
    },
    "local_disk_d": {
        "process_names": ["explorer.exe"],
        "window_class": "CabinetWClass",
        "title_contains": "(D:)",  # verified live: "Local Disk (D:) - File Explorer"
        "launch_command": "explorer.exe D:\\",
    },
    "terminal": {
        # Curated by hand, not discoverable by app_discovery.py at all --
        # confirmed directly: Windows Terminal is UWP/MSIX-packaged
        # (Microsoft.WindowsTerminal), not a classic Start Menu .lnk
        # shortcut, so the discovery scan (which only ever looks at
        # .lnk files) can't see it no matter how it's filtered. "open
        # the terminal" with no curated entry for it had nothing to
        # match and the LLM picked an unrelated discovered app instead
        # (see llm_intent.py's "don't guess" rule for the other half of
        # that fix). wt.exe is the launcher stub already on PATH (also
        # what this project's own jarvis.cmd uses to launch Jarvis
        # itself); the real running process is WindowsTerminal.exe.
        "process_names": ["WindowsTerminal.exe"],
        "window_class": "CASCADIA_HOSTING_WINDOW_CLASS",
        "launch_command": "start wt.exe",
    },
    "spotify": {
        # Curated by hand for the same reason as terminal above --
        # confirmed directly: this machine's Spotify is the MSIX/Store
        # build (SpotifyAB.SpotifyMusic), not a classic Start Menu .lnk
        # shortcut, so app_discovery.py's scan can't see it either.
        # window_class is Chrome_WidgetWin_1 -- Spotify's desktop client
        # is CEF-based, same as Chrome/Discord/VS Code -- so the exe
        # name check in _matches_app is what actually disambiguates it,
        # same as every other Electron/CEF entry here.
        # The WindowsApps\Spotify.exe stub resolves to SpotifyMigrator.exe,
        # not the real client, so launching by exe path is unreliable;
        # "spotify:" is confirmed registered in HKEY_CLASSES_ROOT and
        # launches the real client directly, same shape as settings'
        # "start ms-settings:".
        "process_names": ["Spotify.exe"],
        "window_class": "Chrome_WidgetWin_1",
        "launch_command": "start spotify:",
    },
}

APPS = {**discover_apps(_CURATED_APPS), **_CURATED_APPS}

# The curated subset alone, exposed publicly so other modules can narrow
# to it without reaching into the private _CURATED_APPS -- llm_intent.py
# uses this to keep the LLM fallback's app enum small (see its own
# comment for why: prompt-prefill cost, and a constrained decoder that
# gets handed 30+ discovered junk names as valid choices for a garbled
# transcript will pick one of them rather than refuse).
CURATED_APP_KEYS = tuple(_CURATED_APPS.keys())
