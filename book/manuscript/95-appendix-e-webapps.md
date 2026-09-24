<span class="eyebrow">Appendix E</span>

# Native Web Applications & Model Administration {.chtitle}

PyFly can serve HTML pages and administer the same data used by its REST APIs.
This appendix follows the runnable `samples/webapp` catalog. Its `Product` is an
ordinary PyFly `BaseEntity`; HTML forms and the dashboard read and write the same
rows. Existing applications can register their own SQLAlchemy entities or Beanie
documents without defining a second persistence model.

Lumen remains the book's wallet service. The catalog is a separate, smaller
companion for browser interactions. Do not expose Lumen's balance or ledger as
generic writable records: money movement must continue through its domain
commands. A custom administration provider can delegate to those commands.

## E.1. Run the complete catalog

From the framework checkout, enter `samples/webapp`. The sample's project file
resolves PyFly from the surrounding checkout, so it exercises this implementation.

```bash
cd samples/webapp
uv sync --group dev
export WEBAPP_ADMIN_PASSWORD=\
  "$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
export WEBAPP_EDIT_TOKEN_KEY=\
  "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export WEBAPP_DATABASE_URL="sqlite+aiosqlite:///catalog.db"
export PYFLY_SECURITY_CSRF_COOKIE_SECURE=false
uv run python -m catalog.init_db
uv run uvicorn catalog.app:create_webapp --factory \
  --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/`, sign in as `admin` using the generated password,
and create a product. Open `/admin`, select **Datasources → Products**, and find
the same record. Edit its price there, then reload the catalog. Stop and restart
the server: the SQLite file preserves the product.

The initialization command creates the sample table only if absent. The server
does not create or migrate schema. The sample deliberately places management on
port 8080 to share authentication with the catalog; PyFly's default separate
management port is 9090. Use migrations for deployed databases and HTTPS with
secure cookies. The cookie override above is for local HTTP development.

## E.2. One model, two browser interfaces

::: figure art/webapps-en.svg | Figure E.1 — Both interfaces use the same models and datasource.

The catalog has three layers of metadata with different jobs:

| Type | Responsibility |
|---|---|
| `Product(BaseEntity)` | Persisted columns, keys, constraints, and audit fields. |
| `ProductWrite(BaseModel)` | Accepted input and validation shared by both interfaces. |
| `ModelAdmin` | Visible fields, writable fields, operations, and source selection. |

`ProductForm` extends the input schema with an edit token. Input schemas do not
create tables and `ModelAdmin` does not replace the entity. The catalog owns its
session factory; its HTML controller and dashboard use an administration service
backed by that same factory. Applications may instead keep their usual services
for public pages and expose a separate, restricted administration registration.

::: listing catalog/models.py | Listing E.1 — The existing entity and shared input schema
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import BaseEntity


class Product(BaseEntity):
    __tablename__ = "webapp_products"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class ProductWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ProductForm(ProductWrite):
    edit_version: str = ""
:::

## E.3. Render pages and resolve URLs

Install `pyfly[webapp]` for Starlette, Jinja, and browser security; add
`data-relational` for SQL administration or `data-document` for MongoDB.
The same view contracts work with the `fastapi` extra. Enable rendering explicitly:

::: listing pyfly.yaml | Listing E.2 — Package resources used by the catalog
pyfly:
  web:
    templates:
      enabled: true
      directories: []
      packages: ["catalog:templates"]
    static:
      enabled: true
      directories: []
      packages: ["catalog:static"]
    errors:
      html-enabled: true
:::

Filesystem directories are relative to the process working directory. Package
roots travel with an installed application. Set `directories: []` when relying
only on packages, otherwise PyFly also checks the default local directories.
Templates support inheritance, includes, async helpers, and HTML autoescaping.
Undefined variables fail by default, making misspelled context keys visible.

Use `@controller` and return `ModelAndView("catalog.html", context)` for a page.
`ModelAndView` also accepts `status_code` and response `headers`. The existing
`@rest_controller` continues to provide JSON behavior. Assign unique route names
with `@get_mapping("/", name="catalog")` to avoid hardcoded paths.

In templates, `reverse('catalog')` generates a local URL and
`static_url('catalog.css')` resolves a static asset. In Python, import `reverse`
and `static_url` from `pyfly.web` and pass `request` as the first argument.
`Redirect(reverse(request, "catalog"))` produces a local 303 redirect after a
successful POST. Both helpers include mounted prefixes and the configured static
path. The existing `url_for('catalog')` helper provides an absolute URL.

Pass decoded names and parameters: the helpers encode spaces, Unicode, `#`, and
`?`. Asset paths are relative to the static roots, for example `images/logo.svg`
or `downloads/manual.pdf`, without a leading `/` or `..` segments. Missing route
names raise a resolution error; URL generation does not check file existence.
Jinja `{% include 'partials/menu.html' %}` loads a fragment from the template
roots; it neither generates a URL nor exposes server files.

Explicitly opt in to external HTTP(S) redirects with `allow_external=True`.
Register a `TemplateEngine` bean to replace Jinja or
`TemplateContextProcessor` beans to add request-specific context. View values
override processor values; URL and CSRF helpers remain framework-owned.

## E.4. Bind forms and redisplay validation

The sample's create action accepts `product: Form[ProductForm]`. PyFly parses the
form, converts its fields, and validates the Pydantic model before calling the
controller. The action passes `product.model_dump(exclude={"edit_version"})`
to `AdminDataService.create` and then redirects. The edit action instead passes
the record ID and `product.edit_version` to `AdminDataService.update`.

Each POST form must send a CSRF token:

::: listing catalog/templates/form.html | Listing E.3 — Fields inside the sample form
<input type="hidden" name="{{ csrf_field }}" value="{{ csrf_token }}">
<input type="hidden" name="edit_version"
       value="{{ values.get('edit_version', '') }}">
<label for="name">Name</label>
<input id="name" name="name" value="{{ values.get('name', '') }}"
       required maxlength="80">
<label for="price">Price</label>
<input id="price" name="price" inputmode="decimal"
       value="{{ values.get('price', '') }}" required>
:::

Catch `FormValidationException` using `@exception_handler` and return the form
template with `error.values`, `error.errors`, and status 422. The complete handler
is in `catalog/controllers.py`. Secret-like fields and secret Pydantic types are
removed from redisplay values; validation messages omit submitted input. Keep
escaping enabled. A normal GET does not invalidate the token in another open
tab; session rotation or invalidation rotates it.

Aliases and repeated list fields are supported. Repeated scalar fields are
rejected. An unchecked checkbox is absent, so use a Boolean default of `False`
and a checked value of `"true"`. Multipart text and `File[UploadedFile]` share
bounded parsing. Defaults are 1,000 fields, 20 files, 1 MiB per part, and 16 MiB
per request, configured under `pyfly.web.forms`.

## E.5. Own the HTML error experience

Add `errors/404.html` and `errors/500.html` to the configured template roots.
To map a status explicitly, set `pyfly.web.errors.templates` to a mapping such as
`{"404": "errors/missing.html"}`. Resolution tries that mapping, the exact status,
the status family such as `errors/4xx.html`, and the configured
`default-template` (`errors/error.html`). A missing template advances to the
next candidate; a broken error template falls back to a built-in escaped page.

Error templates receive `status`, `title`, `message`, `path`, and `transaction_id`,
plus `reverse`, `static_url`, and `url_for` for navigation and shared assets.
An error layout can use `static_url('catalog.css')`; it should not depend on
application context processors or CSRF/form helpers. Server errors hide exception
details by default. Browser errors negotiate HTML from `Accept`; REST and data-admin API
errors remain JSON. Authentication and CSRF failures preserve their status and
security headers. Error responses use `no-store` caching and `Vary: Accept`.

### Branded pages and the welcome screen

The framework includes `pyfly/base.html`, `pyfly/welcome.html`, and `pyfly/error.html`,
with the PyFly logo, green palette, responsive layouts, and automatic light/dark colors.
Only the `web` scaffold enables browser features automatically. API, microservice,
hexagonal, library, and CLI projects keep them disabled; installing an extra alone
does not activate pages. An existing API may deliberately opt in through configuration.

When templates are enabled and no application controller handles `/`, the welcome
page appears automatically. Startup-discovered home controllers take precedence, as
the catalog's controller does. Set `pyfly.web.welcome.enabled: false` to remove the
fallback, or `welcome.template` to select another template. A minimal webapp using
only bundled templates sets `templates.enabled: true` and `templates.directories: []`.

Configure `pyfly.web.branding` once for normal views and errors. `name` defaults to
`PyFly`; `tagline` to `Build something that matters.`; `primary-color` to `#4cbb2f`;
and `accent-color` to `#c2e85f`. Colors accept six-digit hex values. `logo-url` and
`favicon-url` accept HTTP(S) URLs or application-relative paths such as
`/static/catalog-logo.svg`; the ASGI mount prefix is added to relative paths. An empty
logo uses the bundled PyFly logo, and an empty favicon omits the icon. Customize
`documentation-url`, `support-url`, and `footer`; empty link URLs hide those links.

Templates receive `branding`, `home_url`, and `web_asset_url`. The framework serves
its logo and styles under `branding.assets-path`, default `/_pyfly/web`, only when
templates or HTML errors are enabled. Permit this reserved prefix through security
rules if pre-login pages need it. External styles work with `style-src 'self'`.
Application assets still use `static_url`, while `web_asset_url('web.css')`,
`web_asset_url('theme.css')`, and `web_asset_url('logo.png')` address bundled resources.

Extend a bundled template and override `title`, `head`, `navigation`, `content`, or
`footer`. For example, the sample's error pages extend `pyfly/error.html` and replace
the navigation block with a catalog link. Application template roots precede bundled
roots. Keep an extending template at a different path from its parent to avoid
recursive inheritance. A custom template engine must supply the chosen welcome
template or disable the fallback.

### Development stack traces

`pyfly.web.errors.include-stacktrace` accepts `never`, `on-debug` (the default), and
`always`. Enable `pyfly.web.debug: true` in a local development profile, or use
`debug=True` in the adapter factory, to activate the default policy. Environment
overrides work too: `PYFLY_WEB_DEBUG=true`. Production defaults hide the diagnostic
panel, and URL query parameters cannot enable it. PyFly owns negotiated debug pages
when HTML errors are enabled; REST controllers still return JSON.

Custom error templates receive `show_stacktrace`, `exception_type`,
`exception_message`, `stack_trace`, and `stack_frames`. Each frame has `filename`,
`lineno`, `function`, and `source`. The trace refers to the original exception even
when an exception converter changes the HTTP status. Disabled diagnostics return
empty strings and an empty frame list. Early security failures may have no exception.

`max-stack-frames` defaults to 30 (1–200 allowed); `max-trace-length` defaults to
20,000 characters (256–100,000 allowed). Traces include the most recent frames of
the current exception, not exception chains or local variables. Request headers,
cookies, query parameters, and bodies are not collected. Exception text and source
lines may still contain secrets, so public production deployments should keep traces
off. Jinja escapes trace text; never mark it `safe`.

`pyfly/error.html` includes the diagnostic panel. In a standalone custom layout use
`{% if show_stacktrace %}<pre>{{ stack_trace }}</pre>{% endif %}`. A broken application
error template falls back to an independent bundled renderer, retaining the original
trace. HTML errors also have an escaped branded fallback when Jinja is absent.

## E.6. Register the existing entity for administration

In a normal scanned application, expose a `ModelAdmin` bean. The following is
an alternative to the sample's explicit registry and provider wiring:

::: listing catalog/administration.py | Listing E.4 — Register the existing Product
from catalog.models import Product, ProductWrite
from pyfly.admin import ModelAdmin
from pyfly.container import bean, configuration


@configuration
class Administration:
    @bean
    def products(self) -> ModelAdmin:
        return ModelAdmin(
            "products", Product, label="Products",
            datasource="primary",
            fields=("id", "name", "price", "updated_at"),
            editable_fields=("name", "price"),
            search_fields=("name",), filter_fields=("name",),
            ordering=("name",),
            operations=("list", "read", "create", "update", "delete"),
            create_schema=ProductWrite, update_schema=ProductWrite,
        )
:::

The primary source borrows the existing `async_session_factory` bean. A named
source borrows `NamedDataSources.get(name)`. For an already initialized Beanie
document use `datasource="document"`. An explicit `provider` takes precedence.
Admin does not create clients, migrate schema, or close application-owned pools.

Enable `pyfly.admin.data.enabled`, configure `allowed-roles`, and provide
`edit-token-key` from an environment secret of at least 32 characters when writes
are enabled. Share the key across workers; rotating it invalidates open edits.
Registrations default to list/read only. No table becomes exposed merely because
it exists in a datasource. Primary keys, audit/version fields, and binary fields
are read-only; writable fields must be explicitly selected.

## E.7. Permissions, conflicts, and business rules

Administration always requires an authenticated existing `SecurityContext` and
an allowed data-admin role, even when monitoring uses `require-auth: false`.
A separate management listener must enable `pyfly.management.security.enabled`;
startup fails otherwise. Configure authentication and path rules on that listener.
Every API mutation also needs the CSRF cookie and matching `X-XSRF-TOKEN` header,
including requests authenticated by a bearer token.

Allowed operations are the intersection of global configuration, the registration,
and `has_permission(operation, context, record=None)`. A hook can narrow permission,
never expand either allowlist. Use `scope(context)` for server-owned equality
restrictions such as tenant IDs. Scope limits counts, lists, reads, and mutations;
creates inherit scope and updates cannot move records outside it. Object-level
permission checks alone do not hide list rows.

The dashboard groups sources and resources, pages records, and supports configured
search/filter/sort fields. Create/edit forms expose typed controls, null values,
and validation feedback. `relations={"category_id": "categories"}` obtains choices
from the authorized target resource and validates selections before writing.
Nested relationship writes require application-specific behavior.

An edit token represents the full persisted snapshot, including hidden fields.
PATCH sends it as `editToken`; DELETE sends it in `If-Match`. PostgreSQL locks
the row before comparison; SQLite holds an immediate transaction; MongoDB makes
one conditional replace/delete against the original BSON document. A stale edit
returns 409 and the dashboard preserves input until the user deliberately reloads.
Constraint conflicts also return 409; invalid input returns 422. Verify equivalent
locking before enabling concurrent writes on other SQL dialects.

Decimals and large scalar integers preserve precision as strings at the browser
boundary. `AdminFieldAdapter(type, serialize, parse)` adapts custom field types.
For service-level invariants or Beanie action hooks, implement `AdminDataProvider`
and pass it as `provider`. The built-in Mongo provider validates the resulting
document but does not execute arbitrary Beanie action hooks. A custom provider
must enforce scope, validation, and atomic stale-edit checks while delegating to
the application's business operations. Successful audit events record the actor,
resource, identifier, operation, field names, and correlation ID, without values.

## E.8. Configure, test, and troubleshoot

All new properties use the existing YAML/TOML, profile, and environment binding.
For example `PYFLY_WEB_TEMPLATES_AUTO_RELOAD=true` enables local template reload,
and `PYFLY_ADMIN_DATA_OPERATIONS=list,read` makes administration globally read-only.
Use profile files for development differences and environment secrets for credentials.
Templates, static resources, HTML errors, and model administration are opt-in.

Run `uv run pytest` inside `samples/webapp`. Its temporary database tests exercise
rendering, validation, CSRF, create/edit persistence, and shared admin IDs. Framework
tests additionally cover both web adapters, real PostgreSQL/MongoDB, browser CRUD,
and installed-wheel variants. See `docs/modules/webapps.md` for exact commands and
the complete property/API reference.

| Symptom | Check |
|---|---|
| Template root fails at startup | Root exists; package is installed; unused filesystem roots are disabled. |
| 403 on a local form POST | Secure cookie on HTTP, missing hidden field, or a rotated session token. |
| Datasources is absent or empty | Data admin enabled, registration discovered, role and list permission granted. |
| Management startup rejects configuration | Enable management security and wire real authentication. |
| 409 when saving | Reload the latest record; inspect uniqueness/constraint failures. |
| Wrong static links under a prefix | Use `reverse`/`static_url` and configure the ASGI root path. |

## Try it yourself {.exercises}

1. Create a product in the catalog, edit it in the dashboard, and verify its ID
   and price through a fresh database session.
2. Open an edit in two tabs. Save the first, then save the second. Confirm a 409
   prevents silently overwriting the newer value.
3. Restrict global operations to `list,read`. Confirm direct mutation requests
   fail as well as the write controls disappearing from the dashboard.
4. Change the 404 template, request a missing page with `Accept: text/html`, then
   request it with `Accept: application/json` and compare the responses.
