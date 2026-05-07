# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""YAML DSL → :class:`RuleSet` value tree."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _field
from typing import Any


@dataclass
class Condition:
    """One condition node — either a leaf comparison or a logical compound.

    *field* is the path into the evaluation context (``order.amount``);
    *operator* is one of ``eq``, ``ne``, ``gt``, ``ge``, ``lt``, ``le``,
    ``in``, ``not_in``, ``regex`` (leaf) or ``and`` / ``or`` / ``not`` (compound).
    """

    operator: str
    field: str | None = None
    value: Any = None
    children: list[Condition] = _field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Condition:
        op = data.get("op") or data.get("operator")
        if op is None:
            msg = "condition missing 'op'"
            raise ValueError(msg)
        if op in {"and", "or", "not"}:
            children_raw = data.get("conditions") or data.get("children") or []
            return cls(
                operator=op,
                children=[cls.from_dict(c) for c in children_raw],
            )
        return cls(operator=op, field=data.get("field"), value=data.get("value"))


@dataclass
class Action:
    """One action node — set / increment / log / call / calculate."""

    type: str
    target: str | None = None
    value: Any = None
    expression: str | None = None
    arguments: dict[str, Any] = _field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        return cls(
            type=data["type"],
            target=data.get("target"),
            value=data.get("value"),
            expression=data.get("expression"),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass
class Rule:
    id: str
    description: str = ""
    when: Condition | None = None
    then: list[Action] = _field(default_factory=list)
    otherwise: list[Action] = _field(default_factory=list)
    priority: int = 0
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        return cls(
            id=data["id"],
            description=data.get("description", ""),
            when=Condition.from_dict(data["when"]) if data.get("when") else None,
            then=[Action.from_dict(a) for a in data.get("then", [])],
            otherwise=[Action.from_dict(a) for a in data.get("otherwise", [])],
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class RuleSet:
    id: str
    name: str = ""
    version: int = 1
    rules: list[Rule] = _field(default_factory=list)

    def sorted_rules(self) -> list[Rule]:
        return sorted(self.rules, key=lambda r: -r.priority)


class RuleSetLoader:
    """Parse YAML / dict to a :class:`RuleSet`."""

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RuleSet:
        return RuleSet(
            id=data["id"],
            name=data.get("name", ""),
            version=int(data.get("version", 1)),
            rules=[Rule.from_dict(r) for r in data.get("rules", [])],
        )

    @staticmethod
    def from_yaml(text: str) -> RuleSet:
        import yaml

        return RuleSetLoader.from_dict(yaml.safe_load(text))
