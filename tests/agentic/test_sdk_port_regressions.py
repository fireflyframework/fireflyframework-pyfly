"""Regressions at the migrated source's installed SDK boundaries."""

from __future__ import annotations

import pytest

from pyfly.agentic.models import Credential, ModelFactory, ModelSpec


def test_vertex_factory_uses_real_google_cloud_transport() -> None:
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google_cloud import GoogleCloudProvider

    spec = ModelSpec(
        provider="google-vertex",
        model="gemini-2.5-flash",
        credential=Credential.none(),
        project="offline-test-project",
        region="us-central1",
    )
    model = ModelFactory().build_with_secret(spec, None)
    assert isinstance(model, GoogleModel)
    assert isinstance(model._provider, GoogleCloudProvider)
    client = model._provider.client
    try:
        assert client.vertexai is True
        assert client._api_client.project == "offline-test-project"
        assert client._api_client.location == "us-central1"
    finally:
        client.close()


def test_vertex_factory_requires_project() -> None:
    from pyfly.agentic.models import ModelBuildError

    spec = ModelSpec(provider="google-vertex", model="gemini-2.5-flash", credential=Credential.none())
    with pytest.raises(ModelBuildError, match="project id"):
        ModelFactory().build_with_secret(spec, None)
