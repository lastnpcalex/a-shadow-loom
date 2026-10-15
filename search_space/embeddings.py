"""On-device semantic encoding. Queries never trigger a model download."""
import threading
from exo_state import state_path

MODEL = "BAAI/bge-small-en-v1.5"


class EmbeddingsUnavailable(RuntimeError):
    pass


class LocalEmbeddings:
    model = MODEL

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or state_path("search-space", "models")
        self._model = None
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            try:
                if self._model is None:
                    from fastembed import TextEmbedding
                    self._model = TextEmbedding(MODEL, cache_dir=str(self.cache_dir),
                                                local_files_only=True, threads=2)
                return [vector.tolist() for vector in self._model.embed(texts, batch_size=16)]
            except Exception as exc:
                raise EmbeddingsUnavailable(
                    "Local semantic model unavailable. Run tools/setup_search_embeddings.py once; "
                    "keyword search remains available."
                ) from exc


def chunks(text: str) -> list[str]:
    # Keep inference bounded for exceptionally large tool dumps. Full-text
    # indexing still covers the complete canonical message.
    return [text[start:start + 1800] for start in range(0, min(len(text), 57600), 1600)] or [""]
