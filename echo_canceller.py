"""
Acoustic echo cancellation for the always-on listener: subtracts
whatever the speakers are playing (loopback.py's reference) from the
mic, so "hey jarvis" can be heard *over* music or a movie instead of
the listener going deaf for the length of it.

Before this, main.py's output gate (output_monitor.py) simply threw
away every mic frame while anything played -- correct about the false
trigger it was added for (Spotify lyrics opening a session and getting
dispatched as volume commands), but it cost ~29.5h of deafness across a
660h log. DECISIONS.md had already named the real fix: "subtracting a
loopback reference from the mic signal". This is that, using WebRTC's
AEC3 through the `livekit` package's AudioProcessingModule -- an 11MB
prebuilt wheel, pure local DSP, no network. Imported lazily, so the
test suite never loads it.

**The gate is not removed; it's made conditional, and it fails closed.**
coverage() answers whether cancellation is *demonstrably* working on
the current output right now -- reference flowing, canceller healthy,
converged, and the measured residual echo low. Anything short of that
reads as not covering, and main.py gates exactly as it did before this
module existed. Nothing here trusts a device list: headphones pass
because almost no echo reaches the mic, laptop speakers pass if AEC3
actually cancels them, and a Bluetooth speaker whose latency AEC3
can't track fails on its own measured residual -- same rule for all.

Choices worth knowing when reading the numbers:

- **Transparent while nothing plays.** Once the reference has been
  silent for IDLE_PASSTHROUGH_SECONDS, process() hands back the raw mic
  (still feeding the canceller, so its state stays continuous). Wake
  word, VAD and Whisper were all tuned on the raw signal; with nothing
  playing, they still get exactly that.
- **Only echo cancellation and the high-pass filter are on.** No noise
  suppression or gain control -- the Intel SST DSP already does its own
  ahead of this process, and stacking a second set would change the
  signal the rest of the pipeline was tuned on.
- **Push-to-talk never goes through this** (see main.py): it stays the
  independent, always-works override.

Every threshold below is provisional in the same sense WAKE_THRESHOLD
is: a reasoned starting point, to be revisited from tools/_spike_aec.py
runs and the `aec_stats` events this writes to session.jsonl.
"""

import atexit
import threading
import time
import traceback
from collections import deque

import numpy as np

import loopback

SAMPLE_RATE = loopback.SAMPLE_RATE
APM_FRAME_SAMPLES = loopback.FRAME_SAMPLES

SILENCE_DBFS = -120.0
# Reference below this counts as "nothing playing". Loopback taps the
# mix before the endpoint volume, so this is about whether anything is
# rendering at all, not how loud the room is.
REFERENCE_ACTIVE_DBFS = -60.0
IDLE_PASSTHROUGH_SECONDS = 2.0
# AEC3 needs a little audio to find the echo path's delay and adapt;
# until this much active reference has gone through since the output
# device last changed, coverage stays "converging" (gated).
AEC_CONVERGE_SECONDS = 1.5
# The core coverage test: a low percentile of the post-cancellation
# level, over the last RESIDUAL_WINDOW_SECONDS of blocks where the
# reference was active. A low percentile rather than the median so the
# user talking over the music -- loud blocks, by design not cancelled --
# can't push a working canceller into "residual_high" mid-command.
AEC_RESIDUAL_MAX_DBFS = -50.0
AEC_RESIDUAL_HYSTERESIS_DB = 6.0
RESIDUAL_WINDOW_SECONDS = 4.0
RESIDUAL_MIN_BLOCKS = 10
COVERAGE_PERCENTILE = 30
# Session speech onset (main.py) also has to clear the echo floor by
# this margin while output plays: residual lyrics are quiet relative to
# the floor they set, the user's near-field voice is not. The floor is
# the median, deliberately more conservative than the coverage
# percentile above.
FLOOR_PERCENTILE = 50
ECHO_ONSET_MARGIN_DB = 12.0
# Alignment. The reference and the mic are paired frame-for-frame, and
# every echo canceller needs each reference frame to reach it *before*
# its echo shows up in the mic -- an echo that arrives first can't be
# predicted, only suffered. Every 10ms of reference left queued when a
# frame is popped makes the reference 10ms later relative to the mic.
#
# Learned the hard way, on the first tools/_spike_aec.py runs on this
# machine's laptop speakers: a queue allowed to sit 16 frames deep made
# the reference arrive ~125ms *after* its own echo, and AEC3 managed 6dB
# -- a session would still have opened on the playback. So the queue is
# trimmed back to the hold-back whenever it runs more than
# REFERENCE_SLACK_FRAMES past it.
#
# The hold-back itself starts at zero: the echo path measured on the
# laptop speakers is short (~25ms from loopback tap to captured mic
# frame), well inside AEC3's own delay search (about half a second: 5
# overlapping 128ms matched filters). DelayAligner re-measures while
# audio plays (GCC-PHAT on exactly what AEC3 was fed) and holds the
# reference back only if the echo comes in later than
# TARGET_RESIDUAL_DELAY_MS -- e.g. a Bluetooth speaker adding a couple
# of hundred ms after the loopback tap -- so AEC3 always has a short,
# positive delay to find.
INITIAL_HOLD_BACK_MS = 0.0
TARGET_RESIDUAL_DELAY_MS = 80.0
MAX_HOLD_BACK_MS = 1000.0
DELAY_WINDOW_SECONDS = 2.0
# GCC-PHAT peak-to-mean ratio below which the window had no clear echo
# in it (headphones, the user talking, a near-silent passage) and the
# current hold-back stands. The spike's laptop-speaker runs scored ~170-330.
DELAY_MIN_STRENGTH = 8.0
DELAY_ADJUST_MIN_MS = 30.0
REFERENCE_SLACK_FRAMES = 6
# AEC3's own initial alignment hint -- what's left after the hold-back.
STREAM_DELAY_HINT_MS = int(TARGET_RESIDUAL_DELAY_MS)


def dbfs(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return SILENCE_DBFS
    power = float(np.mean(np.square(samples, dtype=np.float64)))
    return _power_dbfs(power)


def _power_dbfs(power: float) -> float:
    if power <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, float(10.0 * np.log10(power)))


def _rounded(value: "float | None") -> "float | None":
    return None if value is None else round(value, 1)


def _to_int16(samples: np.ndarray) -> np.ndarray:
    # Clipped first, same reason as main.py's wake-path conversion: the
    # SST APO's gain can overshoot unity, and astype() wraps rather than
    # saturating.
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)


def clears_echo_floor(frame: np.ndarray, floor_dbfs: "float | None", margin_db: float = ECHO_ONSET_MARGIN_DB) -> bool:
    """True if `frame` is loud enough over the echo floor to be the user. No floor -> True."""
    if floor_dbfs is None:
        return True
    return dbfs(frame) >= floor_dbfs + margin_db


def gcc_phat_lag_ms(mic: np.ndarray, reference: np.ndarray, max_lag_ms: float = MAX_HOLD_BACK_MS) -> "tuple[float, float]":
    """
    How far `mic` lags `reference`, in ms, by phase-transform cross-
    correlation -- whitened, so a bass-heavy song doesn't smear the peak
    the way plain correlation would. Returns (lag_ms, strength), strength
    being the peak over the mean correlation: large for a clear echo,
    near 1 for none. Positive lag = the echo arrives after the reference.
    """
    n = min(len(mic), len(reference))
    if n == 0:
        return 0.0, 0.0
    mic, reference = mic[-n:], reference[-n:]
    size = 2 * n
    cross = np.fft.rfft(mic, size) * np.conj(np.fft.rfft(reference, size))
    correlation = np.fft.irfft(cross / (np.abs(cross) + 1e-12), size)
    max_lag = min(n - 1, int(max_lag_ms / 1000 * SAMPLE_RATE))
    window = np.abs(np.concatenate((correlation[-max_lag:], correlation[: max_lag + 1])))
    peak = int(np.argmax(window))
    strength = float(window[peak] / (np.mean(window) + 1e-12))
    return (peak - max_lag) / SAMPLE_RATE * 1000.0, strength


class DelayAligner:
    """
    How many 10ms reference frames to hold back before AEC3 sees them,
    re-measured from the audio itself -- see INITIAL_HOLD_BACK_MS for
    why that's needed at all. Pure (numpy only), so it's testable.

    Keeps the last DELAY_WINDOW_SECONDS of mic and of the reference *as
    it was actually fed* (same frame index for both). GCC-PHAT on that
    pair measures the delay AEC3 is currently being asked to find, and
    the hold-back moves by however far that is from
    TARGET_RESIDUAL_DELAY_MS. Relative on purpose: the exact age of a
    popped frame depends on where the reader happened to be in its own
    10ms cycle, which nothing here can see -- but a correction by the
    measured error is right regardless of that constant. Only a clear
    peak moves anything, and only by more than DELAY_ADJUST_MIN_MS:
    every move makes AEC3 re-adapt, so it mustn't chase jitter.
    """

    def __init__(
        self,
        initial_hold_back_ms: float = INITIAL_HOLD_BACK_MS,
        target_residual_ms: float = TARGET_RESIDUAL_DELAY_MS,
        window_seconds: float = DELAY_WINDOW_SECONDS,
        min_strength: float = DELAY_MIN_STRENGTH,
        adjust_min_ms: float = DELAY_ADJUST_MIN_MS,
        max_hold_back_ms: float = MAX_HOLD_BACK_MS,
    ):
        self.initial_hold_back_ms = initial_hold_back_ms
        self.target_residual_ms = target_residual_ms
        self.window_frames = int(window_seconds * SAMPLE_RATE / APM_FRAME_SAMPLES)
        self.min_strength = min_strength
        self.adjust_min_ms = adjust_min_ms
        self.max_hold_back_ms = max_hold_back_ms
        self.reset()

    def reset(self) -> None:
        self.hold_back_ms = self.initial_hold_back_ms
        self.last_residual_ms: "float | None" = None
        self.last_strength = 0.0
        self._clear_window()

    def _clear_window(self) -> None:
        self._mic: "deque[np.ndarray]" = deque(maxlen=self.window_frames)
        self._reference: "deque[np.ndarray]" = deque(maxlen=self.window_frames)
        self._active_frames = 0

    @property
    def backlog_frames(self) -> int:
        return max(0, round(self.hold_back_ms * SAMPLE_RATE / 1000 / APM_FRAME_SAMPLES))

    def record(self, mic_frame: np.ndarray, reference_frame: "np.ndarray | None", reference_active: bool) -> None:
        self._mic.append(mic_frame)
        self._reference.append(
            reference_frame if reference_frame is not None else np.zeros(APM_FRAME_SAMPLES, dtype=np.float32)
        )
        if reference_active:
            self._active_frames += 1

    def maybe_update(self) -> bool:
        """Re-measures once a full window of mostly-active reference has built up. True if the hold-back moved."""
        if len(self._mic) < self.window_frames or self._active_frames < self.window_frames // 2:
            return False
        residual_ms, strength = gcc_phat_lag_ms(np.concatenate(self._mic), np.concatenate(self._reference))
        self._clear_window()
        self.last_strength = strength
        if strength < self.min_strength:
            return False
        self.last_residual_ms = residual_ms
        hold_back = min(self.max_hold_back_ms, max(0.0, self.hold_back_ms + residual_ms - self.target_residual_ms))
        if abs(hold_back - self.hold_back_ms) < self.adjust_min_ms:
            return False
        self.hold_back_ms = hold_back
        return True

class CoverageTracker:
    """
    The covering/not-covering decision, as a pure state machine over
    per-block measurements -- no audio, no COM, no livekit -- so every
    transition is testable (same reasoning as output_monitor.EdgeDebouncer).
    """

    def __init__(
        self,
        converge_seconds: float = AEC_CONVERGE_SECONDS,
        residual_max_dbfs: float = AEC_RESIDUAL_MAX_DBFS,
        hysteresis_db: float = AEC_RESIDUAL_HYSTERESIS_DB,
        window_seconds: float = RESIDUAL_WINDOW_SECONDS,
        min_blocks: int = RESIDUAL_MIN_BLOCKS,
        idle_seconds: float = IDLE_PASSTHROUGH_SECONDS,
    ):
        self.converge_seconds = converge_seconds
        self.residual_max_dbfs = residual_max_dbfs
        self.hysteresis_db = hysteresis_db
        self.window_seconds = window_seconds
        self.min_blocks = min_blocks
        self.idle_seconds = idle_seconds
        self.reset()

    def reset(self) -> None:
        self._active_since: "float | None" = None
        self._last_active: "float | None" = None
        self._residuals: "deque[tuple[float, float]]" = deque()
        self._covering = False

    def restart_convergence(self, now: float) -> None:
        """The canceller's alignment just moved -- what it measured before says nothing about after."""
        self._active_since = now
        self._residuals.clear()
        self._covering = False

    def update(self, now: float, reference_active: bool, post_dbfs: float) -> None:
        if reference_active:
            if self._active_since is None:
                self._active_since = now
            self._last_active = now
            self._residuals.append((now, post_dbfs))
        while self._residuals and now - self._residuals[0][0] > self.window_seconds:
            self._residuals.popleft()

        residual = self.residual_dbfs(COVERAGE_PERCENTILE)
        if residual is None or now - self._active_since < self.converge_seconds:
            self._covering = False
        else:
            limit = self.residual_max_dbfs + (self.hysteresis_db if self._covering else 0.0)
            self._covering = residual <= limit

    def residual_dbfs(self, percentile: float) -> "float | None":
        if len(self._residuals) < self.min_blocks:
            return None
        return float(np.percentile([level for _, level in self._residuals], percentile))

    def reference_recently_active(self, now: float) -> bool:
        return self._last_active is not None and now - self._last_active <= self.idle_seconds

    def state(self, now: float) -> str:
        if not self.reference_recently_active(now):
            return "idle"
        if self._covering:
            return "covering"
        if (
            self._active_since is None
            or now - self._active_since < self.converge_seconds
            or self.residual_dbfs(COVERAGE_PERCENTILE) is None
        ):
            return "converging"
        return "residual_high"


class _LivekitApm:
    """The only code that touches livekit -- tests inject a fake with the same three methods."""

    def __init__(self):
        from livekit import rtc

        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)

    def _frame(self, samples: np.ndarray):
        return self._rtc.AudioFrame(bytearray(samples.tobytes()), SAMPLE_RATE, 1, len(samples))

    def reverse(self, samples: np.ndarray) -> None:
        self._apm.process_reverse_stream(self._frame(samples))

    def capture(self, samples: np.ndarray) -> np.ndarray:
        frame = self._frame(samples)
        # Must precede every process_stream() while echo cancellation is
        # on -- WebRTC clears it after each call.
        self._apm.set_stream_delay_ms(STREAM_DELAY_HINT_MS)
        self._apm.process_stream(frame)
        return np.frombuffer(frame.data, dtype=np.int16)

    def close(self) -> None:
        # Released explicitly: left to its finalizer it runs after
        # livekit's own atexit hook has torn the native runtime down, and
        # prints an AssertionError traceback on every exit.
        handle = getattr(self._apm, "_ffi_handle", None)
        if handle is not None:
            handle.dispose()


class Canceller:
    """
    One mic stream's canceller. process() runs inside the PortAudio
    callback, so it never raises -- any failure returns the raw block
    and marks this unhealthy, which coverage() reports as not covering.
    """

    def __init__(self, apm_factory, reference, clock=time.monotonic, aligner: "DelayAligner | None" = None):
        self._apm_factory = apm_factory
        self._reference = reference
        self._clock = clock
        self._lock = threading.Lock()
        self._apm = None
        self._enabled = True
        self._healthy = False
        self._generation: "int | None" = None
        self._tracker = CoverageTracker()
        self._aligner = aligner if aligner is not None else DelayAligner()
        self._levels: "tuple[float, float, float] | None" = None
        self._error: "str | None" = None

    def open(self) -> None:
        """Creates the canceller; raises if it can't (livekit missing, native load failure)."""
        self._apm = self._apm_factory()
        self._healthy = True

    def close(self) -> None:
        apm, self._apm = self._apm, None
        if apm is not None:
            try:
                apm.close()
            except Exception:
                pass

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._tracker.reset()

    def is_enabled(self) -> bool:
        return self._enabled

    def process(self, block: np.ndarray) -> np.ndarray:
        apm = self._apm
        if apm is None or not self._enabled:
            return block
        try:
            generation = self._reference.generation()
            if generation != self._generation:
                if self._generation is not None:
                    # A different output device is a different echo
                    # path; a fresh canceller converges on it faster
                    # than one still holding the old one.
                    apm.close()
                    apm = self._apm = self._apm_factory()
                self._generation = generation
                # Delay too: a Bluetooth speaker's latency is not the
                # laptop speakers'. Back to the prior, re-measured from
                # the new device's own audio.
                self._aligner.reset()
                with self._lock:
                    self._tracker.reset()

            n = len(block)
            if n % APM_FRAME_SAMPLES:
                raise ValueError(f"{n}-sample block isn't a whole number of 10ms frames")
            out = np.empty(n, dtype=np.float32)
            reference_power = 0.0
            reference_frames = 0
            backlog = self._aligner.backlog_frames
            for start in range(0, n, APM_FRAME_SAMPLES):
                mic_frame = block[start:start + APM_FRAME_SAMPLES]
                # Held back by `backlog` frames -- see DelayAligner. With
                # the reader gap-filling while idle, the queue is always
                # stocked, so this is a fixed delay, not a starvation.
                reference = self._reference.pop_frame() if self._reference.pending() > backlog else None
                active = False
                if reference is not None:
                    apm.reverse(_to_int16(reference))
                    power = float(np.mean(np.square(reference, dtype=np.float64)))
                    active = _power_dbfs(power) > REFERENCE_ACTIVE_DBFS
                    reference_power += power
                    reference_frames += 1
                self._aligner.record(mic_frame, reference, active)
                cleaned = apm.capture(_to_int16(mic_frame))
                out[start:start + APM_FRAME_SAMPLES] = cleaned.astype(np.float32) / 32768.0
            # Back down to the hold-back itself, not to hold-back + slack:
            # every frame left over makes the reference that much later
            # relative to its echo -- see the alignment notes up top.
            # The slack only stops this firing on ordinary reader jitter.
            if self._reference.pending() > backlog + REFERENCE_SLACK_FRAMES:
                self._reference.drop_oldest(self._reference.pending() - backlog)

            now = self._clock()
            realigned = self._aligner.maybe_update()
            reference_db = _power_dbfs(reference_power / reference_frames) if reference_frames else SILENCE_DBFS
            post_db = dbfs(out)
            with self._lock:
                self._healthy = True
                self._error = None
                if realigned:
                    self._tracker.restart_convergence(now)
                self._tracker.update(now, reference_db > REFERENCE_ACTIVE_DBFS, post_db)
                self._levels = (dbfs(block), post_db, reference_db)
                passthrough = not self._tracker.reference_recently_active(now)
            return block if passthrough else out
        except Exception:
            with self._lock:
                self._healthy = False
                self._error = traceback.format_exc(limit=3)
                self._tracker.reset()
            return block

    def coverage(self) -> "tuple[bool, str]":
        """(covering, reason) -- reason is one of the states `jarvis status` prints."""
        if self._apm is None:
            return False, "unavailable"
        if not self._enabled:
            return False, "off"
        with self._lock:
            if not self._healthy or not self._reference.is_healthy():
                return False, "unhealthy"
            state = self._tracker.state(self._clock())
        return state == "covering", state

    def echo_floor_dbfs(self) -> "float | None":
        with self._lock:
            if not self._tracker.reference_recently_active(self._clock()):
                return None
            return self._tracker.residual_dbfs(FLOOR_PERCENTILE)

    def stats(self) -> dict:
        _covering, reason = self.coverage()
        with self._lock:
            raw_db, post_db, reference_db = self._levels or (None, None, None)
            residual = self._tracker.residual_dbfs(COVERAGE_PERCENTILE)
            floor = self._tracker.residual_dbfs(FLOOR_PERCENTILE)
            error = self._error
        return {
            "state": reason,
            "raw_dbfs": _rounded(raw_db),
            "post_dbfs": _rounded(post_db),
            "reference_dbfs": _rounded(reference_db),
            "residual_dbfs": _rounded(residual),
            "floor_dbfs": _rounded(floor),
            "hold_back_ms": _rounded(self._aligner.hold_back_ms),
            "residual_delay_ms": _rounded(self._aligner.last_residual_ms),
            "delay_strength": _rounded(self._aligner.last_strength),
            "endpoint": self._reference.endpoint_name(),
            "error": error.strip().splitlines()[-1] if error else None,
        }


# --- the process-wide instance main.py uses ----------------------------

_canceller: "Canceller | None" = None


def start() -> "str | None":
    """
    Creates the canceller and starts the loopback reader. Returns None
    on success, or a one-line reason it couldn't -- never raises, so a
    missing wheel or a native load failure leaves Jarvis exactly as it
    was before this module existed (output gate unconditional).
    """
    global _canceller
    if _canceller is not None:
        return None
    canceller = Canceller(_LivekitApm, loopback)
    try:
        canceller.open()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    loopback.start()
    _canceller = canceller
    # Ahead of livekit's own atexit hook (registered when the module
    # above was created, so it runs after this one -- atexit is LIFO).
    atexit.register(canceller.close)
    return None


def stop() -> None:
    global _canceller
    loopback.stop()
    canceller, _canceller = _canceller, None
    if canceller is not None:
        canceller.close()


def process(block: np.ndarray) -> np.ndarray:
    canceller = _canceller
    return block if canceller is None else canceller.process(block)


def coverage() -> "tuple[bool, str]":
    canceller = _canceller
    return (False, "unavailable") if canceller is None else canceller.coverage()


def above_echo_floor(frame: np.ndarray) -> bool:
    canceller = _canceller
    return True if canceller is None else clears_echo_floor(frame, canceller.echo_floor_dbfs())


def set_enabled(enabled: bool) -> None:
    canceller = _canceller
    if canceller is not None:
        canceller.set_enabled(enabled)


def is_enabled() -> bool:
    canceller = _canceller
    return canceller is not None and canceller.is_enabled()


def stats() -> dict:
    canceller = _canceller
    return {"state": "unavailable"} if canceller is None else canceller.stats()
