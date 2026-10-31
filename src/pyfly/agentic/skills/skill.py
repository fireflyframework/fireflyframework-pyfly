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

"""``Skill`` — a unit of know-how an agent carries, in the Agent Skills shape.

A skill is three things, and the shape matters because each goes to a different place:

* **frontmatter** — key, name, description, when to use, version, requirements, a config
  schema, examples, the resource index. Metadata a host stores, versions and shows.
* **body** — markdown instructions, bounded interpolation over the skill's config. This is what
  reaches the system prompt, once per skill, rendered.
* **resources** — reference documents and templates the body points at by path. These do NOT
  reach the prompt: they are read on demand through the ``read_skill`` tool
  (:func:`~pyfly.agentic.skills.tool.build_read_skill_tool`), so an agent carrying
  twelve skills carries twelve bodies and no appendices, and pays for a checklist only on the
  turn it reads it. That is progressive disclosure, and it is the reason a skill is not just a
  longer prompt.

Skills are frozen values. Binding a config (:meth:`Skill.with_config`) or attaching loaded
resources (:meth:`Skill.with_resources`) returns a new skill; :meth:`Skill.digest` names one
exactly, so a host can prove that the prompt a published worker carries has not changed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from pyfly.agentic.skills import _schema, _template, _values

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

ResourceKind = Literal["reference", "template", "script"]


class SkillParseError(ValueError):
    """The SKILL.md (or the frontmatter given in code) is not a skill."""


class SkillConfigError(ValueError):
    """A config does not satisfy the skill's ``parameters_schema``.

    Carries every problem, so an editor can show them all at once.
    """

    def __init__(self, key: str, problems: list[str]) -> None:
        self.key = key
        self.problems = list(problems)
        super().__init__(f"skill {key!r}: config invalid: " + "; ".join(problems))


def _frozen(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    try:
        return _values.mapping(value)
    except ValueError as exc:
        raise SkillParseError(str(exc)) from exc


def validate_resource_path(path: str) -> str:
    """A resource path is relative, POSIX, inside the skill, and not empty.

    A skill's resources are the files it declares, not the directory it lives in: ``..`` and an
    absolute path would turn ``read_skill`` into a file share.
    """
    if not isinstance(path, str) or not path.strip():
        raise SkillParseError("a resource path must be a non-empty string")
    if len(path) > 4096:
        raise SkillParseError("resource paths have at most 4096 characters")
    normalised = posixpath.normpath(path.strip().replace("\\", "/"))
    if normalised.startswith(("/", "../")) or normalised == ".." or normalised == "." or "\x00" in normalised:
        raise SkillParseError(f"resource path {path!r} escapes the skill; paths are relative and stay inside it")
    if len(normalised.split("/")) > 16:
        raise SkillParseError("resource paths have at most 16 components")
    return normalised


@dataclass(frozen=True)
class SkillResource:
    """One file a skill points at: a reference, a template, or a script.

    Content is inline (``content``) or lazy (``loader``); :meth:`read` returns it and caches a
    loaded value. ``kind`` is advisory — a host decides whether a script may run; the framework
    only ever READS resources.
    """

    path: str
    kind: ResourceKind = "reference"
    description: str = ""
    content: str | None = None
    loader: Callable[[], str] | None = field(default=None, repr=False, compare=False)
    media_type: str = "text/markdown"
    _cache: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_resource_path(self.path))
        try:
            _values.text(self.description)
            _values.text(self.media_type)
            if self.kind not in ("reference", "template", "script"):
                raise ValueError("unknown resource kind")
            if self.content is not None:
                _values.resource_text(self.content)
        except ValueError as exc:
            raise SkillParseError(str(exc)) from exc

    def read(self) -> str:
        """The resource text."""
        if self.content is not None:
            return self.content
        if "text" in self._cache:
            return self._cache["text"]
        if self.loader is None:
            raise LookupError(f"resource {self.path!r} has no content and no loader")
        try:
            text = _values.resource_text(self.loader())
        except ValueError as exc:
            raise SkillParseError(str(exc)) from exc
        self._cache["text"] = text
        return text

    @property
    def available(self) -> bool:
        return self.content is not None or self.loader is not None


@dataclass(frozen=True)
class SkillRequirements:
    """What a skill needs from its host to be usable: tool names, connector kinds, other skills."""

    tools: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("tools", "connectors", "skills"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or len(values) > _values.MAX_COLLECTION:
                raise SkillParseError(f"requires.{name} must contain at most 128 names")
            try:
                for value in values:
                    if not _values.text(value, 256):
                        raise ValueError("requirement names must be non-empty")
            except ValueError as exc:
                raise SkillParseError(str(exc)) from exc
            object.__setattr__(self, name, tuple(values))

    def any(self) -> bool:
        return bool(self.tools or self.connectors or self.skills)

    @classmethod
    def from_mapping(cls, raw: Any) -> SkillRequirements:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise SkillParseError("requires must be a mapping with tools / connectors / skills lists")
        unknown = set(raw) - {"tools", "connectors", "skills"}
        if unknown:
            raise SkillParseError(f"requires has unknown keys {sorted(unknown)}")

        def names(field_name: str) -> tuple[str, ...]:
            value = raw.get(field_name) or ()
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v for v in value):
                raise SkillParseError(f"requires.{field_name} must be a list of names")
            return tuple(value)

        return cls(tools=names("tools"), connectors=names("connectors"), skills=names("skills"))


@dataclass(frozen=True)
class Skill:
    """See the module docstring. Build one with :func:`parse_skill` / :func:`load_skill`, or in code."""

    key: str
    name: str
    description: str
    instructions: str = ""
    when_to_use: str = ""
    version: str = "1"
    examples: tuple[str, ...] = ()
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    resources: Mapping[str, SkillResource] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not KEY_PATTERN.match(self.key):
            raise SkillParseError(f"skill key {self.key!r} must match {KEY_PATTERN.pattern}")
        if not self.name or not isinstance(self.name, str):
            raise SkillParseError(f"skill {self.key!r} needs a name")
        if not self.description or not isinstance(self.description, str):
            raise SkillParseError(f"skill {self.key!r} needs a description")
        try:
            for value in (
                self.key,
                self.name,
                self.description,
                self.instructions,
                self.when_to_use,
                str(self.version),
            ):
                _values.text(value)
            if len(self.examples) > _values.MAX_COLLECTION or len(self.resources) > _values.MAX_COLLECTION:
                raise ValueError("a skill has at most 128 examples and resources")
            for value in self.examples:
                _values.text(value)
        except ValueError as exc:
            raise SkillParseError(str(exc)) from exc
        object.__setattr__(self, "examples", tuple(self.examples))
        object.__setattr__(self, "parameters_schema", _frozen(self.parameters_schema))
        object.__setattr__(self, "config", _frozen(self.config))
        object.__setattr__(self, "metadata", _frozen(self.metadata))
        try:
            _schema.check_schema(self.parameters_schema)
        except ValueError as exc:
            raise SkillParseError(str(exc)) from exc
        resources = {}
        for path, resource in dict(self.resources).items():
            normalised = validate_resource_path(path)
            if resource.path != normalised:
                raise SkillParseError(f"resource {path!r} is registered under a different path {resource.path!r}")
            if normalised in resources:
                raise SkillParseError(f"duplicate resource path {normalised!r}")
            resources[normalised] = resource
        object.__setattr__(self, "resources", MappingProxyType(resources))
        object.__setattr__(self, "version", str(self.version))

    # -- values --------------------------------------------------------------

    def with_config(self, config: Mapping[str, Any] | None) -> Skill:
        """The same skill bound to ``config`` (validated on render)."""
        return _replace(self, config=_frozen(config))

    def with_resources(self, resources: Mapping[str, SkillResource]) -> Skill:
        """The same skill with these resources attached.

        The index is REPLACED — a resource the frontmatter declared and this call omits is gone —
        but a resource the frontmatter declared keeps its declared kind and description when the
        attached object carries none: the frontmatter is where an author describes a resource,
        and a loader that only knows the bytes should not erase that.
        """
        attached: dict[str, SkillResource] = {}
        for path, resource in resources.items():
            declared = self.resources.get(validate_resource_path(path))
            if declared is not None:
                resource = SkillResource(
                    path=resource.path,
                    kind=declared.kind if resource.kind == "reference" else resource.kind,
                    description=resource.description or declared.description,
                    content=resource.content,
                    loader=resource.loader,
                    media_type=resource.media_type if resource.media_type != "text/markdown" else declared.media_type,
                )
            attached[path] = resource
        return _replace(self, resources=MappingProxyType(attached))

    def digest(self) -> str:
        """SHA-256 over everything the model can see: frontmatter, body, config, resource texts.

        A host stores it on a published worker to prove later that editing the skill did not
        change what the worker runs with.
        """
        resources = {}
        total = 0
        for path, resource in sorted(self.resources.items()):
            text = resource.read() if resource.available else None
            total += len(text.encode("utf-8")) if text is not None else 0
            if total > 8 * _values.MAX_RESOURCE_BYTES:
                raise SkillParseError("skill resources exceed 8 MiB in aggregate")
            resources[path] = {"kind": resource.kind, "description": resource.description, "text": text}
        payload = {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "version": self.version,
            "instructions": self.instructions,
            "examples": list(self.examples),
            "parameters_schema": dict(self.parameters_schema),
            "requires": {
                "tools": self.requires.tools,
                "connectors": self.requires.connectors,
                "skills": self.requires.skills,
            },
            "config": dict(self.config),
            "resources": resources,
        }
        return hashlib.sha256(_values.canonical(payload).encode("utf-8")).hexdigest()

    # -- config --------------------------------------------------------------

    def validate_config(self, config: Mapping[str, Any] | None = None) -> list[str]:
        """Every way ``config`` (default: the bound config) fails ``parameters_schema``; empty when valid.

        The recursive subset supports JSON types, required/properties, array items, enum,
        numeric/length/item bounds, defaults and boolean additionalProperties. Unsupported
        schema keywords are refused when the skill is constructed. At most 128 problems.
        """
        try:
            values = self._config_with_defaults(config)
        except SkillParseError as exc:
            return [str(exc)]
        return _schema.validate(values, self.parameters_schema)

    def _config_with_defaults(self, config: Mapping[str, Any] | None) -> Mapping[str, Any]:
        values = _frozen(self.config if config is None else config)
        try:
            return _frozen(_schema.defaults(values, self.parameters_schema))
        except ValueError as exc:
            raise SkillParseError(str(exc)) from exc

    # -- requirements --------------------------------------------------------

    def unmet(
        self,
        *,
        tools: Iterable[str] = (),
        connectors: Iterable[str] = (),
        skills: Iterable[str] = (),
    ) -> SkillRequirements:
        """The requirements the host does not satisfy."""
        have_tools, have_connectors, have_skills = set(tools), set(connectors), set(skills)
        return SkillRequirements(
            tools=tuple(t for t in self.requires.tools if t not in have_tools),
            connectors=tuple(c for c in self.requires.connectors if c not in have_connectors),
            skills=tuple(s for s in self.requires.skills if s not in have_skills),
        )

    # -- rendering -----------------------------------------------------------

    def render(self, config: Mapping[str, Any] | None = None) -> str:
        """The body with ``config`` (default: the bound config, plus schema defaults) substituted.

        Raises :class:`SkillConfigError` when the config fails the schema — a body rendered
        from a wrong config is worse than no body, because the model would follow it.
        """
        try:
            values = self._config_with_defaults(config)
        except SkillParseError as exc:
            raise SkillConfigError(self.key, [str(exc)]) from exc
        problems = _schema.validate(values, self.parameters_schema)
        if problems:
            raise SkillConfigError(self.key, problems)
        try:
            return _template.render(self.instructions, values)
        except ValueError as exc:
            raise SkillConfigError(self.key, [f"body could not be rendered: {exc}"]) from exc


def _replace(skill: Skill, **changes: Any) -> Skill:
    return dataclasses.replace(skill, **changes)


def index_skills(skills: Iterable[Skill]) -> dict[str, Skill]:
    """One bounded index shared by prompt and tool assembly; ambiguity fails closed."""
    result: dict[str, Skill] = {}
    for skill in skills:
        if skill.key in result:
            raise SkillParseError(f"duplicate skill key {skill.key!r}")
        if len(result) >= _values.MAX_COLLECTION:
            raise SkillParseError("at most 128 skills may be assembled")
        result[skill.key] = skill
    return result
