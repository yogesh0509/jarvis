"""
WASAPI loopback capture of whatever Windows is sending to the default
output device -- the *reference* signal echo_canceller.py subtracts
from the mic.

output_monitor.py can only answer "is anything playing"; to hear the
user *over* the music instead of going deaf for the length of it
(~29.5h across a 660h log, see DECISIONS.md), the echo canceller needs
the actual samples that are about to come out of the speakers. Loopback is the one
place those exist for every app at once -- Spotify, a browser, a call,
Jarvis's own voice -- and for every output at once too: it taps the
mix Windows hands the default render endpoint, whether that's the
laptop speakers, an HDMI monitor or a Bluetooth speaker.

Hand-rolled comtypes, like output_monitor.py and audio_endpoint.py,
rather than a new dependency: sounddevice's bundled PortAudio build
exposes no loopback devices at all (checked: no "[Loopback]" entries,
PaWasapi_IsLoopback not exported). The stream asks Windows for 16kHz
mono float directly (AUTOCONVERTPCM) -- the engine's own resampler does
the 48kHz-stereo-to-16kHz-mono work, confirmed working on this machine
for a loopback stream, so there's no DSP in here at all.

Two things about loopback that shape the rest of this file:

- **An idle endpoint delivers no packets, not silent ones.** Nothing
  playing means GetNextPacketSize() just keeps saying 0. The echo
  canceller wants an unbroken reference timeline, so GapFiller
  synthesizes the silence the device didn't send.
- **It follows the default device only by being reopened.** A stream
  stays bound to the endpoint it was opened on; plugging in earbuds
  makes them the default but this would keep capturing the old
  speakers. The reader polls the default endpoint's ID once a second
  and reopens on a change, bumping generation() so the canceller knows
  its learned echo path is now meaningless.

Failure here never takes anything down: the reader thread logs nothing
itself (no _log_event from a leaf module), just drops to unhealthy and
retries, and echo_canceller.py treats an unhealthy reference as "not
covering" -- which puts main.py's output gate back exactly where it was
before any of this existed.
"""

import threading
import time
import traceback
from collections import deque
from ctypes import (
    POINTER,
    Structure,
    addressof,
    byref,
    c_int64,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    string_at,
    windll,
    wstring_at,
)

import comtypes
import numpy as np
from comtypes import COMMETHOD, CLSCTX_ALL, GUID, HRESULT, IUnknown

from audio_endpoint import CLSID_MMDeviceEnumerator, IMMDeviceEnumerator

SAMPLE_RATE = 16000
# 10ms -- WebRTC's audio processing module only ever takes frames of
# exactly this length, so the reference is cut to it at the source.
FRAME_SAMPLES = 160
POLL_SECONDS = 0.01
# A healthy stream delivers a packet every ~10ms; a gap this long means
# the endpoint has gone idle and the silence has to be synthesized.
GAP_FILL_AFTER_SECONDS = 0.03
ENDPOINT_CHECK_SECONDS = 1.0
REOPEN_BACKOFF_SECONDS = 1.0
# 1.5s. echo_canceller.DelayAligner deliberately holds the reference
# back by most of the measured echo delay (~0.5s on this machine's
# laptop speakers, more over Bluetooth -- up to its MAX_ECHO_DELAY_MS),
# so the queue has to be deeper than that plus a block of slack. While
# nothing is consuming (paused, or AEC switched off) this also bounds
# memory.
MAX_PENDING_FRAMES = 150
# How stale the last successful poll may be before is_healthy() stops
# vouching for the stream.
HEALTHY_POLL_SECONDS = 0.5

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
_E_RENDER = 0
_E_CONSOLE = 0
_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_IEEE_FLOAT = 3
_BUFFER_DURATION_HNS = 2_000_000  # 200ms, in 100ns units
_STGM_READ = 0
_VT_LPWSTR = 31
_VT_UI4 = 19

# EndpointFormFactor (mmdeviceapi.h) -- for logs and the spike's report,
# never for a decision: whether an output is covered is measured, not
# assumed from what kind of device it claims to be.
FORM_FACTORS = {
    0: "remote network device",
    1: "speakers",
    2: "line level",
    3: "headphones",
    4: "microphone",
    5: "headset",
    6: "handset",
    7: "digital passthrough",
    8: "spdif",
    9: "display audio (hdmi/dp)",
    10: "unknown",
}


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_uint32)]


class PROPVARIANT(Structure):
    # 8-byte header + 16-byte union on x64. Only VT_LPWSTR and VT_UI4 are
    # ever read, both of which live in the first 8 bytes of the union.
    _fields_ = [
        ("vt", c_uint16),
        ("reserved1", c_uint16),
        ("reserved2", c_uint16),
        ("reserved3", c_uint16),
        ("value", c_uint64),
        ("value2", c_uint64),
    ]


PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
PKEY_AudioEndpoint_FormFactor = PROPERTYKEY(GUID("{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}"), 0)


class IPropertyStore(IUnknown):
    _iid_ = IID_IPropertyStore
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "count")),
        COMMETHOD([], HRESULT, "GetAt", (["in"], c_uint32, "index"), (["out"], POINTER(PROPERTYKEY), "key")),
        COMMETHOD(
            [],
            HRESULT,
            "GetValue",
            (["in"], POINTER(PROPERTYKEY), "key"),
            (["in"], POINTER(PROPVARIANT), "value"),
        ),
    ]


class IAudioClient(IUnknown):
    # Full vtable in audioclient.h order -- every slot up to GetService
    # has to be declared for GetService to land at the right offset,
    # even though only a handful are ever called.
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Initialize",
            (["in"], c_uint32, "shareMode"),
            (["in"], c_uint32, "streamFlags"),
            (["in"], c_int64, "bufferDuration"),
            (["in"], c_int64, "periodicity"),
            (["in"], c_void_p, "format"),
            (["in"], c_void_p, "sessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_int64), "latency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD(
            [],
            HRESULT,
            "IsFormatSupported",
            (["in"], c_uint32, "shareMode"),
            (["in"], c_void_p, "format"),
            (["out"], POINTER(c_void_p), "closest"),
        ),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(c_void_p), "format")),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevicePeriod",
            (["out"], POINTER(c_int64), "defaultPeriod"),
            (["out"], POINTER(c_int64), "minimumPeriod"),
        ),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "handle")),
        COMMETHOD(
            [],
            HRESULT,
            "GetService",
            (["in"], POINTER(GUID), "iid"),
            (["out"], POINTER(POINTER(IUnknown)), "service"),
        ),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(c_void_p), "data"),
            (["out"], POINTER(c_uint32), "frames"),
            (["out"], POINTER(c_uint32), "flags"),
            (["out"], POINTER(c_uint64), "devicePosition"),
            (["out"], POINTER(c_uint64), "qpcPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


windll.ole32.CoTaskMemFree.argtypes = [c_void_p]
windll.ole32.PropVariantClear.argtypes = [c_void_p]


# --- pure helpers (no COM) -------------------------------------------


class Framer:
    """
    Cuts an arbitrary-length sample stream into exact FRAME_SAMPLES
    frames, carrying the remainder over to the next push. Module-level
    and COM-free for the same reason output_monitor.EdgeDebouncer is:
    it's the part of this file with logic worth testing.
    """

    def __init__(self, frame_samples: int = FRAME_SAMPLES):
        self.frame_samples = frame_samples
        self._carry = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> "list[np.ndarray]":
        buf = np.concatenate([self._carry, np.asarray(samples, dtype=np.float32)])
        whole = len(buf) // self.frame_samples
        frames = [
            buf[i * self.frame_samples:(i + 1) * self.frame_samples].copy()
            for i in range(whole)
        ]
        self._carry = buf[whole * self.frame_samples:].copy()
        return frames

    def reset(self) -> None:
        self._carry = np.zeros(0, dtype=np.float32)


class GapFiller:
    """
    How much silence to synthesize while the endpoint sends nothing.

    Tracks the wall-clock point up to which the reference timeline is
    already accounted for. Real data resets that to "now"; a poll with
    nothing in it, once the gap passes `fill_after_seconds`, hands back
    exactly the sample count of the gap and advances the mark by that
    much (not to "now" -- int() truncation would otherwise shave a
    fraction of a sample off every fill and drift the timeline short).
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, fill_after_seconds: float = GAP_FILL_AFTER_SECONDS):
        self.sample_rate = sample_rate
        self.fill_after_seconds = fill_after_seconds
        self._covered_until: "float | None" = None

    def on_data(self, now: float) -> None:
        self._covered_until = now

    def silence_due(self, now: float) -> int:
        if self._covered_until is None:
            self._covered_until = now
            return 0
        gap = now - self._covered_until
        if gap < self.fill_after_seconds:
            return 0
        samples = int(gap * self.sample_rate)
        self._covered_until += samples / self.sample_rate
        return samples


# --- COM plumbing ------------------------------------------------------


def _device_id(device) -> str:
    raw = device.GetId()
    try:
        return wstring_at(raw)
    finally:
        windll.ole32.CoTaskMemFree(raw)


def describe_endpoint(device) -> "tuple[str, int | None]":
    """Friendly name and EndpointFormFactor, best-effort -- labels only."""
    name, form_factor = "unknown output", None
    try:
        store = device.OpenPropertyStore(_STGM_READ).QueryInterface(IPropertyStore)
        value = PROPVARIANT()
        store.GetValue(byref(PKEY_Device_FriendlyName), byref(value))
        if value.vt == _VT_LPWSTR and value.value:
            name = wstring_at(value.value)
        windll.ole32.PropVariantClear(byref(value))
        value = PROPVARIANT()
        store.GetValue(byref(PKEY_AudioEndpoint_FormFactor), byref(value))
        if value.vt == _VT_UI4:
            form_factor = int(value.value & 0xFFFFFFFF)
        windll.ole32.PropVariantClear(byref(value))
    except Exception:
        pass
    return name, form_factor


def default_render_endpoint() -> "tuple[str, str, int | None]":
    """
    (id, friendly name, form factor) of the current default output.
    Standalone, for tools/_spike_aec.py -- the reader thread keeps its
    own enumerator. Initializes COM on the calling thread.
    """
    comtypes.CoInitialize()
    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=CLSCTX_ALL
    )
    device = enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE)
    name, form_factor = describe_endpoint(device)
    return _device_id(device), name, form_factor


class _Session:
    """One open loopback stream, bound to one endpoint for its life."""

    def __init__(self, enumerator):
        device = enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE)
        self.endpoint_id = _device_id(device)
        self.name, self.form_factor = describe_endpoint(device)
        client = device.Activate(IID_IAudioClient, CLSCTX_ALL, None).QueryInterface(IAudioClient)
        fmt = WAVEFORMATEX(
            _WAVE_FORMAT_IEEE_FLOAT, 1, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 32, 0
        )
        client.Initialize(
            _AUDCLNT_SHAREMODE_SHARED,
            _AUDCLNT_STREAMFLAGS_LOOPBACK
            | _AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | _AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            _BUFFER_DURATION_HNS,
            0,
            addressof(fmt),
            None,
        )
        self.capture = client.GetService(IID_IAudioCaptureClient).QueryInterface(IAudioCaptureClient)
        client.Start()
        self.client = client

    def read(self) -> "list[np.ndarray]":
        chunks = []
        pending = self.capture.GetNextPacketSize()
        while pending:
            data, frames, flags, _device_pos, _qpc_pos = self.capture.GetBuffer()
            if frames:
                if flags & _AUDCLNT_BUFFERFLAGS_SILENT or not data:
                    chunks.append(np.zeros(frames, dtype=np.float32))
                else:
                    chunks.append(np.frombuffer(string_at(data, frames * 4), dtype=np.float32).copy())
            self.capture.ReleaseBuffer(frames)
            pending = self.capture.GetNextPacketSize()
        return chunks

    def close(self) -> None:
        try:
            self.client.Stop()
        except Exception:
            pass


# --- module state, published by the reader thread ----------------------

_lock = threading.Lock()
_frames: "deque[np.ndarray]" = deque(maxlen=MAX_PENDING_FRAMES)
_generation = 0
_session_open = False
_last_poll = 0.0
_endpoint_name = ""
_form_factor: "int | None" = None
_last_error: "str | None" = None
_thread: "threading.Thread | None" = None
_stop = threading.Event()


def pop_frame() -> "np.ndarray | None":
    """Oldest pending 10ms reference frame, or None if there isn't one."""
    try:
        return _frames.popleft()
    except IndexError:
        return None


def pending() -> int:
    return len(_frames)


def drop_oldest(count: int) -> None:
    for _ in range(count):
        if pop_frame() is None:
            return


def generation() -> int:
    """Bumped every time the stream is (re)opened -- a new echo path."""
    with _lock:
        return _generation


def is_healthy() -> bool:
    with _lock:
        return _session_open and time.monotonic() - _last_poll < HEALTHY_POLL_SECONDS


def endpoint_name() -> str:
    with _lock:
        return _endpoint_name


def endpoint_form_factor() -> "int | None":
    with _lock:
        return _form_factor


def last_error() -> "str | None":
    with _lock:
        return _last_error


def _publish_session(session: "_Session") -> None:
    global _generation, _session_open, _endpoint_name, _form_factor, _last_poll
    _frames.clear()
    with _lock:
        _generation += 1
        _session_open = True
        _endpoint_name = session.name
        _form_factor = session.form_factor
        _last_poll = time.monotonic()


def _publish_failure(error: str) -> None:
    global _session_open, _last_error
    with _lock:
        _session_open = False
        _last_error = error


def _reader_loop() -> None:
    global _last_poll, _session_open
    # This thread's own apartment, for its whole life -- same shape as
    # output_monitor._poll_loop: the only thread that ever touches COM
    # here, so nothing crosses apartments.
    comtypes.CoInitialize()
    enumerator = None
    session: "_Session | None" = None
    framer = Framer()
    gaps = GapFiller()
    next_endpoint_check = 0.0

    while not _stop.is_set():
        try:
            if enumerator is None:
                enumerator = comtypes.CoCreateInstance(
                    CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=CLSCTX_ALL
                )
            if session is None:
                session = _Session(enumerator)
                framer.reset()
                gaps = GapFiller()
                _publish_session(session)

            now = time.monotonic()
            chunks = session.read()
            if chunks:
                gaps.on_data(now)
                for chunk in chunks:
                    _frames.extend(framer.push(chunk))
            else:
                silence = gaps.silence_due(now)
                if silence:
                    _frames.extend(framer.push(np.zeros(silence, dtype=np.float32)))
            with _lock:
                _last_poll = now

            if now >= next_endpoint_check:
                next_endpoint_check = now + ENDPOINT_CHECK_SECONDS
                current = _device_id(enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE))
                if current != session.endpoint_id:
                    session.close()
                    session = None
                    continue
        except Exception:
            # Device invalidated (unplugged, switched, a sleep/resume
            # cycle), no output device at all, a driver hiccup -- all
            # the same answer: report unhealthy, back off, rebuild from
            # scratch. Never let this thread die; a dead reader would
            # leave echo_canceller.py waiting on a reference that never
            # comes, which it already treats as "not covering", but
            # silently and forever.
            _publish_failure(traceback.format_exc(limit=3))
            if session is not None:
                session.close()
            session = None
            enumerator = None
            _stop.wait(REOPEN_BACKOFF_SECONDS)
            continue
        _stop.wait(POLL_SECONDS)

    if session is not None:
        session.close()
    with _lock:
        _session_open = False


def start() -> None:
    """Idempotent, like output_monitor.start()."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_reader_loop, name="loopback", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
