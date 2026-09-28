"""
Detects when the system's default audio output is actually producing
sound -- Spotify, a video, a notification chime, anything rendering
through the speakers, not just Jarvis's own voice.

tts.py's is_speaking() already solves this for Jarvis's own TTS
specifically (a counted flag, incremented/decremented around each
speak() call). It cannot cover anything else: Spotify's own audio has no
hook into this process at all. Confirmed live as a real problem, not a
theoretical one -- a session opened (the wake word almost certainly
false-triggering on loud music; see main.py's `wake_triggered` log line,
added after this exact incident) with no "hey jarvis" anywhere in the
transcript, and several chunks of the song that followed embedded close
enough to real seed phrases (volume_down/up) to actually dispatch,
peak=1.0 on nearly every one of them.

The only way to know "is *anything* coming out of the speakers right
now" without a hook into every app that might be playing something is to
ask Windows what the default render endpoint is actually doing --
IAudioMeterInformation, the same Core Audio API already used elsewhere
in this codebase (virtual_desktop.py, monitors.py) for exactly this kind
of "ask Windows directly" question. It reports the endpoint's peak
level after the system volume is applied, which is what actually matters
here: audio rendered at 0% volume can't leak into a mic regardless of
what any app thinks it's playing.

This is *not* acoustic echo cancellation. It doesn't try to subtract the
speaker signal from the mic signal, isolate a real voice speaking over
music, or handle the case of someone deliberately talking over the
music through push-to-talk (which this deliberately does not touch --
see main.py's listener_loop). It only answers one binary question:
"is the render endpoint at or above its idle floor". While that's true,
main.py stops feeding chunks into wake-word detection and session
dispatch entirely, the same way it already stops for is_speaking(). The
trade-off this accepts: "hey jarvis" (hands-free) will not fire while
system audio is playing. Push-to-talk still does, always -- so voice
control of the very thing that's playing (`pause the music`) is one
Right Ctrl press away, never blocked.

Worth knowing when reading a trace: this endpoint-level meter sees
Jarvis's *own* voice too (boot announcement, every spoken reply) exactly
as it would see Spotify -- it has no way to tell them apart, since both
are just audio hitting the same render endpoint. That overlap with
is_speaking() is harmless (both gates would independently hold at once,
main.py's combined skip condition doesn't care how many of its
conditions are true) and expected, not a bug -- confirmed directly while
verifying this module: a short-lived engage/disengage cycle that looked
at first like a mysterious ~2.5s dropout in a test tone turned out to be
the boot announcement's own speech, correctly detected, coincidentally
overlapping with when the real test tone was about to start.
"""

import threading
from ctypes import POINTER, c_float, c_uint32, c_void_p

import comtypes
from comtypes import COMMETHOD, CLSCTX_ALL, GUID, HRESULT, IUnknown

import hud

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IAudioMeterInformation = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_E_RENDER = 0
_E_CONSOLE = 0


class IAudioMeterInformation(IUnknown):
    _iid_ = IID_IAudioMeterInformation
    _methods_ = [
        COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "peak")),
    ]


class IMMDevice(IUnknown):
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


# Provisional, like VAD_SPEECH_THRESHOLD/WAKE_THRESHOLD elsewhere in this
# codebase -- a reasonable starting point, not a measured value. Peak is
# reported post-volume, so silent or near-silent output (system muted, or
# turned down far enough it couldn't leak into a mic anyway) never trips
# this regardless of what any app thinks it's doing.
PEAK_THRESHOLD = 0.01
POLL_SECONDS = 0.05
# Debounced independently in each direction, same reasoning as
# SPEECH_ONSET_FRAMES/SESSION_IDLE_TIMEOUT_FRAMES in main.py: a single
# stray poll shouldn't flip either way. Asymmetric on purpose --
# engaging fast matters (catch loud audio before it can false-trigger a
# wake word), releasing slow matters more (bridge an ordinary quiet beat
# or the gap between songs without flapping the gate open and shut every
# few seconds).
ON_SECONDS = 0.15
OFF_SECONDS = 1.0

class EdgeDebouncer:
    """
    Turns a noisy per-poll peak reading into a debounced boolean.

    Module-level, not nested in _poll_loop, precisely so this is
    testable without COM or a background thread at all -- same reasoning
    as main.py's _status_line/_should_reject_transcript: this is the one
    piece of this module with actual decision logic in it, everything
    else is COM plumbing around it.

    Requires `on_polls` consecutive above-threshold readings before
    flipping to "playing", and `off_polls` consecutive at-or-below
    readings before flipping back -- see ON_SECONDS/OFF_SECONDS for why
    those two are deliberately different sizes.
    """

    def __init__(self, on_polls: int, off_polls: int):
        self.on_polls = max(1, on_polls)
        self.off_polls = max(1, off_polls)
        self.above_run = 0
        self.below_run = 0
        self.playing = False

    def update(self, peak: float, threshold: float) -> bool:
        """Feed one poll's peak reading; returns the (possibly unchanged) playing state."""
        if peak > threshold:
            self.above_run += 1
            self.below_run = 0
            if self.above_run >= self.on_polls:
                self.playing = True
        else:
            self.below_run += 1
            self.above_run = 0
            if self.below_run >= self.off_polls:
                self.playing = False
        return self.playing


_lock = threading.Lock()
_playing = False
_thread: "threading.Thread | None" = None
_stop = threading.Event()


def is_output_playing() -> bool:
    with _lock:
        return _playing


def _set_playing(value: bool) -> None:
    global _playing
    with _lock:
        if _playing != value:
            _playing = value
            hud.note(f"system output {'started' if value else 'stopped'} playing")


def _make_meter():
    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=CLSCTX_ALL
    )
    device = enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE)
    raw = device.Activate(IID_IAudioMeterInformation, CLSCTX_ALL, None)
    return raw.QueryInterface(IAudioMeterInformation)


def _poll_loop() -> None:
    # This thread's own COM apartment -- never shared with the caller
    # thread (is_output_playing() only ever touches the plain bool
    # above), so no thread-local dance like virtual_desktop.py's
    # _get_manager needs: there is exactly one thread that ever calls
    # into COM here, for this module's entire life.
    comtypes.CoInitialize()
    meter = None
    gate = EdgeDebouncer(
        on_polls=round(ON_SECONDS / POLL_SECONDS),
        off_polls=round(OFF_SECONDS / POLL_SECONDS),
    )

    while not _stop.is_set():
        try:
            if meter is None:
                meter = _make_meter()
            peak = meter.GetPeakValue()
            _set_playing(gate.update(peak, PEAK_THRESHOLD))
        except Exception:
            # The default render device can legitimately go away
            # mid-poll (unplugged headphones, a driver hiccup, a device
            # switch) -- rebuilding next iteration is cheap and this
            # must never take the polling thread down, the same
            # reasoning as every audio callback elsewhere in this
            # codebase. Failing to "not playing" here, not "playing":
            # a broken meter should make Jarvis merely as deaf-to-
            # output-state as before this module existed, never
            # actually deaf to the user by refusing to process real
            # speech.
            meter = None
            gate.above_run = 0
            gate.playing = False
            _set_playing(False)
        _stop.wait(POLL_SECONDS)


def start() -> None:
    """Idempotent -- safe to call more than once (main.py calls it once at boot)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, name="output-monitor", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
