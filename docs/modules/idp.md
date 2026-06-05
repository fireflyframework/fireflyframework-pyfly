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

## Implementing your own

Satisfy the ``IdpAdapter`` Protocol — every method is async; password and
token storage are entirely up to the adapter. Wire your adapter as the
``IdpAdapter`` bean and the framework picks it up.
