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

Failed listeners log a warning and trigger `listener.on_error()`;
duplicate events (same `X-Idempotency-Key`) are silently ignored.

## Idempotency providers (SP-6)

The processor deduplicates events by idempotency key using a pluggable
`WebhookEventStore`. Two providers are available out of the box:

| Provider | Class | Characteristics |
|----------|-------|-----------------|
| `in-memory` (default) | `InMemoryWebhookEventStore` | Single-process, no extra deps; state is lost on restart |
| `redis` | `RedisWebhookEventStore` | Durable, shared across all workers; keys expire automatically |

Select the provider via `pyfly.webhooks.idempotency.provider`.

### Redis store

`RedisWebhookEventStore` stores each idempotency key in Redis with an expiry
TTL, so the store self-prunes without a background job. The `redis.asyncio`
client is injected by auto-configuration; the `redis` package must be present
(`pip install redis[asyncio]`).

```yaml
pyfly:
  webhooks:
    enabled: true
    idempotency:
      provider: redis
      redis:
        url: redis://localhost:6379/0
      ttl-seconds: 86400
```

### Configuration keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.webhooks.enabled` | `bool` | — | Must be `true` to activate auto-configuration |
| `pyfly.webhooks.idempotency.provider` | `in-memory` \| `redis` | `in-memory` | Idempotency store backend |
| `pyfly.webhooks.idempotency.redis.url` | `str` | `redis://localhost:6379/0` | Redis connection URL (redis provider only) |
| `pyfly.webhooks.idempotency.ttl-seconds` | `int` | `86400` | Seconds before an idempotency key expires (redis provider only) |

> **Note on atomicity.** `RedisWebhookEventStore` performs
> `already_processed` + `remember` as two separate calls (non-atomic). For
> most workloads this is acceptable — duplicate delivery is rare and the
> window is negligible. If strict once-exactly semantics are required, wrap
> both calls in a distributed lock.

## Signature validators (SP-6)

### `SignatureValidator` Protocol

All validators implement the `SignatureValidator` Protocol:

```python
class SignatureValidator(Protocol):
    def is_valid(self, *, body: bytes, signature: str | None) -> bool: ...
```

Register validators with `WebhookProcessor` by source name:

```python
processor.register_validator("github", GitHubSignatureValidator(secret="..."))
```

or pass them at construction time via `signature_validators={"source": validator}`.

### Built-in validators

#### `NoOpSignatureValidator`

Accepts every request regardless of the signature value. This is the
**default** when no validator is registered for a source.

> **Security note.** `NoOpSignatureValidator` is intentionally permissive for
> development and testing. Configure a real validator (e.g.
> `HmacSignatureValidator`) for every source in production — an unconfigured
> source accepts forged requests.

#### `HmacSignatureValidator`

Verifies a `sha256=<hex>` style HMAC-SHA256 header. The prefix is
configurable via `header_prefix` (default `"sha256="`).

```python
HmacSignatureValidator(secret="shared_secret")
```

#### `StripeSignatureValidator`

Validates Stripe's `Stripe-Signature` header format:
`t=<unix-timestamp>,v1=<hmac>[,v1=<hmac>...]`. The signed payload is
`f"{timestamp}.{body}"` (UTF-8). Requests older than `tolerance_seconds`
(default 300) are rejected to prevent replay attacks.

```python
StripeSignatureValidator(secret="whsec_...", tolerance_seconds=300)
```

#### `GitHubSignatureValidator`

Validates GitHub's `X-Hub-Signature-256` header (`sha256=<hex>` over the
raw body). A named alias over `HmacSignatureValidator` with the `sha256=`
prefix.

```python
GitHubSignatureValidator(secret="github_webhook_secret")
```

#### `TwilioSignatureValidator` — HTTP middleware only

> **Not compatible with the `SignatureValidator` Protocol.**
>
> Twilio's scheme signs the *request URL* and *form parameters*, not the raw
> body, so `is_valid(*, body, signature)` cannot be used. Do **not** register
> it with `WebhookProcessor`. Instead, verify Twilio requests in an HTTP
> middleware layer that has access to the full URL and decoded form data
> before the body is consumed as JSON.

```python
validator = TwilioSignatureValidator(auth_token="...")
ok = validator.is_valid(
    url="https://example.com/webhooks/twilio",
    params=request.form,
    signature=request.headers["X-Twilio-Signature"],
)
```
