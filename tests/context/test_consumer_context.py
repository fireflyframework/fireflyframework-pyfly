# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from pyfly.context.consumer_context import (
    PRIVATE_CONSUMER_CONTEXT,
    bind_consumer_context,
    consumer_context,
    public_consumer_data,
)


async def test_consumer_context_propagates_to_task_without_sharing_mutable_data():
    original = {"subject": "owner", "facts": {"ids": ["one"]}}
    with bind_consumer_context(original):
        original["facts"]["ids"].append("outside")

        async def read():
            value = consumer_context()
            value["facts"]["ids"].append("inside")
            return consumer_context()

        assert await asyncio.create_task(read()) == {"subject": "owner", "facts": {"ids": ["one"]}}
    assert consumer_context() is None


async def test_closed_parent_context_cannot_be_reused_by_inherited_task():
    proceed = asyncio.Event()

    async def late():
        await proceed.wait()
        return consumer_context()

    with bind_consumer_context({"subject": "first"}):
        task = asyncio.create_task(late())
    with bind_consumer_context({"subject": "second"}):
        proceed.set()
        with pytest.raises(RuntimeError, match="closed"):
            await task
        assert consumer_context() == {"subject": "second"}
    assert consumer_context() is None


def test_nested_context_restores_after_exception_and_never_leaks_between_jobs():
    with bind_consumer_context({"id": "outer"}):
        with pytest.raises(ValueError), bind_consumer_context({"id": "inner"}):
            raise ValueError("stop")
        assert consumer_context() == {"id": "outer"}
    assert consumer_context() is None


@pytest.mark.parametrize("value", [{"invalid": float("nan")}, {"large": "x" * 16384}, {"object": object()}])
def test_context_rejects_non_json_or_oversized_facts(value):
    with pytest.raises(ValueError), bind_consumer_context(value):
        pass
    assert consumer_context() is None


def test_public_redaction_removes_private_envelopes_without_mutating_actor_or_payload():
    value = {
        "actor": "public-owner",
        PRIVATE_CONSUMER_CONTEXT: {"credential_id": "private-id"},
        "nested": [{"text": "retained", PRIVATE_CONSUMER_CONTEXT: {"secret": "no"}}],
    }
    assert public_consumer_data(value) == {"actor": "public-owner", "nested": [{"text": "retained"}]}
    assert PRIVATE_CONSUMER_CONTEXT in value
