# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the starter meta-packages."""

from __future__ import annotations

from pyfly.starters import (
    enable_application_stack,
    enable_core_stack,
    enable_data_stack,
    enable_domain_stack,
)
from pyfly.starters.application import APPLICATION_STACK_PROPERTIES
from pyfly.starters.core import CORE_STACK_PROPERTIES
from pyfly.starters.data import DATA_STACK_PROPERTIES
from pyfly.starters.domain import DOMAIN_STACK_PROPERTIES


def test_core_stack_marks_class() -> None:
    @enable_core_stack
    class App: ...

    assert App.__pyfly_starter_core__ == CORE_STACK_PROPERTIES


def test_application_stack_includes_core() -> None:
    @enable_application_stack
    class App: ...

    for k, v in CORE_STACK_PROPERTIES.items():
        assert App.__pyfly_starter_application__[k] == v
    assert App.__pyfly_starter_application__["pyfly.transactional.enabled"] == "true"
    assert App.__pyfly_starter_application__["pyfly.idp.enabled"] == "true"


def test_data_stack_marks_relational_and_document() -> None:
    @enable_data_stack
    class App: ...

    assert App.__pyfly_starter_data__["pyfly.relational.enabled"] == "true"
    assert App.__pyfly_starter_data__["pyfly.document.enabled"] == "true"


def test_domain_stack_marks_eventsourcing() -> None:
    @enable_domain_stack
    class App: ...

    assert App.__pyfly_starter_domain__["pyfly.eventsourcing.enabled"] == "true"


def test_property_dicts_are_strings() -> None:
    for d in (CORE_STACK_PROPERTIES, APPLICATION_STACK_PROPERTIES, DATA_STACK_PROPERTIES, DOMAIN_STACK_PROPERTIES):
        for k, v in d.items():
            assert isinstance(k, str)
            assert isinstance(v, str)
