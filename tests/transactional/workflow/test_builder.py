# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the programmatic WorkflowBuilder."""

from __future__ import annotations

from pyfly.transactional.workflow.builder import WorkflowBuilder


def test_builder_assembles_definition() -> None:
    async def first() -> str:
        return "first"

    async def second() -> str:
        return "second"

    definition = (
        WorkflowBuilder("foo")
        .description("a programmatic workflow")
        .step("first", first)
        .step("second", second, depends_on=["first"])
        .wait_signal("approve", "approved", depends_on=["second"], timeout_ms=10_000)
        .wait_timer("cool-down", delay_ms=500, depends_on=["approve"])
        .build()
    )
    assert definition.id == "foo"
    assert "first" in definition.steps
    assert definition.steps["second"].depends_on == ["first"]
    assert definition.steps["approve"].wait_for_signal == "approved"
    assert definition.steps["cool-down"].wait_for_timer_ms == 500
