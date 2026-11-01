# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Exact skill dependency closure without implicit tool or file authority."""

from dataclasses import FrozenInstanceError, replace

import pytest

from pyfly.agentic.skills import Skill, SkillPackage, SkillParseError, SkillRequirements, SkillResource


def skill(key, *, dependencies=(), tools=(), resources=None):
    return Skill(
        key=key,
        name=key,
        description=f"Use {key} when requested.",
        instructions=f"Apply {key}.",
        requires=SkillRequirements(skills=dependencies, tools=tools),
        resources=resources or {},
    )


def test_package_resolves_only_transitive_dependencies_in_stable_order():
    package = SkillPackage(
        name="finance",
        version="1",
        skills=(skill("unused"), skill("close", dependencies=("review",)), skill("review")),
        entrypoints=("close",),
    )
    assert [item.key for item in package.resolve()] == ["review", "close"]
    assert [item.key for item in package.resolve(("unused",))] == ["unused"]
    assert {pin.key for pin in package.pins} == {"close", "review", "unused"}
    with pytest.raises(FrozenInstanceError):
        package.version = "2"


@pytest.mark.parametrize(
    "members,entrypoints",
    [
        ((skill("a"), skill("a")), ("a",)),
        ((skill("a", dependencies=("missing",)),), ("a",)),
        ((skill("a", dependencies=("a",)),), ("a",)),
        ((skill("a", dependencies=("b",)), skill("b", dependencies=("a",))), ("a",)),
        ((skill("a"),), ("missing",)),
        ((skill("a"),), ()),
    ],
)
def test_invalid_dependency_graph_is_rejected_before_prompt_or_tool_creation(members, entrypoints):
    with pytest.raises(SkillParseError):
        SkillPackage(name="test", version="1", skills=members, entrypoints=entrypoints)


def test_package_requirements_do_not_grant_tools():
    package = SkillPackage(name="test", version="1", skills=(skill("a", tools=("approve",)),), entrypoints=("a",))
    with pytest.raises(SkillParseError, match="approve"):
        package.resolve()
    assert package.resolve(tools=("approve",))[0].key == "a"


def test_package_freezes_lazy_resource_before_pin_and_model_use():
    state = {"text": "reviewed"}
    resource = SkillResource(path="memo.md", loader=lambda: state["text"])
    original = skill("a", resources={"memo.md": resource})
    package = SkillPackage(name="test", version="1", skills=(original,), entrypoints=("a",))
    digest = package.digest()
    state["text"] = "changed"
    assert package.resolve()[0].resources["memo.md"].read() == "reviewed"
    assert package.digest() == digest
    assert package.pins[0].digest == package.resolve()[0].digest()


def test_package_refuses_unavailable_resource_and_excess_members():
    with pytest.raises(SkillParseError):
        SkillPackage(
            name="test",
            version="1",
            skills=(skill("a", resources={"memo": SkillResource(path="memo")}),),
            entrypoints=("a",),
        )
    with pytest.raises(SkillParseError):
        SkillPackage(name="test", version="1", skills=tuple(skill(f"s{i}") for i in range(129)), entrypoints=("s0",))


def test_package_identity_is_independent_of_member_order_and_pins_visible_changes():
    first = skill("a")
    second = skill("b", dependencies=("a",))
    left = SkillPackage(name="test", version="1", skills=(first, second), entrypoints=("b",))
    right = SkillPackage(name="test", version="1", skills=(second, first), entrypoints=("b",))
    assert left.digest() == right.digest()
    changed = SkillPackage(name="test", version="2", skills=(first, second), entrypoints=("b",))
    assert left.digest() != changed.digest()


@pytest.mark.parametrize("change", ["metadata", "media_type"])
def test_package_identity_covers_admitted_fields_outside_legacy_skill_digest(change):
    original = skill("a", resources={"memo": SkillResource(path="memo", content="approved")})
    modified = (
        replace(original, metadata={"publisher": "another"})
        if change == "metadata"
        else replace(
            original, resources={"memo": SkillResource(path="memo", content="approved", media_type="text/plain")}
        )
    )
    assert original.digest() == modified.digest()
    first = SkillPackage(name="test", version="1", skills=(original,), entrypoints=("a",))
    second = SkillPackage(name="test", version="1", skills=(modified,), entrypoints=("a",))
    assert first.digest() != second.digest()


@pytest.mark.parametrize("selection", [(), ("unknown",), ("a",) * 129, "a", (None,)])
def test_package_rejects_invalid_selection(selection):
    package = SkillPackage(name="test", version="1", skills=(skill("a"),), entrypoints=("a",))
    with pytest.raises(SkillParseError):
        package.resolve(selection)


def test_invalid_graph_is_rejected_before_lazy_resource_callback():
    def unexpected():
        pytest.fail("Invalid dependency graph reached a resource loader")

    member = skill("a", dependencies=("missing",), resources={"memo": SkillResource(path="memo", loader=unexpected)})
    with pytest.raises(SkillParseError, match="missing"):
        SkillPackage(name="test", version="1", skills=(member,), entrypoints=("a",))


async def test_native_agent_reads_resource_from_resolved_dependency():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from pyfly.agentic.agents import Agent
    from pyfly.agentic.config import AgenticConfig
    from pyfly.agentic.skills import build_read_skill_tool, render_skills_section

    package = SkillPackage(
        name="review",
        version="1",
        skills=(
            skill("close", dependencies=("review",)),
            skill("review", resources={"memo.md": SkillResource(path="memo.md", content="approved memo")}),
        ),
        entrypoints=("close",),
    )
    bound = package.resolve()
    seen = []

    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("read_skill", {"key": "review", "path": "memo.md"}, "read-dependency")]
            )
        seen.extend(part.content for message in messages for part in message.parts if isinstance(part, ToolReturnPart))
        return ModelResponse(parts=[TextPart("review complete")])

    agent = Agent(
        "package-reader",
        model=FunctionModel(respond),
        instructions=render_skills_section(bound),
        tools=[build_read_skill_tool(bound)],
        auto_register=False,
        config=AgenticConfig(cost_tracking_enabled=False),
    )
    assert (await agent.run("Close using the review dependency.")).output == "review complete"
    assert seen == ["approved memo"]
