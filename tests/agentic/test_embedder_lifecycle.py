"""Native lifetime of embedding adapters' internally constructed HTTP clients."""

from pyfly.agentic.embeddings.providers.ollama import OllamaEmbedder


async def test_ollama_embedder_closes_its_owned_http_client_once():
    embedder = OllamaEmbedder(base_url="http://127.0.0.1:11434")
    assert not embedder._client.is_closed
    await embedder.aclose()
    assert embedder._client.is_closed
    await embedder.aclose()
    assert embedder._client.is_closed


async def test_embedding_context_releases_client_after_body_failure():
    embedder = OllamaEmbedder(base_url="http://127.0.0.1:11434")
    try:
        async with embedder:
            raise LookupError("application failed")
    except LookupError:
        pass
    assert embedder._client.is_closed
