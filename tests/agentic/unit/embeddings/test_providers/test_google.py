"""Tests for the Google embedding provider."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from pyfly.agentic.embeddings.providers.google import GoogleEmbedder
from pyfly.agentic.embeddings.types import EmbeddingResult


class TestGoogleEmbedder:
    @pytest.fixture(autouse=True)
    def _require_google_sdk(self):
        pytest.importorskip("google.ai.generativelanguage")

    @patch("pyfly.agentic.embeddings.providers.google.glm.GenerativeServiceClient")
    async def test_embed_batch(self, mock_client):
        from google.ai.generativelanguage import BatchEmbedContentsResponse, ContentEmbedding

        mock_client.return_value.batch_embed_contents.return_value = BatchEmbedContentsResponse(
            embeddings=[ContentEmbedding(values=[0.1, 0.2, 0.3]), ContentEmbedding(values=[0.4, 0.5, 0.6])]
        )
        async with GoogleEmbedder() as embedder:
            result = await embedder.embed(["hello", "world"])

        assert isinstance(result, EmbeddingResult)
        assert len(result.embeddings) == 2
        assert result.model == "models/text-embedding-004"
        request = mock_client.return_value.batch_embed_contents.call_args.kwargs["request"]
        assert request.model == "models/text-embedding-004"
        assert [item.content.parts[0].text for item in request.requests] == ["hello", "world"]

    @patch("pyfly.agentic.embeddings.providers.google.glm.GenerativeServiceClient")
    async def test_embed_one(self, mock_client):
        from google.ai.generativelanguage import BatchEmbedContentsResponse, ContentEmbedding

        mock_client.return_value.batch_embed_contents.return_value = BatchEmbedContentsResponse(
            embeddings=[ContentEmbedding(values=[0.1, 0.2, 0.3])]
        )
        async with GoogleEmbedder() as embedder:
            vec = await embedder.embed_one("hello")

        assert isinstance(vec, list)
        assert len(vec) == 3

    @patch("pyfly.agentic.embeddings.providers.google.glm.GenerativeServiceClient")
    def test_default_model(self, mock_client):
        embedder = GoogleEmbedder()
        assert embedder.model == "models/text-embedding-004"

    @patch("pyfly.agentic.embeddings.providers.google.glm.GenerativeServiceClient")
    def test_constructs_instance_client_with_api_key(self, mock_client):
        embedder = GoogleEmbedder(api_key="test-key")
        mock_client.assert_called_once_with(transport="rest", client_options={"api_key": "test-key"})
        assert embedder._client is mock_client.return_value


def test_missing_google_extra_import_contract():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import importlib.abc
import sys

class MissingGoogleExtra(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'google.ai.generativelanguage' or fullname.startswith('google.ai.generativelanguage.'):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, MissingGoogleExtra())
provider = importlib.import_module('pyfly.agentic.embeddings.providers.google')
assert provider.glm is None
try:
    provider.GoogleEmbedder(api_key='test-key')
except ImportError as exc:
    assert 'google-generativeai' in str(exc)
else:
    raise AssertionError('Missing optional SDK must reject provider construction')
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
