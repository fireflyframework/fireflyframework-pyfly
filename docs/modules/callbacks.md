# Callbacks (outbound webhooks)

`pyfly.callbacks` ships outbound notifications to configured external URLs
when domain events fire — the symmetric pair to `pyfly.webhooks`.

## Configure subscriptions

```python
from pyfly.callbacks import (
    CallbackConfig, CallbackSubscription,
    InMemoryCallbackConfigRepository, InMemoryCallbackExecutionRepository,
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
`X-Pyfly-Signature` HMAC header when `secret` is set, and persists a
`CallbackExecution` for observability.

## Real HTTP delivery (SP-6)

Callbacks now perform real HTTP POSTs when the `[client]` extra (httpx) is
available. Auto-configuration detects httpx at startup and wires an
`HttpSender` backed by `make_httpx_sender` from
`pyfly.callbacks.adapters.httpx_sender`. **Without httpx the default sender
is a no-op**: it logs the would-be request at `INFO` level but never opens a
network connection. Install the extra to enable real delivery:

```
pip install pyfly[client]   # pulls in httpx
```

### Circuit breaker + timeout

Every outbound call is guarded by a `pyfly.resilience.CircuitBreaker` and a
per-request timeout:

- The timeout is applied directly to each `httpx.AsyncClient` request.
- The circuit breaker trips after `failure-threshold` consecutive transport
  failures; while open, calls fail immediately with a
  `CircuitBreakerException`. The dispatcher catches this as a regular
  exception, records `last_error`, and retries up to `max_attempts` —
  exhausted attempts mark the execution `FAILED` but dispatch itself never
  crashes.
- Transport errors (connect refused, network timeout, etc.) increment the
  breaker's failure counter; any HTTP response from the server (regardless of
  status code) counts as a network success and resets it.

### Configuration keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.callbacks.enabled` | `bool` | — | Must be `true` to activate auto-configuration |
| `pyfly.callbacks.http.timeout` | duration | `10s` | Per-request HTTP timeout (seconds accepted as a float, or a duration string) |
| `pyfly.callbacks.http.circuit-breaker.failure-threshold` | `int` | `5` | Consecutive transport failures that trip the circuit |
| `pyfly.callbacks.http.circuit-breaker.recovery-timeout` | duration | `30s` | How long the circuit stays open before allowing a probe request |

Example (`application.yml`):

```yaml
pyfly:
  callbacks:
    enabled: true
    http:
      timeout: 5s
      circuit-breaker:
        failure-threshold: 3
        recovery-timeout: 60s
```

## HMAC signing

When a `CallbackConfig` has a non-empty `secret`, the dispatcher signs every
delivery with an HMAC-SHA256 digest and attaches it as:

```
X-Pyfly-Signature: sha256=<hex-digest>
```

The signature is computed over the **canonical JSON** serialization of the
payload — compact, keys sorted, no Python-specific escaping (`json.dumps`
with `separators=(",", ":")` and `sort_keys=True`). Receivers can verify
using `HmacSignatureValidator` from `pyfly.webhooks.signature` against that
same canonical form.

## SSRF protection — authorized-domains allowlist

`CallbackConfig.authorized_domains` is a list of `AuthorizedDomain` entries.
When the list is non-empty, the dispatcher checks the target URL's hostname
against every entry (exact match or subdomain) **before** opening a
connection. Deliveries to non-allowlisted hosts are rejected immediately with
`CallbackStatus.FAILED` and `last_error="Domain not authorized"`. An empty
`authorized_domains` list disables the check (all hosts are permitted).

```python
from pyfly.callbacks.models import AuthorizedDomain, CallbackConfig

config = CallbackConfig(
    tenant_id="acme",
    name="restricted",
    authorized_domains=[
        AuthorizedDomain(domain="customer.example.com", description="prod hook"),
    ],
    subscriptions=[...],
)
```
