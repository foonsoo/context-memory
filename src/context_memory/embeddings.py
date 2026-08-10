from __future__ import annotations

from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    """Optional local or remote adapter; the MVP never calls one implicitly."""

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingsDisabled:
    @property
    def dimensions(self) -> int: return 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embeddings are disabled; FTS5 remains fully functional")
