# Notifications

`pyfly.notifications` exposes three port protocols — ``EmailService``,
``SmsService``, ``PushService`` — backed by pluggable provider adapters.

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
