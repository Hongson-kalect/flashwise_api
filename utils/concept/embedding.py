import logging
import time

import numpy as np
from django.conf import settings
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def embed(texts):
    """Trả ma trận float32 đã chuẩn hoá (len(texts), dim). Thử lại vài lần khi lỗi mạng / quota."""
    texts = list(texts)
    config = types.EmbedContentConfig(output_dimensionality=settings.CONCEPT_EMBED_DIM) if settings.CONCEPT_EMBED_DIM else None
    rows = []
    for i in range(0, len(texts), 90):
        batch = texts[i:i + 90]
        for attempt in range(4):
            try:
                resp = _get_client().models.embed_content(model=settings.CONCEPT_EMBED_MODEL, contents=batch, config=config)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                logger.warning('Embedding lỗi (lần %s): %s', attempt + 1, e)
                time.sleep(1.5 * (attempt + 1))
        rows += [e.values for e in resp.embeddings]
    mat = np.asarray(rows, dtype=np.float32)
    return mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)


def to_bytes(vec):
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(blob):
    return np.frombuffer(bytes(blob), dtype=np.float32)
