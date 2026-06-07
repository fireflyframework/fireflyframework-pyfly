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
"""Public container SPI (v26.06.57) — register_instance / introspection / reset."""

from __future__ import annotations

from pyfly.container.container import Container
from pyfly.container.types import Scope


class Svc:
    pass


def test_register_instance_returns_exact_object() -> None:
    container = Container()
    svc = Svc()
    container.register_instance(Svc, svc)
    assert container.resolve(Svc) is svc
    assert container.contains_type(Svc) is True
    reg = container.get_registration(Svc)
    assert reg is not None
    assert reg.scope is Scope.SINGLETON
    assert reg.instance is svc
    assert Svc in container.registered_types()


def test_register_instance_named() -> None:
    container = Container()
    svc = Svc()
    container.register_instance(Svc, svc, name="primary")
    assert container.resolve_by_name("primary") is svc


def test_introspection_on_empty_container() -> None:
    container = Container()
    assert container.contains_type(Svc) is False
    assert container.get_registration(Svc) is None
    assert Svc not in container.registered_types()


def test_reset_instance_forces_rebuild() -> None:
    container = Container()
    svc = Svc()
    container.register_instance(Svc, svc)

    evicted = container.reset_instance(Svc)
    assert evicted is svc

    rebuilt = container.resolve(Svc)
    assert rebuilt is not svc
    assert isinstance(rebuilt, Svc)


def test_reset_instance_unregistered_is_none() -> None:
    assert Container().reset_instance(Svc) is None
