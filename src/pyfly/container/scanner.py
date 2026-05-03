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
"""Package scanner for auto-discovering stereotype-decorated classes."""

from __future__ import annotations

import abc
import importlib
import inspect
import pkgutil
import types
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pyfly.container.container import Container


def scan_package(package_name: str, container: Container) -> int:
    """Scan a package for stereotype-decorated classes and register them.

    Args:
        package_name: Dotted package name to scan (e.g. "myapp.services").
        container: Container to register discovered classes into.

    Returns:
        Number of classes registered.
    """
    count = 0
    module = importlib.import_module(package_name)

    # Register stereotype-decorated classes from this module
    count += _register_from_module(module, container)

    # If it's a package, scan submodules
    if hasattr(module, "__path__"):
        for _importer, modname, _ispkg in pkgutil.walk_packages(module.__path__, prefix=module.__name__ + "."):
            try:
                submodule = importlib.import_module(modname)
                count += _register_from_module(submodule, container)
            except ImportError:
                continue

    return count


def scan_module_classes(module: types.ModuleType) -> list[type]:
    """Extract all stereotype-decorated classes from a module.

    Returns a list of classes that have ``__pyfly_injectable__ = True``.
    """
    classes: list[type] = []
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if getattr(obj, "__pyfly_injectable__", False) and obj.__module__ == module.__name__:
            classes.append(obj)
    return classes


def _register_from_module(module: types.ModuleType, container: Container) -> int:
    """Register all stereotype-decorated classes from a module."""
    from pyfly.container.types import Scope

    classes = scan_module_classes(module)
    for obj in classes:
        scope = getattr(obj, "__pyfly_scope__", None)
        condition = getattr(obj, "__pyfly_condition__", None)
        name = getattr(obj, "__pyfly_bean_name__", "")
        container.register(
            obj,
            scope=scope or Scope.SINGLETON,
            condition=condition,
            name=name,
        )
        _auto_bind_interfaces(obj, container)
    return len(classes)


def _auto_bind_interfaces(cls: type, container: Container) -> None:
    """Auto-bind a class to its Protocol, ABC, and base class interfaces."""
    for base in inspect.getmro(cls)[1:]:
        if base is object:
            continue
        if _is_protocol(base) or inspect.isabstract(base) or _is_port(base):
            container.bind(base, cls)


def _is_protocol(cls: type) -> bool:
    """Check if a class is a runtime-checkable Protocol."""
    return bool(getattr(cls, "_is_protocol", False)) and cls is not Protocol  # type: ignore[comparison-overlap]


def _is_port(cls: type) -> bool:
    """Check if a class lives in a 'ports' package (PyFly convention)."""
    module = getattr(cls, "__module__", "") or ""
    if ".ports" in module or module.endswith(".ports"):
        return True
    # Also treat abstract base classes (with abc.ABC in MRO) as ports
    return abc.ABC in getattr(cls, "__mro__", ())
