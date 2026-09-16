"""
Turns a raw window title into something worth saying out loud. "main.py
(Working Tree) (main.py) - jarvis - Visual Studio Code" is data a
window manager needs, not something a person wants read back to them.

Pure string functions, one per app, keyed the same as
apps_registry.APPS -- no win32 here at all (the raw title is already in
hand by the time anything calls this) and deliberately no APPS import
either, so this stays independently testable against captured real
titles with no window manager or registry needed, matching
window_context.py's own "pure decision logic, no win32" precedent.

Each parser returns a short descriptor, or None if the title carries no
usable information (Spotify idle/paused, an empty title) -- callers
fall back to the raw title when a parser returns None. describe_title
never raises: a parser bug degrades to the raw (trimmed) title rather
than taking down the caller.

Validated against titles captured live (see actions.describe_screen):
VS Code's `[WSL: ...]` suffix, `(Working Tree)` and duplicated
`(filename)` tags, and trailing "..." truncation; Chrome's `- Google
Chrome`/`- Google Search` suffixes, unread-count "(3) " prefixes, and
PWA windows that carry no browser suffix at all; Spotify's idle-vs-
playing title shape ("Spotify Premium" vs "Artist - Track").
"""

import re

_WS = re.compile(r"\s+")
_TRUNCATION = "…"  # the real ellipsis character VS Code truncates long titles with


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


# --- VS Code --------------------------------------------------------------

_VSCODE_SUFFIX = re.compile(r"\s*-\s*Visual Studio Code$")
_VSCODE_WSL = re.compile(r"\s*\[WSL:[^\]]*\]")
_WORKING_TREE = re.compile(r"\s*\(Working Tree\)")
_TRAILING_PAREN_TAG = re.compile(r"\s*\([^)]*\)\s*$")


def describe_vscode(title: str) -> "str | None":
    if not title:
        return None
    t = _VSCODE_SUFFIX.sub("", title)
    t = _VSCODE_WSL.sub("", t)
    t = _WORKING_TREE.sub("", t)
    t = _collapse(t)
    if not t:
        return None
    # Truncation happens per-segment (VS Code cuts the file/description
    # part, not the whole title), so strip the ellipsis per part below
    # rather than once at the end of the whole string.
    parts = [p.strip().rstrip(_TRUNCATION).strip() for p in t.split(" - ")]
    parts = [_TRAILING_PAREN_TAG.sub("", p).strip() or p for p in parts if p]
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) >= 2:
        return f"{parts[0]}, in {parts[-1]}"
    return parts[0]


# --- Chrome (and Chrome-family: Edge shares this shape closely enough) ----

_CHROME_SUFFIX = re.compile(r"\s*-\s*Google Chrome$")
_CHROME_SEARCH_SUFFIX = re.compile(r"\s*-\s*Google Search$")
_UNREAD_PREFIX = re.compile(r"^\(\d+\)\s*")


def describe_chrome(title: str) -> "str | None":
    if not title:
        return None
    t = _UNREAD_PREFIX.sub("", title)
    t = _CHROME_SUFFIX.sub("", t)  # a no-op for a PWA window, which carries no browser suffix at all
    t = _CHROME_SEARCH_SUFFIX.sub("", t)
    t = _collapse(t)
    return t or None


# --- Spotify ----------------------------------------------------------------

_SPOTIFY_IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}


def describe_spotify(title: str) -> "str | None":
    """
    None specifically means "nothing useful to say" (idle/paused) --
    Spotify's title only ever carries "Artist - Track" while something
    is actually playing; there is no track info to read back otherwise.
    """
    if not title or title.strip().lower() in _SPOTIFY_IDLE_TITLES:
        return None
    if " - " in title:
        artist, track = (part.strip() for part in title.split(" - ", 1))
        if artist and track:
            return f'"{track}" by {artist}'
        return track or artist or None
    return title.strip() or None


# --- Discord ------------------------------------------------------------

_DISCORD_SUFFIX = re.compile(r"\s*-\s*Discord$")


def describe_discord(title: str) -> "str | None":
    t = _DISCORD_SUFFIX.sub("", title or "")
    return _collapse(t) or None


# --- fallback -------------------------------------------------------------


def describe_generic(title: str) -> "str | None":
    """Used for any app with no dedicated parser -- just the trimmed raw title."""
    return _collapse(title or "") or None


_PARSERS = {
    "vscode": describe_vscode,
    "chrome": describe_chrome,
    "spotify": describe_spotify,
    "discord": describe_discord,
}


def describe_title(app_key: "str | None", title: str) -> "str | None":
    """
    The public entry point -- dispatches to the app-specific parser if
    one exists for `app_key`, else the generic trim. Never raises.
    """
    parser = _PARSERS.get(app_key, describe_generic)
    try:
        return parser(title)
    except Exception:
        return describe_generic(title)
