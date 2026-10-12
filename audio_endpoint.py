"""
Exact volume control and mic-level reading via IAudioEndpointVolume --
the interface behind the physical Windows volume slider (both the
speaker slider and the microphone level slider under Settings -> Sound).

Two separate, unrelated uses share this one interface:

- **Render endpoint (speakers), read/write** -- actions.py's volume_up/
  volume_down/set_volume need an exact percentage, not "tap the volume
  key N times and hope" (see the log: "reduce the volume by 10" said
  three separate times, because a single VK_VOLUME_DOWN tap moves ~2%
  and there was no way to ask for more than one step).
- **Capture endpoint (microphone), read-only** -- a boot-time check for
  DECISIONS.md's own noted gap: "a manual check of the Windows mic
  input level/boost -- there's no simple built-in PowerShell way to read
  or adjust this, so it wasn't automated." This module automates the
  *read* half. It deliberately never writes the capture level: that
  slider may already be tuned for a call or a recording this process
  knows nothing about, and silently changing a system audio setting is
  exactly the kind of side effect actions.ACTIONS' allowlist exists to
  prevent. Detection and an honest complaint only -- see main.py's
  clipping-fraction check (computed straight from captured samples, the
  authoritative signal) for the half of this that actually decides
  anything.

Why this isn't in output_monitor.py: that module is a single dedicated
background-polling thread with its own COM apartment for its own life,
built around IAudioMeterInformation specifically. This is called
synchronously, on demand, from whichever thread is dispatching an
action (main.py's listener thread for a wake-word session, or a
per-press worker thread for push-to-talk -- see virtual_desktop.py's
_get_manager docstring for why that matters: a COM interface pointer
can't safely cross threads without marshalling). Same problem
virtual_desktop.py already solved, so this reuses its exact fix:
thread-local, one CoCreateInstance per thread that ever calls in here,
never a shared global.
"""

import threading
from ctypes import POINTER, c_float, c_uint32, c_void_p

import comtypes
from comtypes import COMMETHOD, CLSCTX_ALL, GUID, HRESULT, IUnknown

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

_E_RENDER = 0
_E_CAPTURE = 1
_E_CONSOLE = 0


class IAudioEndpointVolume(IUnknown):
    """
    Declared only up through GetMasterVolumeLevelScalar (vtable slot 6)
    -- the four slots ahead of it (RegisterControlChangeNotify /
    UnregisterControlChangeNotify / GetChannelCount / SetMasterVolumeLevel)
    are declared too, with accurate parameter shapes taken straight from
    endpointvolume.h, purely so their vtable slots land at the right
    offset for the ones after them. None of the four is ever called.

    Per KNOWN_LIMITATIONS.md's own standing caution about hand-declared
    vtables (raised there against UIA's ~80-method interface): every
    method actually invoked below has its exact documented signature;
    nothing here is a guess.
    """

    _iid_ = IID_IAudioEndpointVolume
    _methods_ = [
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "notify")),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "notify")),
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint32), "count")),
        COMMETHOD(
            [],
            HRESULT,
            "SetMasterVolumeLevel",
            (["in"], c_float, "levelDB"),
            (["in"], c_void_p, "eventContext"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetMasterVolumeLevelScalar",
            (["in"], c_float, "level"),
            (["in"], c_void_p, "eventContext"),
        ),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "levelDB")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "level")),
    ]


class IMMDevice(IUnknown):
    """
    OpenPropertyStore/GetId are declared for loopback.py (which reuses
    this declaration rather than growing a third copy -- output_monitor.py
    has its own, Activate-only). GetId's string comes back as a raw
    pointer, not c_wchar_p, on purpose: it's CoTaskMemAlloc'd and the
    caller has to free it, which comtypes' automatic conversion would
    silently leak -- loopback.py polls it once a second for the
    process's whole life.
    """

    _iid_ = IID_IMMDevice
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Activate",
            (["in"], POINTER(GUID), "iid"),
            (["in"], c_uint32, "clsctx"),
            (["in"], c_void_p, "params"),
            (["out"], POINTER(POINTER(IUnknown)), "iface"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "OpenPropertyStore",
            (["in"], c_uint32, "access"),
            (["out"], POINTER(POINTER(IUnknown)), "properties"),
        ),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "id")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = IID_IMMDeviceEnumerator
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "EnumAudioEndpoints",
            (["in"], c_uint32, "dataFlow"),
            (["in"], c_uint32, "stateMask"),
            (["out"], POINTER(POINTER(IUnknown)), "devices"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDefaultAudioEndpoint",
            (["in"], c_uint32, "dataFlow"),
            (["in"], c_uint32, "role"),
            (["out"], POINTER(POINTER(IMMDevice)), "device"),
        ),
    ]


def _clamp_scalar(value: float) -> float:
    """
    The one piece of decision logic in this module worth its own test,
    kept COM-free the same way output_monitor.EdgeDebouncer is -- the
    rest of this file is COM plumbing that needs a real audio stack to
    exercise, which the test environment deliberately doesn't have (see
    CLAUDE.md: no audio device present).
    """
    return max(0.0, min(1.0, value))


_thread_local = threading.local()


def _get_enumerator():
    """
    Thread-local for the same reason virtual_desktop._get_manager is --
    see that module's docstring for the exact failure mode a shared
    global would reintroduce.
    """
    if not hasattr(_thread_local, "enumerator"):
        comtypes.CoInitialize()
        _thread_local.enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=CLSCTX_ALL
        )
    return _thread_local.enumerator


def _endpoint_volume(data_flow: int):
    device = _get_enumerator().GetDefaultAudioEndpoint(data_flow, _E_CONSOLE)
    raw = device.Activate(IID_IAudioEndpointVolume, CLSCTX_ALL, None)
    return raw.QueryInterface(IAudioEndpointVolume)


def get_output_volume():
    """Current speaker volume as a 0.0-1.0 scalar, or None if it couldn't be read."""
    try:
        return float(_endpoint_volume(_E_RENDER).GetMasterVolumeLevelScalar())
    except Exception:
        return None


def set_output_volume(scalar: float):
    """
    Set speaker volume to an exact 0.0-1.0 scalar. Returns the value
    actually applied (clamped into range), or None on failure -- the
    caller (actions.set_volume) uses the returned value to word its
    reply ("forty percent, sir") rather than echoing back whatever was
    asked for, in case it got clamped.
    """
    clamped = _clamp_scalar(scalar)
    try:
        _endpoint_volume(_E_RENDER).SetMasterVolumeLevelScalar(clamped, None)
        return clamped
    except Exception:
        return None


def step_output_volume(delta: float):
    """
    Adjust speaker volume by a relative amount (positive or negative),
    clamped to 0.0-1.0. Returns the new value, or None on failure --
    read-then-write rather than a hardware step, since a scalar step
    doesn't have a "step size" independent of what's already declared
    for VK_VOLUME_UP/DOWN (actions.py keeps using those for the no-
    amount case; this is only for "by N").
    """
    current = get_output_volume()
    if current is None:
        return None
    return set_output_volume(current + delta)


def get_input_volume():
    """
    Current microphone level as a 0.0-1.0 scalar, or None if it
    couldn't be read. Read-only by design -- see the module docstring.

    This is the software level slider Windows shows under Settings ->
    Sound -> Input -> Device properties -> Volume. It is NOT the same
    thing as a driver's separate "Microphone Boost" checkbox (that is
    typically a vendor-specific property with no interface as stable as
    this one, so it is outside what this function can see) -- a machine
    can clip with this reading low if boost is on elsewhere. Treat a
    high reading here as one confirmable cause, not the only possible
    one; the per-utterance clipped-sample fraction computed straight
    from captured audio in main.py is the signal that actually knows.
    """
    try:
        return float(_endpoint_volume(_E_CAPTURE).GetMasterVolumeLevelScalar())
    except Exception:
        return None
