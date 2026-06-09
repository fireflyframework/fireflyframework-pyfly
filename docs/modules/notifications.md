# Notifications

`pyfly.notifications` exposes three port protocols — ``EmailService``,
``SmsService``, ``PushService`` — backed by pluggable provider adapters.

## Installation

The `notifications` extra pulls in Jinja2 (required by
`Jinja2TemplateEngine`):

```
pip install pyfly[notifications]
```

## Built-in adapters

| Adapter               | Channel | Notes                                          |
|-----------------------|---------|------------------------------------------------|
| `DummyEmailProvider`  | email   | logs only — for dev/tests                      |
| `DummySmsProvider`    | sms     | logs only — for dev/tests                      |
| `DummyPushProvider`   | push    | logs only — for dev/tests                      |
| `SmtpEmailProvider`   | email   | uses stdlib `smtplib` from a thread            |
| `SendGridEmailProvider` | email | SendGrid HTTP API                              |
| `ResendEmailProvider` | email   | Resend HTTP API                                |
| `TwilioSmsProvider`   | sms     | Twilio Messaging API                           |
| `FirebasePushProvider`| push    | Firebase Cloud Messaging (FCM)                 |

## Sending

```python
from pyfly.notifications import (
    EmailMessage, DefaultEmailService, SmtpEmailProvider,
)

provider = SmtpEmailProvider("smtp.example.com", username="u", password="p")
service = DefaultEmailService(provider=provider)

await service.send(EmailMessage(
    to=["customer@example.com"],
    sender="no-reply@example.com",
    subject="Welcome",
    body_text="Thanks for joining.",
))
```

Implement third-party providers (SendGrid, Twilio, Resend, Firebase) by
satisfying the ``EmailProvider`` / ``SmsProvider`` / ``PushProvider``
protocol — the framework already discovers them via the
``EmailService`` / ``SmsService`` / ``PushService`` ports.

## Template engine

### Overview

`DefaultEmailService` supports optional local Jinja2 rendering via a
`NotificationTemplateEngine`.  The two rendering paths are mutually exclusive —
choose either server-side Jinja2 rendering **or** the provider's own template
system, not both.

**Precedence**

1. **Engine injected + `message.template_id` set** — `engine.render(template_id, data)` is called and the result is written to `message.body_html`.  `template_id` and `template_data` are then cleared so provider-native template routing is **not** triggered.
2. **No engine** (default) — `message.template_id` / `message.template_data` are forwarded to the provider unchanged, enabling provider-native template routing (e.g. SendGrid Dynamic Templates).

### Classes

**`NotificationTemplateEngine`** (Protocol, `runtime_checkable`)

```python
async def render(self, template_id: str, data: dict[str, object]) -> str: ...
```

Raises `KeyError` for an unknown `template_id`.  Implementations must be
thread-safe when used from an async context.

**`Jinja2TemplateEngine(templates: dict[str, str])`**

In-memory Jinja2 engine.  Templates are raw Jinja2 source strings keyed by
`template_id`.  `autoescape=True` is enabled so HTML output is safe by default.
Raises `ImportError` if `jinja2` is not installed — install
`pyfly[notifications]` to include it.

```python
from pyfly.notifications.template import Jinja2TemplateEngine

engine = Jinja2TemplateEngine({
    "welcome": "<h1>Hello, {{ name }}!</h1>",
})
service = DefaultEmailService(provider=provider, template_engine=engine)

await service.send(EmailMessage(
    to=["alice@example.com"],
    sender="no-reply@example.com",
    subject="Welcome",
    template_id="welcome",
    template_data={"name": "Alice"},
))
```

### Configuration

| Key | Values | Default | Effect |
|-----|--------|---------|--------|
| `pyfly.notifications.template.engine` | `jinja2` \| `none` | `none` | `jinja2` registers a `Jinja2TemplateEngine` bean; `none` disables local rendering and provider-native `template_id` routing is used instead |

## Preferences / opt-out

### Overview

`DefaultEmailService`, `DefaultSmsService`, and `DefaultPushService` each
accept an optional `preference_service` constructor argument.  When present,
**every** recipient is checked before the provider is called.

- **Email** — all addresses in `to`, `cc`, and `bcc` are checked individually.
  Opted-out addresses are pruned from the message so the provider never
  delivers to them.  Only when **all** recipients have opted out is a
  `SUPPRESSED` result returned and the provider call skipped entirely.
- **Push** — every device token in `device_tokens` is checked.  Opted-out
  tokens are pruned; a `SUPPRESSED` result is returned when no tokens remain.
- **SMS** — the single `to` number is checked; a `SUPPRESSED` result is
  returned immediately if the recipient has opted out.

Recipient keys are normalised before comparison (case-insensitive for email
addresses and push tokens; non-digit formatting stripped for SMS numbers while
preserving a leading `+`), so `Alice@X.com` and `alice@x.com` are treated as
the same recipient.

### Classes

**`NotificationPreferenceService`** (Protocol, `runtime_checkable`)

```python
async def is_opted_in(self, recipient: str, channel: str) -> bool: ...
```

Returns `True` unless the recipient has opted out of `channel`.  `channel` is
one of `"email"`, `"sms"`, or `"push"`.

**`InMemoryPreferenceService`**

Thread-safe in-memory implementation.  All recipients are opted-in by default.

```python
from pyfly.notifications.preferences import InMemoryPreferenceService

prefs = InMemoryPreferenceService()
prefs.opt_out("alice@example.com", "email")   # suppress future emails to Alice
prefs.opt_in("alice@example.com", "email")    # restore
```

Methods:

- `opt_out(recipient: str, channel: str) -> None`
- `opt_in(recipient: str, channel: str) -> None`
- `async is_opted_in(recipient: str, channel: str) -> bool`

### `EmailStatus.SUPPRESSED`

`NotificationResult.status` is set to `EmailStatus.SUPPRESSED` when a send is
short-circuited because all recipients have opted out.  No provider call is
made in this case.

### Configuration

| Key | Values | Default | Effect |
|-----|--------|---------|--------|
| `pyfly.notifications.preference.store` | `memory` \| `none` | `none` | `memory` registers an `InMemoryPreferenceService` bean; `none` disables opt-out suppression entirely |

## Metrics

When a `MetricsRecorder` bean is present in the container it is automatically
injected into each service.  The following counters are emitted:

| Counter | Labels | Incremented on |
|---------|--------|----------------|
| `pyfly_notifications_sent_total` | `channel`, `provider` | `SENT` result |
| `pyfly_notifications_failed_total` | `channel`, `provider` | `FAILED` result |
| `pyfly_notifications_suppressed_total` | `channel` | `SUPPRESSED` result (once per pruned recipient) |

`channel` is `"email"`, `"sms"`, or `"push"`.  `provider` is the value
returned by the adapter's `name` attribute.

When no `MetricsRecorder` is present the counters are no-ops and no
instrumentation overhead is incurred.

## Auto-configuration reference

All keys are under `pyfly.notifications.*`.  The module is activated by
`pyfly.notifications.enabled = true`.

| Key | Values | Default |
|-----|--------|---------|
| `pyfly.notifications.enabled` | `true` \| `false` | — |
| `pyfly.notifications.email.provider` | `sendgrid` \| `resend` \| `smtp` \| `dummy` | `dummy` |
| `pyfly.notifications.sms.provider` | `twilio` \| `dummy` | `dummy` |
| `pyfly.notifications.push.provider` | `firebase` \| `dummy` | `dummy` |
| `pyfly.notifications.template.engine` | `jinja2` \| `none` | `none` |
| `pyfly.notifications.preference.store` | `memory` \| `none` | `none` |

## Testing

Provider adapters are tested against real transports in the SP-10 suite:

- `SendGridEmailProvider` — verified against a fake-HTTP behaviour test.
- `SmtpEmailProvider` — verified against an in-process `aiosmtpd` server.
