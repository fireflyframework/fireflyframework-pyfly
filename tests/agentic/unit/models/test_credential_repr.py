# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Credential representations omit secrets while intentional credential use remains intact."""

from dataclasses import asdict, replace

import pytest

from pyfly.agentic.models import Credential, ModelFactory, ModelSpec

FIXTURE_SECRET = "fixture-repr-key-value"
CREDENTIALS = [
    Credential.api_key(FIXTURE_SECRET),
    Credential.reference("fixture-account", version=3),
    Credential.none(),
]


@pytest.mark.parametrize("credential", CREDENTIALS, ids=["inline", "reference", "none"])
def test_credential_representations_do_not_disclose_the_secret_field(credential: Credential) -> None:
    for representation in (repr(credential), str(credential)):
        assert FIXTURE_SECRET not in representation
        assert "secret=" not in representation
        assert credential.kind in representation


@pytest.mark.parametrize("credential", CREDENTIALS, ids=["inline", "reference", "none"])
def test_model_spec_representations_keep_nested_credentials_private(credential: Credential) -> None:
    spec = ModelSpec(provider="openai-chat", model="gpt-4o", credential=credential)
    for representation in (repr(spec), str(spec)):
        assert FIXTURE_SECRET not in representation
        assert "secret=" not in representation
        assert "gpt-4o" in representation


def test_intentional_access_serialization_and_equality_are_preserved() -> None:
    credential = Credential.api_key(FIXTURE_SECRET)
    assert credential.secret == FIXTURE_SECRET
    assert asdict(credential) == {
        "kind": "api_key",
        "secret": FIXTURE_SECRET,
        "reference_id": None,
        "version": 1,
    }
    assert credential == Credential("api_key", FIXTURE_SECRET)
    assert credential != replace(credential, secret="different-fixture-value")


class FixtureResolver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []

    async def resolve(self, reference: str, version: int) -> str:
        self.requests.append((reference, version))
        return FIXTURE_SECRET


@pytest.mark.parametrize("reference", [False, True], ids=["inline", "resolved-reference"])
async def test_factory_still_receives_inline_and_resolved_credentials(reference: bool) -> None:
    resolver = FixtureResolver()
    credential = Credential.reference("fixture-account", version=3) if reference else Credential.api_key(FIXTURE_SECRET)
    spec = ModelSpec(provider="openai-chat", model="gpt-4o", credential=credential)
    model = await ModelFactory(credentials=resolver).build(spec)
    try:
        assert model.provider.client.api_key == FIXTURE_SECRET
        assert resolver.requests == ([("fixture-account", 3)] if reference else [])
    finally:
        await model.provider.client.close()
