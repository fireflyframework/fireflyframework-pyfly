# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""OrderService application entry point.

Run with::

    cd samples/order_service
    uv sync
    uv run pyfly run

The :func:`pyfly_application` decorator marks the class as a PyFly app
and ``scan_packages`` tells the DI container which subpackages to
introspect for ``@service`` / ``@repository`` / ``@rest_controller`` /
``@command_handler`` / ``@query_handler`` / ``@saga`` declarations.
The :func:`enable_domain_stack` decorator activates the domain-tier
auto-configurations (CQRS, transactional engine, event sourcing,
relational data, rule engine).
"""

from __future__ import annotations

from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="orders-service",
    version="1.0.0",
    description="DDD-flavoured Order Service sample for the PyFly framework.",
    scan_packages=[
        "order_service.models.repositories",
        "order_service.core.services.orders",
        "order_service.web.controllers",
    ],
)
class OrdersServiceApplication:
    pass
