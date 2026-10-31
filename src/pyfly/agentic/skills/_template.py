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

"""Interpret a bounded interpolation subset; never compile or execute Jinja templates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from jinja2 import Environment, TemplateError, nodes

from pyfly.agentic.skills._values import MAX_TEXT, canonical, text

_parser = Environment()
_MISSING = object()
_FILTERS = {"default", "upper", "lower", "title", "trim", "join"}


def _string(value: Any) -> str:
    if value is _MISSING:
        raise ValueError("undefined config value")
    return text(canonical(value) if isinstance(value, (Mapping, tuple)) else str(value))


def render(body: str, config: Mapping[str, Any]) -> str:
    try:
        tree = _parser.parse(body)
        pending: list[tuple[nodes.Node, int]] = [(tree, 0)]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 1024 or depth > 32:
                raise ValueError("template exceeds 1024 nodes or depth 32")
            if type(node) not in (
                nodes.Template,
                nodes.Output,
                nodes.TemplateData,
                nodes.Name,
                nodes.Getattr,
                nodes.Getitem,
                nodes.Const,
                nodes.Filter,
            ):
                raise ValueError(f"unsupported template syntax: {type(node).__name__}")
            if isinstance(node, nodes.Filter) and (
                node.name not in _FILTERS
                or node.kwargs
                or node.dyn_args
                or node.dyn_kwargs
                or any(not isinstance(arg, nodes.Const) for arg in node.args)
            ):
                raise ValueError("unsupported template filter or arguments")
            if isinstance(node, nodes.Getitem) and (
                not isinstance(node.arg, nodes.Const) or type(node.arg.value) not in (str, int)
            ):
                raise ValueError("config indexes must be literal strings or integers")
            pending.extend((child, depth + 1) for child in node.iter_child_nodes())

        def evaluate(node: nodes.Node) -> Any:
            if isinstance(node, nodes.Const):
                return node.value
            if isinstance(node, nodes.Name):
                return config.get(node.name, _MISSING)
            if isinstance(node, (nodes.Getattr, nodes.Getitem)):
                parent = evaluate(node.node)
                key = node.attr if isinstance(node, nodes.Getattr) else cast(nodes.Const, node.arg).value
                if isinstance(parent, Mapping) and isinstance(key, str):
                    return parent.get(key, _MISSING)
                if isinstance(parent, tuple) and type(key) is int and -len(parent) <= key < len(parent):
                    return parent[key]
                return _MISSING
            if isinstance(node, nodes.Filter):
                assert node.node is not None
                value = evaluate(node.node)
                args = [cast(nodes.Const, arg).value for arg in node.args]
                if node.name == "default":
                    if len(args) > 2 or (len(args) == 2 and type(args[1]) is not bool):
                        raise ValueError("default accepts a literal fallback and optional boolean")
                    if value is _MISSING or (len(args) == 2 and args[1] and not value):
                        return args[0] if args else ""
                    return value
                if node.name == "join":
                    if len(args) > 1 or (args and not isinstance(args[0], str)) or not isinstance(value, tuple):
                        raise ValueError("join accepts an array and a literal separator")
                    separator = args[0] if args else ""
                    parts = [_string(item) for item in value]
                    if sum(map(len, parts)) + max(0, len(parts) - 1) * len(separator) > MAX_TEXT:
                        raise ValueError("rendered body exceeds limit")
                    return separator.join(parts)
                if args:
                    raise ValueError(f"{node.name} does not accept arguments")
                value = _string(value)
                operation: dict[str, Callable[[str], str]] = {
                    "upper": str.upper,
                    "lower": str.lower,
                    "title": str.title,
                    "trim": str.strip,
                }
                return text(operation[node.name](value))
            raise ValueError("unsupported expression")

        parts: list[str] = []
        length = 0
        for output in tree.body:
            assert isinstance(output, nodes.Output)
            for node in output.nodes:
                part = node.data if isinstance(node, nodes.TemplateData) else _string(evaluate(node))
                length += len(part)
                if length > MAX_TEXT:
                    raise ValueError(f"rendered body exceeds {MAX_TEXT} characters")
                parts.append(part)
        return "".join(parts).strip()
    except (TemplateError, RecursionError) as exc:
        raise ValueError("invalid or excessively nested template") from exc
