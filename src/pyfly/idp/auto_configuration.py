# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the IDP module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter


@auto_configuration
@conditional_on_property("pyfly.idp.enabled", having_value="true")
class IdpAutoConfiguration:
    @bean
    def idp_adapter(self) -> InternalDbIdpAdapter:
        return InternalDbIdpAdapter()
