"""
Phase 6: expands apps_registry.APPS at startup by scanning the Start
Menu's shortcut folders, instead of relying solely on the hand-curated
table there for every app the user might want to open. Runs once, at
apps_registry.py's import time -- the result is folded into APPS and
stays fixed for the rest of the process's life, same as the curated
entries always have been; "restart Jarvis" is how a newly-installed
app gets picked up, not anything live (see apps_registry.py's own
docstring for the fuller rationale, and why the curated entries always
win on any collision -- this module never touches or overrides them).

Deliberately conservative about what counts as a real app:
- Only shortcuts (.lnk) whose resolved target is an existing .exe file
  -- skips links to websites (.url), documents, folders, uninstallers,
  and anything whose target has since been removed.
- Skips anything whose target executable is already covered by a
  curated entry (matched by exe name, not by shortcut name) -- avoids
  a redundant, differently-keyed duplicate for the same real app (e.g.
  a "Google Chrome" shortcut targeting chrome.exe when "chrome" is
  already curated).
- No window_class is set for a discovered entry (there's no way to
  know it without the app actually running at least once) -- matches
  the existing "settings" entry's pattern of window_class=None, which
  already means "don't check, process name alone is enough". This is
  a real, accepted gap for Electron apps: a discovered Electron app
  could get confused with another one the same way chrome/discord/
  vscode did before they were curated by hand (see actions.py's
  _matches_app) -- the curated table is exactly the escape hatch for
  when that actually happens for an app worth the extra precision.

Discovered apps are LLM-fallback only, never added as fast-path
embedding seeds in intent_matcher.py -- there could be dozens of them,
and seeding all of them would add real embedding cost at every startup
for entries most sessions will never say aloud (same reasoning already
applied to maximize_app there).
"""

import os
from pathlib import Path

import win32com.client

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
]

# Shortcut *names* (not targets) containing any of these are skipped
# outright -- cheap to filter before ever touching the COM shortcut
# resolver, and these are never things worth saying "open X" to anyway.
# "(x86)"/"x86" specifically: a legacy 32-bit duplicate sitting right
# alongside the real (64-bit) shortcut for the same app on every
# machine that has one -- confirmed directly, "Windows PowerShell
# (x86)" showed up as its own separate discovered entry.
_SKIP_NAME_SUBSTRINGS = (
    "uninstall",
    "readme",
    "release notes",
    "documentation",
    "help",
    "website",
    "license",
    "(x86)",
    "x86",
    "administrative tools",
)

# Shortcut *folder names* (Start Menu subfolders these live in) that
# are Windows' own dumping ground for admin/accessibility utilities --
# confirmed directly: a first pass registered dfrgui, iscsicpl,
# odbcad32, regedit, msconfig, msinfo32, narrator, magnify, osk and a
# dozen more, none of which anyone says "open X" for by name. Matched
# against every folder component between the Start Menu root and the
# shortcut, not just its immediate parent.
_SKIP_FOLDER_NAMES = {"administrative tools", "accessibility", "system tools", "startup"}

# A real app's shortcut name is a handful of words; this is a length
# cap, not a word-count one, to keep the check simple -- confirmed
# directly: an audio-driver utility's shortcut name ran to over 90
# characters ("MaxxAudio Pro by Waves Speaker and Microphone Audio
# Control and Nx 3D Sound"), which would both make a useless voice
# target and bloat the LLM's app enum for zero benefit.
_MAX_NAME_LENGTH = 60


def _normalize_key(name: str) -> str:
    key = "".join(c.lower() if c.isalnum() else "_" for c in name)
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _resolve_shortcut_target(shell, lnk_path: Path) -> "str | None":
    try:
        target = shell.CreateShortCut(str(lnk_path)).TargetPath
    except Exception:
        # Any one shortcut being corrupt/unreadable shouldn't sink the
        # whole scan -- skip it and move on.
        return None
    if not target or not target.lower().endswith(".exe"):
        return None
    if not os.path.isfile(target):
        return None
    return target


def discover_apps(curated: dict) -> dict:
    """
    Returns new APPS-shaped entries for shortcuts found in the Start
    Menu that aren't already covered by `curated` (matched by target
    exe name, not by shortcut name). Never overwrites or duplicates a
    curated entry. Fails closed to an empty dict on any unexpected
    error -- worst case is "fewer apps auto-discovered this run", never
    a crash on startup over what's meant to be a convenience layer.
    """
    try:
        curated_exe_names = {exe.lower() for entry in curated.values() for exe in entry.get("process_names", [])}
        discovered: dict = {}

        shell = win32com.client.Dispatch("WScript.Shell")

        for start_dir in _START_MENU_DIRS:
            if not start_dir.is_dir():
                continue
            for lnk_path in start_dir.rglob("*.lnk"):
                name = lnk_path.stem
                if len(name) > _MAX_NAME_LENGTH:
                    continue
                if any(skip in name.lower() for skip in _SKIP_NAME_SUBSTRINGS):
                    continue
                folder_parts = lnk_path.relative_to(start_dir).parts[:-1]
                if any(part.lower() in _SKIP_FOLDER_NAMES for part in folder_parts):
                    continue

                key = _normalize_key(name)
                if not key or key in curated or key in discovered:
                    continue

                target = _resolve_shortcut_target(shell, lnk_path)
                if target is None:
                    continue

                exe_name = os.path.basename(target).lower()
                if exe_name in curated_exe_names:
                    continue

                discovered[key] = {
                    "process_names": [os.path.basename(target)],
                    "window_class": None,
                    "launch_command": f'start "" "{target}"',
                }
                # First shortcut found for a given exe wins; a later
                # duplicate (e.g. the same app also has a shortcut in
                # a per-user folder) is skipped rather than adding a
                # second, differently-keyed entry for the same exe.
                curated_exe_names.add(exe_name)

        return discovered
    except Exception:
        return {}
