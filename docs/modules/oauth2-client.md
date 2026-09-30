# Standalone OAuth client

Install `pyfly[oauth2-client]` for `pyfly.oauth2`. This optional module imports no web
routes, application context, JWT verifier or provider administration client. It
provides token acquisition primitives for native clients and other applications;
there is no password grant.

```python
from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints, generate_pkce

endpoints = OAuth2Endpoints(
    token_endpoint="https://identity.example.com/oauth/token",
    device_authorization_endpoint="https://identity.example.com/oauth/device",
)

async def acquire():
    async with OAuth2Client("my-public-client", endpoints) as client:
        grant = await client.authorize_device(scopes=("openid", "api"))
        # Display grant.verification_uri and grant.user_code to the user.
        return await client.poll_device_token(grant)

async def exchange_verified_callback(code: str, redirect_uri: str, verifier: str):
    async with OAuth2Client("my-public-client", endpoints) as client:
        return await client.exchange_code(
            code, redirect_uri=redirect_uri, code_verifier=verifier,
        )

proof = generate_pkce()
# Send proof.challenge and code_challenge_method=S256 in the authorization URL.
# Retain proof.verifier only in the pending transaction until code exchange.
```

`pkce_challenge(verifier)` also accepts an existing RFC 7636 verifier. PKCE pairs,
returned tokens and device grants omit secret fields from `repr`. Token responses
are never logged. `OAuth2ClientError.code` is an allowlisted protocol/error code;
provider descriptions, bodies and transport exception strings are not exposed.
Treat PKCE pairs, device grants and token objects as opaque in-memory values. Do
not log them or serialize them with `dataclasses.asdict()` or another serializer:
`repr` redaction does not remove secrets from their fields.

For a provider that supports or requires PKCE on device authorization, opt in per
request:

```python
async def acquire_with_device_pkce():
    async with OAuth2Client("my-public-client", endpoints) as client:
        grant = await client.authorize_device(scopes=("openid", "api"), use_pkce=True)
        # Display only grant.verification_uri and grant.user_code.
        return await client.poll_device_token(grant)
```

`authorize_device(*, scopes=(), use_pkce=False)` keeps its existing wire format by
default; not every device provider supports this extension. With `use_pkce=True`,
the client generates a fresh S256 pair internally and sends `code_challenge` and
`code_challenge_method=S256` to the device endpoint. It never sends the verifier
there. The returned grant privately retains its verifier; every subsequent poll
for that grant automatically sends the same `code_verifier` to the token endpoint.
Concurrent grants have independent verifiers and can be polled in any order.
`use_pkce` must be a boolean; truthy strings and integers are rejected before HTTP.
There is no `plain` mode or fallback to an unprotected request after rejection.
Keep the grant with its originating client until completion, cancellation or expiry;
do not extract, persist or separately pass its private verifier. The S256 pair does
not change the existing polling intervals, ownership check, deadline or cleanup.

Before `exchange_code`, the caller must validate a one-time callback state,
redirect URI/path and issuer (including mix-up/replay protection). The library
creates no callback listener or browser process. `OAuth2Tokens.id_token` is an
opaque value, not verified identity; never substitute it for `access_token`.
Credential storage, refresh serialization/rotation and local roles remain the
application's responsibility. An explicitly supplied confidential-client secret
uses form authentication; public clients send no secret.

Endpoints are explicit trusted configuration, never taken from a token. HTTPS is
required; `allow_loopback_http=True` permits HTTP only for literal loopback or
localhost development endpoints. No discovery, redirect following, environment
proxies or automatic retries occur. Device verification URLs are returned to the
caller for display, never automatically opened or fetched. A provider can use a
different verification origin; the application controls which providers it trusts.

The client owns its HTTPX client and any supplied `transport`. Always use async
context management or `aclose()`. A custom transport can configure TLS, proxies,
network policy and test responses; it is trusted infrastructure, including its
chunk allocations and any retry policy. The stock path has zero retries.

Responses use identity encoding and reject compression and redirects before body
consumption. `max_response_bytes` (default 65,536) bounds bytes accumulated by
PyFly, checked before extending the buffer. It does not bound allocations inside
a supplied transport. `timeout` (default 10 seconds) bounds each request through response reading.
Timeout/cancellation waits for transport cleanup to finish; custom transports
must provide a terminating `aclose()` implementation.

Device polling follows [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628#section-3.5):
wait before every poll (default 5 seconds), add 5 seconds cumulatively after each
`slow_down`, double the delay after a timeout, and stop on denial, expiry or other
errors. A monotonic deadline also bounds in-flight requests and rejects late
successes. Cancellation propagates through sleep and requests; it does not revoke
the pending grant at the provider. A grant is bound to the client instance that
created it. Injected `clock`/`sleep` functions support deterministic tests.
