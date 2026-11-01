# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Frozen in-memory skill packages; no implicit installation or tool authorization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from pyfly.agentic.skills import _values
from pyfly.agentic.skills.skill import Skill, SkillParseError, SkillResource, index_skills


@dataclass(frozen=True)
class SkillPin:
    key: str
    version: str
    digest: str


@dataclass(frozen=True)
class SkillPackage:
    """A bounded snapshot and exact dependency graph over native skills.

    Resources are frozen at admission, before a worker or model sees the package.
    ``resolve`` checks host-provided requirements and returns dependency-first skills
    for the existing prompt and read-tool APIs. Package creation grants no capabilities.
    This value is not an archive format or a signature establishing publisher trust.
    """

    name: str
    version: str
    skills: tuple[Skill, ...]
    entrypoints: tuple[str, ...]
    pins: tuple[SkillPin, ...] = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or len(self.name) > 64
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.name)
        ):
            raise SkillParseError("package name must be a lowercase hyphenated name of at most 64 characters")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 128:
            raise SkillParseError("package version must be a non-empty string of at most 128 characters")
        indexed = index_skills(self.skills)
        if not indexed:
            raise SkillParseError("a package must contain skills")
        if not isinstance(self.entrypoints, (tuple, list)) or not 1 <= len(self.entrypoints) <= 128:
            raise SkillParseError("a package requires between 1 and 128 entrypoints")
        roots = tuple(self.entrypoints)
        if any(not isinstance(key, str) or key not in indexed for key in roots) or len(set(roots)) != len(roots):
            raise SkillParseError("package entrypoints must be unique member keys")
        self._ordered(indexed, tuple(sorted(indexed)))
        frozen: list[Skill] = []
        total = 0
        for key in sorted(indexed):
            original = indexed[key]
            resources = {}
            for path, resource in sorted(original.resources.items()):
                if not resource.available:
                    raise SkillParseError(f"package resource {key}/{path} is unavailable")
                content = resource.read()
                total += len(content.encode("utf-8"))
                if total > 8 * _values.MAX_RESOURCE_BYTES:
                    raise SkillParseError("package resources exceed 8 MiB in aggregate")
                resources[path] = SkillResource(
                    path=path,
                    kind=resource.kind,
                    description=resource.description,
                    content=content,
                    media_type=resource.media_type,
                )
            frozen.append(replace(original, resources=resources))
        object.__setattr__(self, "skills", tuple(frozen))
        object.__setattr__(self, "entrypoints", roots)
        object.__setattr__(self, "pins", tuple(SkillPin(s.key, s.version, s.digest()) for s in frozen))

    @staticmethod
    def _ordered(indexed: dict[str, Skill], roots: tuple[str, ...]) -> tuple[Skill, ...]:
        active: set[str] = set()
        done: set[str] = set()
        ordered: list[Skill] = []

        def visit(key: str) -> None:
            if key not in indexed:
                raise SkillParseError(f"missing package skill {key!r}")
            if key in active:
                raise SkillParseError(f"cyclic package skill dependency at {key!r}")
            if key in done:
                return
            active.add(key)
            for dependency in sorted(indexed[key].requires.skills):
                visit(dependency)
            active.remove(key)
            done.add(key)
            ordered.append(indexed[key])

        for root in roots:
            visit(root)
        return tuple(ordered)

    def resolve(
        self,
        entrypoints: tuple[str, ...] | None = None,
        *,
        tools: Iterable[str] = (),
        connectors: Iterable[str] = (),
    ) -> tuple[Skill, ...]:
        """Resolve the closure using capabilities already admitted by the host."""
        roots = self.entrypoints if entrypoints is None else entrypoints
        if (
            not isinstance(roots, (tuple, list))
            or not 1 <= len(roots) <= 128
            or any(not isinstance(key, str) for key in roots)
        ):
            raise SkillParseError("selection requires between 1 and 128 skill keys")
        bound = self._ordered(index_skills(self.skills), tuple(roots))
        have_tools, have_connectors = set(tools), set(connectors)
        for skill in bound:
            missing = skill.unmet(tools=have_tools, connectors=have_connectors, skills=(s.key for s in bound))
            if missing.any():
                raise SkillParseError(f"skill {skill.key!r} requires host capabilities: {missing}")
        return bound

    def digest(self) -> str:
        """Identity includes member metadata omitted from legacy native skill digests."""
        indexed = index_skills(self.skills)
        payload = {
            "format": "pyfly-skill-package-v1",
            "name": self.name,
            "version": self.version,
            "entrypoints": self.entrypoints,
            "skills": tuple(
                {
                    "key": pin.key,
                    "version": pin.version,
                    "digest": pin.digest,
                    "metadata": indexed[pin.key].metadata,
                    "resource_media_types": {
                        path: resource.media_type for path, resource in indexed[pin.key].resources.items()
                    },
                }
                for pin in self.pins
            ),
        }
        return hashlib.sha256(_values.canonical(payload).encode("utf-8")).hexdigest()
