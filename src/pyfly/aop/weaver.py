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
"""AOP weaver — wraps bean methods with matching advice chains."""

from __future__ import annotations

import functools
import inspect
from typing import Any

from pyfly.aop.registry import AspectRegistry
from pyfly.aop.types import JoinPoint


def weave_bean(bean: Any, qualified_prefix: str, registry: AspectRegistry) -> None:
    """Weave advice into public methods of *bean*.

    For each public method (name not starting with ``_``), build a qualified
    name ``f"{qualified_prefix}.{method_name}"`` and ask the *registry* for
    matching bindings.  If any bindings match, replace the method on the
    instance with a wrapper that executes the advice chain.

    Async methods support all advice types including ``@around``.
    Sync methods support ``@before``, ``@after_returning``, ``@after_throwing``,
    and ``@after`` (no ``@around``).
    """
    for attr_name in dir(bean):
        if attr_name.startswith("_"):
            continue

        # Look the attribute up statically on the class FIRST so we never trigger
        # descriptor ``__get__`` side effects. A plain ``getattr`` here would
        # evaluate @property / cached_property getters during weaving — a
        # side-effecting or raising property would then abort application startup.
        static_attr = inspect.getattr_static(bean, attr_name, None)
        if isinstance(static_attr, (property, functools.cached_property)):
            continue

        attr = getattr(bean, attr_name, None)
        if attr is None or not callable(attr):
            continue

        qualified_name = f"{qualified_prefix}.{attr_name}"
        bindings = registry.get_matching(qualified_name)
        if not bindings:
            continue

        # Group bindings by advice type
        before_bindings = [b for b in bindings if b.advice_type == "before"]
        after_returning_bindings = [b for b in bindings if b.advice_type == "after_returning"]
        after_throwing_bindings = [b for b in bindings if b.advice_type == "after_throwing"]
        after_bindings = [b for b in bindings if b.advice_type == "after"]
        around_bindings = [b for b in bindings if b.advice_type == "around"]

        original = attr

        if inspect.iscoroutinefunction(original):
            wrapper = _build_async_wrapper(
                bean,
                attr_name,
                original,
                before_bindings,
                after_returning_bindings,
                after_throwing_bindings,
                after_bindings,
                around_bindings,
            )
        else:
            wrapper = _build_sync_wrapper(
                bean,
                attr_name,
                original,
                before_bindings,
                after_returning_bindings,
                after_throwing_bindings,
                after_bindings,
                around_bindings,
            )

        # Replace the method on the instance
        setattr(bean, attr_name, wrapper)


def _build_async_wrapper(
    bean: Any,
    method_name: str,
    original: Any,
    before_bindings: list[Any],
    after_returning_bindings: list[Any],
    after_throwing_bindings: list[Any],
    after_bindings: list[Any],
    around_bindings: list[Any],
) -> Any:
    """Build an async wrapper that applies the advice chain."""

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        jp = JoinPoint(
            target=bean,
            method_name=method_name,
            args=args,
            kwargs=kwargs,
        )

        # 1. Run @before advice (sync)
        for binding in before_bindings:
            binding.handler(jp)

        try:
            # 2. Execute the method (with or without @around)
            if around_bindings:
                # Build a chain: last around calls original, each prior around calls next
                async def _invoke_original(*_a: Any, **_kw: Any) -> Any:
                    return await original(*args, **kwargs)

                # Build proceed chain in reverse so first around is outermost
                proceed_fn = _invoke_original
                for binding in reversed(around_bindings):
                    proceed_fn = _make_around_link(binding.handler, jp, proceed_fn)

                result = await proceed_fn()
            else:
                result = await original(*args, **kwargs)

            # 3. On success: set return_value, run @after_returning
            jp.return_value = result
            for binding in after_returning_bindings:
                binding.handler(jp)

            return result

        except Exception as exc:
            # 4. On exception: set exception, run @after_throwing, re-raise
            jp.exception = exc
            for binding in after_throwing_bindings:
                binding.handler(jp)
            raise

        finally:
            # 5. Run @after (always)
            for binding in after_bindings:
                binding.handler(jp)

    return wrapper


def _make_around_link(handler: Any, jp: JoinPoint, next_proceed: Any) -> Any:
    """Build one link in the around advice chain.

    Returns an async callable that sets ``jp.proceed`` to *next_proceed*
    and then invokes *handler*.
    """

    async def chained(*_a: Any, **_kw: Any) -> Any:
        jp.proceed = next_proceed
        result = handler(jp)
        if inspect.isawaitable(result):
            result = await result
        return result

    return chained


def _make_sync_around_link(handler: Any, jp: JoinPoint, next_proceed: Any) -> Any:
    """Build one link in a synchronous around advice chain.

    Returns a sync callable that sets ``jp.proceed`` to *next_proceed* and then
    invokes *handler*. The handler is expected to be synchronous; if it returns
    an awaitable the value is passed through unchanged (the sync path cannot
    await), so users must keep @around advice on sync joinpoints synchronous.
    """

    def chained(*_a: Any, **_kw: Any) -> Any:
        jp.proceed = next_proceed
        return handler(jp)

    return chained


def _build_sync_wrapper(
    bean: Any,
    method_name: str,
    original: Any,
    before_bindings: list[Any],
    after_returning_bindings: list[Any],
    after_throwing_bindings: list[Any],
    after_bindings: list[Any],
    around_bindings: list[Any],
) -> Any:
    """Build a sync wrapper that applies the advice chain (including @around)."""

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        jp = JoinPoint(
            target=bean,
            method_name=method_name,
            args=args,
            kwargs=kwargs,
        )

        # 1. Run @before advice (sync)
        for binding in before_bindings:
            binding.handler(jp)

        try:
            # 2. Execute the method (with or without @around)
            if around_bindings:

                def _invoke_original(*_a: Any, **_kw: Any) -> Any:
                    return original(*args, **kwargs)

                proceed_fn = _invoke_original
                for binding in reversed(around_bindings):
                    proceed_fn = _make_sync_around_link(binding.handler, jp, proceed_fn)

                result = proceed_fn()
            else:
                result = original(*args, **kwargs)

            # 3. On success: set return_value, run @after_returning
            jp.return_value = result
            for binding in after_returning_bindings:
                binding.handler(jp)

            return result

        except Exception as exc:
            # 4. On exception: set exception, run @after_throwing, re-raise
            jp.exception = exc
            for binding in after_throwing_bindings:
                binding.handler(jp)
            raise

        finally:
            # 5. Run @after (always)
            for binding in after_bindings:
                binding.handler(jp)

    return wrapper
