"""
Streaming speech/silence detection via the silero_vad.onnx model that
openWakeWord's own downloader fetches (GitHub-hosted -- unlike
huggingface.co, no TLS trouble there, see README). Used to detect when
a spoken command has finished after the wake word triggers, since
there's no hotkey release to mark the end anymore.

Silero (v4, per the shipped silero_vad.onnx -- confirmed via its
input/output shapes) is trained on 512-sample (32ms @ 16kHz) windows.
Feeding it main.py's native 1280-sample (80ms) openWakeWord chunks
directly, as this used to do, skews its output: verified directly,
white noise at a fixed amplitude got flagged as speech in ~8% of
1280-sample calls at threshold 0.15, versus ~2% at 512 or 1536 --
not a monotonic size effect, just 1280 landing badly off Silero's
trained grid. That mismatch is the likely single root cause of the
VAD_SPEECH_THRESHOLD tuning history in main.py (0.5 down to 0.15) and
the fan-noise false positives ("bdk"/"boom") noted there.

speech_prob() rebuffers incoming audio internally into native
512-sample windows regardless of the caller's chunk size, carrying any
partial window over to the next call, and returns the mean probability
across whichever windows completed during that call -- smooths a
transient noise spike inside one window rather than letting it alone
decide the call's verdict, without changing the one-probability-per-call
interface every caller already relies on.
"""

import numpy as np
import onnxruntime as ort
import openwakeword
import os

_MODEL_PATH = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", "silero_vad.onnx")
_NATIVE_WINDOW = 512  # Silero v4's trained window size at 16kHz -- see module docstring


class SileroVAD:
    def __init__(self, sample_rate: int = 16000):
        self.session = ort.InferenceSession(_MODEL_PATH, providers=["CPUExecutionProvider"])
        self.sample_rate = sample_rate
        self.reset()

    def reset(self):
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buffer = np.zeros(0, dtype=np.float32)

    def _infer_window(self, window: np.ndarray) -> float:
        x = window.reshape(1, -1).astype(np.float32)
        out, self._h, self._c = self.session.run(
            None,
            {
                "input": x,
                "sr": np.array(self.sample_rate, dtype=np.int64),
                "h": self._h,
                "c": self._c,
            },
        )
        return float(out[0][0])

    def speech_prob(self, chunk: np.ndarray) -> float:
        self._buffer = np.concatenate([self._buffer, chunk.astype(np.float32)])
        probs = []
        while len(self._buffer) >= _NATIVE_WINDOW:
            window, self._buffer = self._buffer[:_NATIVE_WINDOW], self._buffer[_NATIVE_WINDOW:]
            probs.append(self._infer_window(window))
        # Every caller in this project passes chunks well above
        # _NATIVE_WINDOW (main.py's 1280 samples, always >= 2 full
        # windows), so this is never actually reached in practice --
        # kept only so a future smaller-chunk caller degrades to "no new
        # evidence yet" instead of crashing on an empty mean.
        if not probs:
            return 0.0
        return float(np.mean(probs))
