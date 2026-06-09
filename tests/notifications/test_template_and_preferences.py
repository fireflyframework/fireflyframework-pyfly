# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for template engine, preference service, and metrics wiring.

Covers:
- Jinja2TemplateEngine: render happy path + unknown template_id raises KeyError
- NoOpTemplateEngine: raises NotImplementedError on any render call
- Render-then-send via DefaultEmailService: injected engine → provider receives
  rendered body_html; template_id cleared (not forwarded to provider)
- Preference opt-out: SUPPRESSED result returned; provider NOT called
- Metrics counters increment on SENT / FAILED; suppressed counter on SUPPRESSED
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

jinja2 = pytest.importorskip("jinja2")  # skip whole module if jinja2 absent

from pyfly.notifications.models import (  # noqa: E402
    EmailMessage,
    EmailStatus,
    NotificationResult,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.preferences import InMemoryPreferenceService  # noqa: E402
from pyfly.notifications.providers.dummy import (  # noqa: E402
    DummyEmailProvider,
    DummyPushProvider,
    DummySmsProvider,
)
from pyfly.notifications.services import (  # noqa: E402
    DefaultEmailService,
    DefaultPushService,
    DefaultSmsService,
)
from pyfly.notifications.template import (  # noqa: E402
    Jinja2TemplateEngine,
    NoOpTemplateEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeCounter:
    """Tracks .labels(**kw).inc() calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self._labels: dict[str, str] = {}

    def labels(self, **kwargs: str) -> FakeCounter:
        self._labels = dict(kwargs)
        return self

    def inc(self) -> None:
        self.calls.append(dict(self._labels))


class FakeMetricsRecorder:
    """Fake MetricsRecorder that vends FakeCounter instances by name."""

    def __init__(self) -> None:
        self._counters: dict[str, FakeCounter] = {}

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> FakeCounter:  # noqa: ARG002
        if name not in self._counters:
            self._counters[name] = FakeCounter()
        return self._counters[name]

    def histogram(self, name: str, description: str, labels: list[str] | None = None, buckets: Any = None) -> Any:  # noqa: ARG002
        return MagicMock()

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Any:  # noqa: ARG002
        return MagicMock()

    def get_counter(self, name: str) -> FakeCounter:
        return self._counters[name]


# ---------------------------------------------------------------------------
# Jinja2TemplateEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jinja2_engine_renders_template() -> None:
    engine = Jinja2TemplateEngine({"welcome": "<h1>Hello, {{ name }}!</h1>"})
    result = await engine.render("welcome", {"name": "Alice"})
    assert result == "<h1>Hello, Alice!</h1>"


@pytest.mark.asyncio
async def test_jinja2_engine_renders_multiple_variables() -> None:
    engine = Jinja2TemplateEngine({"order": "Order #{{ order_id }} for {{ customer }}"})
    result = await engine.render("order", {"order_id": 42, "customer": "Bob"})
    assert result == "Order #42 for Bob"


@pytest.mark.asyncio
async def test_jinja2_engine_raises_key_error_for_unknown_template() -> None:
    engine = Jinja2TemplateEngine({"a": "hello"})
    with pytest.raises(KeyError, match="unknown_tmpl"):
        await engine.render("unknown_tmpl", {})


@pytest.mark.asyncio
async def test_jinja2_engine_autoescape_html() -> None:
    """autoescape=True should escape user-supplied HTML in variables."""
    engine = Jinja2TemplateEngine({"tmpl": "<p>{{ body }}</p>"})
    result = await engine.render("tmpl", {"body": "<script>alert(1)</script>"})
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


# ---------------------------------------------------------------------------
# NoOpTemplateEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_engine_raises_not_implemented() -> None:
    engine = NoOpTemplateEngine()
    with pytest.raises(NotImplementedError, match="NoOpTemplateEngine"):
        await engine.render("any", {})


# ---------------------------------------------------------------------------
# Render-then-send integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_renders_template_and_clears_template_id() -> None:
    """Injected engine renders template; provider receives body_html, not template_id."""
    engine = Jinja2TemplateEngine({"greet": "<p>Hi {{ user }}</p>"})
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, template_engine=engine)

    msg = EmailMessage(
        to=["u@example.com"],
        sender="s@example.com",
        subject="Test",
        template_id="greet",
        template_data={"user": "Carol"},
    )
    result = await service.send(msg)

    assert result.status == EmailStatus.SENT
    # provider received the rendered HTML
    assert len(provider.sent) == 1
    sent = provider.sent[0]
    assert sent.body_html == "<p>Hi Carol</p>"
    # template_id cleared so provider-native routing is NOT triggered
    assert sent.template_id is None
    assert sent.template_data == {}


@pytest.mark.asyncio
async def test_service_skips_render_when_no_template_id() -> None:
    """Engine is present but message has no template_id → no render; body unchanged."""
    engine = Jinja2TemplateEngine({"t": "x"})
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, template_engine=engine)

    msg = EmailMessage(
        to=["u@example.com"],
        sender="s@example.com",
        subject="Plain",
        body_text="just text",
    )
    result = await service.send(msg)

    assert result.status == EmailStatus.SENT
    assert provider.sent[0].body_html is None
    assert provider.sent[0].body_text == "just text"


@pytest.mark.asyncio
async def test_service_passes_template_id_through_when_no_engine() -> None:
    """Without an engine, template_id is forwarded to the provider as-is."""
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider)

    msg = EmailMessage(
        to=["u@example.com"],
        sender="s@example.com",
        subject="Native",
        template_id="d-abc123",
        template_data={"k": "v"},
    )
    result = await service.send(msg)

    assert result.status == EmailStatus.SENT
    sent = provider.sent[0]
    assert sent.template_id == "d-abc123"
    assert sent.template_data == {"k": "v"}


# ---------------------------------------------------------------------------
# Preference / opt-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_opted_out_returns_suppressed_without_calling_provider() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("alice@example.com", "email")
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, preference_service=prefs)

    msg = EmailMessage(to=["alice@example.com"], sender="s@example.com", subject="Promo")
    result = await service.send(msg)

    assert result.status == EmailStatus.SUPPRESSED
    assert len(provider.sent) == 0  # provider must NOT have been called


@pytest.mark.asyncio
async def test_email_opted_in_delivers_normally() -> None:
    prefs = InMemoryPreferenceService()
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, preference_service=prefs)

    msg = EmailMessage(to=["bob@example.com"], sender="s@example.com", subject="Hi")
    result = await service.send(msg)

    assert result.status == EmailStatus.SENT
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_email_opt_out_then_opt_in_delivers() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("carol@example.com", "email")
    prefs.opt_in("carol@example.com", "email")
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, preference_service=prefs)

    msg = EmailMessage(to=["carol@example.com"], sender="s@example.com", subject="Back")
    result = await service.send(msg)

    assert result.status == EmailStatus.SENT


@pytest.mark.asyncio
async def test_sms_opted_out_returns_suppressed() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("+10000000000", "sms")
    provider = DummySmsProvider()
    service = DefaultSmsService(provider=provider, preference_service=prefs)

    result = await service.send(SmsMessage(to="+10000000000", body="hi"))
    assert result.status == EmailStatus.SUPPRESSED
    assert len(provider.sent) == 0


@pytest.mark.asyncio
async def test_push_opted_out_returns_suppressed() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("device-token-xyz", "push")
    provider = DummyPushProvider()
    service = DefaultPushService(provider=provider, preference_service=prefs)

    result = await service.send(PushMessage(device_tokens=["device-token-xyz"], title="hi", body="body"))
    assert result.status == EmailStatus.SUPPRESSED
    assert len(provider.sent) == 0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_sent_counter_increments_on_success() -> None:
    recorder = FakeMetricsRecorder()
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider, metrics=recorder)

    msg = EmailMessage(to=["u@example.com"], sender="s@example.com", subject="m")
    result = await service.send(msg)
    assert result.status == EmailStatus.SENT

    sent_calls = recorder.get_counter("pyfly_notifications_sent_total").calls
    assert len(sent_calls) == 1
    assert sent_calls[0] == {"channel": "email", "provider": "dummy"}


@pytest.mark.asyncio
async def test_metrics_failed_counter_increments_on_failure() -> None:
    """Use a provider that always raises to trigger the FAILED path."""

    class BrokenProvider:
        name = "broken"

        async def send(self, message: EmailMessage) -> NotificationResult:
            raise RuntimeError("boom")

    recorder = FakeMetricsRecorder()
    service = DefaultEmailService(provider=BrokenProvider(), metrics=recorder)  # type: ignore[arg-type]

    msg = EmailMessage(to=["u@example.com"], sender="s@example.com", subject="x")
    result = await service.send(msg)
    assert result.status == EmailStatus.FAILED

    failed_calls = recorder.get_counter("pyfly_notifications_failed_total").calls
    assert len(failed_calls) == 1
    assert failed_calls[0] == {"channel": "email", "provider": "broken"}


@pytest.mark.asyncio
async def test_metrics_suppressed_counter_increments_on_opt_out() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("x@x.io", "email")
    provider = DummyEmailProvider()
    recorder = FakeMetricsRecorder()
    service = DefaultEmailService(provider=provider, preference_service=prefs, metrics=recorder)

    msg = EmailMessage(to=["x@x.io"], sender="s@x.io", subject="y")
    result = await service.send(msg)
    assert result.status == EmailStatus.SUPPRESSED

    suppressed_calls = recorder.get_counter("pyfly_notifications_suppressed_total").calls
    assert len(suppressed_calls) == 1
    assert suppressed_calls[0] == {"channel": "email"}


@pytest.mark.asyncio
async def test_no_metrics_no_error() -> None:
    """Services work fine when no metrics recorder is provided."""
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider)
    msg = EmailMessage(to=["u@example.com"], sender="s@example.com", subject="x")
    result = await service.send(msg)
    assert result.status == EmailStatus.SENT
