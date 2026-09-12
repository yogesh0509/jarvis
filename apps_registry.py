"""
Phase 5: the registered-apps table. This IS the security boundary for
"open/minimize apps in general" -- the LLM's grammar restricts its
`app` field to exactly these keys, so it can never reference an app
that isn't explicitly registered here, no matter how a request is
phrased. Adding a new app means adding an entry here, nothing more.

Each entry:
- process_names: exe name(s) to match against open windows (psutil).
- window_class: required window class, if the process name alone is
  ambiguous (explorer.exe is also the desktop/taskbar process).
- title_contains: optional substring the window title must contain
  (used to pick out one specific explorer window, e.g. the D: drive,
  from among all explorer.exe/CabinetWClass windows).
- launch_command: what to run if no matching window is found.
"""

APPS = {
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
}
