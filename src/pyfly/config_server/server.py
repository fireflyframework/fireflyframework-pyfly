# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ConfigServer — exposes ``/{app}/{profile}/{label}`` over HTTP."""

from __future__ import annotations

from typing import Any

from pyfly.config_server.backend import ConfigBackend, ConfigSource


class ConfigServer:
    """Framework-agnostic controller class — mount it on any HTTP framework."""

    base_path = "/config"

    def __init__(self, backend: ConfigBackend) -> None:
        self._backend = backend

    async def fetch(self, application: str, profile: str = "default", label: str = "main") -> dict[str, Any] | None:
        source = await self._backend.fetch(application, profile, label)
        if source is None:
            return None
        return {
            "name": source.application,
            "profiles": [source.profile],
            "label": source.label,
            "propertySources": [
                {
                    "name": f"{source.application}-{source.profile}",
                    "source": source.properties,
                }
            ],
        }

    async def save(
        self,
        application: str,
        profile: str,
        properties: dict[str, Any],
        label: str = "main",
    ) -> dict[str, Any]:
        source = ConfigSource(application=application, profile=profile, label=label, properties=properties)
        await self._backend.save(source)
        return {"saved": True}

    async def list(self) -> list[dict[str, Any]]:
        return [
            {"application": s.application, "profile": s.profile, "label": s.label} for s in await self._backend.list()
        ]
