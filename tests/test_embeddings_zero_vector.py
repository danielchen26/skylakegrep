"""Regression tests for the zero-vector fallback dimension.

Background
----------
When an embedding request fails *before* any request has succeeded,
``_zero_vector()`` has no observed dimension to fall back on. It used to
hard-code 768 (the ``nomic-embed-text`` width). With the default model now
``bge-m3`` (1024-d), a brand-new index could end up with a handful of 768-d
zero vectors mixed into 1024-d data. Those chunks are silently unsearchable
and ``_filter_to_matching_dim`` tells the user to ``--reset`` an index they
just built.

The fallback must derive the width from the configured model name.
"""

from __future__ import annotations

import unittest
from unittest import mock

import requests

from skylakegrep.src import embeddings
from skylakegrep.src.embeddings import (
    OllamaEmbedder,
    SentenceTransformersEmbedder,
    embedding_dim_for_model,
)


class EmbeddingDimForModelTests(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(embedding_dim_for_model("bge-m3"), 1024)
        self.assertEqual(embedding_dim_for_model("bge-large-en-v1.5"), 1024)
        self.assertEqual(embedding_dim_for_model("mxbai-embed-large"), 1024)
        self.assertEqual(embedding_dim_for_model("nomic-embed-text"), 768)
        self.assertEqual(embedding_dim_for_model("nomic-embed-text-v1.5"), 768)

    def test_ollama_tag_is_stripped(self):
        self.assertEqual(embedding_dim_for_model("bge-m3:latest"), 1024)
        self.assertEqual(embedding_dim_for_model("nomic-embed-text:v1.5"), 768)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(embedding_dim_for_model("some-custom-model"))
        self.assertIsNone(embedding_dim_for_model(""))


class OllamaZeroVectorTests(unittest.TestCase):
    def _failing_embedder(self, model: str) -> OllamaEmbedder:
        return OllamaEmbedder("http://127.0.0.1:1", model)

    def test_zero_vector_uses_model_dim_before_any_success(self):
        emb = self._failing_embedder("bge-m3:latest")
        self.assertEqual(len(emb._zero_vector()), 1024)

    def test_zero_vector_nomic_stays_768(self):
        emb = self._failing_embedder("nomic-embed-text")
        self.assertEqual(len(emb._zero_vector()), 768)

    def test_observed_dim_still_wins_over_registry(self):
        emb = self._failing_embedder("bge-m3")
        emb._zero_dim = 512  # pretend a real response taught us the width
        self.assertEqual(len(emb._zero_vector()), 512)

    def test_first_batch_failure_yields_correct_width(self):
        """The actual bug path: batch fails, per-chunk fails, nothing has
        succeeded yet. Every returned vector must be the model's width."""
        emb = self._failing_embedder("bge-m3:latest")
        with mock.patch.object(
            embeddings.requests,
            "post",
            side_effect=requests.ConnectionError("simulated outage"),
        ):
            vecs = emb.embed_batch(["alpha", "beta", "gamma"])
        self.assertEqual(len(vecs), 3)
        for v in vecs:
            self.assertEqual(len(v), 1024)
            self.assertTrue(all(x == 0.0 for x in v))


class SentenceTransformersZeroVectorTests(unittest.TestCase):
    def test_zero_vector_uses_model_dim(self):
        emb = SentenceTransformersEmbedder("nomic-embed-text-v1.5")
        self.assertEqual(len(emb._zero_vector()), 768)

    def test_zero_vector_bge(self):
        emb = SentenceTransformersEmbedder("bge-m3")
        self.assertEqual(len(emb._zero_vector()), 1024)


if __name__ == "__main__":
    unittest.main()
