# Copyright 2026 Firefly Software Foundation.
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
"""Beans data provider -- extracts bean metadata from ApplicationContext."""

from __future__ import annotations

import inspect
import types
import typing
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from pyfly.container.autowired import Autowired

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class BeansProvider:
    """Provides detailed bean information for the admin dashboard."""

    _MAX_DEPENDENCY_DEPTH = 10

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    # ------------------------------------------------------------------
    # Category inference
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_category(cls: type, stereotype: str) -> str:
        """Infer a human-friendly category when stereotype is 'none'."""
        if stereotype != "none":
            return stereotype

        if getattr(cls, "__pyfly_bean_method__", None):
            return "bean_method"

        name = cls.__name__
        suffixes: list[tuple[str, str]] = [
            ("AutoConfiguration", "auto_configuration"),
            ("Adapter", "adapter"),
            ("Provider", "provider"),
            ("Filter", "filter"),
            ("Middleware", "middleware"),
            ("Handler", "handler"),
            ("Factory", "factory"),
            ("Listener", "listener"),
        ]
        for suffix, category in suffixes:
            if name.endswith(suffix):
                return category

        return "component"

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    def _get_metrics_dict(self, cls: type) -> dict[str, Any]:
        """Return metrics fields for *cls*, with safe defaults."""
        metrics = self._context.container.get_bean_metrics(cls)
        if metrics is None:
            return {
                "creation_time_ms": None,
                "resolution_count": 0,
                "created_at": None,
            }
        return {
            "creation_time_ms": round(metrics.creation_time_ns / 1_000_000, 2),
            "resolution_count": metrics.resolution_count,
            "created_at": metrics.created_at,
        }

    # ------------------------------------------------------------------
    # Constructor dependency helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_constructor_hints(cls: type) -> dict[str, Any]:
        """Return resolved __init__ type-hint dict, excluding self/return."""
        try:
            if not hasattr(cls, "__init__") or cls.__init__ is object.__init__:  # type: ignore[misc]
                return {}
            hints = inspect.get_annotations(cls.__init__, eval_str=True)  # type: ignore[misc]
            return {k: v for k, v in hints.items() if k not in ("self", "return")}
        except Exception:
            return {}

    @staticmethod
    def _type_name(t: Any) -> str:
        return getattr(t, "__name__", str(t))

    def _build_deps_list(self, cls: type) -> list[dict[str, str]]:
        """Build the flat dependency list used by ``get_beans``."""
        return [
            {"name": name, "type": self._type_name(hint)} for name, hint in self._get_constructor_hints(cls).items()
        ]

    # ------------------------------------------------------------------
    # Recursive dependency chain
    # ------------------------------------------------------------------

    def _walk_dependency_chain(
        self,
        cls: type,
        *,
        depth: int = 0,
        visited: set[type] | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively walk constructor type hints up to ``_MAX_DEPENDENCY_DEPTH``."""
        if visited is None:
            visited = set()
        if depth >= self._MAX_DEPENDENCY_DEPTH or cls in visited:
            return []

        visited.add(cls)
        chain: list[dict[str, Any]] = []
        for param_name, param_type in self._get_constructor_hints(cls).items():
            if not isinstance(param_type, type):
                chain.append({"name": param_name, "type": self._type_name(param_type), "dependencies": []})
                continue
            children = self._walk_dependency_chain(param_type, depth=depth + 1, visited=visited)
            chain.append(
                {
                    "name": param_name,
                    "type": self._type_name(param_type),
                    "dependencies": children,
                }
            )
        return chain

    # ------------------------------------------------------------------
    # Condition helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_conditions(cls: type) -> list[dict[str, Any]]:
        """Return conditions with a ``passed`` flag.

        For ``on_class`` conditions, the embedded ``check`` callable is
        evaluated.  Other condition types are assumed to have passed (the
        bean is registered, so its startup conditions succeeded).
        """
        raw: list[dict[str, Any]] = getattr(cls, "__pyfly_conditions__", [])
        result: list[dict[str, Any]] = []
        for cond in raw:
            cond_type = cond.get("type", "unknown")
            if cond_type == "on_class":
                check = cond.get("check")
                passed = bool(check()) if callable(check) else True
            else:
                # The bean is registered, so all startup conditions passed.
                passed = True
            # Build a serialisable copy (skip the callable ``check`` key).
            entry: dict[str, Any] = {
                k: (v.__name__ if isinstance(v, type) else v) for k, v in cond.items() if k != "check"
            }
            entry["passed"] = passed
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Lifecycle & autowired helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_lifecycle_methods(cls: type) -> dict[str, list[str]]:
        """Scan *cls* for @post_construct / @pre_destroy methods."""
        post: list[str] = []
        pre: list[str] = []
        for name in dir(cls):
            try:
                attr = getattr(cls, name, None)
            except Exception:
                continue
            if callable(attr):
                if getattr(attr, "__pyfly_post_construct__", False):
                    post.append(name)
                if getattr(attr, "__pyfly_pre_destroy__", False):
                    pre.append(name)
        return {"post_construct": post, "pre_destroy": pre}

    @staticmethod
    def _find_autowired_fields(cls: type) -> list[dict[str, Any]]:
        """Return Autowired field descriptors from class attributes."""
        fields: list[dict[str, Any]] = []
        for attr_name in vars(cls):
            val = getattr(cls, attr_name, None)
            if isinstance(val, Autowired):
                fields.append(
                    {
                        "name": attr_name,
                        "qualifier": val.qualifier,
                        "required": val.required,
                    }
                )
        return fields

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_beans(self) -> dict[str, Any]:
        beans: list[dict[str, Any]] = []
        for cls, reg in self._context.container._registrations.items():
            bean_name = reg.name or cls.__name__
            stereotype = getattr(cls, "__pyfly_stereotype__", "none")
            conditions = getattr(cls, "__pyfly_conditions__", [])
            order = getattr(cls, "__pyfly_order__", None)
            profile = getattr(cls, "__pyfly_profile__", None)
            primary = getattr(cls, "__pyfly_primary__", False)

            deps = self._build_deps_list(cls)
            metrics = self._get_metrics_dict(cls)

            category = self._infer_category(cls, stereotype)

            beans.append(
                {
                    "name": bean_name,
                    "type": f"{cls.__module__}.{cls.__qualname__}",
                    "scope": reg.scope.name,
                    "stereotype": stereotype,
                    "category": category,
                    "primary": primary,
                    "order": order,
                    "profile": profile,
                    "conditions": [str(c) for c in conditions] if conditions else [],
                    "dependencies": deps,
                    "initialized": reg.instance is not None,
                    "creation_time_ms": metrics["creation_time_ms"],
                    "resolution_count": metrics["resolution_count"],
                    "created_at": metrics["created_at"],
                }
            )

        beans.sort(key=lambda b: (b["stereotype"], b["name"]))
        return {"beans": beans, "total": len(beans)}

    async def get_bean_detail(self, name: str) -> dict[str, Any] | None:
        for cls, reg in self._context.container._registrations.items():
            bean_name = reg.name or cls.__name__
            if bean_name == name:
                return await self._build_detail(cls, reg)
        return None

    @staticmethod
    def _extract_base_type(hint: Any) -> type | None:
        """Extract the concrete base type from a type hint.

        Handles ``Optional[T]``, ``list[T]``, ``Annotated[T, ...]``,
        and ``T | None`` (PEP 604) so we can find a matching registered bean.
        """
        if isinstance(hint, type):
            return hint

        origin = get_origin(hint)

        # Annotated[T, Qualifier(...)] → unwrap to T
        if origin is Annotated:
            args = get_args(hint)
            if args:
                return BeansProvider._extract_base_type(args[0])
            return None

        # Optional[T] / Union[T, None] / T | None
        if origin is Union or isinstance(hint, types.UnionType):
            args = get_args(hint)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return BeansProvider._extract_base_type(non_none[0])
            return None

        # list[T] → unwrap to T
        if origin is list:
            args = get_args(hint)
            if args:
                return BeansProvider._extract_base_type(args[0])
            return None

        return None

    def _find_matching_bean(self, target_type: type, registered_names: dict[type, str]) -> str | None:
        """Find a registered bean matching *target_type* (direct or subclass)."""
        if target_type in registered_names:
            return registered_names[target_type]
        for reg_cls, reg_name in registered_names.items():
            try:
                if issubclass(reg_cls, target_type):
                    return reg_name
            except TypeError:
                continue
        return None

    def _get_autowired_hints(self, cls: type) -> dict[str, Any]:
        """Return type hints for @Autowired fields on *cls*."""
        result: dict[str, Any] = {}
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except Exception:
            return result
        for attr_name, attr_type in hints.items():
            default = getattr(cls, attr_name, None)
            if isinstance(default, Autowired):
                result[attr_name] = attr_type
        return result

    async def get_bean_graph(self) -> dict[str, Any]:
        """Build a dependency graph of all registered beans.

        Returns ``{nodes, edges}`` where each node contains bean metadata
        and each edge represents a constructor or autowired dependency.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        registered_names: dict[type, str] = {}

        for cls, reg in self._context.container._registrations.items():
            bean_name = reg.name or cls.__name__
            registered_names[cls] = bean_name
            stereotype = getattr(cls, "__pyfly_stereotype__", "none")
            metrics = self._get_metrics_dict(cls)
            category = self._infer_category(cls, stereotype)
            nodes.append(
                {
                    "id": bean_name,
                    "name": bean_name,
                    "type": f"{cls.__module__}.{cls.__qualname__}",
                    "stereotype": stereotype,
                    "category": category,
                    "scope": reg.scope.name,
                    "initialized": reg.instance is not None,
                    "order": getattr(cls, "__pyfly_order__", None),
                    "resolution_count": metrics["resolution_count"],
                    "creation_time_ms": metrics["creation_time_ms"],
                }
            )

        # Build edges from constructor dependency hints
        for cls, reg in self._context.container._registrations.items():
            source = reg.name or cls.__name__
            for _param_name, param_type in self._get_constructor_hints(cls).items():
                base = self._extract_base_type(param_type)
                if base is None:
                    continue
                target = self._find_matching_bean(base, registered_names)
                if target and target != source:
                    edges.append({"source": source, "target": target, "type": "constructor"})

        # Build edges from @Autowired field injection
        for cls, reg in self._context.container._registrations.items():
            source = reg.name or cls.__name__
            for _field_name, field_type in self._get_autowired_hints(cls).items():
                base = self._extract_base_type(field_type)
                if base is None:
                    continue
                target = self._find_matching_bean(base, registered_names)
                if target and target != source:
                    edges.append({"source": source, "target": target, "type": "autowired"})

        # Deduplicate edges (same source-target pair)
        seen: set[tuple[str, str]] = set()
        unique_edges: list[dict[str, Any]] = []
        for edge in edges:
            key = (edge["source"], edge["target"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        return {"nodes": nodes, "edges": unique_edges}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_getfile(cls: type) -> str | None:
        try:
            return inspect.getfile(cls)
        except (TypeError, OSError):
            return None

    async def _build_detail(self, cls: type, reg: Any) -> dict[str, Any]:
        stereotype = getattr(cls, "__pyfly_stereotype__", "none")
        category = self._infer_category(cls, stereotype)
        lifecycle = self._find_lifecycle_methods(cls)
        metrics = self._get_metrics_dict(cls)

        return {
            "name": reg.name or cls.__name__,
            "type": f"{cls.__module__}.{cls.__qualname__}",
            "scope": reg.scope.name,
            "stereotype": stereotype,
            "category": category,
            "module": cls.__module__,
            "file": self._safe_getfile(cls),
            "doc": inspect.getdoc(cls) or "",
            "primary": getattr(cls, "__pyfly_primary__", False),
            "order": getattr(cls, "__pyfly_order__", None),
            "profile": getattr(cls, "__pyfly_profile__", None),
            "initialized": reg.instance is not None,
            # Enriched fields
            "dependency_chain": self._walk_dependency_chain(cls),
            "conditions": self._evaluate_conditions(cls),
            "bean_method_origin": getattr(cls, "__pyfly_bean_method__", None),
            "post_construct": lifecycle["post_construct"],
            "pre_destroy": lifecycle["pre_destroy"],
            "autowired_fields": self._find_autowired_fields(cls),
            "creation_time_ms": metrics["creation_time_ms"],
            "resolution_count": metrics["resolution_count"],
            "created_at": metrics["created_at"],
        }
