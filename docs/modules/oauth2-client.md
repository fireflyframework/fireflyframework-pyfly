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
application's responsibility. Authorization-code and device operations use form
authentication for an explicitly supplied confidential-client secret; public
clients send no secret.

For machine access, use the public client-credentials operation:

```python
import os

async def acquire_machine_token(remaining_seconds: float):
    async with OAuth2Client(
        os.environ["OAUTH_CLIENT_ID"],
        OAuth2Endpoints("https://identity.example.com/oauth/token"),
        client_secret=os.environ["OAUTH_CLIENT_SECRET"],
        timeout=10.0,
    ) as client:
        return await client.client_credentials(
            scopes=("api.read",),
            timeout=remaining_seconds,
        )
```

The exact signature is
`client_credentials(*, scopes: tuple[str, ...] = (), authentication: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic", timeout: float | None = None) -> OAuth2Tokens`.
It defaults to HTTP Basic, form-encoding each credential before base64 encoding
as required by [RFC 6749 section 2.3.1](https://www.rfc-editor.org/rfc/rfc6749#section-2.3.1).
Choose `authentication="client_secret_post"` explicitly when the trusted provider
requires credentials in the form body. A request uses only the chosen method;
there is no authentication fallback. Both credentials must be nonempty UTF-8
strings of at most 4096 characters with no C0/C1 control characters. Invalid
credentials, scopes, authentication methods or timeouts fail before HTTP with
redacted `ValueError` messages.

Scopes must be a tuple of at most 64 nonempty tokens, each at most 256 ASCII
characters, with at most 4096 characters including the separating spaces.
They follow [RFC 6749 scope syntax](https://www.rfc-editor.org/rfc/rfc6749#section-3.3):
printable ASCII excluding spaces, double quotes and backslashes within a token.
An empty tuple omits the scope parameter. The operation adds only `grant_type`,
optional `scope` and the selected client authentication; arbitrary extra form
fields are not accepted.

Machine acquisition accepts only valid Bearer token responses, including the
[RFC 6750 token syntax](https://www.rfc-editor.org/rfc/rfc6750#section-2.1), and
applies the same scope limits to a returned scope. The presence of either
`refresh_token` or `id_token`, even with a null value, fails with
`OAuth2ClientError("invalid_response")`. This is a strict machine-token contract;
authorization-code and device token response behavior is unchanged. No refresh
grant, persistence, automatic renewal or retry is performed. Scope and access
policy remain the application's responsibility.

The per-call `timeout` must be finite and positive; booleans are not accepted.
It bounds the request and response read by the smaller of this value and the
constructor timeout. Omit it to use the constructor limit, or pass the caller's
remaining operation budget. Cancellation propagates, and resource cleanup is
awaited before returning, so cleanup may exceed that budget. As with every
operation on this client, a supplied transport must cooperate with cancellation
and finish its close operations.

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
