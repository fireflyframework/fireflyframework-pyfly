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
"""Base BeanPostProcessor for wiring query methods onto repository beans.

Provides the shared iteration loop, stub detection, and derived-query
prefix matching used by both the SQLAlchemy and MongoDB adapters.
Adapter-specific behaviour is supplied via abstract hook methods.

**Which methods are implemented.** Every public method the repository's class declares, or inherits from an
intermediate base or a mixin (the class's MRO up to the framework's repository classes; the most derived
definition of a name wins): a ``@query`` method always, and a derived-query method (``find_by_``,
``count_by_``, ``exists_by_``, ``delete_by_``) when its body is a **stub**. A stub is recognized by the shape of
its body only (:func:`is_stub`): an optional docstring, then nothing else, ``...``, ``pass`` or ``raise
NotImplementedError`` (with or without a literal message). Any other body is a hand-written implementation and
is never replaced, however little it holds: a case-insensitive lookup, a join, a delegation to another method,
a hand-written soft delete.

**Arguments.** A compiled method takes its arguments as the stub declares them, by position or by keyword. The
number of value parameters must match what the name asks for (a ``_between`` takes two, an ``_is_null`` none),
or the repository fails to build with :class:`~pyfly.data.query_parser.InvalidQueryMethodError`; parameters
annotated ``Pageable`` or ``Sort`` are special and bind no value.
"""

from __future__ import annotations

import dis
import functools
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, get_type_hints

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.data.query_parser import InvalidQueryMethodError, ParsedQuery, QueryMethodParser, value_parameters

# Prefixes that indicate a derived query method.
DERIVED_PREFIXES = ("find_by_", "count_by_", "exists_by_", "delete_by_")

#: The order of every repository post-processor: ahead of the AOP post-processor (order 0), so that
#: advice wraps the compiled derived and ``@query`` methods instead of being replaced by them.
REPOSITORY_POST_PROCESSOR_ORDER = HIGHEST_PRECEDENCE + 100


# ---------------------------------------------------------------------------
# Stub detection
# ---------------------------------------------------------------------------

_SIGNIFICANT_ARGUMENTS = frozenset({"LOAD_CONST", "RETURN_CONST", "LOAD_SMALL_INT", "LOAD_GLOBAL", "LOAD_NAME"})
"""The instructions whose argument a body's shape keeps (a constant, a global's name); for any other, only the
instruction itself counts."""

_IGNORED = frozenset({"CACHE", "NOP", "EXTENDED_ARG"})

_Shape = tuple[tuple[str, Any], ...]


def _shape(code: Any) -> _Shape:
    """The instructions of *code*, with the arguments that tell a stub from a real body. A string constant
    counts as its type, so every ``raise NotImplementedError("...")`` has one shape."""
    shape: list[tuple[str, Any]] = []
    for instruction in dis.get_instructions(code):
        if instruction.opname in _IGNORED:
            continue
        argument = instruction.argval if instruction.opname in _SIGNIFICANT_ARGUMENTS else None
        if isinstance(argument, str) and instruction.opname != "LOAD_GLOBAL":
            argument = str
        shape.append((instruction.opname, argument))
    return tuple(shape)


# The reference stubs, compiled by the running interpreter: a body's shape is compared with theirs, so the
# detection does not depend on what a Python version compiles a stub to.
async def _async_empty(self: Any) -> Any: ...


async def _async_raises(self: Any) -> Any:
    raise NotImplementedError


async def _async_raises_instance(self: Any) -> Any:
    raise NotImplementedError()


async def _async_raises_message(self: Any) -> Any:
    raise NotImplementedError("stub")


def _sync_empty(self: Any) -> Any: ...


def _sync_raises(self: Any) -> Any:
    raise NotImplementedError


def _sync_raises_instance(self: Any) -> Any:
    raise NotImplementedError()


def _sync_raises_message(self: Any) -> Any:
    raise NotImplementedError("stub")


_ASYNC_STUBS: frozenset[_Shape] = frozenset(
    _shape(function.__code__)
    for function in (_async_empty, _async_raises, _async_raises_instance, _async_raises_message)
)
_SYNC_STUBS: frozenset[_Shape] = frozenset(
    _shape(function.__code__) for function in (_sync_empty, _sync_raises, _sync_raises_instance, _sync_raises_message)
)


def is_stub(method: Any) -> bool:
    """Whether *method*'s body is a stub for the post-processor to implement: after an optional docstring,
    nothing else, ``...``, ``pass``, or ``raise NotImplementedError`` (bare, called, or with a literal message).

    The body's shape decides, never its content: a body that calls anything, reads an attribute or returns a
    value is real, even without a single literal. Wrappers (``functools.wraps``, the repository's operation
    wrapper), ``staticmethod`` and ``classmethod`` are looked through; generators are never stubs.
    """
    function = method
    if isinstance(function, (staticmethod, classmethod)):
        function = function.__func__
    try:
        function = inspect.unwrap(function)
    except ValueError:  # a __wrapped__ cycle: not a function the post-processor can read
        return False
    code = getattr(function, "__code__", None)
    if code is None:
        return False
    if code.co_flags & (inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR):
        return False
    stubs = _ASYNC_STUBS if code.co_flags & inspect.CO_COROUTINE else _SYNC_STUBS
    return _shape(code) in stubs


def is_derived_query_name(name: str) -> bool:
    """Whether *name* starts with a derived-query prefix (:data:`DERIVED_PREFIXES`)."""
    return name.startswith(DERIVED_PREFIXES)


# ---------------------------------------------------------------------------
# Query methods
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryMethod:
    """A repository method the post-processor implements, as its stub declares it."""

    name: str
    """The method's name."""
    owner: type
    """The repository class the method is implemented for (the bean's class)."""
    function: Callable[..., Any]
    """The stub (unwrapped)."""
    signature: inspect.Signature
    """The stub's signature without ``self``, with its annotations resolved."""

    @property
    def qualified_name(self) -> str:
        """``Repository.method``, for messages."""
        return f"{self.owner.__name__}.{self.name}"

    @property
    def parameters(self) -> list[inspect.Parameter]:
        """The parameters after ``self``, in order."""
        return list(self.signature.parameters.values())

    @property
    def return_type(self) -> Any:
        """The resolved return annotation, or ``inspect.Signature.empty`` when the stub declares none."""
        return self.signature.return_annotation

    @property
    def is_variadic(self) -> bool:
        """Whether the stub takes ``*args``."""
        return any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in self.parameters)

    def bind(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        """The call's arguments by parameter name, defaults applied (``TypeError`` naming the method for a
        call the stub's signature refuses)."""
        try:
            bound = self.signature.bind(*args, **kwargs)
        except TypeError as error:
            raise TypeError(f"{self.qualified_name}(): {error}") from None
        bound.apply_defaults()
        return dict(bound.arguments)

    def arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[Any]:
        """The call's arguments in parameter order, keyword arguments included (``*args`` expanded,
        ``**kwargs`` left out)."""
        named = self.bind(args, kwargs)
        values: list[Any] = []
        for parameter in self.parameters:
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                values.extend(named.get(parameter.name, ()))
            elif parameter.kind is not inspect.Parameter.VAR_KEYWORD:
                values.append(named[parameter.name])
        return values


def resolved_annotations(
    function: Callable[..., Any], localns: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Exception]]:
    """*function*'s annotations evaluated as :func:`typing.get_type_hints` evaluates them, each on its own: the
    ones that resolve, by name (``"return"`` for the return annotation), and the error of each one that does not
    (a name imported only for type checking), so one unresolved annotation never hides the others."""
    try:
        return get_type_hints(function, localns=localns), {}
    except Exception:  # noqa: BLE001 — one annotation (at least) does not resolve: read them one by one
        pass
    globalns = getattr(function, "__globals__", {})
    hints: dict[str, Any] = {}
    unresolved: dict[str, Exception] = {}
    for name, annotation in getattr(function, "__annotations__", {}).items():
        try:
            hints.update(get_type_hints(SimpleNamespace(__annotations__={name: annotation}), globalns, localns))
        except Exception as error:  # noqa: BLE001 — any failure to evaluate an annotation
            unresolved[name] = error
    return hints, unresolved


def describe_method(
    owner: type, name: str, method: Any, entity: type | None = None, *, resolve: bool = True
) -> QueryMethod:
    """The :class:`QueryMethod` of *owner*'s method *name* (*method* is the class attribute): its unwrapped
    stub and its signature with the annotations resolved (the entity's name resolves too, for a module that
    imports it only for type checking). An annotation that does not resolve raises
    :class:`~pyfly.data.query_parser.InvalidQueryMethodError` naming it; with *resolve* false, the signature keeps
    the annotations that do not resolve as written instead (for a caller that needs only the parameters)."""
    function = method.__func__ if isinstance(method, (staticmethod, classmethod)) else method
    function = inspect.unwrap(function)
    localns = {entity.__name__: entity} if entity is not None else None
    hints, unresolved = resolved_annotations(function, localns)
    if unresolved and resolve:
        reasons = "; ".join(f"{key}: {type(error).__name__}: {error}" for key, error in unresolved.items())
        raise InvalidQueryMethodError(
            f"{owner.__name__}.{name}: its annotations do not resolve ({reasons}); import the types they name at "
            "runtime (a derived query's annotations decide its result and its Pageable or Sort parameter)"
        ) from next(iter(unresolved.values()))
    signature = inspect.signature(function)
    parameters = list(signature.parameters.values())
    if not isinstance(method, staticmethod) and parameters:
        parameters = parameters[1:]  # self (or cls)
    resolved = signature.replace(
        parameters=[
            parameter.replace(annotation=hints.get(parameter.name, parameter.annotation)) for parameter in parameters
        ],
        return_annotation=hints.get("return", inspect.Signature.empty),
    )
    return QueryMethod(name=name, owner=owner, function=function, signature=resolved)


def check_arguments(method: QueryMethod, parsed: ParsedQuery) -> None:
    """Refuse a stub whose value parameters do not match what its name asks for (a forgotten ``_and_id`` would
    drop an argument and widen a ``delete_by``; a missing one would fail on every call)."""
    if method.is_variadic:
        return
    declared = value_parameters(
        [parameter for parameter in method.parameters if parameter.kind is not inspect.Parameter.VAR_KEYWORD]
    )
    expected = parsed.argument_count
    if declared != expected:
        raise InvalidQueryMethodError(
            f"{method.qualified_name}: its name takes {expected} argument{'s' if expected != 1 else ''} "
            f"({_describe_arguments(parsed)}), and it declares {declared} value parameter"
            f"{'s' if declared != 1 else ''} (a Pageable or Sort parameter binds no value)"
        )


def _describe_arguments(parsed: ParsedQuery) -> str:
    parts = [
        f"{predicate.field_name} {predicate.operator}: {predicate.arguments}"
        for predicate in parsed.predicates
        if predicate.arguments
    ]
    return ", ".join(parts) or "none"


def declared_methods(cls: type, repository_type: type) -> Iterator[tuple[str, Any]]:
    """The public callables *cls* declares or inherits from its own bases and mixins, each name once (its most
    derived definition), in MRO order: everything but what the framework's repository classes define
    (*repository_type*, its bases, and the framework's subclasses of it, which set
    ``_pyfly_framework_repository``)."""
    framework = set(repository_type.__mro__)
    seen: set[str] = set()
    for klass in cls.__mro__:
        names = list(vars(klass))
        skipped = klass in framework or bool(vars(klass).get("_pyfly_framework_repository", False))
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if skipped or name.startswith("_"):
                continue
            attribute = getattr(cls, name, None)
            if attribute is not None and callable(attribute):
                yield name, attribute


def _positional(function: Callable[..., Any], method: QueryMethod) -> Callable[..., Any]:
    """*function* (``(self, *args)``) called with the arguments of a call bound to *method*'s signature, so the
    compiled method accepts them by position or by keyword."""

    @functools.wraps(function)
    async def bound(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
        return await function(self_arg, *method.arguments(args, kwargs))

    return bound


@order(REPOSITORY_POST_PROCESSOR_ORDER)
class BaseRepositoryPostProcessor(ABC):
    """Template base for repository bean post-processors.

    Subclasses implement the adapter-specific hooks while inheriting the
    shared iteration loop, stub detection, and ``before_init``.

    They run before every post-processor of the default order, the AOP one included
    (:data:`REPOSITORY_POST_PROCESSOR_ORDER`). The compiled queries are bound on the instance, so a
    post-processor that ran earlier and wrapped the stubs, as AOP weaving does, would lose its
    wrappers; ties used to be broken by the alphabetical order of the auto-configuration entry points,
    which put ``aop`` first and dropped every aspect on derived and ``@query`` methods.
    """

    def __init__(self) -> None:
        self._query_parser = QueryMethodParser()

    def before_init(self, bean: Any, bean_name: str) -> Any:
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        repo_type = self._get_repository_type()
        if not isinstance(bean, repo_type):
            return bean

        entity = bean._model  # type: ignore[attr-defined]
        cls = type(bean)

        # Collect names defined on the base repository class so we never
        # replace them.
        base_names = set(dir(repo_type))

        for attr_name, attr in declared_methods(cls, repo_type):
            # --- Adapter-specific decorated methods (e.g., @query) ---
            if self._process_query_decorated(bean, cls, attr_name, attr, entity):
                continue

            # --- Derived query methods ---
            if attr_name in base_names:
                continue

            if is_derived_query_name(attr_name) and self._is_stub(attr):
                method = describe_method(cls, attr_name, attr, entity)
                implementation = self._implement_derived(bean, method)
                setattr(bean, attr_name, implementation.__get__(bean, cls))

        return bean

    # ------------------------------------------------------------------
    # Derived query methods
    # ------------------------------------------------------------------

    def _properties(self, entity: Any) -> Collection[str] | None:
        """The property names a derived query name is parsed against (``None``: the name alone decides how it
        splits, with the original keyword set). Adapters that can list their entity's properties return them,
        so a typo fails at startup and the full keyword set applies."""
        return None

    def _parse(self, method: QueryMethod, entity: Any) -> ParsedQuery:
        """Parse *method*'s name (against the entity's properties when :meth:`_properties` lists them)."""
        try:
            return self._query_parser.parse(method.name, properties=self._properties(entity))
        except InvalidQueryMethodError as error:
            raise InvalidQueryMethodError(f"{method.owner.__name__}.{error}") from None

    def _implement_derived(self, bean: Any, method: QueryMethod) -> Callable[..., Any]:
        """The implementation of the derived-query stub *method* (an unbound ``(self, *args, **kwargs)``
        coroutine function).

        By default: the parsed name, its arguments checked against the stub's parameters, compiled with
        :meth:`_compile_derived` and wrapped with :meth:`_wrap_derived_method`, taking its arguments by
        position or by keyword. An adapter that needs more of the method overrides it.
        """
        entity = bean._model
        parsed = self._parse(method, entity)
        check_arguments(method, parsed)
        return_type = None if method.return_type is inspect.Signature.empty else method.return_type
        compiled_fn = self._compile_derived(parsed, entity, bean, return_type=return_type)
        return _positional(self._wrap_derived_method(compiled_fn), method)

    # ------------------------------------------------------------------
    # Abstract hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _get_repository_type(self) -> type:
        """Return the base repository class this post-processor targets."""
        ...

    @abstractmethod
    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        """Compile a parsed derived query method name into an executable callable."""
        ...

    @abstractmethod
    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a compiled derived-query function for binding onto the bean."""
        ...

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Process adapter-specific decorated methods (e.g., ``@query``).

        Return ``True`` if the attribute was handled, ``False`` otherwise.
        Default implementation does nothing — override in adapters that
        support decorator-based queries.
        """
        return False

    # ------------------------------------------------------------------
    # Stub detection
    # ------------------------------------------------------------------

    _is_stub = staticmethod(is_stub)
