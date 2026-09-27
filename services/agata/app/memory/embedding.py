"""Real embedding providers for pgvector.

Replaces DeterministicFakeEmbeddingProvider with actual API calls.
"""

from __future__ import annotations

import math
from typing import Protocol

import httpx


class EmbeddingError(ValueError):
    pass


class EmbeddingProvider(Protocol):
    """Interface for embedding generation."""

    @property
    def dimensions(self) -> int: ...

    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """OpenAI text-embedding-3-small: 1536 dims, $0.02/1M tokens."""

    dimensions = 1536
    model = "text-embedding-3-small"
    model_name = "text-embedding-3-small"

    def __init__(self, *, api_key: str, timeout: float = 10.0) -> None:
        if not api_key:
            raise EmbeddingError("missing_api_key")
        self._api_key = api_key
        self._timeout = timeout

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EmbeddingError("provider_transport") from exc
        if response.status_code >= 400:
            raise EmbeddingError(f"provider_error_{response.status_code}")
        try:
            body = response.json()
            return [item["embedding"] for item in body["data"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("provider_malformed") from exc


class DeterministicFakeEmbeddingProvider:
    """Test seam: produces fixed vectors. Use only for local testing."""

    dimensions = 1536
    model_name = "deterministic-embedding-v1"

    async def embed(self, text: str) -> list[float]:
        """Return exactly the declared number of deterministic finite values.

        SHA-256 emits only 32 bytes.  Repeated, domain-separated counter hashes
        avoid the former silent 32-value vector while keeping this provider a
        deterministic test seam (never a production semantic model).
        """
        import hashlib

        if not isinstance(text, str):
            raise EmbeddingError("embedding_text_invalid")
        seed = text.encode("utf-8")
        vector: list[float] = []
        counter = 0
        while len(vector) < self.dimensions:
            digest = hashlib.sha256(
                b"gptgirls/deterministic-embedding/v1\x00" + counter.to_bytes(4, "big") + seed
            ).digest()
            vector.extend((float(value) / 127.5) - 1.0 for value in digest)
            counter += 1
        return vector[: self.dimensions]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = []
        for t in texts:
            results.append(await self.embed(t))
        return results


class BGEInlineEmbeddingProvider:
    """Local BGE-M3 embeddings: no API key, 1024 dims, multilingual."""

    dimensions = 1024
    model_name = "bge-m3"

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("BAAI/bge-m3")
        return self._model

    async def embed(self, text: str) -> list[float]:
        import asyncio

        model = self._load()
        result = await asyncio.to_thread(model.encode, [text], normalize_embeddings=True)
        return result[0].tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        model = self._load()
        results = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)
        return [r.tolist() for r in results]


class LocalMiniLMEmbeddingProvider:
    """Local all-MiniLM-L6-v2 via sentence-transformers: 384 dims, ~80 MB."""

    dimensions = 384
    model_name = "all-MiniLM-L6-v2-local"

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        return self._model

    async def embed(self, text: str) -> list[float]:
        import asyncio

        model = self._load()
        result = await asyncio.to_thread(model.encode, [text], normalize_embeddings=True)
        return result[0].tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        model = self._load()
        results = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)
        return [r.tolist() for r in results]


class HuggingFaceFreeEmbeddingProvider:
    """Hugging Face Inference Providers multilingual embeddings.

    The project schema currently uses ``vector(1536)``.  The selected
    multilingual-e5-large model returns 1024 dimensions, so we preserve the
    existing schema by zero-padding the provider vector.  The model name is
    versioned with that representation to prevent mixing incompatible vectors.
    """

    provider_dimensions = 1024
    dimensions = 1536
    model = "intfloat/multilingual-e5-large"
    model_name = "hf-intfloat-multilingual-e5-large-padded1536-v1"
    endpoint = "https://router.huggingface.co/hf-inference/models/"

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport
        self.available = bool(api_key)

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._api_key:
            raise EmbeddingError("missing_api_key")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self.endpoint}{self.model}",
                    # hf-inference rejects the legacy `normalize` request field
                    # for this router/model; normalize deterministically below.
                    json={"inputs": texts},
                    headers=headers,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EmbeddingError("provider_transport") from exc
        if response.status_code >= 400:
            raise EmbeddingError(f"provider_error_{response.status_code}")
        try:
            body = response.json()
            vectors = body if isinstance(body[0], list) else [body]
            output: list[list[float]] = []
            for vector in vectors:
                if len(vector) != self.provider_dimensions:
                    raise EmbeddingError("provider_dimensions_invalid")
                converted = [float(item) for item in vector]
                if not all(math.isfinite(item) for item in converted):
                    raise EmbeddingError("provider_values_invalid")
                norm = math.sqrt(sum(item * item for item in converted))
                if not math.isfinite(norm) or norm <= 0:
                    raise EmbeddingError("provider_norm_invalid")
                converted = [item / norm for item in converted]
                output.append(converted + [0.0] * (self.dimensions - len(vector)))
            if len(output) != len(texts):
                raise EmbeddingError("provider_batch_mismatch")
            return output
        except (ValueError, IndexError, TypeError) as exc:
            raise EmbeddingError("provider_malformed") from exc
