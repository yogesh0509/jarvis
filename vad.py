"""
Streaming speech/silence detection via the silero_vad.onnx model that
openWakeWord's own downloader fetches (GitHub-hosted -- unlike
huggingface.co, no TLS trouble there, see README). Used to detect when
a spoken command has finished after the wake word triggers, since
there's no hotkey release to mark the end anymore.
"""

import os

import numpy as np
import onnxruntime as ort
import openwakeword

_MODEL_PATH = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", "silero_vad.onnx")


class SileroVAD:
    def __init__(self, sample_rate: int = 16000):
        self.session = ort.InferenceSession(_MODEL_PATH, providers=["CPUExecutionProvider"])
        self.sample_rate = sample_rate
        self.reset()

    def reset(self):
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def speech_prob(self, chunk: np.ndarray) -> float:
        x = chunk.reshape(1, -1).astype(np.float32)
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
