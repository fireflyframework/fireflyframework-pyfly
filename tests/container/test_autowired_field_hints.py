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
"""Field injection reads only the annotations of the fields it injects.

Field injection used to resolve every annotation of every class it built, third-party classes
included. One annotation that could not be resolved anywhere in the MRO (a ``TYPE_CHECKING``-only
import, SQLAlchemy's own ``dispatch: dispatcher[Session]`` on ``AsyncSession``) made it log one
WARNING and skip every ``Autowired``/``Value`` field of the class:

- each transient ``AsyncSession`` the container built logged that WARNING (one per repository at
  boot, one per ``get_bean(AsyncSession)``), which buried the real wiring warnings (C164);
- a user class with such an annotation silently kept the ``Autowired`` sentinel in its required
  fields, so the context booted healthy and the first call failed with ``AttributeError``.

A class without ``Autowired``/``Value`` fields is now left alone, the annotation of each field is
resolved on its own, and a required field whose annotation cannot be resolved fails the creation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import pytest

from pyfly.container import Autowired, BeanCreationException, Container, NoSuchBeanError, Qualifier
from pyfly.core.config import Config
from pyfly.core.value import Value

if TYPE_CHECKING:
    from decimal import Decimal as _OnlyForTypeChecking


class Greeter:
    def greet(self) -> str:
        return "hello"


class _ServiceWithForeignAnnotation:
    """An ``Autowired`` field next to an attribute annotated with a ``TYPE_CHECKING``-only name."""

    greeter: Greeter = Autowired()
    port: str = Value("${app.port:8080}")
    note: _OnlyForTypeChecking | None = None


class _Base:
    limit: _OnlyForTypeChecking | None = None


class _SubclassOfForeignAnnotation(_Base):
    greeter: Greeter = Autowired()


class _RequiredFieldWithUnresolvableHint:
    amount: _OnlyForTypeChecking = Autowired()


class _OptionalFieldWithUnresolvableHint:
    amount: _OnlyForTypeChecking = Autowired(required=False)


class _OptionalAnnotatedField:
    greeter: Annotated[Greeter, Qualifier("absent")] = Autowired(required=False)


class _OptionalGenericField:
    greeters: list[Greeter] | None = Autowired(required=False)


def _container() -> Container:
    container = Container()
    container.register_instance(Config, Config({}))
    container.register(Greeter)
    return container


def test_autowired_field_is_injected_next_to_an_unresolvable_annotation() -> None:
    container = _container()
    container.register(_ServiceWithForeignAnnotation)

    service = container.resolve(_ServiceWithForeignAnnotation)

    assert isinstance(service.greeter, Greeter)
    assert service.port == "8080"


def test_an_unresolvable_annotation_on_a_base_class_does_not_disable_injection() -> None:
    container = _container()
    container.register(_SubclassOfForeignAnnotation)

    assert isinstance(container.resolve(_SubclassOfForeignAnnotation).greeter, Greeter)


def test_required_field_with_an_unresolvable_annotation_fails_the_creation() -> None:
    container = _container()
    container.register(_RequiredFieldWithUnresolvableHint)

    with pytest.raises(BeanCreationException) as raised:
        container.resolve(_RequiredFieldWithUnresolvableHint)

    assert not isinstance(raised.value, NoSuchBeanError)  # an Optional parameter must not swallow it
    assert "_RequiredFieldWithUnresolvableHint.amount" in str(raised.value)


def test_optional_field_with_an_unresolvable_annotation_is_left_unset(caplog: pytest.LogCaptureFixture) -> None:
    container = _container()
    container.register(_OptionalFieldWithUnresolvableHint)

    with caplog.at_level(logging.WARNING, logger="pyfly.container.container"):
        service = container.resolve(_OptionalFieldWithUnresolvableHint)

    assert service.amount is None
    assert "_OptionalFieldWithUnresolvableHint.amount" in caplog.text


def test_optional_annotated_field_without_a_candidate_is_left_unset() -> None:
    container = _container()
    container.register(_OptionalAnnotatedField)

    assert container.resolve(_OptionalAnnotatedField).greeter is None


def test_optional_generic_field_receives_every_bean() -> None:
    container = _container()
    container.register(_OptionalGenericField)

    greeters = container.resolve(_OptionalGenericField).greeters
    assert greeters is not None
    assert [type(greeter) for greeter in greeters] == [Greeter]


async def test_transient_async_sessions_are_built_without_a_type_hint_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import AsyncSession

    from pyfly.context.application_context import ApplicationContext
    from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": "true",
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'hints.db'}",
                            "ddl-auto": "none",
                        }
                    }
                }
            }
        )
    )
    context.register_bean(RelationalAutoConfiguration)
    with caplog.at_level(logging.WARNING, logger="pyfly.container.container"):
        await context.start()
        try:
            sessions = [context.get_bean(AsyncSession) for _ in range(3)]
            for session in sessions:
                await session.close()
        finally:
            await context.stop()

    assert "Could not resolve type hints" not in caplog.text
