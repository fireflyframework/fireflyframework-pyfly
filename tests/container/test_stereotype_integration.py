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
"""Integration tests: every stereotype resolves through the container.

Covers registration, resolution, constructor injection, field injection,
auto-binding, Optional/list parameters, and circular dependency detection
for ALL six stereotypes.
"""

import abc
from typing import Protocol, runtime_checkable

import pytest

from pyfly.container import (
    Autowired,
    BeanCurrentlyInCreationError,
    Container,
    Scope,
    component,
    configuration,
    controller,
    repository,
    rest_controller,
    service,
)
from pyfly.container.scanner import _auto_bind_interfaces, scan_module_classes, scan_package

# ---------------------------------------------------------------------------
# Shared fixtures used across stereotype tests
# ---------------------------------------------------------------------------


class Logger:
    def log(self, msg: str) -> str:
        return msg


class Greeter:
    def greet(self) -> str:
        return "hello"


# ---------------------------------------------------------------------------
# 1. Every stereotype resolves through the container
# ---------------------------------------------------------------------------


@component
class MyComponent:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@service
class MyService:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@repository
class MyRepository:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@controller
class MyController:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@rest_controller
class MyRestController:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@configuration
class MyConfiguration:
    pass


class TestEveryStereotypeResolves:
    """Verify that ALL six stereotypes can be registered and resolved."""

    @pytest.fixture
    def container(self) -> Container:
        c = Container()
        c.register(Greeter)
        return c

    def test_component_resolves(self, container: Container):
        container.register(MyComponent)
        inst = container.resolve(MyComponent)
        assert isinstance(inst, MyComponent)
        assert inst.greeter.greet() == "hello"

    def test_service_resolves(self, container: Container):
        container.register(MyService)
        inst = container.resolve(MyService)
        assert isinstance(inst, MyService)
        assert inst.greeter.greet() == "hello"

    def test_repository_resolves(self, container: Container):
        container.register(MyRepository)
        inst = container.resolve(MyRepository)
        assert isinstance(inst, MyRepository)
        assert inst.greeter.greet() == "hello"

    def test_controller_resolves(self, container: Container):
        container.register(MyController)
        inst = container.resolve(MyController)
        assert isinstance(inst, MyController)
        assert inst.greeter.greet() == "hello"

    def test_rest_controller_resolves(self, container: Container):
        container.register(MyRestController)
        inst = container.resolve(MyRestController)
        assert isinstance(inst, MyRestController)
        assert inst.greeter.greet() == "hello"

    def test_configuration_resolves(self, container: Container):
        container.register(MyConfiguration)
        inst = container.resolve(MyConfiguration)
        assert isinstance(inst, MyConfiguration)


# ---------------------------------------------------------------------------
# 2. Autowired field injection works with every stereotype
# ---------------------------------------------------------------------------


@component
class ComponentWithAutowired:
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@service
class ServiceWithAutowired:
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@repository
class RepoWithAutowired:
    logger: Logger = Autowired()


@controller
class ControllerWithAutowired:
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


@rest_controller
class RestControllerWithAutowired:
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


class TestAutowiredWithStereotypes:
    """Autowired field injection works across all stereotypes."""

    @pytest.fixture
    def container(self) -> Container:
        c = Container()
        c.register(Greeter)
        c.register(Logger)
        return c

    def test_component_autowired(self, container: Container):
        container.register(ComponentWithAutowired)
        inst = container.resolve(ComponentWithAutowired)
        assert isinstance(inst.greeter, Greeter)
        assert isinstance(inst.logger, Logger)

    def test_service_autowired(self, container: Container):
        container.register(ServiceWithAutowired)
        inst = container.resolve(ServiceWithAutowired)
        assert isinstance(inst.greeter, Greeter)
        assert isinstance(inst.logger, Logger)

    def test_repository_autowired(self, container: Container):
        container.register(RepoWithAutowired)
        inst = container.resolve(RepoWithAutowired)
        assert isinstance(inst.logger, Logger)

    def test_controller_autowired(self, container: Container):
        container.register(ControllerWithAutowired)
        inst = container.resolve(ControllerWithAutowired)
        assert isinstance(inst.greeter, Greeter)
        assert isinstance(inst.logger, Logger)

    def test_rest_controller_autowired(self, container: Container):
        container.register(RestControllerWithAutowired)
        inst = container.resolve(RestControllerWithAutowired)
        assert isinstance(inst.greeter, Greeter)
        assert isinstance(inst.logger, Logger)


# ---------------------------------------------------------------------------
# 3. Optional and list injection with stereotypes
# ---------------------------------------------------------------------------


class CacheAdapter:
    pass


class Validator:
    pass


class EmailValidator(Validator):
    pass


class PhoneValidator(Validator):
    pass


@service
class ServiceWithOptional:
    def __init__(self, cache: CacheAdapter | None = None) -> None:
        self.cache = cache


@controller
class ControllerWithOptional:
    def __init__(self, cache: CacheAdapter | None = None) -> None:
        self.cache = cache


@service
class ServiceWithList:
    def __init__(self, validators: list[Validator]) -> None:
        self.validators = validators


@controller
class ControllerWithList:
    def __init__(self, validators: list[Validator]) -> None:
        self.validators = validators


class TestOptionalAndListWithStereotypes:
    def test_service_optional_none(self):
        c = Container()
        c.register(ServiceWithOptional)
        inst = c.resolve(ServiceWithOptional)
        assert inst.cache is None

    def test_service_optional_present(self):
        c = Container()
        c.register(CacheAdapter)
        c.register(ServiceWithOptional)
        inst = c.resolve(ServiceWithOptional)
        assert isinstance(inst.cache, CacheAdapter)

    def test_controller_optional_none(self):
        c = Container()
        c.register(ControllerWithOptional)
        inst = c.resolve(ControllerWithOptional)
        assert inst.cache is None

    def test_controller_optional_present(self):
        c = Container()
        c.register(CacheAdapter)
        c.register(ControllerWithOptional)
        inst = c.resolve(ControllerWithOptional)
        assert isinstance(inst.cache, CacheAdapter)

    def test_service_list_injection(self):
        c = Container()
        c.register(EmailValidator)
        c.register(PhoneValidator)
        c.bind(Validator, EmailValidator)
        c.bind(Validator, PhoneValidator)
        c.register(ServiceWithList)
        inst = c.resolve(ServiceWithList)
        assert len(inst.validators) == 2

    def test_controller_list_injection(self):
        c = Container()
        c.register(EmailValidator)
        c.register(PhoneValidator)
        c.bind(Validator, EmailValidator)
        c.bind(Validator, PhoneValidator)
        c.register(ControllerWithList)
        inst = c.resolve(ControllerWithList)
        assert len(inst.validators) == 2

    def test_service_list_empty(self):
        c = Container()
        c.register(ServiceWithList)
        inst = c.resolve(ServiceWithList)
        assert inst.validators == []


# ---------------------------------------------------------------------------
# 4. Auto-binding works for ALL stereotypes
# ---------------------------------------------------------------------------


@runtime_checkable
class GreeterPort(Protocol):
    def greet(self) -> str: ...


class AbstractRepository(abc.ABC):
    @abc.abstractmethod
    def find(self, id: int) -> dict: ...


@component
class ComponentImpl(GreeterPort):
    def greet(self) -> str:
        return "component"


@service
class ServiceImpl(GreeterPort):
    def greet(self) -> str:
        return "service"


@repository
class RepositoryImpl(AbstractRepository):
    def find(self, id: int) -> dict:
        return {"id": id, "source": "repository"}


@controller
class ControllerImpl(GreeterPort):
    def greet(self) -> str:
        return "controller"


@rest_controller
class RestControllerImpl(GreeterPort):
    def greet(self) -> str:
        return "rest_controller"


class TestAutoBindingWithStereotypes:
    """Auto-binding works for all stereotypes, not just @service."""

    def test_component_auto_binds_to_protocol(self):
        c = Container()
        c.register(ComponentImpl)
        _auto_bind_interfaces(ComponentImpl, c)
        result = c.resolve(GreeterPort)
        assert isinstance(result, ComponentImpl)
        assert result.greet() == "component"

    def test_service_auto_binds_to_protocol(self):
        c = Container()
        c.register(ServiceImpl)
        _auto_bind_interfaces(ServiceImpl, c)
        result = c.resolve(GreeterPort)
        assert isinstance(result, ServiceImpl)

    def test_repository_auto_binds_to_abc(self):
        c = Container()
        c.register(RepositoryImpl)
        _auto_bind_interfaces(RepositoryImpl, c)
        result = c.resolve(AbstractRepository)
        assert isinstance(result, RepositoryImpl)
        assert result.find(1) == {"id": 1, "source": "repository"}

    def test_controller_auto_binds_to_protocol(self):
        c = Container()
        c.register(ControllerImpl)
        _auto_bind_interfaces(ControllerImpl, c)
        result = c.resolve(GreeterPort)
        assert isinstance(result, ControllerImpl)
        assert result.greet() == "controller"

    def test_rest_controller_auto_binds_to_protocol(self):
        c = Container()
        c.register(RestControllerImpl)
        _auto_bind_interfaces(RestControllerImpl, c)
        result = c.resolve(GreeterPort)
        assert isinstance(result, RestControllerImpl)
        assert result.greet() == "rest_controller"


# ---------------------------------------------------------------------------
# 5. scan_package discovers ALL stereotypes (end-to-end)
# ---------------------------------------------------------------------------


class TestScannerDiscoversAllStereotypes:
    """scan_package and scan_module_classes find all six stereotypes."""

    def test_scan_module_finds_all_stereotypes(self):
        import tests.container.test_stereotype_integration as mod

        classes = scan_module_classes(mod)
        names = {cls.__name__ for cls in classes}

        # Every stereotype-decorated class at module level should be found
        for expected in [
            "MyComponent",
            "MyService",
            "MyRepository",
            "MyController",
            "MyRestController",
            "MyConfiguration",
        ]:
            assert expected in names, f"{expected} not found by scan_module_classes"

    def test_scan_package_registers_and_auto_binds(self):
        c = Container()
        # Register non-stereotype dependencies that stereotyped classes need
        c.register(Greeter)
        c.register(Logger)
        c.register(CacheAdapter)
        c.register(EmailValidator)
        c.register(PhoneValidator)
        c.bind(Validator, EmailValidator)
        c.bind(Validator, PhoneValidator)

        count = scan_package("tests.container.test_stereotype_integration", c)
        # Should find many stereotype-decorated classes
        assert count >= 6

        # All stereotyped classes should be resolvable
        assert isinstance(c.resolve(MyComponent), MyComponent)
        assert isinstance(c.resolve(MyService), MyService)
        assert isinstance(c.resolve(MyRepository), MyRepository)
        assert isinstance(c.resolve(MyController), MyController)
        assert isinstance(c.resolve(MyRestController), MyRestController)
        assert isinstance(c.resolve(MyConfiguration), MyConfiguration)

    def test_scan_package_auto_binds_interfaces(self):
        """scan_package auto-binds implementations to their interfaces."""
        c = Container()
        scan_package("tests.container.test_stereotype_integration", c)

        # RepositoryImpl should be auto-bound to AbstractRepository
        repo = c.resolve(AbstractRepository)
        assert isinstance(repo, RepositoryImpl)

    def test_scan_package_auto_binds_protocol_with_resolve_all(self):
        """All Protocol implementations are auto-bound and collectible."""
        c = Container()
        scan_package("tests.container.test_stereotype_integration", c)

        # Multiple classes implement GreeterPort — resolve_all should find them
        all_greeters = c.resolve_all(GreeterPort)
        assert len(all_greeters) >= 4  # ComponentImpl, ServiceImpl, ControllerImpl, RestControllerImpl
        greetings = {g.greet() for g in all_greeters}
        assert "component" in greetings
        assert "service" in greetings
        assert "controller" in greetings
        assert "rest_controller" in greetings


# ---------------------------------------------------------------------------
# 6. Stereotype-specific scope behavior
# ---------------------------------------------------------------------------


@service(scope=Scope.TRANSIENT)
class TransientService:
    pass


@controller(scope=Scope.TRANSIENT)
class TransientController:
    pass


class TestStereotypeScopeHonored:
    """Scope set via stereotype parameter is respected during resolution."""

    def test_service_transient_creates_new_instances(self):
        c = Container()
        c.register(TransientService)
        a = c.resolve(TransientService)
        b = c.resolve(TransientService)
        assert a is not b

    def test_controller_transient_creates_new_instances(self):
        c = Container()
        c.register(TransientController)
        a = c.resolve(TransientController)
        b = c.resolve(TransientController)
        assert a is not b

    def test_service_default_singleton(self):
        c = Container()
        c.register(MyService)
        c.register(Greeter)
        a = c.resolve(MyService)
        b = c.resolve(MyService)
        assert a is b

    def test_controller_default_singleton(self):
        c = Container()
        c.register(MyController)
        c.register(Greeter)
        a = c.resolve(MyController)
        b = c.resolve(MyController)
        assert a is b


# ---------------------------------------------------------------------------
# 7. Named beans with stereotypes
# ---------------------------------------------------------------------------


@service(name="order_svc")
class NamedOrderService:
    pass


@controller(name="order_ctrl")
class NamedOrderController:
    pass


class TestNamedStereotypes:
    """Named beans work with all stereotypes."""

    def test_service_resolved_by_name(self):
        c = Container()
        c.register(NamedOrderService)
        inst = c.resolve_by_name("order_svc")
        assert isinstance(inst, NamedOrderService)

    def test_controller_resolved_by_name(self):
        c = Container()
        c.register(NamedOrderController)
        inst = c.resolve_by_name("order_ctrl")
        assert isinstance(inst, NamedOrderController)

    def test_contains_named_service(self):
        c = Container()
        c.register(NamedOrderService)
        assert c.contains("order_svc")

    def test_contains_named_controller(self):
        c = Container()
        c.register(NamedOrderController)
        assert c.contains("order_ctrl")


# ---------------------------------------------------------------------------
# 8. Circular dependency detection with stereotypes
# ---------------------------------------------------------------------------


@service
class CircularServiceB:
    def __init__(self, a: "CircularServiceA") -> None:
        self.a = a


@service
class CircularServiceA:
    def __init__(self, b: CircularServiceB) -> None:
        self.b = b


class TestCircularDetectionWithStereotypes:
    def test_circular_services_detected(self):
        c = Container()
        c.register(CircularServiceA)
        c.register(CircularServiceB)
        with pytest.raises(BeanCurrentlyInCreationError, match="Circular dependency"):
            c.resolve(CircularServiceA)


# ---------------------------------------------------------------------------
# 9. Controller resolving another stereotype as dependency
# ---------------------------------------------------------------------------


@service
class ItemService:
    def find(self, id: int) -> dict:
        return {"id": id, "name": "item"}


@controller
class ItemController:
    def __init__(self, item_service: ItemService) -> None:
        self.item_service = item_service

    def get_item(self, id: int) -> dict:
        return self.item_service.find(id)


@rest_controller
class ItemRestController:
    def __init__(self, item_service: ItemService) -> None:
        self.item_service = item_service

    def get_item(self, id: int) -> dict:
        return self.item_service.find(id)


class TestControllerWithServiceDependency:
    """Controllers can depend on services (the most common real-world pattern)."""

    def test_controller_injects_service(self):
        c = Container()
        c.register(ItemService)
        c.register(ItemController)
        ctrl = c.resolve(ItemController)
        assert ctrl.get_item(42) == {"id": 42, "name": "item"}

    def test_rest_controller_injects_service(self):
        c = Container()
        c.register(ItemService)
        c.register(ItemRestController)
        ctrl = c.resolve(ItemRestController)
        assert ctrl.get_item(42) == {"id": 42, "name": "item"}

    def test_controller_shares_singleton_service(self):
        c = Container()
        c.register(ItemService)
        c.register(ItemController)
        c.register(ItemRestController)
        ctrl = c.resolve(ItemController)
        rest = c.resolve(ItemRestController)
        assert ctrl.item_service is rest.item_service  # same singleton


# ---------------------------------------------------------------------------
# 10. Autowired(required=False) on controller
# ---------------------------------------------------------------------------


@controller
class ControllerWithOptionalAutowired:
    cache: CacheAdapter = Autowired(required=False)
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


class TestControllerOptionalAutowired:
    def test_optional_field_is_none_when_missing(self):
        c = Container()
        c.register(Greeter)
        c.register(Logger)
        c.register(ControllerWithOptionalAutowired)
        ctrl = c.resolve(ControllerWithOptionalAutowired)
        assert ctrl.cache is None
        assert isinstance(ctrl.logger, Logger)
        assert isinstance(ctrl.greeter, Greeter)

    def test_optional_field_injected_when_present(self):
        c = Container()
        c.register(Greeter)
        c.register(Logger)
        c.register(CacheAdapter)
        c.register(ControllerWithOptionalAutowired)
        ctrl = c.resolve(ControllerWithOptionalAutowired)
        assert isinstance(ctrl.cache, CacheAdapter)
