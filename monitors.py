r"""
Monitor identification -- maps a physical display to a stable, speakable
name ("laptop" vs "monitor") so window-selection code (window_context.py)
and voice commands can refer to "my monitor"/"the laptop screen" instead
of Windows' own renumbering-prone `\\.\DISPLAYn` device names.

Two Win32 layers, deliberately kept separate:
  - `EnumDisplayMonitors`/`GetMonitorInfo` (pywin32, already a dependency)
    gives geometry and the primary-monitor flag. Confirmed live on a
    two-screen setup: the PRIMARY monitor is the *external* one here, not
    the laptop panel -- so `is_primary` cannot be used as an "is this the
    laptop" heuristic; it's the wrong signal entirely on this exact
    machine.
  - `QueryDisplayConfig`/`DisplayConfigGetDeviceInfo` (raw ctypes -- not
    wrapped by pywin32) gives `outputTechnology`, which distinguishes
    `DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL` (a laptop's own panel)
    from HDMI/DisplayPort/DVI (an external monitor) -- confirmed live,
    correctly identifying the laptop panel as internal on the same
    two-screen setup where the primary-flag heuristic gets it backwards.
    No new dependency: `ctypes.windll.user32` ships with the OS.

Fails closed to neutral names ("screen_1", "screen_2", ...) if
QueryDisplayConfig is unavailable or its shape doesn't match what's
expected here (a VM, RDP session, or unusual driver) -- never guesses
which one is the laptop. A wrong guess here is the worst kind of wrong:
"move this to my monitor" silently moving it to the laptop instead reads
as Jarvis doing the opposite of what was asked, whereas a neutral name
just means voice commands naming "laptop"/"monitor" don't resolve and
window_context.py's other ranking signals (hint text, recency) carry the
decision instead.

Deliberately NOT frozen at import time the way apps_registry.APPS is --
monitors get plugged and unplugged mid-session, unlike installed apps.
Cached with a short TTL instead (see list_monitors), refreshed
automatically once the cache goes stale.
"""

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import win32api

# DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL, from the Windows SDK's
# wingdi.h -- the sentinel value ctypes has no built-in name for.
_OUTPUT_TECHNOLOGY_INTERNAL = 0x80000000
_QDC_ONLY_ACTIVE_PATHS = 2
_DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class _DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRateNumerator", wintypes.UINT),
        ("refreshRateDenominator", wintypes.UINT),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class _DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", _DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", _DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.UINT),
    ]


class _DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # Fields beyond the header are a union this project never reads --
    # a fixed byte blob sized to the real struct is enough to keep
    # QueryDisplayConfig's array stride correct.
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", _LUID),
        ("_blob", ctypes.c_byte * 64),
    ]


class _DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
    ]


class _DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("edidManufactureId", wintypes.USHORT),
        ("edidProductCodeId", wintypes.USHORT),
        ("connectorInstance", wintypes.UINT),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


class _DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", wintypes.WCHAR * 32),
    ]


@dataclass(frozen=True)
class MonitorInfo:
    device: str  # GDI name, e.g. r"\\.\DISPLAY1" -- NOT stable across replug, join key only
    key: str  # stable spoken name: "laptop", "monitor" (or "monitor_2", ...), or "screen_N" on fallback
    is_internal: bool
    is_primary: bool
    friendly_name: str  # "" for an internal panel or when unavailable
    rect: "tuple[int, int, int, int]"  # (left, top, right, bottom), virtualized coords -- see module note
    work_rect: "tuple[int, int, int, int]"


def _query_output_technologies() -> "dict[str, int]":
    """
    GDI device name -> DISPLAYCONFIG_OUTPUT_TECHNOLOGY_* for every active
    path, via QueryDisplayConfig. Returns {} on any failure (missing API,
    unexpected return code, a struct field ctypes couldn't marshal) --
    the caller falls back to neutral naming rather than guessing.
    """
    try:
        user32 = ctypes.windll.user32
        num_paths = wintypes.UINT()
        num_modes = wintypes.UINT()
        if user32.GetDisplayConfigBufferSizes(
            _QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), ctypes.byref(num_modes)
        ):
            return {}
        paths = (_DISPLAYCONFIG_PATH_INFO * num_paths.value)()
        modes = (_DISPLAYCONFIG_MODE_INFO * num_modes.value)()
        if user32.QueryDisplayConfig(
            _QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(num_paths),
            paths,
            ctypes.byref(num_modes),
            modes,
            None,
        ):
            return {}

        result: "dict[str, int]" = {}
        for i in range(num_paths.value):
            path = paths[i]
            source = _DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source.header.type = 1  # DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source.header.size = ctypes.sizeof(source)
            source.header.adapterId = path.sourceInfo.adapterId
            source.header.id = path.sourceInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(source)):
                continue
            result[source.viewGdiDeviceName] = path.targetInfo.outputTechnology
        return result
    except Exception:
        return {}


def _classify(
    raw_monitors: "list[tuple[str, tuple, tuple, bool]]",
    output_technologies: "dict[str, int]",
) -> "list[MonitorInfo]":
    """
    Pure -- no win32 calls -- so this is testable with synthetic monitor
    lists. `raw_monitors` is (device, rect, work_rect, is_primary);
    `output_technologies` is device -> outputTechnology from
    _query_output_technologies (possibly empty, if that failed).

    Naming: the internal panel (if identified) is always "laptop". The
    first external monitor is "monitor"; a second, third, ... external
    gets "monitor_2", "monitor_3" (realistically rare -- most setups have
    at most one external). If output_technologies is empty (query
    failed) or doesn't cover every device, every monitor falls back to
    neutral "screen_1", "screen_2", ... in enumeration order -- fails
    closed rather than guessing which one is the laptop.
    """
    known = output_technologies and all(device in output_technologies for device, *_ in raw_monitors)
    if not known:
        return [
            MonitorInfo(
                device=device,
                key=f"screen_{i + 1}",
                is_internal=False,
                is_primary=is_primary,
                friendly_name="",
                rect=rect,
                work_rect=work_rect,
            )
            for i, (device, rect, work_rect, is_primary) in enumerate(raw_monitors)
        ]

    external_count = 0
    result = []
    for device, rect, work_rect, is_primary in raw_monitors:
        is_internal = output_technologies[device] == _OUTPUT_TECHNOLOGY_INTERNAL
        if is_internal:
            key = "laptop"
        else:
            external_count += 1
            key = "monitor" if external_count == 1 else f"monitor_{external_count}"
        result.append(
            MonitorInfo(
                device=device,
                key=key,
                is_internal=is_internal,
                is_primary=is_primary,
                friendly_name="",
                rect=rect,
                work_rect=work_rect,
            )
        )
    return result


_CACHE_TTL_SECONDS = 10.0
_cache: "Optional[list[MonitorInfo]]" = None
_cache_ts = 0.0


def list_monitors(*, force_refresh: bool = False) -> "list[MonitorInfo]":
    """
    All active monitors, classified. Cached for _CACHE_TTL_SECONDS --
    cheap to call (a few hundred microseconds measured) but not free,
    and this can be called once per candidate window during ranking.
    Not invalidated by a display-change event (that would need a message
    pump); a short TTL is the simpler tradeoff for a value that changes
    only when a cable gets plugged or unplugged.
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if not force_refresh and _cache is not None and (now - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache

    raw = []
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        raw.append((info["Device"], info["Monitor"], info["Work"], bool(info["Flags"] & 1)))

    technologies = _query_output_technologies()
    _cache = _classify(raw, technologies)
    _cache_ts = now
    return _cache


def monitor_key_for_point(x: int, y: int) -> Optional[str]:
    """The key of whichever monitor contains (x, y), or None if it's off every screen."""
    for monitor in list_monitors():
        left, top, right, bottom = monitor.rect
        if left <= x < right and top <= y < bottom:
            return monitor.key
    return None


def monitor_key_for_rect(rect: "tuple[int, int, int, int]") -> Optional[str]:
    """The key of whichever monitor contains the rect's center point."""
    left, top, right, bottom = rect
    return monitor_key_for_point((left + right) // 2, (top + bottom) // 2)


# --- spoken-mention resolution --------------------------------------------
#
# Word -> monitor key. Shared vocabulary for describe_screen ("what's on
# my laptop screen") and, later, any command that needs to target a
# named screen -- kept in one place so it only needs tuning once.
# Deliberately excludes numbered forms ("screen 2", "the second
# monitor") -- EnumDisplayMonitors' enumeration order carries no
# user-facing meaning (confirmed live: it does not even agree with
# is_primary, let alone a left-to-right/first-to-second reading a user
# would expect), so a number is refused rather than guessed at.
_LAPTOP_WORDS = {"laptop", "internal", "builtin", "notebook"}
_EXTERNAL_WORDS = {"monitor", "external", "display", "big"}
_OTHER_WORDS = {"other", "another", "opposite", "second"}
_THIS_WORDS = {"this", "here", "current"}
_SCREEN_NOUNS = {"screen", "monitor", "display", "laptop"}
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "1", "2", "3", "4", "5",
    "first", "second", "third", "fourth", "fifth",
}


def resolve_monitor_mention(
    text: str, monitors_list: "list[MonitorInfo]", current_key: "Optional[str]" = None
) -> "tuple[Optional[str], str]":
    """
    Pure -- no win32 -- resolves a spoken mention of a screen ("my
    laptop", "the other monitor", "the big screen") against
    `monitors_list` to a monitor key. Returns (key, "ok") on success, or
    (None, reason) when it can't be resolved:
      - "none"      -- no screen word found at all; caller should treat
                        this as "no screen was named", not an error.
      - "unnamed"    -- a screen word was used but nothing in
                        monitors_list matches it (no internal panel
                        found, or the fail-closed screen_N naming means
                        "laptop"/"monitor" don't resolve to anything).
      - "numbered"   -- a specific number was used; declined on
                        principle (see module note above), never guessed.
      - "single"     -- "the other screen" with only one monitor, which
                        has nothing to mean.
      - "ambiguous"  -- "the other screen" with 3+ monitors and no way
                        to know which "other" is meant.
    """
    words = set(text.lower().replace("'", "").split())
    if words & _NUMBER_WORDS:
        return None, "numbered"
    if not words & (_SCREEN_NOUNS | _OTHER_WORDS | _THIS_WORDS):
        return None, "none"

    by_key = {m.key: m for m in monitors_list}

    # "other" checked first, deliberately ahead of the laptop/external
    # word checks below -- "my other monitor" said while already on the
    # monitor means "the other one" (the laptop), not "stay on the
    # monitor because the phrase happens to contain the word too".
    # Confirmed as a real ordering bug during testing: checking
    # laptop/external first made "other monitor" a no-op when already on
    # the monitor, instead of switching to the laptop as intended.
    if words & _OTHER_WORDS:
        if len(monitors_list) <= 1:
            return None, "single"
        if current_key and len(monitors_list) == 2:
            other = next((m for m in monitors_list if m.key != current_key), None)
            return (other.key, "ok") if other else (None, "unnamed")
        return None, "ambiguous"

    if words & _LAPTOP_WORDS:
        laptop = next((m for m in monitors_list if m.is_internal), None)
        return (laptop.key, "ok") if laptop else (None, "unnamed")

    if words & _EXTERNAL_WORDS:
        external = by_key.get("monitor")
        return (external.key, "ok") if external else (None, "unnamed")

    if words & _THIS_WORDS and current_key:
        return current_key, "ok"

    return None, "none"


# --- move-target geometry --------------------------------------------------


def plan_move_rect(
    window_rect: "tuple[int, int, int, int]",
    source_work: "tuple[int, int, int, int]",
    target_work: "tuple[int, int, int, int]",
    min_size: "tuple[int, int]" = (320, 200),
) -> "tuple[int, int, int, int]":
    """
    Pure -- no win32 -- proportional remap of a window's rect from one
    monitor's work area into another's, preserving relative position and
    size rather than preserving absolute pixels: a window occupying 60%
    of the source screen's width occupies 60% of the target's, not 100%
    clipped or a fixed size that ignores how different the two screens
    are. This is what Windows' own Win+Shift+Arrow does, and it handles
    both shrinking (external -> laptop) and growing (laptop -> external)
    symmetrically with no special-casing.

    Not DPI compensation -- this project's process is DPI-unaware, so
    every rect already lives in the same virtualized coordinate space
    (see monitors.py's module docstring); the ratio math here is purely
    about screen-to-screen proportion, nothing to do with per-monitor
    scaling. Do not "fix" this by adding SetProcessDpiAwareness -- that
    would change every rect this module and window_context.py already
    produce, all at once.

    Clamped to fit entirely inside target_work, with a minimum-size
    floor so a pathological ratio can't produce an unusable sliver.
    Degenerate input (a zero-area source or target work rect) falls back
    to a safe inset placement rather than dividing by zero.
    """
    sl, st, sr, sb = source_work
    sw, sh = sr - sl, sb - st
    tl, tt, tr, tb = target_work
    tw, th = tr - tl, tb - tt

    if sw <= 0 or sh <= 0 or tw <= 0 or th <= 0:
        margin = 40
        new_l, new_t = tl + margin, tt + margin
        new_r = max(new_l + min_size[0], tr - margin)
        new_b = max(new_t + min_size[1], tb - margin)
        return (new_l, new_t, new_r, new_b)

    wl, wt, wr, wb = window_rect
    ww, wh = wr - wl, wb - wt

    rel_x = (wl - sl) / sw
    rel_y = (wt - st) / sh
    rel_w = ww / sw
    rel_h = wh / sh

    new_w = max(min_size[0], min(tw, rel_w * tw))
    new_h = max(min_size[1], min(th, rel_h * th))
    new_l = tl + rel_x * tw
    new_t = tt + rel_y * th

    new_l = max(tl, min(new_l, tr - new_w))
    new_t = max(tt, min(new_t, tb - new_h))

    return (int(new_l), int(new_t), int(new_l + new_w), int(new_t + new_h))
