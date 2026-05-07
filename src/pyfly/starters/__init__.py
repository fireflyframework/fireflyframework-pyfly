# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly starter meta-packages.

These mirror the Java framework's ``starter-core`` / ``starter-application`` /
``starter-data`` / ``starter-domain`` modules — opinionated bundles that wire
common stacks into a brand-new service.

Usage::

    from pyfly.starters.application import enable_application_stack

    @enable_application_stack
    class MyApp: ...

Each starter's ``enable_*`` decorator simply marks the application class so
the auto-configurations of every module in the stack activate at boot.
"""

from __future__ import annotations

from pyfly.starters.application import enable_application_stack
from pyfly.starters.core import enable_core_stack
from pyfly.starters.data import enable_data_stack
from pyfly.starters.domain import enable_domain_stack

__all__ = [
    "enable_application_stack",
    "enable_core_stack",
    "enable_data_stack",
    "enable_domain_stack",
]
