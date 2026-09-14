"""Client for the local Ollama embeddings API (nomic-embed-text)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from .config import (
    EMBED_CONCURRENCY,
    EMBED_ENDPOINT,
    EMBED_MAX_RETRIES,
    EMBED_MODEL,
    EMBED_TIMEOUT_SECONDS,
    OLLAMA_URL,
)

logger = logging.getLogger("semantic_index.embedder")


class EmbeddingError(RuntimeError):
    """Raised when an embedding cannot be produced after all retries."""


class OllamaEmbedder:
    """Talks to ``POST {OLLAMA_URL}{EMBED_ENDPOINT}`` with retry/backoff."""

    def __init__(
        self,
        base_url: str = OLLAMA_URL,
        model: str = EMBED_MODEL,
        timeout: float = EMBED_TIMEOUT_SECONDS,
        max_retries: int = EMBED_MAX_RETRIES,
        concurrency: int = EMBED_CONCURRENCY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.concurrency = concurrency

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text, retrying transient failures with backoff."""
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        url = self.base_url + EMBED_ENDPOINT
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                vector = body.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise EmbeddingError(f"Malformed embeddings response: {body!r:.200}")
                return [float(x) for x in vector]
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, EmbeddingError) as exc:
                last_exc = exc
                logger.warning(
                    "Embedding attempt %d/%d failed: %s", attempt, self.max_retries, exc
                )
                time.sleep(min(2.0 * attempt, 6.0))
        raise EmbeddingError(
            f"Ollama embedding failed after {self.max_retries} attempts: {last_exc}"
        )

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts concurrently; result order matches input order."""
        if not texts:
            return []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return list(pool.map(self.embed_one, texts))
