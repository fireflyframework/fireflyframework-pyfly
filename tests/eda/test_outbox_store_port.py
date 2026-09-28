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
"""The ``OutboxStore`` port (WP09b): its shape, the SQL adapter behind it, and the compatibility names.

What a store does is proven by the contract suite (``tests/support/outbox_contract.py``) on every backend; this
file pins the port itself: the SQL store is one, ``Outbox`` is still its name, the value types are the port's,
and the port module stays free of any database driver.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import pyfly.eda.outbox as sql_outbox
import pyfly.eda.ports.outbox as port
from pyfly.eda.ports import OutboxStore
from pyfly.eda.ports.outbox import OutboxStore as OutboxStoreFromModule
from tests.support.outbox_contract import PortOnlyStore


def test_the_sql_store_is_an_outbox_store_and_outbox_is_still_its_name() -> None:
    store = sql_outbox.SqlOutboxStore()
    assert isinstance(store, OutboxStore)
    assert OutboxStore is OutboxStoreFromModule
    assert sql_outbox.Outbox is sql_outbox.SqlOutboxStore


@pytest.mark.parametrize(
    "name",
    [
        "Delivery",
        "PendingDelivery",
        "PruneResult",
        "Retention",
        "StartPosition",
        "EVERY_DESTINATION",
        "ADDRESSED_DESTINATION_PREFIX",
    ],
)
def test_the_value_types_are_the_ports_and_still_importable_from_the_outbox_module(name: str) -> None:
    assert getattr(sql_outbox, name) is getattr(port, name)


def test_every_method_of_the_port_is_one_the_sql_store_defines() -> None:
    """The port is the surface the relay, the buses and the event-sourcing outbox use: every member of it is a
    method of the SQL store, with the store's own signature."""
    import inspect

    members = [name for name in vars(OutboxStore) if not name.startswith("_") and callable(vars(OutboxStore)[name])]
    assert sorted(members) == sorted(
        [
            "append",
            "claim",
            "complete",
            "dead_letters",
            "extend",
            "now",
            "pending",
            "prune",
            "register",
            "release",
            "settle",
            "start",
            "stop",
            "unregister",
        ]
    )
    for name in members:
        declared = inspect.signature(getattr(OutboxStore, name))
        implemented = inspect.signature(getattr(sql_outbox.SqlOutboxStore, name))
        assert list(declared.parameters) == list(implemented.parameters), name
        for parameter in declared.parameters.values():
            assert parameter.kind == implemented.parameters[parameter.name].kind, (name, parameter.name)
            assert parameter.default == implemented.parameters[parameter.name].default, (name, parameter.name)


def test_a_store_that_is_only_the_port_is_one() -> None:
    """The contract suite's wrapper forwards the port's methods and nothing else: code typed against the port
    runs on it, and code that reaches for the SQL store's own methods fails."""
    store = PortOnlyStore(sql_outbox.SqlOutboxStore())
    assert isinstance(store, OutboxStore)
    assert not isinstance(store, sql_outbox.SqlOutboxStore)
    assert not hasattr(store, "engine")


def test_the_port_module_imports_no_database_driver() -> None:
    code = (
        "import sys; import pyfly.eda.ports.outbox; "
        "print(sorted(m for m in ('sqlalchemy', 'pymongo', 'motor', 'beanie') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
