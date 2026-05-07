# Callbacks (outbound webhooks)

`pyfly.callbacks` ships outbound notifications to configured external URLs
when domain events fire — the symmetric pair to `pyfly.webhooks`.

## Configure subscriptions

```python
from pyfly.callbacks import (
    CallbackConfig, CallbackSubscription, InMemoryCallbackConfigRepository,
)

config = CallbackConfig(
    tenant_id="acme",
    name="webhook-suite",
    secret="topsecret",
    subscriptions=[
        CallbackSubscription(
            event_type="OrderPlaced",
            target_url="https://customer.example.com/hooks/orders",
        ),
        CallbackSubscription(
            event_type="*",
            target_url="https://audit.example.com/all",
        ),
    ],
)
await configs.save(config)
```

## Dispatch an event

```python
results = await callback_dispatcher.dispatch(
    "acme", "OrderPlaced", {"id": 1, "amount": 99}
)
```

Each match retries on failure (`max_attempts`, `backoff_ms`), records an
``X-Pyfly-Signature`` HMAC header when ``secret`` is set, and persists a
``CallbackExecution`` for observability.
