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
        # Emit the full Spring-Cloud-Config overlay set, highest priority first:
        # the requested app+profile, then the app's default bundle, then the
        # shared ``application`` config for the profile and its default. A client
        # merges these with the first winning (audit #85). Returns None only when
        # every overlay is absent.
        candidates = [
            (application, profile),
            (application, "default"),
            ("application", profile),
            ("application", "default"),
        ]
        seen: set[tuple[str, str]] = set()
        sources: list[ConfigSource] = []
        for app_name, prof in candidates:
            key = (app_name, prof)
            if key in seen:
                continue
            seen.add(key)
            source = await self._backend.fetch(app_name, prof, label)
            if source is not None:
                sources.append(source)

        if not sources:
            return None
        return {
            "name": application,
            "profiles": [profile],
            "label": label,
            "propertySources": [{"name": f"{s.application}-{s.profile}", "source": s.properties} for s in sources],
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
