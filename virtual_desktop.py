"""
Windows Virtual Desktop awareness via the officially documented
IVirtualDesktopManager COM interface -- stable across Windows 10/11
builds, unlike the various reverse-engineered internal interfaces
some third-party libraries lean on for extra features we don't need.
"""

import ctypes
from ctypes import wintypes

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

CLSID_VirtualDesktopManager = GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
IID_IVirtualDesktopManager = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")


class IVirtualDesktopManager(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IVirtualDesktopManager
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "IsWindowOnCurrentVirtualDesktop",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["out"], ctypes.POINTER(wintypes.BOOL), "onCurrentDesktop"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetWindowDesktopId",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["out"], ctypes.POINTER(GUID), "desktopId"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "MoveWindowToDesktop",
            (["in"], wintypes.HWND, "topLevelWindow"),
            (["in"], ctypes.POINTER(GUID), "desktopId"),
        ),
    ]


_manager = None


def _get_manager():
    global _manager
    if _manager is None:
        comtypes.CoInitialize()
        # This is an in-process COM server (twinapi/shell), not a local
        # out-of-process one -- CLSCTX_LOCAL_SERVER fails with "Class
        # not registered" (verified the hard way: it does NOT raise
        # loudly on its own here, it just means every subsequent call
        # would silently need a fallback -- which is exactly why the
        # per-window fallback below fails *closed*, not open).
        _manager = comtypes.CoCreateInstance(
            CLSID_VirtualDesktopManager,
            interface=IVirtualDesktopManager,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
    return _manager


def is_window_on_current_desktop(hwnd: int) -> bool:
    """
    Fails *closed* (treats an unconfirmed window as NOT on the current
    desktop) rather than open. Getting this wrong the other way once
    already caused a real bug: SetForegroundWindow on a window that's
    actually on a different virtual desktop makes Windows switch your
    active desktop to follow it, so a silent "assume yes" here means
    surprise desktop-switching. Worst case on this side is a redundant
    extra Chrome window, which is harmless.
    """
    try:
        return bool(_get_manager().IsWindowOnCurrentVirtualDesktop(hwnd))
    except Exception:
        return False
