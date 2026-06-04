# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the IDP module.

Provider selection (``pyfly.idp.provider``: internal-db | keycloak | cognito |
azure-ad) wires the chosen :class:`IdpAdapter` (audit #25), and a web
auto-configuration mounts the :class:`IdpController` over HTTP (audit #22).
"""

from __future__ import annotations

from typing import Any

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.idp.port import IdpAdapter


@auto_configuration
@conditional_on_property("pyfly.idp.enabled", having_value="true")
class IdpAutoConfiguration:
    @bean
    def idp_adapter(self, config: Config) -> IdpAdapter:
        provider = str(config.get("pyfly.idp.provider", "internal-db")).lower()

        if provider == "keycloak":
            from pyfly.idp.adapters.keycloak import KeycloakIdpAdapter

            return KeycloakIdpAdapter(
                base_url=str(config.get("pyfly.idp.keycloak.base-url", "")),
                realm=str(config.get("pyfly.idp.keycloak.realm", "")),
                client_id=str(config.get("pyfly.idp.keycloak.client-id", "")),
                client_secret=str(config.get("pyfly.idp.keycloak.client-secret", "")),
            )
        if provider in ("cognito", "aws-cognito"):
            from pyfly.idp.adapters.aws_cognito import AwsCognitoIdpAdapter

            return AwsCognitoIdpAdapter(
                user_pool_id=str(config.get("pyfly.idp.cognito.user-pool-id", "")),
                client_id=str(config.get("pyfly.idp.cognito.client-id", "")),
                region=str(config.get("pyfly.idp.cognito.region", "")),
                client_secret=str(config.get("pyfly.idp.cognito.client-secret", "")) or None,
            )
        if provider in ("azure-ad", "azuread", "entra"):
            from pyfly.idp.adapters.azure_ad import AzureAdIdpAdapter

            return AzureAdIdpAdapter(
                tenant_id=str(config.get("pyfly.idp.azure.tenant-id", "")),
                client_id=str(config.get("pyfly.idp.azure.client-id", "")),
                client_secret=str(config.get("pyfly.idp.azure.client-secret", "")),
            )

        from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter

        return InternalDbIdpAdapter()

    @bean
    @conditional_on_class("starlette")
    def idp_controller(self, idp_adapter: IdpAdapter) -> Any:
        # Mounts the /idp REST surface; registered in the same config as the
        # adapter it injects to avoid cross-config bean ordering (audit #22).
        from pyfly.idp.web import IdpController

        return IdpController(idp_adapter=idp_adapter)
