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
| `AzureCommunicationEmailProvider` | email | Optional official async Azure SDK; explicit managed identity or injected credential |
| `TwilioSmsProvider`   | sms     | Twilio Messaging API                           |
| `FirebasePushProvider`| push    | Firebase Cloud Messaging (FCM)                 |

## Sending

```python
from pyfly.notifications import (
    EmailMessage, DefaultEmailService, SmtpEmailProvider,
)

import os

provider = SmtpEmailProvider("smtp.example.com", username=os.environ["SMTP_USERNAME"], password=os.environ["SMTP_PASSWORD"])
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
| `pyfly.notifications.email.provider` | `sendgrid` \| `resend` \| `smtp` \| `azure-communication-email` \| `dummy` | `dummy` |
| `pyfly.notifications.sms.provider` | `twilio` \| `dummy` | `dummy` |
| `pyfly.notifications.push.provider` | `firebase` \| `dummy` | `dummy` |
| `pyfly.notifications.template.engine` | `jinja2` \| `none` | `none` |
| `pyfly.notifications.preference.store` | `memory` \| `none` | `none` |

## Testing

Provider adapters are tested against real transports in the SP-10 suite:

- `SendGridEmailProvider` — verified against a fake-HTTP behaviour test.
- `SmtpEmailProvider` — verified against an in-process `aiosmtpd` server.


## Email transport security and outcomes

SMTP uses a verified default SSL context for certificate-chain and hostname checks. STARTTLS is the default; `smtp.tls-mode=implicit` selects verified `SMTP_SSL`. Credentials cannot be used with `none`/plaintext mode. A missing STARTTLS capability never causes an authentication downgrade. Socket timeouts are finite. Cancellation interrupts the owned SMTP connection and waits for its thread to finish before propagating, retaining the caller's admission/lease during cleanup. It cannot recall DATA already transmitted; callers must preserve the message ID and record an uncertain outcome. Socket timeouts apply to network operations; operating system name resolution may take longer.

Under `pyfly.notifications.email.smtp`, configure `host`, `port`, `username`, `password`, `timeout` and `tls-mode` (`starttls`, `implicit`, `none`). Legacy constructor `use_tls=False` remains available for explicit unauthenticated local/relay use. `tls.ca-file` supplies a private CA; `tls.certfile` and `tls.keyfile` must be configured together. Alternatively, register a verified `SSLContext` bean named `smtp_email_ssl_context`. File-based TLS and a supplied context are mutually exclusive. SendGrid and Resend endpoints require HTTPS without URL credentials and do not automatically follow redirects or retry sends.

A custom `EmailProvider` bean overrides native provider selection. Absent selection and explicit `dummy` retain local development compatibility; an unrecognized explicit email provider fails configuration. `email_capabilities(provider)` returns immutable `EmailProviderCapabilities(real_delivery, inline_cid, operation_lookup)` declarations. Dummy and undeclared custom providers do not declare real delivery. These booleans describe supported operations; they do not prove a reachable server, verified mailbox, allowed sender or delivered email.

`SENT` means the provider accepted the send operation. SMTP/API acceptance does not prove mailbox delivery. A possible DATA write, lost response, partial recipient acceptance, unclassified provider exception or Azure polling timeout yields `UNKNOWN`. Do not automatically replay such a message. Native `error` values are bounded safe codes such as `SMTP_TLS_FAILED`, `EMAIL_HTTP_429` and `EMAIL_SEND_OUTCOME_UNKNOWN`; private response bodies and exception messages are not returned. Message payloads, headers and attachment contents are excluded from default representations. Diagnostic counter failures cannot turn known acceptance into a retryable failure.

Keep one immutable message ID and payload for an operation. Native transports add a stable RFC Message-ID when no explicit valid one was supplied. Its presence is correlation, not deduplication or an exactly-once guarantee. Persist application outbox admission with the business mutation, dispatch outside its transaction, and reconcile uncertain attempts rather than silently replacing their IDs.

## Inline images

Existing three-argument attachments remain compatible. An inline attachment explicitly declares both a canonical content ID and `inline=True`:

```python
from pyfly.notifications import Attachment, EmailMessage

message = EmailMessage(
    sender="sender@example.com",
    to=["recipient@example.com"],
    body_text="Your account update is available.",
    body_html='<p>Your account update is available.</p><img src="cid:brand-logo" alt="Organization logo">',
    attachments=[Attachment("logo.png", "image/png", b"owned-image-bytes", content_id="brand-logo", inline=True)],
)
```

Load validated image bytes from an application-owned asset; the example bytes are a placeholder. SMTP groups inline resources with their HTML body, and native HTTP adapters map the same identifier to their vendor CID field. Provider-native templates must explicitly reference the same ID. Never use attachments or tracking pixels to expose private source content.

## Optional Azure Communication Email

Install `pyfly[notifications-azure]` to use `AzureCommunicationEmailProvider`. This extra uses the official async `azure-communication-email` SDK and async managed identity credentials. Importing the base notifications module neither imports the optional Azure SDK nor contacts an identity endpoint.

```yaml
pyfly:
  notifications:
    enabled: true
    email:
      provider: azure-communication-email
      from: donotreply@your-managed-domain.azurecomm.net
      azure:
        endpoint: https://your-resource.communication.azure.com
        credential-mode: managed-identity
        # Set managed-identity-client-id for an explicitly selected user-assigned identity.
        timeout: 10
        poll-timeout: 30
        poll-interval: 1
```

Use the actual endpoint and verified domain MailFrom read back from the configured resource. Sender/domain provisioning and role assignment belong to the operator. The adapter does not provision resources, alter DNS, construct a default environment/CLI credential chain or fall back to an account key. Programmatic native DI can instead supply an async token credential through a custom `EmailProvider` factory. Injected complete SDK clients and credentials remain caller-owned; clients must preserve the documented verified-TLS/no-replay policy. Owned clients and managed identity resources are closed with bounded cleanup.

The native client disables retries, redirects, credential-challenge POST replay and body diagnostics. It supplies a deterministic operation UUID, then uses bounded status requests against the same configured HTTPS origin instead of following an arbitrary Operation-Location header. `EmailOperationProvider.get_status(message_id, operation_id=None)` looks up the original operation without resending; IDs must match the original message. `QUEUED` from lookup means that operation is still pending, not that a new POST is permitted. A succeeded operation maps to `SENT`; mailbox delivery requires separate trusted evidence. See the [official async SDK contract](https://learn.microsoft.com/en-us/python/api/azure-communication-email/azure.communication.email.aio.emailclient?view=azure-python) and [delivery-event guidance](https://learn.microsoft.com/en-us/azure/communication-services/quickstarts/email/handle-email-events).

Azure Communication Email retires on September 30, 2028. Microsoft's email-specific guidance currently permits onboarding new workloads subject to change, while recommending migration. Keep provider selection replaceable and review the [current lifecycle guide](https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide) before a new deployment. Local SDK/SMTP fixtures do not establish actual managed identity, sender authorization, mailbox receipt or customer acceptance.
