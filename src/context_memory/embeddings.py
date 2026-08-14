from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    """Local or remote adapter behind a rebuildable retrieval projection."""

    @property
    def dimensions(self) -> int: ...

    @property
    def vector_only_threshold(self) -> float | None: ...

    @property
    def supplements_lexical_results(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingsDisabled:
    @property
    def dimensions(self) -> int: return 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embeddings are disabled; FTS5 remains fully functional")
class LocalHashEmbedding:
    """Dependency-free local similarity projection using word and character features.

    This is intentionally not presented as a neural semantic model. It improves fuzzy,
    morphological, and partial-phrase recall while keeping personal data on-device.
    """

    def __init__(self, dimensions: int = 1024):
        if dimensions < 32:
            raise ValueError("dimensions must be at least 32")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"local-hash-v2-{self.dimensions}"

    @property
    def vector_only_threshold(self) -> float:
        return 0.20

    @property
    def supplements_lexical_results(self) -> bool:
        return False

    def _features(self, text: str) -> list[str]:
        normalized = " ".join(text.casefold().split())
        words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
        compact = re.sub(r"\s+", "", normalized)
        grams = [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
        # Hangul syllable bigrams recover spacing and particle changes that lose
        # whole-word overlap. Restricting the shorter feature to Hangul avoids
        # the common ASCII bigrams that raised negative-query similarities.
        hangul_bigrams = [
            compact[i:i + 2] for i in range(max(0, len(compact) - 1))
            if re.fullmatch(r"[가-힣]{2}", compact[i:i + 2])
        ]
        return ([f"w:{word}" for word in words]
                + [f"c3:{gram}" for gram in grams]
                + [f"k2:{gram}" for gram in hangul_bigrams])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                index = value % self.dimensions
                vector[index] += 1.0 if value & 1 else -1.0
            norm = math.sqrt(sum(value * value for value in vector))
            result.append([value / norm for value in vector] if norm else vector)
        return result


class SentenceTransformerEmbedding:
    """Explicit opt-in adapter for a local sentence-transformers model.

    Importing Context Memory never imports or downloads a neural model. Constructing
    this adapter requires the optional dependency and either a local model path or an
    explicitly chosen model identifier.
    """

    def __init__(self, model: str, *, device: str | None = None):
        if not model.strip():
            raise ValueError("model must be a local path or sentence-transformers model identifier")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "neural embeddings require the optional 'neural' dependency; "
                "install context-memory[neural]"
            ) from exc
        self.model_id = model.strip()
        self._model = SentenceTransformer(self.model_id, device=device)
        get_dimensions = getattr(
            self._model,
            "get_embedding_dimension",
            self._model.get_sentence_embedding_dimension,
        )
        dimensions = get_dimensions()
        if not dimensions:
            raise RuntimeError("the sentence-transformers model did not report embedding dimensions")
        self._dimensions = int(dimensions)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        digest = hashlib.sha256(self.model_id.encode("utf-8")).hexdigest()[:12]
        return f"sentence-transformers-{digest}-{self.dimensions}"

    @property
    def vector_only_threshold(self) -> float:
        return 0.20

    @property
    def supplements_lexical_results(self) -> bool:
        return True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts),
            convert_to_numpy=False,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]
