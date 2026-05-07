# Identity Provider (IDP)

`pyfly.idp` bridges to an external identity provider behind a single
``IdpAdapter`` protocol — covering user management, authentication,
session introspection, MFA challenges and role assignment.

## Built-in adapter

`pyfly.idp.adapters.internal_db.InternalDbIdpAdapter` is a reference
implementation backed by an in-memory map and bcrypt-hashed passwords.

```python
from pyfly.idp import IdpUser, LoginRequest, InternalDbIdpAdapter

adapter = InternalDbIdpAdapter()
user = await adapter.create_user(IdpUser(username="alice", email="a@x.com"), "secret123")
auth = await adapter.login(LoginRequest(username="alice", password="secret123"))
print(auth.access_token)
```

## Implementing your own

Satisfy the ``IdpAdapter`` Protocol — every method is async; password and
token storage are entirely up to the adapter. Wire your adapter as the
``IdpAdapter`` bean and the framework picks it up.
