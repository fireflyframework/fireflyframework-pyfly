# Webhooks (inbound)

`pyfly.webhooks` accepts webhook callbacks from external systems
(Stripe, GitHub, custom partners). It validates HMAC signatures, dedupes
by idempotency key, and dispatches to listener subclasses.

## Listener

```python
from pyfly.webhooks import AbstractWebhookEventListener, WebhookEvent

class StripeListener(AbstractWebhookEventListener):
    source = "stripe"

    async def handle(self, event: WebhookEvent) -> None:
        if event.event_type == "payment_intent.succeeded":
            ...
```

## Processor

```python
from pyfly.webhooks import HmacSignatureValidator, WebhookProcessor

processor = WebhookProcessor(
    listeners=[StripeListener()],
    signature_validators={"stripe": HmacSignatureValidator(secret="whsec_...")},
)
await processor.process(
    source="stripe",
    raw_body=request_body,
    headers={"X-Signature": "sha256=...", "X-Idempotency-Key": "evt_123"},
)
```

Failed listeners log a warning and trigger ``listener.on_error()``;
duplicate events (same `X-Idempotency-Key`) are silently ignored.
