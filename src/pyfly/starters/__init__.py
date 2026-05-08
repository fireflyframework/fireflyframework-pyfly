# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly starter meta-packages — opinionated bundles for each tier.

Mirror the Java framework's ``starter-core`` / ``starter-application`` /
``starter-data`` / ``starter-domain`` modules and the .NET equivalents
``FireflyFramework.Starter.*``. Each starter:

1. Defines a property dict (e.g. :data:`CORE_STACK_PROPERTIES`) listing
   every ``pyfly.X.enabled`` flag it activates at boot.
2. Ships an ``@enable_*_stack`` *decorator* that marks the application
   class so :class:`PyFlyApplication` merges the property dict into the
   live config before auto-configurations run.
3. Ships a ``register_*_stack(app)`` *function* for imperative
   bootstrapping — the Pythonic counterpart to .NET's
   ``services.AddFireflyXxx(...)`` extension methods.
4. Re-exports the most commonly used types and decorators of the layer
   so a controller / service file needs only a single import line.

Available starters:

* :mod:`pyfly.starters.core` — web + observability + cache + EDA + CQRS
  + resilience + actuator + AOP. The foundation every other starter
  pulls in.
* :mod:`pyfly.starters.web` — Web tier specifically: web framework
  adapter (Starlette/FastAPI), ASGI server, validation, actuator,
  observability. Use for HTTP-API services that don't need EDA, CQRS,
  or cache.
* :mod:`pyfly.starters.application` — core stack + plugin SPI +
  security (JWT + password) + sessions + i18n + scheduling +
  transactional engine + IDP + callbacks + webhooks + notifications.
* :mod:`pyfly.starters.data` — core stack + relational + document +
  HTTP client + scheduling + resilience.
* :mod:`pyfly.starters.domain` — core stack + event sourcing +
  transactional engine + rule engine + relational + HTTP client +
  plugins, plus re-exports of every :mod:`pyfly.domain` DDD primitive.
"""

from __future__ import annotations

from pyfly.starters.application import (
    APPLICATION_STACK_PROPERTIES,
    enable_application_stack,
    register_application_stack,
)
from pyfly.starters.core import (
    CORE_STACK_PROPERTIES,
    enable_core_stack,
    register_core_stack,
)
from pyfly.starters.data import (
    DATA_STACK_PROPERTIES,
    enable_data_stack,
    register_data_stack,
)
from pyfly.starters.domain import (
    DOMAIN_STACK_PROPERTIES,
    enable_domain_stack,
    register_domain_stack,
)
from pyfly.starters.web import (
    WEB_STACK_PROPERTIES,
    enable_web_stack,
    register_web_stack,
)

__all__ = [
    # Property dicts
    "APPLICATION_STACK_PROPERTIES",
    "CORE_STACK_PROPERTIES",
    "DATA_STACK_PROPERTIES",
    "DOMAIN_STACK_PROPERTIES",
    "WEB_STACK_PROPERTIES",
    # Declarative decorators
    "enable_application_stack",
    "enable_core_stack",
    "enable_data_stack",
    "enable_domain_stack",
    "enable_web_stack",
    # Imperative APIs
    "register_application_stack",
    "register_core_stack",
    "register_data_stack",
    "register_domain_stack",
    "register_web_stack",
]
