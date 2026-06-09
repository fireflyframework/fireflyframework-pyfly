# Identity Provider (IDP)

`pyfly.idp` bridges to an external identity provider behind a single
``IdpAdapter`` protocol — covering user management, authentication,
session introspection, MFA challenges and role assignment.

## Built-in adapters

| Adapter | Description |
|---------|-------------|
| `InternalDbIdpAdapter` | In-memory reference implementation with bcrypt-hashed passwords |
| `KeycloakIdpAdapter` | Keycloak REST API adapter |
| `AwsCognitoIdpAdapter` | AWS Cognito User Pools adapter |
| `AzureAdIdpAdapter` | Azure Active Directory / Entra ID adapter |

```python
from pyfly.idp import IdpUser, LoginRequest, InternalDbIdpAdapter

adapter = InternalDbIdpAdapter()
user = await adapter.create_user(IdpUser(username="alice", email="a@x.com"), "secret123")
auth = await adapter.login(LoginRequest(username="alice", password="secret123"))
print(auth.access_token)
```

## Optional-dependency extras

Each external IDP adapter requires a separate optional-dependency group. Install
only what you need:

| Extra | Dependency | Adapters that need it |
|---|---|---|
| `pyfly[idp-cognito]` | `boto3` | `AwsCognitoIdpAdapter` |
| `pyfly[idp-azure]` | `httpx` | `AzureAdIdpAdapter` |
| `pyfly[idp-keycloak]` | `httpx` | `KeycloakIdpAdapter` |

`idp-azure` and `idp-keycloak` pull in the same `httpx` stack that
`pyfly[client]` already provides — if you have the `client` extra installed you
can use both HTTP-backed adapters without an additional install. `AzureAdIdpAdapter`
uses the Microsoft Graph and Azure AD OAuth2 HTTP APIs directly; it does **not**
use `msal`.

`InternalDbIdpAdapter` has no additional runtime dependencies (bcrypt is optional
— the adapter falls back to salted SHA-256 when `bcrypt` is absent, which is not
suitable for production; install `pyfly[security]` to get `bcrypt` and `pyotp`).

## Auto-configuration & provider selection

`IdpAutoConfiguration` activates when `pyfly.idp.enabled=true`. The adapter is
selected from `pyfly.idp.provider` (mirroring Spring's IDP starter):

```yaml
pyfly:
  idp:
    enabled: true
    provider: internal-db    # internal-db (default) | keycloak | cognito | azure-ad
    keycloak:
      base-url: https://keycloak.example.com
      realm: myrealm
      client-id: my-client
      client-secret: secret
    cognito:
      user-pool-id: eu-west-1_XXXXXX
      client-id: abc123
      region: eu-west-1
      client-secret: ""      # optional
    azure:
      tenant-id: ...
      client-id: ...
      client-secret: ...
```

| `pyfly.idp.provider` value | Adapter wired |
|---|---|
| `internal-db` (default) | `InternalDbIdpAdapter` |
| `keycloak` | `KeycloakIdpAdapter` |
| `cognito` / `aws-cognito` | `AwsCognitoIdpAdapter` |
| `azure-ad` / `azuread` / `entra` | `AzureAdIdpAdapter` |

When `starlette` is installed, an `IdpController` bean is also registered,
mounting authentication and admin endpoints under `/idp`:

| Route | Method | Description |
|-------|--------|-------------|
| `/idp/login` | POST | Authenticate with username + password (+ optional MFA code) |
| `/idp/refresh` | POST | Refresh an access token |
| `/idp/logout` | POST | Revoke a token |
| `/idp/introspect` | POST | Inspect an active session |
| `/idp/admin/users` | POST | Create a user |
| `/idp/admin/users` | GET | List users |
| `/idp/admin/users/{user_id}` | GET / DELETE | Get or delete a user |
| `/idp/admin/users/{user_id}/roles/{role}` | POST / DELETE | Assign or revoke a role |
| `/idp/admin/roles` | GET | List all roles |

## MFA (TOTP)

`InternalDbIdpAdapter` supports TOTP multi-factor authentication via
[pyotp](https://pyauth.github.io/pyotp/). Install `pyfly[security]` to get the
`pyotp` (and `bcrypt`) dependencies.

### Enabling MFA for a user

```python
secret = await adapter.enable_mfa(user_id)
# Present secret to the user, e.g. via a QR code:
import pyotp
uri = pyotp.TOTP(secret).provisioning_uri(name="alice", issuer_name="MyApp")
```

`enable_mfa(user_id)` returns the provisioning secret as a base-32 string.
Raises `KeyError` if the user does not exist.

### Login flow when MFA is active

When a user has MFA enabled, `login()` behaves differently depending on whether
a code is supplied:

**Step 1 — caller does not supply `mfa_code`:**

```python
result = await adapter.login(LoginRequest(username="alice", password="secret123"))
# result.mfa_required == True
# result.mfa_challenge.challenge_id  — opaque, single-use token
# result.access_token == ""          — no tokens issued yet
```

`AuthResult.mfa_required` is `True` and `AuthResult.mfa_challenge` carries an
`MfaChallenge` with a `challenge_id` and `method="TOTP"`. The `MfaChallenge.user_id`
field is intentionally empty — the returned DTO does not expose the internal user
identifier, preventing user-enumeration via the MFA flow.

**Step 2 — verify the TOTP code:**

```python
result = await adapter.mfa_verify(challenge_id=result.mfa_challenge.challenge_id, code="123456")
# result.access_token  — valid access token
# result.refresh_token — valid refresh token
```

The challenge is single-use: `mfa_verify` removes it from the challenge store on
the first call. Passing an unknown or already-used `challenge_id` raises
`PermissionError("invalid or expired MFA challenge")`.

**Inline alternative** — supply the TOTP code directly in `LoginRequest`:

```python
result = await adapter.login(LoginRequest(username="alice", password="secret123", mfa_code="123456"))
# Tokens are issued immediately if the code is correct.
```

### MFA on external adapters

Keycloak, Cognito, and Azure AD manage MFA entirely within their own
authentication flows (OTP challenge, push notification, authenticator app, etc.).
Their `mfa_challenge` and `mfa_verify` methods raise `NotImplementedError` to
signal that the adapter does not own this step — MFA happens inside the
provider's own auth flow, before tokens are returned to PyFly.

## New port methods (SP-7 / Java parity)

The following methods were added to the `IdpAdapter` protocol in SP-7 to reach
parity with the Java Firefly IDP starter. All four built-in adapters implement
them.

### `get_user_info(access_token) -> IdpUser | None`

Resolve a live access token to the owning `IdpUser`.

```python
user = await adapter.get_user_info(access_token)
```

| Adapter | Backend call |
|---|---|
| `InternalDbIdpAdapter` | Token-to-user lookup in the in-memory store |
| `KeycloakIdpAdapter` | Keycloak realm `/protocol/openid-connect/userinfo` |
| `AwsCognitoIdpAdapter` | Cognito `GetUser` API |
| `AzureAdIdpAdapter` | Microsoft Graph `GET /me` |

Returns `None` when the token is unknown or expired.

### `register_user(user, password) -> IdpUser`

Public self-registration endpoint — a user signs up without admin involvement.
Distinct from `create_user`, which is an admin operation.

```python
new_user = await adapter.register_user(
    IdpUser(username="bob", email="bob@example.com"),
    "strongpassword",
)
```

All adapters enforce registration defaults before delegating to `create_user`:
`enabled` is forced to `True` and the `admin` role cannot be claimed via
self-registration.

### `get_roles(user_id) -> list[IdpRole]`

Return the roles (or equivalent provider concept) assigned to a user.

```python
roles = await adapter.get_roles(user_id)
# [IdpRole(name="editor", description=""), ...]
```

| Adapter | Provider concept returned |
|---|---|
| `InternalDbIdpAdapter` | Roles stored on `IdpUser.roles` |
| `KeycloakIdpAdapter` | Keycloak realm role-mappings |
| `AwsCognitoIdpAdapter` | Cognito group memberships |
| `AzureAdIdpAdapter` | Azure AD group memberships |

Returns `[]` for unknown users across all adapters — never raises on a missing
user.

### Intentionally deferred methods

`listSessions`, `revokeSession`, and `createScope` are **not** on the
`IdpAdapter` port. They belong to the session registry and authorization server
respectively, and will be addressed in a future milestone. The port comment
(`# DEFER`) in `src/pyfly/idp/port.py` records this decision.

## Implementing your own

Satisfy the ``IdpAdapter`` Protocol — every method is async; password and
token storage are entirely up to the adapter. Wire your adapter as the
``IdpAdapter`` bean and the framework picks it up.
