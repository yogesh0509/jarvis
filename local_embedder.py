"""
Local ONNX embedding inference for BAAI/bge-small-en-v1.5, bypassing
fastembed's automatic Hugging Face download -- Python's HTTPS client
gets blocked/reset on this machine while PowerShell/.NET's doesn't
(see README's TLS notes for the investigation -- tls_compat.py, an
early monkeypatch shim for this, was removed once every model moved to
this manual-fetch pattern). Model files were fetched once via
PowerShell into models/bge-small-en-v1.5-onnx-q/ and are loaded from
disk here; no network involved at import or inference time.

BGE models use CLS-token pooling (not mean pooling) + L2 normalization
-- this is the model card's documented usage, not a guess.
"""

from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_DIR = Path(__file__).parent / "models" / "bge-small-en-v1.5-onnx-q"


class LocalEmbedder:
    def __init__(self, model_dir: Path = MODEL_DIR):
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_padding()
        self.tokenizer.enable_truncation(max_length=512)
        self.session = ort.InferenceSession(
            str(model_dir / "model_optimized.onnx"),
            providers=["CPUExecutionProvider"],
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        encodings = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self.session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        cls_embeddings = outputs[0][:, 0, :]
        norms = np.linalg.norm(cls_embeddings, axis=1, keepdims=True)
        return cls_embeddings / norms
