from dataclasses import dataclass

import pytest


@dataclass
class Product:
    id: int
    name: str


def test_explicit_registry_rejects_duplicates_and_unknown_resources():
    from pyfly.admin.data.models import ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.kernel.exceptions import ResourceNotFoundException

    registry = AdminResourceRegistry()
    resource = ModelAdmin("products", Product, fields=("id", "name"))
    registry.register(resource)
    assert registry.get("products") is resource
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(resource)
    with pytest.raises(ResourceNotFoundException):
        registry.get("users")
