from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    """Local or remote adapter behind a rebuildable retrieval projection."""

    @property
    def dimensions(self) -> int: ...

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

    def __init__(self, dimensions: int = 256):
        if dimensions < 32:
            raise ValueError("dimensions must be at least 32")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"local-hash-v1-{self.dimensions}"

    def _features(self, text: str) -> list[str]:
        normalized = " ".join(text.casefold().split())
        words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
        compact = re.sub(r"\s+", "", normalized)
        grams = [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
        return [f"w:{word}" for word in words] + [f"c3:{gram}" for gram in grams]

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
