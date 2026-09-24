# Native PyFly catalog

A working HTML application and model-admin dashboard backed by **the same `Product`
SQLAlchemy entity** (`BaseEntity`). Native forms and dashboard writes share `ProductWrite`
validation and the administration service. It includes Jinja templates, static CSS,
CSRF, HTTP Basic authentication, POST/redirect/GET, custom 404/500 pages, and persistent SQLite.

From this directory:

```sh
uv sync --group dev
export WEBAPP_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
export WEBAPP_EDIT_TOKEN_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export WEBAPP_DATABASE_URL="sqlite+aiosqlite:///catalog.db"
export PYFLY_SECURITY_CSRF_COOKIE_SECURE=false  # local HTTP only
uv run python -m catalog.init_db
uv run uvicorn catalog.app:create_webapp --factory --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/` and sign in as `admin` with the generated password.
The browser's HTTP Basic credentials also authorize `/admin`; select **Datasources →
Products** to inspect and change the same rows. For HTTPS deployment keep secure cookies
enabled and supply credentials/secrets through your secret manager. The sample has no
default password. Use your existing PyFly form-login or identity provider for application
login requirements beyond this compact example.

`pyfly.yaml` configures template/static package roots, HTML errors, role requirements,
and administration. Environment overrides follow normal PyFly precedence. Primary and
management ports are equal here, so both surfaces share the application's security chain.
If you move management to a separate port, also enable `pyfly.management.security.enabled`.

The sample's `/` controller takes precedence over the framework welcome page. Its custom
404/500 templates extend `pyfly/error.html`, using the shared PyFly branding and diagnostic
panel. Set `PYFLY_WEB_DEBUG=true` locally to include stack traces; production defaults
hide them. Configure `pyfly.web.branding` for your name, logo, colors, and links, or override
the template blocks. These pages remain opt-in; API and microservice scaffolds do not
enable browser features.

The initialization command only creates the sample table if absent. The server does not
create or drop schema; use migrations for production. Admin borrows an application-owned
session factory and never manages schema itself. Restarting the server preserves rows.

```sh
uv run pytest
```

Tests create their own temporary database and explicitly request test schema setup. They
verify HTML rendering, CSRF, invalid form redisplay, create/edit persistence, and shared
admin record IDs. See [the framework webapp guide](../../docs/modules/webapps.md) for
registration, providers, field adapters, scopes, named datasources, and MongoDB administration.
