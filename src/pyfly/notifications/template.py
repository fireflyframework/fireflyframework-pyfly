# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Notification template engine port and built-in implementations.

Precedence when used with :class:`~pyfly.notifications.services.DefaultEmailService`
-------------------------------------------------------------------------------------
1. **Injected engine present** — if ``message.template_id`` is set, the engine
   renders the template locally (``Jinja2TemplateEngine``) and the result is
   stored as ``message.body_html``.  The provider-native ``template_id`` /
   ``template_data`` fields are **not** forwarded to the provider in this path.
2. **No engine** (default) — ``message.template_id`` / ``template_data`` are
   forwarded to the provider as-is, enabling provider-native template routing
   (e.g. SendGrid Dynamic Templates, Resend template IDs).

Keeping the two paths mutually exclusive means you choose *either* server-side
Jinja2 rendering *or* the provider's own template system — not both.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotificationTemplateEngine(Protocol):
    """Port for rendering a notification template to a string.

    Implementations must be thread-safe if used from an async context.
    """

    async def render(self, template_id: str, data: dict[str, object]) -> str:
        """Return the rendered template string.

        Parameters
        ----------
        template_id:
            An opaque key identifying the template (e.g. ``"welcome"``).
        data:
            Variables to interpolate into the template.

        Raises
        ------
        KeyError
            If the requested *template_id* is unknown.
        """
        ...


class Jinja2TemplateEngine:
    """Template engine backed by Jinja2 (``pip install pyfly[notifications]``).

    Templates are stored in-memory as raw Jinja2 source strings, keyed by
    *template_id*.  ``autoescape=True`` is enabled so HTML is safe by default.

    Parameters
    ----------
    templates:
        Mapping of template_id → Jinja2 source string.

    Example
    -------
    >>> engine = Jinja2TemplateEngine({"welcome": "<h1>Hello, {{ name }}!</h1>"})
    >>> import asyncio
    >>> asyncio.run(engine.render("welcome", {"name": "Alice"}))
    '<h1>Hello, Alice!</h1>'
    """

    def __init__(self, templates: dict[str, str]) -> None:
        self._templates = templates
        self._env: object | None = None  # lazy; avoids import at module load time

    def _get_env(self) -> object:
        if self._env is None:
            try:
                from jinja2 import Environment  # noqa: PLC0415
            except ImportError as exc:
                msg = "Jinja2TemplateEngine requires jinja2 — `pip install pyfly[notifications]`"
                raise ImportError(msg) from exc
            self._env = Environment(autoescape=True)
        return self._env

    async def render(self, template_id: str, data: dict[str, object]) -> str:
        """Render *template_id* with *data* and return the HTML string."""
        if template_id not in self._templates:
            msg = f"Unknown template_id: {template_id!r}. Available: {sorted(self._templates)}"
            raise KeyError(msg)
        env = self._get_env()
        from jinja2 import Environment  # noqa: PLC0415

        assert isinstance(env, Environment)
        tmpl = env.from_string(self._templates[template_id])
        return str(tmpl.render(**data))


class NoOpTemplateEngine:
    """A template engine that always raises :exc:`NotImplementedError`.

    This is the safe default for contexts where no template engine is configured:
    any attempt to render a template raises immediately, which surfaces
    configuration mistakes rather than silently sending empty bodies.
    """

    async def render(self, template_id: str, data: dict[str, object]) -> str:  # noqa: ARG002
        msg = (
            f"NoOpTemplateEngine cannot render {template_id!r}. "
            "Inject a Jinja2TemplateEngine or configure pyfly.notifications.template.engine=jinja2."
        )
        raise NotImplementedError(msg)
