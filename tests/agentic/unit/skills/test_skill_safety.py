# Copyright 2026 Firefly Software Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Untrusted skill authoring must stay bounded and cannot acquire host capabilities."""

from pathlib import Path

import pytest

from pyfly.agentic.skills import (
    Skill,
    SkillConfigError,
    SkillParseError,
    SkillRequirements,
    SkillResource,
    build_read_skill_tool,
    load_skill,
    parse_skill,
    render_skills_section,
)


def skill(body: str = "Body", **kwargs):
    return Skill(key="safe", name="Safe", description="d", instructions=body, **kwargs)


@pytest.mark.parametrize(
    "body",
    [
        "{{ cycler.__init__.__globals__.__builtins__.len([1]) }}",
        "{{ 'x' * 100 }}",
        "{{ 2 ** 10 }}",
        "{% for x in range(3) %}x{% endfor %}",
        "{{ value | attr('__class__') }}",
        "{% include 'other' %}",
    ],
)
def test_rejects_executable_and_expanding_template_syntax(body):
    with pytest.raises(SkillConfigError):
        skill(body).render({"value": "safe"})


def test_safe_formatting_only_reads_json_values_and_keeps_markdown():
    bound = skill("{{ client.name | upper }}: {{ rows[0] }} {{ missing | default('n/a') }} {{ tags | join(', ') }}")
    assert bound.render({"client": {"name": "A & B"}, "rows": [3], "tags": ["one", "two"]}) == "A & B: 3 n/a one, two"


def test_rejects_output_growth_from_repeated_config_substitution():
    with pytest.raises(SkillConfigError):
        skill("{{ value }}{{ value }}").render({"value": "x" * 40_000})


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), {1: "non-string key"}])
def test_rejects_non_json_config_values(value):
    with pytest.raises(SkillParseError):
        skill(config={"value": value})


def test_nested_config_and_schema_are_independent_and_immutable():
    config = {"client": {"names": ["Original"]}}
    schema = {"properties": {"client": {"type": "object"}}}
    bound = skill("{{ client.names[0] }}", config=config, parameters_schema=schema)
    digest = bound.digest()
    config["client"]["names"][0] = "Changed"
    schema["properties"]["client"]["type"] = "string"
    assert bound.render() == "Original"
    assert bound.digest() == digest
    with pytest.raises(TypeError):
        bound.config["client"]["names"][0] = "Changed"


def test_digest_is_canonical_for_nested_mapping_order():
    assert skill(config={"x": {"a": 1, "b": [2]}}).digest() == skill(config={"x": {"b": [2], "a": 1}}).digest()


@pytest.mark.parametrize("which", ["depth", "nodes", "string", "body"])
def test_authoring_limits_fail_before_render(which):
    kwargs = {}
    value = {}
    if which == "depth":
        for _ in range(20):
            value = {"nested": value}
    elif which == "nodes":
        value = {"many": list(range(5000))}
    elif which == "string":
        value = {"large": "x" * 70_000}
    else:
        kwargs["instructions"] = "x" * 70_000
    with pytest.raises(SkillParseError):
        Skill(key="safe", name="Safe", description="d", config=value, **kwargs)


def test_recursive_schema_validates_nested_values_items_and_defaults():
    bound = skill(
        "{{ account.currency }}",
        parameters_schema={
            "type": "object",
            "required": ["account"],
            "properties": {
                "account": {
                    "type": "object",
                    "required": ["currency"],
                    "additionalProperties": False,
                    "properties": {
                        "currency": {"type": "string", "enum": ["EUR"], "default": "EUR"},
                        "amounts": {"type": "array", "maxItems": 2, "items": {"type": "integer", "minimum": 0}},
                    },
                },
            },
        },
    )
    assert bound.render({"account": {}}) == "EUR"
    problems = bound.validate_config({"account": {"amounts": [-1, True, 3], "extra": 1}})
    assert any("account.amounts[0]" in p for p in problems)
    assert any("account.amounts[1]" in p for p in problems)
    assert any("account.extra" in p for p in problems)
    assert any("2" in p for p in problems)


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"x": {"pattern": ".*"}}},
        {"$ref": "https://invalid/schema"},
        {"type": "unexpected"},
        {"properties": {"x": {"minimum": "zero"}}},
        {"properties": {"x": {"type": "integer", "default": "wrong"}}},
    ],
)
def test_unsupported_or_malformed_schema_fails_at_authoring(schema):
    with pytest.raises(SkillParseError):
        skill(parameters_schema=schema)


@pytest.mark.parametrize("assemble", [render_skills_section, build_read_skill_tool])
def test_duplicate_keys_never_select_a_different_resource_than_the_prompt(assemble):
    with pytest.raises(SkillParseError, match="duplicate"):
        assemble([skill(), skill("Conflicting instructions")])


def write_skill(root: Path):
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: Safe\ndescription: d\nresources:\n  refs/file.md: Ref\n---\nBody")
    (root / "refs").mkdir()
    (root / "refs" / "file.md").write_text("approved")


@pytest.mark.parametrize("target", ["file", "directory", "skill"])
def test_symlink_files_and_directories_cannot_escape_root(tmp_path, target):
    root = tmp_path / "safe"
    write_skill(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.md").write_text("private")
    if target == "file":
        (root / "refs" / "file.md").unlink()
        (root / "refs" / "file.md").symlink_to(outside / "file.md")
    elif target == "directory":
        (root / "refs").rename(root / "old")
        (root / "refs").symlink_to(outside, target_is_directory=True)
    else:
        (root / "SKILL.md").unlink()
        (root / "SKILL.md").symlink_to(outside / "file.md")
    with pytest.raises(SkillParseError):
        load_skill(root)


def test_lazy_loader_refuses_directory_swapped_to_symlink(tmp_path):
    root = tmp_path / "safe"
    write_skill(root)
    loaded = load_skill(root)
    (root / "refs").rename(root / "old")
    (root / "refs").symlink_to(root / "old", target_is_directory=True)
    with pytest.raises(SkillParseError):
        loaded.resources["refs/file.md"].read()


def test_lazy_loader_refuses_root_replacement(tmp_path):
    root = tmp_path / "safe"
    write_skill(root)
    loaded = load_skill(root)
    root.rename(tmp_path / "old")
    write_skill(root)
    with pytest.raises(SkillParseError):
        loaded.resources["refs/file.md"].read()


@pytest.mark.parametrize("filename", ["../private.md", "/etc/passwd"])
def test_custom_skill_filename_must_be_root_contained(tmp_path, filename):
    with pytest.raises(SkillParseError):
        load_skill(tmp_path, key="safe", filename=filename)


def test_resource_read_bounds_also_apply_to_inline_and_custom_loaders():
    with pytest.raises(SkillParseError):
        SkillResource(path="file.md", content="x" * (1_048_576 + 1))
    with pytest.raises(SkillParseError):
        SkillResource(path="file.md", loader=lambda: "x" * (1_048_576 + 1)).read()


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: First\nname: Conflicting\ndescription: d\n---\nBody",
        "---\nname: Safe\ndescription: d\nmetadata: &cycle [*cycle]\n---\nBody",
    ],
)
def test_ambiguous_or_recursive_frontmatter_is_refused(text):
    with pytest.raises(SkillParseError):
        parse_skill(text, key="safe")


@pytest.mark.parametrize("schema", [{"type": []}, {"type": {}}, {"required": "field"}, {"enum": []}])
def test_malformed_schema_never_escapes_as_an_unhandled_type_error(schema):
    with pytest.raises(SkillParseError):
        skill(parameters_schema=schema)


def test_bounded_aggregate_resource_digest_does_not_load_every_appendix():
    calls = []

    def load():
        calls.append(1)
        return "x" * 1_048_576

    bound = skill(resources={f"{i}.md": SkillResource(path=f"{i}.md", loader=load) for i in range(20)})
    with pytest.raises(SkillParseError):
        bound.digest()
    assert len(calls) <= 9


def test_filesystem_resource_limit_applies_before_lazy_content_is_read(tmp_path):
    root = tmp_path / "safe"
    write_skill(root)
    (root / "refs" / "file.md").write_bytes(b"x" * 1_048_577)
    with pytest.raises(SkillParseError):
        load_skill(root)


def test_deep_yaml_input_fails_closed():
    text = "---\nname: n\ndescription: d\nvalue: " + "[" * 100 + "0" + "]" * 100 + "\n---\nBody"
    with pytest.raises(SkillParseError):
        parse_skill(text, key="safe")


def test_collection_and_prompt_limits_are_enforced_without_reading_resources():
    with pytest.raises(SkillParseError):
        build_read_skill_tool(Skill(key=f"safe_{i}", name="n", description="d") for i in range(129))
    with pytest.raises(SkillParseError):
        render_skills_section([skill("x" * 65_536)])


@pytest.mark.parametrize("body", ["{{ value | upper(1) }}", "{{ rows | join(3) }}", "{{ x | default(1, 2) }}"])
def test_invalid_safe_filter_arguments_fail_with_config_error(body):
    with pytest.raises(SkillConfigError):
        skill(body).render({"value": "a", "rows": [1]})


async def test_native_agent_uses_frozen_skill_resource_and_preserves_tool_call_id():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from pyfly.agentic.agents import Agent
    from pyfly.agentic.config import AgenticConfig

    seen = []

    class Listener:
        async def after_call(self, tool, kwargs, ctx, result):
            seen.append((ctx.tool_call_id, result))

    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_skill", {"key": "safe", "path": "memo.md"}, "read-1")])
        return ModelResponse(parts=[TextPart("done")])

    bound = skill("Use the approved memo.", resources={"memo.md": SkillResource(path="memo.md", content="approved")})
    agent = Agent(
        "safe",
        model=FunctionModel(respond),
        instructions=render_skills_section([bound]),
        tools=[build_read_skill_tool([bound], listeners=[Listener()])],
        auto_register=False,
        config=AgenticConfig(cost_tracking_enabled=False),
    )
    result = await agent.run("Read the memo.")
    assert result.output == "done"
    assert seen == [("read-1", "approved")]


def test_requirements_are_frozen_even_when_built_from_mutable_lists():
    tools = ["search"]
    bound = skill(requires=SkillRequirements(tools=tools))
    digest = bound.digest()
    tools.append("write")
    assert bound.unmet().tools == ("search",)
    assert bound.digest() == digest


@pytest.mark.parametrize("body", ["{{ missing }}", "{{ config.items }}", "{{ rows[index] }}"])
def test_undefined_values_and_python_attributes_do_not_reach_the_prompt(body):
    with pytest.raises(SkillConfigError):
        skill(body).render({"config": {}, "rows": [1], "index": 0})


def test_template_filter_and_syntax_budgets_are_bounded():
    with pytest.raises(SkillConfigError):
        skill("{{ value" + " | upper" * 40 + " }}").render({"value": "x"})
    with pytest.raises(SkillConfigError):
        skill("{{ value }}" * 1030).render({"value": "x"})
    assert (
        skill("{{ empty | default('fallback', true) }} {{ x | trim | lower | title }}").render(
            {"empty": "", "x": "  hello WORLD  "}
        )
        == "fallback Hello World"
    )


def test_join_and_unicode_formatting_cannot_expand_past_the_output_budget():
    with pytest.raises(SkillConfigError):
        skill("{{ words | join('---') }}").render({"words": ["x" * 40_000, "x" * 40_000]})
    with pytest.raises(SkillConfigError):
        skill("{{ value | upper }}").render({"value": "ß" * 40_000})


@pytest.mark.parametrize(
    "schema",
    [
        {"title": 3},
        {"minimum": 2, "maximum": 1},
        {"additionalProperties": {}},
        {"properties": []},
        {"properties": {"x": 3}},
        {"items": []},
    ],
)
def test_schema_structure_is_validated_before_config(schema):
    with pytest.raises(SkillParseError):
        skill(parameters_schema=schema)


def test_nested_enum_keeps_booleans_distinct_from_numbers():
    bound = skill(parameters_schema={"properties": {"x": {"enum": [{"flags": [1]}]}}})
    assert bound.validate_config({"x": {"flags": [1]}}) == []
    assert bound.validate_config({"x": {"flags": [True]}})


@pytest.mark.parametrize("value", [{"n": 1 << 5000}, {"x": "é" * 50_000, "y": "é" * 50_000}, []])
def test_json_size_integer_and_mapping_contracts_are_enforced(value):
    with pytest.raises(SkillParseError):
        skill(config=value)


def test_lazy_file_read_rejects_invalid_utf8_and_unicode_resource_size(tmp_path):
    root = tmp_path / "safe"
    write_skill(root)
    loaded = load_skill(root)
    (root / "refs" / "file.md").write_bytes(b"\xff")
    with pytest.raises(SkillParseError):
        loaded.resources["refs/file.md"].read()
    with pytest.raises(SkillParseError):
        SkillResource(path="unicode.md", content="é" * 600_000)


@pytest.mark.parametrize("path", ["/".join(["directory"] * 17), "x" * 4097], ids=["too-deep", "too-long"])
def test_resource_path_limits_bound_descriptor_depth(path):
    with pytest.raises(SkillParseError):
        SkillResource(path=path)


@pytest.mark.parametrize(
    "kind, valid, invalid",
    [
        ("string", "EUR", 2),
        ("number", 1.5, True),
        ("integer", 2, 1.5),
        ("boolean", True, 1),
        ("array", ["EUR"], "EUR"),
        ("object", {"name": "EUR"}, []),
    ],
)
def test_nullable_types_preserve_type_checks_and_null_defaults(kind, valid, invalid):
    bound = skill(parameters_schema={"properties": {"value": {"type": [kind, "null"], "default": None}}})
    assert bound.validate_config({}) == []
    assert bound.validate_config({"value": None}) == []
    assert bound.validate_config({"value": valid}) == []
    assert bound.validate_config({"value": invalid})


def test_nullable_nested_objects_and_arrays_still_validate_children():
    bound = skill(
        parameters_schema={
            "properties": {
                "account": {
                    "type": ["null", "object"],
                    "properties": {
                        "flags": {
                            "type": ["array", "null"],
                            "items": {"type": ["integer", "null"], "enum": [1, None]},
                        }
                    },
                }
            }
        }
    )
    assert bound.validate_config({"account": None}) == []
    assert bound.validate_config({"account": {"flags": None}}) == []
    assert bound.validate_config({"account": {"flags": [1, None]}}) == []
    assert bound.validate_config({"account": {"flags": [True]}})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["null", "null"]},
        {"type": ["string", "integer"]},
        {"type": ["string", "number", "null"]},
        {"required": ["missing"]},
        {"properties": {"x": {}}, "required": ["x", "x"]},
    ],
)
def test_ambiguous_nullable_types_and_required_names_are_authoring_errors(schema):
    with pytest.raises(SkillParseError):
        skill(parameters_schema=schema)


@pytest.mark.parametrize("budget", ["nodes", "bytes"])
def test_defaults_expansion_stops_before_building_an_oversized_result(budget):
    from pyfly.agentic.skills import _schema

    reads = []

    class CountedDefault(dict):
        def __getitem__(self, key):
            if key == "default":
                reads.append(1)
            return super().__getitem__(key)

    value = {"a": 1, "b": (2, 3)} if budget == "nodes" else "x" * 2000
    item_schema = {"properties": {f"field_{i}": CountedDefault(default=value) for i in range(100)}}
    schema = {"properties": {"rows": {"items": item_schema}}}
    with pytest.raises(ValueError, match="limit|exceed"):
        _schema.defaults({"rows": tuple({} for _ in range(1000))}, schema)
    # A post-expansion freeze would read all 100,000 defaults before rejecting it.
    assert len(reads) < 1000


def test_expansion_budget_surfaces_through_the_public_validation_and_render_apis():
    bound = skill(
        parameters_schema={
            "properties": {
                "rows": {
                    "items": {
                        "properties": {f"field_{i}": {"default": {"a": 1, "b": [2, 3]}} for i in range(100)},
                    }
                }
            }
        }
    )
    config = {"rows": [{} for _ in range(1000)]}
    assert bound.validate_config(config)
    with pytest.raises(SkillConfigError):
        bound.render(config)
