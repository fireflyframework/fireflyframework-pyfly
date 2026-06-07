# Web Layer Guide

The PyFly web layer provides enterprise-grade HTTP routing, controller registration, parameter binding, parameter validation, a composable filter chain, exception handling, CORS, security headers, and OpenAPI documentation. It follows a hexagonal (ports-and-adapters) architecture: the framework-agnostic core defines decorators, parameter types, and configuration dataclasses, while a pluggable adapter layer (Starlette by default) handles the actual HTTP server integration. A config-driven adapter selection mechanism allows the framework to auto-detect the best available web provider at startup.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Config-Driven Web Adapter Selection](#config-driven-web-adapter-selection)
  - [WebProperties](#webproperties)
  - [Auto-Detection](#auto-detection)
  - [StarletteWebAdapter](#starlettewebadapter)
- [REST Controllers](#rest-controllers)
  - [Defining a Controller](#defining-a-controller)
  - [@rest_controller Stereotype](#rest_controller-stereotype)
  - [@request_mapping: Class-Level Base Path](#request_mapping-class-level-base-path)
- [HTTP Method Mappings](#http-method-mappings)
  - [@get_mapping](#get_mapping)
  - [@post_mapping](#post_mapping)
  - [@put_mapping](#put_mapping)
  - [@delete_mapping](#delete_mapping)
  - [@patch_mapping](#patch_mapping)
  - [Parameters Reference](#parameters-reference)
- [Request Parameter Binding](#request-parameter-binding)
  - [PathVar\[T\] -- Path Variables](#pathvart----path-variables)
  - [QueryParam\[T\] -- Query Parameters](#queryparamt----query-parameters)
  - [Body\[T\] -- Request Body](#bodyt----request-body)
  - [Header\[T\] -- HTTP Headers](#headert----http-headers)
  - [Cookie\[T\] -- Cookies](#cookiet----cookies)
  - [Type Coercion](#type-coercion)
- [Valid\[T\] -- Parameter Validation](#validt----parameter-validation)
  - [Standalone Usage: Valid\[T\]](#standalone-usage-validt)
  - [Wrapping Body\[T\]: Valid\[Body\[T\]\]](#wrapping-bodyt-validbodyt)
  - [Wrapping Other Binding Types](#wrapping-other-binding-types)
  - [Behavior Summary](#behavior-summary)
  - [Structured 422 Error Response](#structured-422-error-response)
  - [Valid\[T\] vs. Bare Body\[T\]](#validt-vs-bare-bodyt)
  - [ParameterResolver Internals for Valid\[T\]](#parameterresolver-internals-for-validt)
- [Response Handling](#response-handling)
  - [Return Value Conversion](#return-value-conversion)
  - [handle_return_value()](#handle_return_value)
- [JSON & Content Negotiation](#json--content-negotiation)
  - [Global JSON Config: pyfly.web.json.\*](#global-json-config-pyflywebjson)
  - [CamelModel: Opt-in camelCase Models](#camelmodel-opt-in-camelcase-models)
  - [JsonSerializers: Custom Non-Pydantic Types](#jsonserializers-custom-non-pydantic-types)
  - [HttpMessageConverter Chain](#httpmessageconverter-chain)
  - [Registering a Custom MessageConverterRegistry](#registering-a-custom-messageconverterregistry)
  - [RFC 7807 problem+json](#rfc-7807-problemjson)
- [Exception Handling](#exception-handling)
  - [Controller-Level Exception Handlers](#controller-level-exception-handlers)
  - [Global Exception Handler](#global-exception-handler)
  - [Exception-to-Status Mapping](#exception-to-status-mapping)
  - [Exception Converters](#exception-converters)
- [WebFilter Chain](#webfilter-chain)
  - [WebFilter Protocol](#webfilter-protocol)
  - [OncePerRequestFilter Base Class](#oncerequestfilter-base-class)
  - [Built-in Filters](#built-in-filters)
    - [TransactionIdFilter](#transactionidfilter)
    - [RequestLoggingFilter](#requestloggingfilter)
    - [SecurityHeadersFilter](#securityheadersfilter)
    - [SecurityFilter](#securityfilter)
  - [WebFilterChainMiddleware](#webfilterchainmiddleware)
  - [Filter Ordering with @order](#filter-ordering-with-order)
  - [Auto-Discovery of User Filters](#auto-discovery-of-user-filters)
  - [Creating Custom Filters](#creating-custom-filters)
- [Server-Sent Events (SSE)](#server-sent-events-sse)
  - [@sse_mapping Decorator](#sse_mapping-decorator)
  - [format_sse_event()](#format_sse_event)
  - [SseEmitter](#sseemitter)
  - [SSE Route Discovery](#sse-route-discovery)
  - [Complete SSE Example](#complete-sse-example)
- [CORS Configuration](#cors-configuration)
- [Security Headers](#security-headers)
- [OpenAPI and Swagger Documentation](#openapi-and-swagger-documentation)
  - [Automatic Generation](#automatic-generation)
  - [Swagger UI](#swagger-ui)
  - [ReDoc](#redoc)
  - [OpenAPIGenerator Internals](#openapigenerator-internals)
- [Application Factory: create_app()](#application-factory-create_app)
  - [Full Parameter Reference](#full-parameter-reference)
  - [What create_app() Does](#what-create_app-does)
- [ControllerRegistrar](#controllerregistrar)
  - [Route Collection](#route-collection)
  - [Route Metadata for OpenAPI](#route-metadata-for-openapi)
  - [Exception Handler Discovery](#exception-handler-discovery)
- [ParameterResolver](#parameterresolver)
- [WebServerPort](#webserverport)
- [Module Exports Reference](#module-exports-reference)
- [Complete CRUD Example](#complete-crud-example)

---

## Architecture Overview

PyFly's web layer is organized into three tiers:

1. **Framework-agnostic core** (`pyfly.web`): Decorators (`@get_mapping`, `@post_mapping`, etc.), parameter binding types (`PathVar`, `QueryParam`, `Body`, `Header`, `Cookie`, `File`), the validation marker type (`Valid`), file upload types (`File`, `UploadedFile`), the `WebFilter` protocol, the `OncePerRequestFilter` base class, configuration dataclasses (`CORSConfig`, `SecurityHeadersConfig`), and the `@exception_handler` decorator. These contain no HTTP-framework-specific code and could be backed by any ASGI or WSGI framework.

2. **Port interfaces** (`pyfly.web.ports`): The `WebServerPort` protocol defines the contract that any web server adapter must implement, and the `WebFilter` protocol defines the contract for request/response filters. These ensure that the application layer never depends directly on Starlette, Flask, or any other framework.

3. **Starlette adapter** (`pyfly.web.adapters.starlette`): The default implementation that translates the framework-agnostic decorators and types into Starlette routes, filters, and request handling. This includes the `ControllerRegistrar`, `ParameterResolver`, response handler, error handler, `WebFilterChainMiddleware`, built-in filter implementations (`TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter`, `SecurityFilter`), the `StarletteWebAdapter`, and OpenAPI endpoint generators.

For convenience, the top-level `pyfly.web` package re-exports both the core types and the Starlette adapter, so most applications need only a single import:

```python
from pyfly.web import (
    # Decorators
    get_mapping, post_mapping, put_mapping, patch_mapping,
    delete_mapping, request_mapping, exception_handler,
    # Parameter binding types
    Body, PathVar, QueryParam, Header, Cookie, Valid, File, UploadedFile,
    # Filter system
    WebFilter, OncePerRequestFilter,
    # Configuration
    CORSConfig, SecurityHeadersConfig,
    # SSE
    sse_mapping,
    # Controller advice
    controller_advice,
)

# Starlette adapter utilities (imported from adapter package directly)
from pyfly.web.adapters.starlette import create_app
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from pyfly.web.adapters.starlette.response import handle_return_value
```

Source files:
- `src/pyfly/web/__init__.py` -- top-level re-exports
- `src/pyfly/web/params.py` -- `PathVar`, `QueryParam`, `Body`, `Header`, `Cookie`, `Valid`
- `src/pyfly/web/mappings.py` -- `@request_mapping`, `@get_mapping`, etc.
- `src/pyfly/web/filters.py` -- `OncePerRequestFilter`
- `src/pyfly/web/ports/filter.py` -- `WebFilter` protocol
- `src/pyfly/web/ports/outbound.py` -- `WebServerPort` protocol
- `src/pyfly/web/adapters/starlette/` -- Starlette adapter package

---

## Config-Driven Web Adapter Selection

PyFly uses a config-driven auto-configuration system to select the web framework adapter at startup. This mirrors the Spring Boot auto-configuration pattern: the framework detects which libraries are installed and wires the appropriate adapter bean automatically.

### WebProperties

The `WebProperties` dataclass (`src/pyfly/config/properties/web.py`) captures all web subsystem configuration under the `pyfly.web.*` namespace:

```python
from pyfly.core.config import config_properties
from dataclasses import dataclass, field


@config_properties(prefix="pyfly.web")
@dataclass
class WebProperties:
    adapter: str = "auto"
    port: int = 8000
    host: str = "0.0.0.0"
    debug: bool = False
    docs: dict = field(default_factory=lambda: {"enabled": True})
    actuator: dict = field(default_factory=lambda: {"enabled": False})
```

| Field      | Type   | Default                   | Description                                                |
|------------|--------|---------------------------|------------------------------------------------------------|
| `adapter`  | `str`  | `"auto"`                  | Web adapter to use: `"auto"`, `"starlette"`, or `"none"`   |
| `port`     | `int`  | `8000`                    | HTTP server listen port                                    |
| `host`     | `str`  | `"0.0.0.0"`              | HTTP server bind address                                   |
| `debug`    | `bool` | `False`                   | Enable Starlette debug mode                                |
| `docs`     | `dict` | `{"enabled": True}`       | OpenAPI documentation settings                             |
| `actuator` | `dict` | `{"enabled": False}`      | Actuator endpoint settings                                 |

You can set these values in your `application.yml` or `application.toml`:

```yaml
pyfly:
  web:
    adapter: starlette
    port: 9090
    host: 0.0.0.0
    debug: false
    docs:
      enabled: true
    actuator:
      enabled: true
```

### Auto-Detection

Web adapter selection uses the decentralized auto-configuration pattern. The `WebAutoConfiguration` class (`src/pyfly/web/auto_configuration.py`) declares the conditions under which the Starlette adapter is activated:

```python
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
)
from pyfly.web.ports.outbound import WebServerPort


@auto_configuration
@conditional_on_class("starlette")
@conditional_on_missing_bean(WebServerPort)
class WebAutoConfiguration:
    """Auto-configures the Starlette web adapter when available."""

    @bean
    def web_adapter(self) -> WebServerPort:
        from pyfly.web.adapters.starlette.adapter import StarletteWebAdapter

        return StarletteWebAdapter()
```

The three decorators express the full activation logic declaratively:

1. **`@auto_configuration`** -- marks the class for discovery via the `pyfly.auto_configuration` entry-point group.
2. **`@conditional_on_class("starlette")`** -- the class is only processed when the `starlette` package is importable. If it is not installed, the entire configuration is skipped and the application can still function as a non-web service.
3. **`@conditional_on_missing_bean(WebServerPort)`** -- the `@bean` method is only invoked when no `WebServerPort` bean is already registered in the container, allowing user overrides to take precedence.

Discovery happens through the standard `pyfly.auto_configuration` entry-point group declared in `pyproject.toml`:

```toml
[project.entry-points."pyfly.auto_configuration"]
web = "pyfly.web.auto_configuration:WebAutoConfiguration"
```

At startup, `discover_auto_configurations()` in `src/pyfly/config/auto.py` loads all entry points in this group and evaluates their conditions. This is fully pluggable -- third-party packages can register their own auto-configuration classes in the same group.

### StarletteWebAdapter

The `StarletteWebAdapter` (`src/pyfly/web/adapters/starlette/adapter.py`) implements the `WebServerPort` protocol by delegating to the `create_app()` factory:

```python
class StarletteWebAdapter:
    """WebServerPort implementation backed by Starlette."""

    def create_app(self, **kwargs: Any) -> Starlette:
        return create_app(**kwargs)
```

This simple delegation pattern keeps the adapter thin. All the real work happens in `create_app()`, which is described in detail in the [Application Factory](#application-factory-create_app) section.

Source files:
- `src/pyfly/config/properties/web.py` -- `WebProperties`
- `src/pyfly/web/auto_configuration.py` -- `WebAutoConfiguration`
- `src/pyfly/config/auto.py` -- `AutoConfiguration`, `discover_auto_configurations()`
- `src/pyfly/web/adapters/starlette/adapter.py` -- `StarletteWebAdapter`

---

## REST Controllers

### Defining a Controller

A REST controller in PyFly is a plain Python class decorated with `@rest_controller` and `@request_mapping`. The framework discovers it automatically from the DI container and registers its handler methods as HTTP routes.

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping, post_mapping, Body, PathVar, Valid


@rest_controller
@request_mapping("/api/orders")
class OrderController:

    def __init__(self, order_service: OrderService) -> None:
        self._service = order_service

    @get_mapping("/")
    async def list_orders(self) -> list[dict]:
        return await self._service.find_all()

    @get_mapping("/{order_id}")
    async def get_order(self, order_id: PathVar[str]) -> dict:
        return await self._service.find_by_id(order_id)

    @post_mapping("/", status_code=201)
    async def create_order(self, body: Valid[CreateOrderRequest]) -> dict:
        return await self._service.create(body)
```

### @rest_controller Stereotype

`@rest_controller` is a stereotype decorator from `pyfly.container`. It marks the class as both a DI-managed bean and a REST controller. Internally it sets:

- `__pyfly_injectable__ = True` -- registers the class in the DI container
- `__pyfly_stereotype__ = "rest_controller"` -- identifies the class for route auto-discovery

It supports all standard stereotype options:

```python
@rest_controller                              # Simple form
@rest_controller(name="orderCtrl")            # Named bean
@rest_controller(scope=Scope.SINGLETON)       # Custom scope
@rest_controller(profile="api")               # Profile-conditional
```

### @request_mapping: Class-Level Base Path

`@request_mapping(path)` sets the base URL path for all handler methods in the controller. The path is combined with each method's path to form the full route.

```python
@request_mapping("/api/v2/orders")
class OrderController:
    @get_mapping("/{id}")        # Full path: /api/v2/orders/{id}
    async def get(self, id: PathVar[str]): ...

    @get_mapping("/")            # Full path: /api/v2/orders/
    async def list_all(self): ...
```

Trailing slashes on the base path are automatically stripped to prevent double-slash issues.

Source file: `src/pyfly/web/mappings.py`

---

## HTTP Method Mappings

PyFly provides five HTTP method decorators, each created by the internal `_make_method_mapping()` factory. Every decorator accepts an optional relative path and an optional `status_code`.

### @get_mapping

Map a handler to HTTP GET requests. Used for read operations.

```python
@get_mapping("/")
async def list_items(self) -> list[dict]:
    return await self._service.find_all()

@get_mapping("/{item_id}")
async def get_item(self, item_id: PathVar[str]) -> dict:
    return await self._service.find_by_id(item_id)
```

### @post_mapping

Map a handler to HTTP POST requests. Typically used for create operations.

```python
@post_mapping("/", status_code=201)
async def create_item(self, body: Valid[CreateItemRequest]) -> dict:
    return await self._service.create(body)
```

### @put_mapping

Map a handler to HTTP PUT requests. Used for full-replacement updates.

```python
@put_mapping("/{item_id}")
async def replace_item(self, item_id: PathVar[str], body: Valid[UpdateItemRequest]) -> dict:
    return await self._service.replace(item_id, body)
```

### @delete_mapping

Map a handler to HTTP DELETE requests.

```python
@delete_mapping("/{item_id}", status_code=204)
async def delete_item(self, item_id: PathVar[str]) -> None:
    await self._service.delete(item_id)
```

### @patch_mapping

Map a handler to HTTP PATCH requests. Used for partial updates.

```python
@patch_mapping("/{item_id}")
async def update_item(self, item_id: PathVar[str], body: Valid[PatchItemRequest]) -> dict:
    return await self._service.patch(item_id, body)
```

### Parameters Reference

All five decorators share the same parameters:

| Parameter     | Type  | Default | Description                                                          |
|---------------|-------|---------|----------------------------------------------------------------------|
| `path`        | `str` | `""`    | Relative path appended to the `@request_mapping` base path           |
| `status_code` | `int` | `200`   | HTTP status code for the response                                    |

Path parameters use `{name}` syntax in the path string: `@get_mapping("/{order_id}")`.

Internally, each decorator stores a `__pyfly_mapping__` dict on the function with keys `"method"`, `"path"`, and `"status_code"`.

Source file: `src/pyfly/web/mappings.py`

---

## Request Parameter Binding

PyFly uses generic type annotations to bind handler parameters from the HTTP request. The `ParameterResolver` inspects handler signatures at startup and builds a resolution plan that is executed at request time.

### PathVar[T] -- Path Variables

Extracts a value from the URL path. The parameter name must match a `{placeholder}` in the route path.

```python
@get_mapping("/{order_id}")
async def get_order(self, order_id: PathVar[str]) -> dict:
    # order_id is extracted from the URL, e.g., "/api/orders/abc-123"
    return await self._service.find_by_id(order_id)
```

With type conversion:

```python
@get_mapping("/{order_id}")
async def get_order(self, order_id: PathVar[int]) -> dict:
    # Automatically converts the path segment to int
    return await self._service.find_by_id(order_id)
```

If the path variable is missing and no default is provided, a `ValueError` is raised with the message `"Missing path variable: {name}"`.

### QueryParam[T] -- Query Parameters

Extracts a value from the URL query string. Supports default values and optional parameters.

```python
@get_mapping("/")
async def list_orders(
    self,
    page: QueryParam[int] = 1,
    size: QueryParam[int] = 20,
    status: QueryParam[str] = None,
) -> dict:
    # GET /api/orders?page=2&size=10&status=active
    return await self._service.find_all(page=page, size=size, status=status)
```

- If the query parameter is not present and a default is provided, the default is used.
- If the query parameter is not present, has no default, and its type is **optional**
  (e.g. `QueryParam[str | None]`), `None` is returned.
- If the query parameter is not present, has no default, and is a **required**
  (non-`Optional`) type, the resolver raises `InvalidRequestException`
  (`code="MISSING_PARAMETER"`), which maps to **HTTP 400** — it no longer silently binds
  `None`. To make a parameter optional, give it a default (`status: QueryParam[str] = None`)
  or annotate it `QueryParam[str | None]`.
- The raw string value is automatically coerced to the annotated type `T`.

> **Note:** a parameter is treated as *required* when it has no Python default **and** its
> type does not admit `None`. The same rule applies to `Header[T]` and `Cookie[T]` below.

### Body[T] -- Request Body

Deserializes the JSON request body. When `T` is a Pydantic `BaseModel`, automatic validation is applied via `model_validate_json()`.

```python
from pydantic import BaseModel


class CreateOrderRequest(BaseModel):
    product_id: str
    quantity: int
    customer_id: str


@post_mapping("/", status_code=201)
async def create_order(self, body: Body[CreateOrderRequest]) -> dict:
    # body is a fully validated CreateOrderRequest instance
    return await self._service.create(body)
```

When `T` is not a Pydantic model, the raw body bytes are decoded as UTF-8 and passed to `T(decoded_string)`.

**Note:** With bare `Body[T]`, Pydantic validation still runs (via `model_validate_json()`), but validation errors propagate as raw Pydantic `ValidationError` exceptions. To get structured 422 error responses with detailed error information, use `Valid[T]` or `Valid[Body[T]]` instead. See the [Valid[T] -- Parameter Validation](#validt----parameter-validation) section.

### Header[T] -- HTTP Headers

Extracts an HTTP header value. The parameter name is converted from `snake_case` to `kebab-case` for the header lookup:

```python
@get_mapping("/")
async def list_orders(self, x_api_key: Header[str]) -> dict:
    # Reads the "x-api-key" header (snake_case -> kebab-case)
    return await self._service.find_all_for_key(x_api_key)
```

- A missing header with a default returns the default; a missing **optional** header
  (`Header[str | None]`) returns `None`.
- A missing **required** header (no default, non-`Optional` type) raises
  `InvalidRequestException` (`code="MISSING_PARAMETER"`) — **HTTP 400**.
- Supports type coercion just like other parameter types.

### Cookie[T] -- Cookies

Extracts a cookie value from the request:

```python
@get_mapping("/me")
async def get_current_user(self, session_id: Cookie[str]) -> dict:
    # Reads the "session_id" cookie
    return await self._service.get_by_session(session_id)
```

Like `QueryParam` and `Header`, a missing **required** cookie (no default, non-`Optional`
type) raises `InvalidRequestException` (`code="MISSING_PARAMETER"`) — **HTTP 400**. Use a
default or `Cookie[str | None]` to make it optional.

### Type Coercion

The `ParameterResolver` automatically coerces string values from the HTTP request to the annotated inner type `T`:

| Target Type | Conversion                          |
|-------------|-------------------------------------|
| `str`       | Passed through unchanged            |
| `int`       | `int(value)`                        |
| `float`     | `float(value)`                      |
| `bool`      | `bool(value)`                       |
| `UUID`      | `UUID(value)`                       |
| Any other   | `T(value)` -- calls the constructor |

The coercion is performed by the `_coerce()` method of `ParameterResolver`.

Source files:
- `src/pyfly/web/params.py` -- binding type definitions
- `src/pyfly/web/adapters/starlette/resolver.py` -- `ParameterResolver`

---

## Valid[T] -- Parameter Validation

`Valid[T]` is a generic marker type that triggers explicit Pydantic validation with structured 422 error responses. It is defined alongside the other binding types in `src/pyfly/web/params.py`:

```python
class Valid(Generic[T]):
    """Marks a parameter for explicit Pydantic validation with structured 422 errors."""
```

`Valid[T]` works by wrapping another binding type (or implying `Body[T]` when used standalone) and instructing the `ParameterResolver` to catch Pydantic `ValidationError` exceptions and convert them into `ValidationException` instances with `code="VALIDATION_ERROR"` and a `context` dict containing the full list of field-level errors.

### Standalone Usage: Valid[T]

When `Valid[T]` is used without an inner binding type, it implies `Body[T]` -- the request body is deserialized as JSON and validated:

```python
from pyfly.web import Valid, post_mapping


@post_mapping("/", status_code=201)
async def create_order(self, body: Valid[CreateOrderRequest]) -> OrderResponse:
    # body is a validated CreateOrderRequest instance
    # If validation fails, a structured 422 response is returned automatically
    return await self._service.create(body)
```

This is the most common usage pattern. It combines body deserialization with structured error handling in a single annotation.

### Wrapping Body[T]: Valid[Body[T]]

You can explicitly wrap `Body[T]` with `Valid` for clarity. The behavior is identical to standalone `Valid[T]`:

```python
from pyfly.web import Valid, Body, post_mapping


@post_mapping("/", status_code=201)
async def create_order(self, body: Valid[Body[CreateOrderRequest]]) -> OrderResponse:
    # Equivalent to Valid[CreateOrderRequest]
    return await self._service.create(body)
```

### Wrapping Other Binding Types

`Valid` can wrap any binding type to apply validation after the parameter has been resolved:

```python
from pyfly.web import Valid, QueryParam, Header, get_mapping


@get_mapping("/search")
async def search(
    self,
    page: Valid[QueryParam[int]],
    x_correlation_id: Valid[Header[str]],
) -> list[dict]:
    # page is resolved from query string, then validated
    # x_correlation_id is resolved from headers, then validated
    return await self._service.search(page=page)
```

For non-`Body` binding types (`QueryParam`, `Header`, `PathVar`, `Cookie`), the resolver first extracts the raw value using the normal resolution logic, then runs `_run_validation()` on the resolved value. For `BaseModel` instances (which have already been validated by Pydantic), this is a no-op. For dicts that need to be validated against a Pydantic model, `validate_model()` is called.

### Behavior Summary

| Annotation              | Binding Type | Validation Behavior                                               |
|-------------------------|--------------|-------------------------------------------------------------------|
| `Body[T]`               | Body         | Pydantic validation via `model_validate_json()`. Errors propagate as raw `ValidationError`. |
| `Valid[T]`              | Body         | Same deserialization, but errors are caught and converted to `ValidationException` with structured context. |
| `Valid[Body[T]]`        | Body         | Identical to `Valid[T]`.                                          |
| `Valid[QueryParam[T]]`  | QueryParam   | Query param resolved first, then validated. Errors become structured 422. |
| `Valid[Header[T]]`      | Header       | Header resolved first, then validated. Errors become structured 422. |
| `Valid[PathVar[T]]`     | PathVar      | Path variable resolved first, then validated.                     |
| `Valid[Cookie[T]]`      | Cookie       | Cookie resolved first, then validated.                            |

### Structured 422 Error Response

When validation fails on a `Valid`-annotated parameter, the `ParameterResolver` catches the Pydantic `ValidationError` and raises a `ValidationException`:

```python
raise ValidationException(
    f"Validation failed: {detail}",
    code="VALIDATION_ERROR",
    context={"errors": errors},
)
```

This exception is then handled by the global exception handler, producing a structured 422 response:

```json
{
  "error": {
    "message": "Validation failed: quantity: Input should be greater than 0",
    "code": "VALIDATION_ERROR",
    "status": 422,
    "path": "/api/orders/",
    "timestamp": "2026-02-15T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "errors": [
        {
          "type": "greater_than",
          "loc": ["quantity"],
          "msg": "Input should be greater than 0",
          "input": -1,
          "ctx": {"gt": 0}
        }
      ]
    }
  }
}
```

The `context.errors` array contains the full list of Pydantic validation errors, each with `type`, `loc` (field location), `msg` (human-readable message), `input` (the invalid value), and `ctx` (constraint context).

### Valid[T] vs. Bare Body[T]

The key difference between `Valid[T]` and bare `Body[T]`:

| Aspect                  | `Body[T]`                                     | `Valid[T]` / `Valid[Body[T]]`                |
|-------------------------|-----------------------------------------------|----------------------------------------------|
| Deserialization         | `model_validate_json(body_bytes)`             | Same                                         |
| On validation error     | Raw Pydantic `ValidationError` propagates     | Caught and converted to `ValidationException`|
| HTTP response           | May produce 500 if no converter configured    | Always produces structured 422               |
| Error detail            | Pydantic's default format                     | `code="VALIDATION_ERROR"`, `context={"errors": [...]}` |
| OpenAPI                 | Same request body schema                      | Same request body schema                     |

**Recommendation:** Use `Valid[T]` for all endpoints that accept user input. It ensures that validation failures always produce clean, structured 422 responses that API consumers can programmatically parse.

### ParameterResolver Internals for Valid[T]

The `ParameterResolver` handles `Valid[T]` in three stages:

1. **Inspection** (`_inspect()` at startup): When the resolver encounters a `Valid` origin type, it sets `validate=True` on the `ResolvedParam`, peels the `Valid` layer, and determines the inner binding type. If the inner type is itself a binding type (`Body`, `QueryParam`, etc.), that becomes the `binding_type`. If the inner type is a plain type (e.g., a Pydantic model), `Body` is implied.

2. **Resolution** (`resolve()` at request time): After resolving each parameter via `_resolve_one()`, if `param.validate` is `True`, the resolver calls `_run_validation(value, param)`.

3. **Body resolution** (`_resolve_body()` with validation): For `Body` parameters with `validate=True`, the resolver wraps the `model_validate_json()` call in a `try/except` block. If a Pydantic `ValidationError` is caught, it is converted into a `ValidationException` with a semicolon-delimited detail string and the raw error list in `context`.

The `ResolvedParam` dataclass carries the `validate` flag:

```python
@dataclass
class ResolvedParam:
    name: str
    binding_type: type       # PathVar, QueryParam, Body, Header, Cookie, or File
    inner_type: type         # The type argument T
    default: Any = _MISSING
    validate: bool = False   # True when wrapped in Valid[...]
    required: bool = True    # default is _MISSING and type does not admit None
```

The `required` flag is computed at inspection time as `default is _MISSING and not
_permits_none(inner_type)`. When a required scalar binding (`QueryParam`, `Header`,
`Cookie`) is absent at request time, the resolver raises `InvalidRequestException`
(`code="MISSING_PARAMETER"`, HTTP 400).

Source files:
- `src/pyfly/web/params.py` -- `Valid` class definition
- `src/pyfly/web/adapters/starlette/resolver.py` -- `ParameterResolver`, `ResolvedParam`

---

## Response Handling

### Return Value Conversion

PyFly automatically converts handler return values into HTTP responses using the `handle_return_value()` function:

| Return Value            | Response                                        |
|-------------------------|-------------------------------------------------|
| `None`                  | 204 No Content (unless `status_code` explicitly set) |
| `Response` (Starlette)  | Passed through unchanged                        |
| `BaseModel` (Pydantic)  | JSON via `model_dump(mode="json")`               |
| `dict`, `list`, `str`   | JSON response                                   |

Examples:

```python
@get_mapping("/{id}")
async def get_order(self, id: PathVar[str]) -> dict:
    return {"id": id, "status": "active"}       # -> 200 JSON

@delete_mapping("/{id}", status_code=204)
async def delete_order(self, id: PathVar[str]) -> None:
    await self._service.delete(id)               # -> 204 No Content

@get_mapping("/{id}")
async def get_order(self, id: PathVar[str]) -> OrderResponse:
    order = await self._service.find(id)
    return OrderResponse.model_validate(order)   # -> 200 JSON via model_dump
```

### handle_return_value()

The `handle_return_value(result, status_code=200, accept=None)` function is the core of response conversion. It is called by the `ControllerRegistrar` after each handler invocation. The optional `accept` parameter allows content negotiation — when `accept` contains `"application/xml"`, the result is serialized as XML instead of JSON.

```python
from pyfly.web.adapters.starlette.response import handle_return_value

response = handle_return_value({"key": "value"}, status_code=200)
# -> JSONResponse({"key": "value"}, status_code=200)

response = handle_return_value(None)
# -> Response(status_code=204)

response = handle_return_value(None, status_code=200)
# -> Response(status_code=204) -- when status_code is 200, None produces 204

response = handle_return_value(None, status_code=201)
# -> Response(status_code=201) -- non-200 status_code is preserved even for None
```

Source file: `src/pyfly/web/adapters/starlette/response.py`

---

## JSON & Content Negotiation

PyFly centralizes JSON (de)serialization the way Spring centralizes Jackson's `ObjectMapper`: a single, app-wide configuration boundary plus a registry for serializing arbitrary, non-Pydantic types. On top of that sits an ordered, pluggable `HttpMessageConverter` chain that reads request bodies and writes responses in multiple formats (JSON and XML out of the box), negotiating the format from the `Accept` header (with q-values) on write and the `Content-Type` header on read.

This layer is deliberately **not** a Jackson clone — there is no `@JsonView`, no Modules SPI, no `ObjectMapper` god-object, and no global `alias_generator` injected into your models. **Per-field behavior stays Pydantic's job**: `Field(alias=...)`, `@field_serializer`, discriminated unions, and validators all work exactly as they do in plain Pydantic. The pyfly layer adds only what Pydantic does not centralize — global toggles applied at the one serialization boundary, and a way to serialize types Pydantic never sees.

Source files:
- `src/pyfly/web/json.py` -- `JsonProperties`, `CamelModel`, `JsonSerializers`, `PyFlyJsonSerializer`, `json_properties_from_config()`
- `src/pyfly/web/message_converters.py` -- `MessageConverter`, `JsonMessageConverter`, `XmlMessageConverter`, `MessageConverterRegistry`, `default_message_converters()`, `parse_accept()`
- `src/pyfly/web/adapters/starlette/app.py` -- wires config + registry into `app.state`

### Global JSON Config: pyfly.web.json.\*

The `JsonProperties` model captures the global JSON settings, bound from the `pyfly.web.json.*` namespace at startup via `json_properties_from_config()`:

```yaml
# application.yml
pyfly:
  web:
    json:
      property-naming-strategy: as-is   # as-is | camelCase (camelCase implies by-alias on output)
      by-alias: false
      exclude-none: false
      exclude-defaults: false
      fail-on-unknown-properties: false
```

| Config key (`pyfly.web.json.*`) | `JsonProperties` field      | Type                       | Default  | Effect                                                                 |
|---------------------------------|-----------------------------|----------------------------|----------|------------------------------------------------------------------------|
| `property-naming-strategy`      | `property_naming_strategy`  | `"as-is"` \| `"camelCase"` | `as-is`  | `camelCase` dumps responses using field aliases (implies `by-alias`).  |
| `by-alias`                      | `by_alias`                  | `bool`                     | `false`  | Dump models using their Pydantic field aliases.                        |
| `exclude-none`                  | `exclude_none`              | `bool`                     | `false`  | Omit fields whose value is `None` from the JSON output.                |
| `exclude-defaults`              | `exclude_defaults`          | `bool`                     | `false`  | Omit fields still set to their default value.                          |
| `fail-on-unknown-properties`    | `fail_on_unknown_properties`| `bool`                     | `false`  | Reject request bodies containing keys not declared on the model (422). |

These settings apply at the single serialization boundary in `PyFlyJsonSerializer`, so they affect every response and every JSON request body — you do not annotate individual models. The `camelCase` strategy is a convenience: `JsonProperties.effective_by_alias()` returns `True` whenever `by_alias` is set **or** the strategy is `camelCase`, so picking `camelCase` automatically dumps by alias.

```python
from pyfly.web.json import JsonProperties, PyFlyJsonSerializer

serializer = PyFlyJsonSerializer(JsonProperties(exclude_none=True))
serializer.to_response_data(Item(item_name="a", qty=1))
# -> {"item_name": "a", "qty": 1}   # the None "note" field is dropped
```

`fail-on-unknown-properties` is enforced by the JSON message converter on **read**: when enabled, an unknown key in the request body raises a Pydantic `ValidationError` (which the converter chain surfaces as a structured 422). It does this without mutating your model — it transparently overlays an `extra="forbid"` subclass for the validation only.

> **Naming note.** `JsonProperties` is bound from `pyfly.web.json.*` by `json_properties_from_config()`, which reads kebab-case keys directly from the `Config` object. It is **not** registered with `@config_properties`; `create_app()` calls `json_properties_from_config(context.config)` itself.

### CamelModel: Opt-in camelCase Models

Because pyfly never injects a global `alias_generator` into your models, opting a model into camelCase JSON I/O is done per-model by inheriting from `CamelModel`:

```python
from pyfly.web.json import CamelModel


class CreateOrderRequest(CamelModel):
    customer_id: str
    line_items: list[str]
    unit_price: float
```

`CamelModel` configures `alias_generator=to_camel`, `populate_by_name=True`, and `serialize_by_alias=True`, so it:

- **accepts** both camelCase (`customerId`) and snake_case (`customer_id`) on input, and
- **emits** camelCase on output.

```python
from pyfly.web.json import CamelModel, JsonProperties, PyFlyJsonSerializer


class Item(CamelModel):
    item_name: str
    unit_price: float


s = PyFlyJsonSerializer(JsonProperties(property_naming_strategy="camelCase"))
s.to_response_data(Item(item_name="a", unit_price=1.5))
# -> {"itemName": "a", "unitPrice": 1.5}
```

Use `CamelModel` when you want a camelCase wire contract for specific DTOs without flipping the global strategy. (With the global `camelCase` strategy set, `CamelModel`'s aliases are what gets emitted; plain `BaseModel` fields have no aliases and so serialize unchanged.)

### JsonSerializers: Custom Non-Pydantic Types

Some values are not Pydantic models and are not JSON primitives — a `Money` value object, a domain enum wrapper, a third-party type. `JsonSerializers` is a registry of encoders for exactly these. Register an encoder once, typically via a DI bean, and the type serializes consistently everywhere:

```python
from decimal import Decimal

from pyfly.web.json import JsonSerializers


class Money:
    def __init__(self, amount: Decimal, currency: str) -> None:
        self.amount = amount
        self.currency = currency


registry = JsonSerializers()
registry.register(Money, encode=lambda m: {"amount": str(m.amount), "ccy": m.currency})
```

`encode_for()` honors inheritance (it walks the type's MRO), so an encoder registered for a base class also covers its subclasses. The serializer applies the registry recursively, so a `Money` nested inside a dict, list, or another structure is encoded correctly:

```python
from pyfly.web.json import JsonProperties, PyFlyJsonSerializer

s = PyFlyJsonSerializer(JsonProperties(), registry)
s.to_response_data({"price": Money(Decimal("9.99"), "USD")})
# -> {"price": {"amount": "9.99", "ccy": "USD"}}
```

**Wiring it into the app.** At startup, `create_app()` looks for a `JsonSerializers` bean in the `ApplicationContext`; if present, it is used as the registry for the app-wide `PyFlyJsonSerializer`. Register one as a bean so all responses use your encoders:

```python
from decimal import Decimal

from pyfly.container.bean import bean
from pyfly.web.json import JsonSerializers


@bean
def json_serializers() -> JsonSerializers:
    registry = JsonSerializers()
    registry.register(Money, encode=lambda m: {"amount": str(m.amount), "ccy": m.currency})
    return registry
```

Note that even without a registered encoder, `PyFlyJsonSerializer` already handles common stdlib types that bare `json.dumps` cannot: `datetime`/`date`/`time` (ISO format), `UUID` and `Decimal` (as strings, matching Pydantic's `mode="json"`), `Enum` (its `.value`), `set`/`frozenset` (as a list), `bytes` (UTF-8 decoded), and dataclasses (via `asdict`). The registry is for your own types beyond these.

### HttpMessageConverter Chain

The `MessageConverterRegistry` is an ordered list of `MessageConverter` instances, each bound to one or more media types and used for **both** reading request bodies and writing responses. The built-in registry from `default_message_converters()` contains, in order:

1. `JsonMessageConverter` -- `application/json` (the default)
2. `XmlMessageConverter` -- `application/xml`, `text/xml`

Both converters share the same `PyFlyJsonSerializer`, so the global `pyfly.web.json.*` config applies to every format — XML responses are normalized through the serializer too.

**Write — `Accept` negotiation with q-values.** `find_writer(accept)` parses the `Accept` header into media types ordered by descending q-value (`parse_accept()`) and returns the first converter that supports the highest-priority type. JSON is the fallback when nothing matches or no `Accept` header is sent.

```python
from pyfly.web.message_converters import default_message_converters

reg = default_message_converters()
reg.find_writer("application/xml")                                   # -> XmlMessageConverter
reg.find_writer("application/json")                                  # -> JsonMessageConverter
reg.find_writer(None)                                                # -> JsonMessageConverter (default)
reg.find_writer("application/json;q=0.8, application/xml;q=0.9")     # -> XmlMessageConverter (higher q)
```

In a controller, this happens automatically — the `ControllerRegistrar` passes the request's `Accept` header and the app's registry to `handle_return_value()`, which negotiates the writer:

```python
@rest_controller
class WidgetController:
    @get_mapping("/widget")
    async def get_widget(self) -> Widget:
        return Widget(name="gadget", qty=3)
```

```
GET /widget   Accept: application/json   ->  {"name": "gadget", "qty": 3}
GET /widget   Accept: application/xml    ->  <response><name>gadget</name><qty>3</qty></response>
```

**Read — `Content-Type` parsing (including XML bodies).** `find_reader(content_type)` selects the converter for the request's `Content-Type`, falling back to the first converter (JSON) when the header is absent or unrecognized. This means `Body[T]` and `Valid[T]` parameters now accept XML request bodies, not just JSON:

```python
@post_mapping("/widget")
async def echo_widget(self, widget: Body[Widget]) -> Widget:
    return widget
```

```
POST /widget   Content-Type: application/json
{"name": "x", "qty": 5}
->  Widget(name="x", qty=5)

POST /widget   Content-Type: application/xml
<widget><name>y</name><qty>7</qty></widget>
->  Widget(name="y", qty=7)        # XML text "7" is coerced back to int by Pydantic
```

The XML converter unwraps the single root element and validates the inner dict against the target Pydantic model, so XML and JSON bodies bind to the same model identically.

### Registering a Custom MessageConverterRegistry

To support an additional format (CBOR, MessagePack, etc.) or to reorder priority, expose a `MessageConverterRegistry` bean. When present in the `ApplicationContext`, it **fully replaces** the default registry for the app. Build on `default_message_converters()` and `add()` your converter — `add()` inserts at the front, so user-added converters take priority:

```python
from pyfly.container.bean import bean
from pyfly.web.json import PyFlyJsonSerializer
from pyfly.web.message_converters import MessageConverter, MessageConverterRegistry, default_message_converters


class CborConverter(MessageConverter):
    media_types = ("application/cbor",)

    def read(self, body: bytes, target_type: type):
        import cbor2

        data = cbor2.loads(body)
        if isinstance(target_type, type) and issubclass(target_type, BaseModel):
            return target_type.model_validate(data)
        return data

    def write(self, value) -> tuple[bytes, str]:
        import cbor2

        return cbor2.dumps(value), "application/cbor"


@bean
def message_converters() -> MessageConverterRegistry:
    registry = default_message_converters(PyFlyJsonSerializer())
    registry.add(CborConverter())   # inserted at front; JSON + XML still work
    return registry
```

A `MessageConverter` subclass defines its `media_types` tuple and implements `read(body, target_type)` and `write(value) -> (body_bytes, content_type)`. The base `supports()` matches the bare media type (a `;`-suffixed `Content-Type` is normalized, and `*/*` matches anything).

> **How it is wired.** `create_app()` stores the negotiated serializer on `app.state.pyfly_json_serializer` and the converter registry on `app.state.pyfly_message_converters`. The `ParameterResolver` reads `pyfly_message_converters` to pick a reader by `Content-Type`; the `ControllerRegistrar` reads it to pick a writer by `Accept`. `fail-on-unknown-properties` is threaded into the default JSON converter via `default_message_converters(serializer, fail_on_unknown=...)`.

### RFC 7807 problem+json

By default, the global exception handler emits the `{"error": {...}}` envelope (see [Global Exception Handler](#global-exception-handler)). Setting `pyfly.web.problem-details.enabled: true` switches it to RFC 7807 `application/problem+json` responses instead, matching Spring Boot 3's `spring.mvc.problemdetails.enabled`:

```yaml
pyfly:
  web:
    problem-details:
      enabled: false   # opt-in; default off preserves the {"error": {...}} envelope
```

With it enabled, a `ResourceNotFoundException("widget not found", code="WIDGET_NOT_FOUND")` raised at `/widgets/42` produces:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "widget not found",
  "instance": "/widgets/42",
  "code": "WIDGET_NOT_FOUND",
  "transactionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "timestamp": "2026-06-07T10:30:00+00:00"
}
```

The response uses the `application/problem+json` media type. `type`, `title`, `status`, `detail`, and `instance` are the standard RFC 7807 members (`title` is the HTTP reason phrase for the status; `instance` is the request path). `code`, `transactionId`, `timestamp`, and an optional `context` are pyfly extension members. The same exception-to-status mapping described under [Exception-to-Status Mapping](#exception-to-status-mapping) applies in both modes.

Source files:
- `src/pyfly/web/adapters/starlette/errors.py` -- `global_exception_handler`, problem+json emission
- `src/pyfly/web/adapters/starlette/app.py` -- reads `pyfly.web.problem-details.enabled` onto `app.state.pyfly_problem_details`

---

## Exception Handling

PyFly provides a two-tier exception handling system: controller-level handlers for specific exceptions, and a global handler for everything else.

### Controller-Level Exception Handlers

Use the `@exception_handler` decorator to define methods that catch specific exceptions within a controller:

```python
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import exception_handler


@rest_controller
@request_mapping("/api/orders")
class OrderController:

    @get_mapping("/{order_id}")
    async def get_order(self, order_id: PathVar[str]) -> dict:
        order = await self._service.find_by_id(order_id)
        if not order:
            raise ResourceNotFoundException(f"Order {order_id} not found")
        return order

    @exception_handler(ResourceNotFoundException)
    async def handle_not_found(self, exc: ResourceNotFoundException):
        return 404, {"error": str(exc), "code": exc.code}

    @exception_handler(ValueError)
    async def handle_bad_input(self, exc: ValueError):
        return 400, {"error": str(exc)}
```

Exception handler methods can return:

- A `(status_code, body)` tuple -- converted to a `JSONResponse`
- A Starlette `Response` object -- passed through unchanged
- Any other value -- processed through `handle_return_value()`

When multiple exception handlers could match (e.g., both `Exception` and `ValueError`), handlers are sorted by MRO depth (most specific first), so subclass exceptions are matched before their parents.

Source file: `src/pyfly/web/exception_handler.py`

### Global Exception Handler

Unhandled exceptions (those not caught by controller-level handlers) are caught by the global exception handler, which produces RFC 7807-style structured JSON error responses:

```json
{
  "error": {
    "message": "Order not found",
    "code": "ORDER_NOT_FOUND",
    "status": 404,
    "path": "/api/orders/abc-123",
    "timestamp": "2026-02-15T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }
}
```

For `PyFlyException` subclasses, the response includes the exception's `message`, `code`, and optional `context` dict. For unknown exceptions, the message is generalized to `"Internal server error"` with code `"INTERNAL_ERROR"` to avoid leaking internal details.

Source file: `src/pyfly/web/adapters/starlette/errors.py`

### Exception-to-Status Mapping

The global handler maps `PyFlyException` subclasses to HTTP status codes:

| Exception                     | HTTP Status |
|-------------------------------|-------------|
| `ValidationException`         | 422         |
| `ResourceNotFoundException`   | 404         |
| `ConflictException`           | 409         |
| `PreconditionFailedException` | 412         |
| `GoneException`               | 410         |
| `InvalidRequestException`     | 400         |
| `LockedResourceException`     | 423         |
| `MethodNotAllowedException`   | 405         |
| `UnsupportedMediaTypeException` | 415       |
| `PayloadTooLargeException`    | 413         |
| `UnauthorizedException`       | 401         |
| `ForbiddenException`          | 403         |
| `SecurityException`           | 403         |
| `QuotaExceededException`      | 429         |
| `RateLimitException`          | 429         |
| `CircuitBreakerException`     | 503         |
| `BulkheadException`           | 503         |
| `ServiceUnavailableException` | 503         |
| `DegradedServiceException`    | 503         |
| `OperationTimeoutException`   | 504         |
| `NotImplementedException`     | 501         |
| `BadGatewayException`         | 502         |
| `GatewayTimeoutException`     | 504         |
| `BusinessException` (catch-all) | 400       |
| `InfrastructureException` (catch-all) | 502 |
| Unknown exceptions            | 500         |

### Exception Converters

PyFly includes a chain-of-responsibility exception converter system (`pyfly.web.converters`) that translates common library exceptions into PyFly exceptions before the global handler responds. When the global handler catches a **non-`PyFlyException`**, it first runs the per-app `ExceptionConverterService` (stashed on `app.state.pyfly_exception_converter_service`); if a converter matches, the converted PyFly exception is mapped to its proper status code instead of a generic 500.

The built-in chain (`default_exception_converters()`) is:

| Library Exception                              | PyFly Exception             | HTTP |
|------------------------------------------------|-----------------------------|------|
| `pydantic.ValidationError`                     | `ValidationException`       | 422  |
| `json.JSONDecodeError`                         | `InvalidRequestException`   | 400  |
| `TimeoutError` / `asyncio.TimeoutError`        | `OperationTimeoutException` | 504  |

User-provided `ExceptionConverter` beans discovered in the `ApplicationContext` are appended to this chain by `build_exception_converter_service()`.

You can register custom converters by implementing the `ExceptionConverter` protocol:

```python
from pyfly.web.converters import ExceptionConverter
from pyfly.kernel.exceptions import PyFlyException, BusinessException


class MyLibraryExceptionConverter:
    def can_handle(self, exc: Exception) -> bool:
        return isinstance(exc, MyLibraryError)

    def convert(self, exc: Exception) -> PyFlyException:
        return BusinessException(str(exc), code="MY_LIB_ERROR")
```

The `ExceptionConverterService` iterates through registered converters and returns the first match:

```python
class ExceptionConverterService:
    def __init__(self, converters: list[ExceptionConverter]) -> None:
        self._converters = converters

    def convert(self, exc: Exception) -> PyFlyException | None:
        for converter in self._converters:
            if converter.can_handle(exc):
                return converter.convert(exc)
        return None
```

Source file: `src/pyfly/web/converters.py`

---

## WebFilter Chain

PyFly replaces traditional per-layer middleware with a composable **WebFilter chain**. All filters are wrapped inside a single `WebFilterChainMiddleware` instance, which avoids the per-middleware task-context overhead that Starlette's `BaseHTTPMiddleware` incurs when multiple middleware layers are stacked. Filters are sorted by priority using the `@order` decorator and executed in sequence.

### WebFilter Protocol

The `WebFilter` protocol (`src/pyfly/web/ports/filter.py`) defines the contract that all filters must satisfy:

```python
from typing import Any, Protocol, runtime_checkable
from collections.abc import Callable, Coroutine

CallNext = Callable[..., Coroutine[Any, Any, Any]]


@runtime_checkable
class WebFilter(Protocol):
    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        """Execute this filter's logic."""
        ...

    def should_not_filter(self, request: Any) -> bool:
        """Return True to skip this filter for the given request."""
        ...
```

| Method             | Description                                                                                       |
|--------------------|---------------------------------------------------------------------------------------------------|
| `do_filter()`      | Performs the filter logic. Must call `await call_next(request)` to continue the chain.            |
| `should_not_filter()` | Returns `True` to skip this filter for the given request. Called before `do_filter()`.          |

The protocol uses generic `Any` types for Request and Response so that vendor-specific types (e.g., Starlette's `Request` and `Response`) remain confined to the adapter layer. The `CallNext` type alias represents the next callable in the chain.

Source file: `src/pyfly/web/ports/filter.py`

### OncePerRequestFilter Base Class

The `OncePerRequestFilter` (`src/pyfly/web/filters.py`) is an abstract base class that provides automatic URL-pattern matching. Most filters should extend this class rather than implementing `WebFilter` directly:

```python
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


class MyFilter(OncePerRequestFilter):
    url_patterns = ["/api/*"]
    exclude_patterns = ["/api/public/*"]

    async def do_filter(self, request, call_next: CallNext):
        # Filter logic here
        response = await call_next(request)
        return response
```

| Attribute          | Type         | Default | Description                                                       |
|--------------------|--------------|---------|-------------------------------------------------------------------|
| `url_patterns`     | `list[str]`  | `[]`    | Glob patterns this filter applies to. Empty = all paths.          |
| `exclude_patterns` | `list[str]`  | `[]`    | Glob patterns to exclude, even if `url_patterns` matches.         |

The `should_not_filter()` implementation uses `fnmatch` (glob-style matching) to check the request path:

1. If `url_patterns` is non-empty, at least one pattern must match. If none match, the filter is skipped.
2. If any `exclude_patterns` pattern matches, the filter is skipped (even if `url_patterns` matched).
3. If `url_patterns` is empty (default), the filter applies to all paths (subject to `exclude_patterns`).

Subclasses only need to implement `do_filter()`. The `should_not_filter()` logic is handled automatically.

Source file: `src/pyfly/web/filters.py`

### Built-in Filters

PyFly ships with several built-in filter implementations in `src/pyfly/web/adapters/starlette/filters/`. Five of them (`RequestContextFilter`, `CorrelationFilter`, `TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter`) are automatically included in every application created by `create_app()`. The `SecurityFilter` and `HttpSecurityFilter` are opt-in and must be registered as beans in the DI container.

#### TransactionIdFilter

Ensures every request/response pair carries a unique transaction ID for distributed tracing.

**Order:** `HIGHEST_PRECEDENCE + 100` (runs after `RequestContextFilter` and `CorrelationFilter`)

**Behavior:**
1. Checks for an incoming `X-Transaction-Id` header.
2. If present, propagates it. If absent, generates a new UUID.
3. Stores the ID on `request.state.transaction_id`.
4. Adds the `X-Transaction-Id` header to the response.

```
Request  -> X-Transaction-Id: abc-123    -> propagated
Request  -> (no header)                  -> X-Transaction-Id: <generated-uuid>
Response -> X-Transaction-Id: abc-123
```

Source file: `src/pyfly/web/adapters/starlette/filters/transaction_id_filter.py`

#### RequestLoggingFilter

Logs every HTTP request with structured fields using `structlog`.

**Order:** `HIGHEST_PRECEDENCE + 200` (runs after TransactionIdFilter)

Logged fields:
- HTTP method
- Request path
- Response status code
- Duration in milliseconds
- Transaction ID

Sample structured log output:

```
event=http_request method=GET path=/api/orders status_code=200 duration_ms=12.34 transaction_id=abc-123
```

Failed requests are logged at the `error` level with the exception type and message:

```
event=http_request_failed method=POST path=/api/orders duration_ms=5.67 transaction_id=abc-123 error="Validation failed" error_type=ValidationException
```

Source file: `src/pyfly/web/adapters/starlette/filters/request_logging_filter.py`

#### SecurityHeadersFilter

Adds OWASP-recommended security headers to every response.

**Order:** `HIGHEST_PRECEDENCE + 300` (runs after RequestLoggingFilter)

Accepts an optional `SecurityHeadersConfig` in its constructor. If none is provided, OWASP defaults are used:

```python
class SecurityHeadersFilter(OncePerRequestFilter):
    def __init__(self, config: SecurityHeadersConfig | None = None) -> None:
        self._config = config or SecurityHeadersConfig()
```

The filter adds these headers to every response:

| Header                        | Default Value                                |
|-------------------------------|----------------------------------------------|
| `X-Content-Type-Options`      | `nosniff`                                    |
| `X-Frame-Options`             | `DENY`                                       |
| `Strict-Transport-Security`   | `max-age=31536000; includeSubDomains`        |
| `X-XSS-Protection`            | `0`                                          |
| `Referrer-Policy`             | `strict-origin-when-cross-origin`            |
| `Content-Security-Policy`     | *(omitted unless configured)*                |
| `Permissions-Policy`          | *(omitted unless configured)*                |

Source file: `src/pyfly/web/adapters/starlette/filters/security_headers_filter.py`

#### SecurityFilter

Extracts JWT Bearer tokens from the `Authorization` header and populates a `SecurityContext` on `request.state`. This filter is **not** automatically included -- it must be registered as a DI bean.

```python
from pyfly.web.adapters.starlette.filters import SecurityFilter

security_filter = SecurityFilter(
    jwt_service=my_jwt_service,
    exclude_patterns=["/api/public/*", "/docs", "/openapi.json"],
)
```

**Behavior:**
1. Reads the `Authorization` header.
2. If it starts with `Bearer `, extracts the token and calls `jwt_service.to_security_context(token)`.
3. If the token is missing or invalid, sets `SecurityContext.anonymous()`.
4. Stores the `SecurityContext` on `request.state.security_context`.
5. Uses `OncePerRequestFilter.exclude_patterns` to skip public endpoints.

```python
from pyfly.container import component
from pyfly.security.jwt import JWTService
from pyfly.web.adapters.starlette.filters import SecurityFilter


@component
class AppSecurityFilter(SecurityFilter):
    def __init__(self, jwt_service: JWTService) -> None:
        super().__init__(
            jwt_service=jwt_service,
            exclude_patterns=["/api/auth/*", "/docs", "/redoc", "/openapi.json"],
        )
```

When registered as a DI bean, the filter is automatically discovered by `create_app()` and added to the filter chain.

Source file: `src/pyfly/web/adapters/starlette/filters/security_filter.py`

### WebFilterChainMiddleware

The `WebFilterChainMiddleware` (`src/pyfly/web/adapters/starlette/filter_chain.py`) is a pure ASGI middleware (not Starlette's `BaseHTTPMiddleware`) that wraps all `WebFilter` instances into a chain. Using raw ASGI avoids the `anyio` dependency that caused `ModuleNotFoundError` with servers like Granian that do not register with sniffio.

```python
class WebFilterChainMiddleware:
    def __init__(self, app: ASGIApp, filters: Sequence[WebFilter] = ()) -> None:
        self.app = app
        self._filters = filters if isinstance(filters, list) else list(filters)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive, send)
        chain = _call_app  # terminal: runs the downstream ASGI app
        for f in reversed(self._filters):
            chain = _wrap(f, chain)
        response = await chain(request)
        await response(scope, receive, send)
```

The chain is built from right to left: the last filter in the list wraps `call_next` (the route handler), the second-to-last wraps that, and so on. The first filter in the list is the outermost wrapper, meaning it executes first.

Each filter's `should_not_filter()` is checked before invocation. If it returns `True`, the filter is skipped and the next one in the chain runs:

```python
def _wrap(web_filter: WebFilter, next_call: CallNext) -> CallNext:
    async def _inner(request: Request) -> Response:
        if web_filter.should_not_filter(request):
            return await next_call(request)
        return await web_filter.do_filter(request, next_call)
    return _inner
```

Source file: `src/pyfly/web/adapters/starlette/filter_chain.py`

### Filter Ordering with @order

Filters are sorted by priority using the `@order` decorator from `pyfly.container.ordering`. Lower values run first (higher priority).

```python
from pyfly.container.ordering import order, HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE


class RequestContextFilter(OncePerRequestFilter):
    __pyfly_order__ = HIGHEST_PRECEDENCE  # Runs first: order = -2147483648

@order(HIGHEST_PRECEDENCE + 50)
class CorrelationFilter(OncePerRequestFilter):
    ...  # Runs second: order = -2147483598

@order(HIGHEST_PRECEDENCE + 100)
class TransactionIdFilter(OncePerRequestFilter):
    ...  # Runs third: order = -2147483548

@order(HIGHEST_PRECEDENCE + 200)
class RequestLoggingFilter(OncePerRequestFilter):
    ...  # Runs fourth: order = -2147483448

@order(HIGHEST_PRECEDENCE + 300)
class SecurityHeadersFilter(OncePerRequestFilter):
    ...  # Runs fifth: order = -2147483348
```

| Constant             | Value          | Description                         |
|----------------------|----------------|-------------------------------------|
| `HIGHEST_PRECEDENCE` | `-2147483648`  | Smallest possible order value       |
| `LOWEST_PRECEDENCE`  | `2147483647`   | Largest possible order value        |
| *(undecorated)*      | `0`            | Default order for user filters      |

Built-in filters use `HIGHEST_PRECEDENCE + offset` to ensure they always run before user-defined filters. User filters that are not decorated with `@order` default to `0`, placing them after all built-in filters.

To control the order of your custom filters:

```python
from pyfly.container.ordering import order


@order(10)
class AuthorizationFilter(OncePerRequestFilter):
    ...  # Runs before filters with order > 10

@order(20)
class AuditFilter(OncePerRequestFilter):
    ...  # Runs after AuthorizationFilter
```

Source file: `src/pyfly/container/ordering.py`

### Auto-Discovery of User Filters

When `create_app()` is called with an `ApplicationContext`, it automatically discovers all `WebFilter` beans registered in the DI container. The discovery logic:

1. Iterates over all bean registrations in the container.
2. For each bean instance that implements the `WebFilter` protocol (checked via `isinstance`), adds it to the filter list.
3. Skips built-in filter types (`TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter`) to avoid duplicates, since these are always included.
4. Sorts the combined list (built-in + user filters) by `@order` value.
5. Wraps all filters into a single `WebFilterChainMiddleware`.

```python
# In create_app():
filters: list[WebFilter] = [
    RequestContextFilter(),
    CorrelationFilter(),
    TransactionIdFilter(),
    RequestLoggingFilter(),
    SecurityHeadersFilter(),
]

builtin_filter_types = (
    RequestContextFilter,
    CorrelationFilter,
    TransactionIdFilter,
    RequestLoggingFilter,
    SecurityHeadersFilter,
)

# Auto-discover user WebFilter beans from context
if context is not None:
    for _cls, reg in context.container._registrations.items():
        inst = reg.instance
        if (
            inst is not None
            and isinstance(inst, WebFilter)
            and not isinstance(inst, builtin_filter_types)
        ):
            filters.append(inst)

# Sort all filters by @order
filters.sort(key=lambda f: get_order(type(f)))

middleware = [Middleware(WebFilterChainMiddleware, filters=filters)]
```

### Creating Custom Filters

To create a custom filter, extend `OncePerRequestFilter` and register it as a DI bean:

```python
from pyfly.container import component
from pyfly.container.ordering import order
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


@component
@order(50)
class RateLimitFilter(OncePerRequestFilter):
    """Rate-limits API requests by client IP."""

    url_patterns = ["/api/*"]
    exclude_patterns = ["/api/health", "/api/public/*"]

    def __init__(self, rate_limiter: RateLimiterService) -> None:
        self._limiter = rate_limiter

    async def do_filter(self, request, call_next: CallNext):
        client_ip = request.client.host
        if not await self._limiter.allow(client_ip):
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"error": {"message": "Rate limit exceeded", "code": "RATE_LIMITED"}},
                status_code=429,
            )
        return await call_next(request)
```

Key points:

1. **Extend `OncePerRequestFilter`** to get automatic URL-pattern matching via `url_patterns` and `exclude_patterns`.
2. **Decorate with `@component`** (or any DI stereotype) so the filter is managed by the container and auto-discovered by `create_app()`.
3. **Use `@order(N)`** to control execution order. Undecorated filters default to `0`.
4. **Implement `do_filter()`** -- call `await call_next(request)` to continue the chain, or return a response early to short-circuit.
5. **Dependencies are injected** via the constructor, just like any other PyFly bean.

Alternatively, you can implement the `WebFilter` protocol directly for full control over `should_not_filter()`:

```python
from pyfly.container import component
from pyfly.web.ports.filter import WebFilter, CallNext


@component
class CustomFilter:
    """Implements WebFilter protocol directly."""

    async def do_filter(self, request, call_next: CallNext):
        response = await call_next(request)
        response.headers["X-Custom-Header"] = "custom-value"
        return response

    def should_not_filter(self, request) -> bool:
        # Custom skip logic
        return request.url.path.startswith("/internal/")
```

---

## Server-Sent Events (SSE)

PyFly provides built-in support for Server-Sent Events, allowing controllers to stream real-time data to clients over HTTP. The SSE module follows the same decorator-driven approach as HTTP mappings and integrates with the controller auto-discovery system.

```python
from pyfly.web.sse import SseEmitter, format_sse_event, sse_mapping
```

### @sse_mapping Decorator

The `@sse_mapping(path)` decorator marks a controller method as an SSE endpoint. The decorated method should be an async generator that yields data objects. Each yielded value is automatically formatted as an SSE event and streamed to the client.

```python
import asyncio
from pyfly.container import rest_controller
from pyfly.web import request_mapping
from pyfly.web.sse import sse_mapping


@rest_controller
@request_mapping("/events")
class StockController:

    @sse_mapping("/prices")
    async def stream_prices(self):
        while True:
            yield {"symbol": "AAPL", "price": 150.25}
            await asyncio.sleep(1)
```

The path is appended to the controller's `@request_mapping` base path, so the full SSE endpoint in this example is `GET /events/prices`.

**How it works internally:**

1. The `@sse_mapping` decorator stores `__pyfly_sse_mapping__ = {"path": path}` on the method.
2. The `SSERegistrar` discovers these methods during `create_app()` and creates Starlette `Route` objects.
3. At request time, the handler method is called, its async generator output is wrapped in a `StreamingResponse` with `Content-Type: text/event-stream`, and data is streamed to the client.

Yields that are already formatted SSE strings are passed through unchanged. All other yields (dicts, Pydantic models, lists, etc.) are auto-wrapped with `format_sse_event()`.

**Source:** `src/pyfly/web/sse/decorators.py`

### format_sse_event()

The `format_sse_event()` function formats a single Server-Sent Event according to the SSE specification:

```python
from pyfly.web.sse import format_sse_event

# Basic event with dict data
event = format_sse_event({"price": 42.0})
# "data: {\"price\": 42.0}\n\n"

# Named event with ID
event = format_sse_event({"price": 42.0}, event="tick", id="123")
# "id: 123\nevent: tick\ndata: {\"price\": 42.0}\n\n"

# With retry interval
event = format_sse_event("reconnect", retry=5000)
# "retry: 5000\ndata: reconnect\n\n"
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `data` | `Any` | required | Event payload. Pydantic models are serialized via `model_dump_json()`, dicts/lists via `json.dumps()`, and other types via `str()`. |
| `event` | `str \| None` | `None` | Optional event type name (maps to the `event:` SSE field) |
| `id` | `str \| None` | `None` | Optional event ID (maps to the `id:` SSE field) |
| `retry` | `int \| None` | `None` | Optional reconnection time in milliseconds (maps to the `retry:` SSE field) |

The function returns a fully-formed SSE event string terminated by a double newline (`\n\n`), as required by the SSE specification.

**Source:** `src/pyfly/web/sse/response.py`

### SseEmitter

The `SseEmitter` class provides a queue-based interface for building SSE events. It is itself an async iterable, making it suitable for passing to a streaming response:

```python
from pyfly.web.sse import SseEmitter

emitter = SseEmitter()
emitter.send({"price": 42.0}, event="tick")
emitter.send({"price": 43.5}, event="tick", id="2")
emitter.close()

# Iterate over queued events
async for chunk in emitter:
    print(chunk)
```

**Methods:**

| Method | Description |
|---|---|
| `send(data, event=None, id=None)` | Enqueue an SSE event. Ignored after `close()`. |
| `close()` | Mark the emitter as closed. No further events are accepted. |
| `__aiter__()` | Iterate over all queued events as formatted SSE strings. |

The `SseEmitter` is useful when you need to build up a batch of events before streaming, or when you want to encapsulate SSE event creation in a service layer.

**Source:** `src/pyfly/web/sse/response.py`

### SSE Route Discovery

SSE routes are automatically discovered by the `SSERegistrar` during `create_app()`. The registrar scans all `@rest_controller` and `@controller` beans for methods decorated with `@sse_mapping` and creates Starlette `Route` objects.

The route creation follows the same lazy-resolution pattern used for HTTP and WebSocket routes:

1. The controller bean is resolved from the `ApplicationContext` on the first request.
2. A `ParameterResolver` is built for the handler method, enabling parameter binding (e.g., `QueryParam`, `PathVar`).
3. The handler's async generator output is wrapped in a `StreamingResponse` with SSE headers.

**SSE response headers:**

| Header | Value | Purpose |
|---|---|---|
| `Content-Type` | `text/event-stream` | Identifies the response as an SSE stream |
| `Cache-Control` | `no-cache` | Prevents caching of the event stream |
| `Connection` | `keep-alive` | Keeps the connection open for streaming |
| `X-Accel-Buffering` | `no` | Disables proxy buffering (nginx, etc.) |

**Source:** `src/pyfly/web/sse/adapters/starlette.py`

### Complete SSE Example

A full example demonstrating SSE streaming with a controller, including typed events and parameter binding:

```python
import asyncio
from pydantic import BaseModel

from pyfly.container import rest_controller, service
from pyfly.web import request_mapping, QueryParam
from pyfly.web.sse import sse_mapping, format_sse_event


class PriceUpdate(BaseModel):
    symbol: str
    price: float
    volume: int


@service
class MarketDataService:
    async def stream_prices(self, symbol: str):
        """Simulate a real-time price feed."""
        import random
        price = 150.0
        while True:
            price += random.uniform(-1.0, 1.0)
            yield PriceUpdate(
                symbol=symbol,
                price=round(price, 2),
                volume=random.randint(100, 10000),
            )
            await asyncio.sleep(0.5)


@rest_controller
@request_mapping("/api/market")
class MarketController:

    def __init__(self, market_service: MarketDataService) -> None:
        self._service = market_service

    @sse_mapping("/prices")
    async def stream_prices(self, symbol: QueryParam[str] = "AAPL"):
        """Stream real-time price updates for a given symbol.

        Connect via: GET /api/market/prices?symbol=AAPL
        """
        async for update in self._service.stream_prices(symbol):
            yield update  # Auto-formatted as SSE via format_sse_event()

    @sse_mapping("/heartbeat")
    async def heartbeat(self):
        """Simple heartbeat stream for connection health monitoring."""
        counter = 0
        while True:
            # Yield a pre-formatted SSE string
            yield format_sse_event(
                {"seq": counter, "status": "alive"},
                event="heartbeat",
                id=str(counter),
            )
            counter += 1
            await asyncio.sleep(5)
```

**Client-side JavaScript:**

```javascript
const eventSource = new EventSource('/api/market/prices?symbol=AAPL');

eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log(`${data.symbol}: $${data.price} (vol: ${data.volume})`);
};

eventSource.addEventListener('heartbeat', (event) => {
    console.log('Heartbeat:', JSON.parse(event.data));
});

eventSource.onerror = () => {
    console.log('Connection lost, reconnecting...');
};
```

---

## CORS Configuration

Configure Cross-Origin Resource Sharing with the `CORSConfig` dataclass:

```python
from pyfly.web import CORSConfig

cors = CORSConfig(
    allowed_origins=["https://myapp.com", "http://localhost:3000"],
    allowed_methods=["GET", "POST", "PUT", "DELETE"],
    allowed_headers=["*"],
    allow_credentials=True,
    exposed_headers=["X-Transaction-Id"],
    max_age=3600,
)

app = create_app(title="My API", context=ctx, cors=cors)
```

| Field               | Type         | Default   | Description                            |
|---------------------|--------------|-----------|----------------------------------------|
| `allowed_origins`   | `list[str]`  | `["*"]`   | Origins permitted to access the API    |
| `allowed_methods`   | `list[str]`  | `["GET"]` | HTTP methods allowed in CORS requests  |
| `allowed_headers`   | `list[str]`  | `["*"]`   | Request headers allowed in CORS        |
| `allow_credentials` | `bool`       | `False`   | Allow cookies and auth headers         |
| `exposed_headers`   | `list[str]`  | `[]`      | Response headers visible to the browser|
| `max_age`           | `int`        | `600`     | Preflight cache duration in seconds    |

`CORSConfig` is a frozen dataclass. When passed to `create_app()`, it is translated into Starlette's built-in `CORSMiddleware`.

### Auto-Configuration from `pyfly.web.cors.*`

You usually do not need to construct `CORSConfig` by hand. When no explicit `cors=` is
passed to `create_app()` and an `ApplicationContext` is available, the framework calls
`CORSConfig.from_config(context.config)` to build CORS from configuration:

```yaml
pyfly:
  web:
    cors:
      enabled: true                       # secure-by-default: CORS is OFF unless set
      allowed-origins: ["https://app.example.com"]
      allowed-methods: ["GET", "POST"]
      allowed-headers: ["*"]
      allow-credentials: false
      exposed-headers: ["X-Transaction-Id"]
      max-age: 600
```

| Config key (`pyfly.web.cors.*`) | Maps to | Default when CORS enabled |
|---|---|---|
| `enabled` | (gate) | `false` — when falsey, `from_config()` returns `None` and **no** CORS middleware is added |
| `allowed-origins` | `allowed_origins` | `["*"]` |
| `allowed-methods` | `allowed_methods` | `["GET", "HEAD", "POST"]` (Spring permit-default set) |
| `allowed-headers` | `allowed_headers` | `["*"]` |
| `allow-credentials` | `allow_credentials` | `false` |
| `exposed-headers` | `exposed_headers` | `[]` |
| `max-age` | `max_age` | `600` |

Both kebab-case (`allowed-origins`) and snake_case (`allowed_origins`) keys are accepted,
and list values may be given as a YAML list or a comma-separated string. CORS is
**secure-by-default**: it is disabled unless `pyfly.web.cors.enabled` is truthy. Note that
the auto-config default method set is `["GET", "HEAD", "POST"]`, which differs from the
`CORSConfig` dataclass field default of `["GET"]` used when you construct it directly.

Source file: `src/pyfly/web/cors.py`

---

## Security Headers

The `SecurityHeadersConfig` dataclass controls which security headers are added to every response by the `SecurityHeadersFilter`:

```python
from pyfly.web import SecurityHeadersConfig

config = SecurityHeadersConfig(
    x_content_type_options="nosniff",                              # Prevent MIME sniffing
    x_frame_options="DENY",                                        # Prevent clickjacking
    strict_transport_security="max-age=31536000; includeSubDomains",  # HSTS
    x_xss_protection="0",                                          # Disable legacy XSS auditor
    referrer_policy="strict-origin-when-cross-origin",             # Control Referer
    content_security_policy="default-src 'self'",                  # CSP (None = omit)
    permissions_policy="geolocation=()",                           # Permissions (None = omit)
)
```

| Header                        | Config Field                   | Default                                    |
|-------------------------------|--------------------------------|--------------------------------------------|
| `X-Content-Type-Options`      | `x_content_type_options`       | `"nosniff"`                                |
| `X-Frame-Options`             | `x_frame_options`              | `"DENY"`                                   |
| `Strict-Transport-Security`   | `strict_transport_security`    | `"max-age=31536000; includeSubDomains"`    |
| `X-XSS-Protection`            | `x_xss_protection`             | `"0"` (modern recommendation)              |
| `Referrer-Policy`             | `referrer_policy`              | `"strict-origin-when-cross-origin"`        |
| `Content-Security-Policy`     | `content_security_policy`      | `None` (too app-specific; omitted)         |
| `Permissions-Policy`          | `permissions_policy`           | `None` (too app-specific; omitted)         |

Source file: `src/pyfly/web/security_headers.py`

---

## OpenAPI and Swagger Documentation

### Automatic Generation

When `docs_enabled=True` (the default), `create_app()` generates a complete OpenAPI 3.1 specification from controller metadata and mounts three documentation endpoints.

### Swagger UI

Available at `/docs`. Provides an interactive API explorer where you can try out requests directly:

```
http://localhost:8080/docs
```

### ReDoc

Available at `/redoc`. Provides a clean, readable API reference:

```
http://localhost:8080/redoc
```

### OpenAPI JSON

The raw OpenAPI 3.1 specification is served at `/openapi.json`.

### OpenAPIGenerator Internals

The `OpenAPIGenerator` class (`src/pyfly/web/openapi.py`) builds the spec:

1. **Info** -- populated from `title`, `version`, and `description` passed to `create_app()`.
2. **Tags** -- derived from controller class names (`OrderController` becomes the `Order` tag).
3. **Paths** -- built from `RouteMetadata` collected by `ControllerRegistrar.collect_route_metadata()`. Each handler contributes an operation with `operationId`, parameters, request body, responses, tags, summary, description, and deprecated flag.
4. **Components/Schemas** -- Pydantic models used as `Body[T]` or `Valid[T]` types are registered in `components.schemas` via `model_json_schema()`, and referenced using `$ref`. Nested `$defs` from Pydantic v2 are automatically hoisted into `components/schemas` and `$ref` paths are rewritten accordingly.
5. **Validation Error Schemas** -- Endpoints with request bodies automatically include a `422 Validation Error` response with the standard `HTTPValidationError` and `ValidationError` schemas.

Parameters are extracted from handler type hints (with `Valid` wrapper automatically peeled):
- `PathVar[T]` becomes an `in: path` parameter
- `QueryParam[T]` becomes an `in: query` parameter
- `Header[T]` becomes an `in: header` parameter
- `Cookie[T]` becomes an `in: cookie` parameter
- `Body[BaseModel]` or `Valid[BaseModel]` becomes a `requestBody` with a JSON schema reference

Response schemas are derived from handler return types:
- `BaseModel` subclass returns produce a `$ref` to the model's schema
- `list[BaseModel]` returns produce an `array` schema with `items.$ref`
- `None` returns (204) produce a `"No Content"` description
- Other returns produce a generic `"Successful response"` description

Source file: `src/pyfly/web/openapi.py`

---

### Kubernetes Health Probes

PyFly supports Kubernetes-style liveness and readiness probes via the actuator health endpoint.

#### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/actuator/health` | Overall application health |
| `/actuator/health/liveness` | Liveness probe — is the app alive? |
| `/actuator/health/readiness` | Readiness probe — is the app ready to serve traffic? |

#### Probe Groups

Health indicators can be assigned to probe groups:

```python
from pyfly.actuator import HealthAggregator, HealthIndicator, ProbeGroup

aggregator.add_indicator("db", db_indicator, groups={ProbeGroup.READINESS})
aggregator.add_indicator("disk", disk_indicator, groups={ProbeGroup.LIVENESS})
aggregator.add_indicator("app", app_indicator)  # included in all probes
```

| Group Assignment | Liveness | Readiness | General Health |
|-----------------|----------|-----------|----------------|
| No groups (default) | Included | Included | Included |
| `{LIVENESS}` only | Included | Excluded | Included |
| `{READINESS}` only | Excluded | Included | Included |
| `{LIVENESS, READINESS}` | Included | Included | Included |

#### Kubernetes Configuration

```yaml
# kubernetes deployment
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 15
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

---

## Application Factory: create_app()

The `create_app()` function is the primary entry point for building a PyFly web application:

```python
from pyfly.web import CORSConfig
from pyfly.web.adapters.starlette import create_app

app = create_app(
    title="Order Service",
    version="1.0.0",
    description="REST API for order management",
    debug=False,
    context=application_context,
    docs_enabled=True,
    actuator_enabled=True,
    cors=CORSConfig(allowed_origins=["*"], allowed_methods=["GET", "POST"]),
    extra_routes=[],
)
```

### Full Parameter Reference

| Parameter          | Type                         | Default    | Description                                               |
|--------------------|------------------------------|------------|-----------------------------------------------------------|
| `title`            | `str`                        | `"PyFly"`  | API title for OpenAPI spec                                |
| `version`          | `str`                        | `"0.1.0"`  | API version for OpenAPI spec                              |
| `description`      | `str`                        | `""`       | API description for OpenAPI spec                          |
| `debug`            | `bool`                       | `False`    | Starlette debug mode                                      |
| `context`          | `ApplicationContext \| None` | `None`     | DI context for auto-discovering `@rest_controller` beans  |
| `docs_enabled`     | `bool`                       | `True`     | Mount OpenAPI spec, Swagger UI, and ReDoc                 |
| `extra_routes`     | `list[Route] \| None`        | `None`     | Additional Starlette routes to mount                      |
| `actuator_enabled` | `bool`                       | `False`    | Mount actuator health/info endpoints                      |
| `cors`             | `CORSConfig \| None`         | `None`     | CORS configuration. When `None` and a `context` is given, it is auto-built from `pyfly.web.cors.*` (disabled unless `pyfly.web.cors.enabled` is true) |
| `lifespan`         | `object \| None`             | `None`     | ASGI lifespan handler for startup/shutdown hooks           |

### What create_app() Does

1. **Builds the WebFilter chain.** Creates instances of `RequestContextFilter`, `CorrelationFilter`, `TransactionIdFilter`, `RequestLoggingFilter`, and `SecurityHeadersFilter` (in `@order` priority). Also conditionally adds `MetricsFilter` (when observability is enabled) and `TraceCollectorFilter` (when the admin dashboard is enabled). Auto-discovers any additional user `WebFilter` beans from the `ApplicationContext`. Sorts all filters by `@order` value. Wraps them in a single `WebFilterChainMiddleware`.
2. Adds `CORSMiddleware` if a `CORSConfig` is provided, or auto-builds one from `pyfly.web.cors.*` (when a `context` is present and CORS is enabled in config).
3. Uses `ControllerRegistrar.collect_routes(context)` to discover all `@rest_controller` beans and build Starlette `Route` objects.
4. Appends any `extra_routes`.
5. Mounts actuator endpoints if `actuator_enabled=True` (health, info, beans, env, loggers, metrics, plus custom actuator endpoint beans).
6. Collects `RouteMetadata` for OpenAPI generation.
7. Generates OpenAPI spec and mounts `/openapi.json`, `/docs`, and `/redoc` if `docs_enabled=True`.
8. Builds the Starlette `Application` with the middleware stack, routes, and optional lifespan handler.
9. Stores `pyfly_route_metadata` and `pyfly_docs_enabled` on `app.state` for startup logging.
10. Registers the `global_exception_handler` for all `Exception` types.
11. Returns the fully configured `Starlette` application instance, ready to be served by Uvicorn or any ASGI server.

Source file: `src/pyfly/web/adapters/starlette/app.py`

---

## ControllerRegistrar

The `ControllerRegistrar` (`src/pyfly/web/adapters/starlette/controller.py`) is the bridge between PyFly's decorator-based controller definitions and Starlette's routing system.

### Route Collection

`collect_routes(context)` iterates over all beans in the `ApplicationContext` and, for each class with `__pyfly_stereotype__ == "rest_controller"`:

1. Reads the `__pyfly_request_mapping__` base path from the class.
2. Iterates over all methods to find those with a `__pyfly_mapping__` attribute.
3. Combines the base path with each method's relative path.
4. Creates a `ParameterResolver` for each handler.
5. Collects `@exception_handler` methods for the controller.
6. Wraps each handler in an async endpoint that resolves parameters, calls the handler, converts the return value, and dispatches exceptions to the appropriate handler.
7. Returns a list of Starlette `Route` objects.

### Route Metadata for OpenAPI

`collect_route_metadata(context)` performs the same discovery but returns `RouteMetadata` objects instead of `Route` objects. Each `RouteMetadata` contains:

- `path`, `http_method`, `status_code`
- `handler`, `handler_name`
- `parameters` -- list of OpenAPI parameter dicts extracted from type hints
- `request_body_model` -- the Pydantic model class if `Body[BaseModel]` or `Valid[BaseModel]` is used
- `return_type` -- the handler's return type annotation
- `tag` -- derived from the controller class name (e.g., `OrderController` becomes `Order`)
- `summary` -- first line of the handler's docstring
- `description` -- remaining lines of the handler's docstring (after a blank separator)
- `deprecated` -- `True` if the handler is marked with `__pyfly_deprecated__`

This metadata is consumed by `OpenAPIGenerator` to build the spec. The `Valid` wrapper is automatically peeled during metadata extraction, so `Valid[Body[CreateOrderRequest]]` produces the same OpenAPI schema as `Body[CreateOrderRequest]`.

### Exception Handler Discovery

`_collect_exception_handlers(instance)` scans a controller instance for methods decorated with `@exception_handler`. Handlers are sorted by MRO depth (most specific first) so that `ValueError` is matched before `Exception`, for instance.

Source file: `src/pyfly/web/adapters/starlette/controller.py`

---

## ParameterResolver

The `ParameterResolver` (`src/pyfly/web/adapters/starlette/resolver.py`) is responsible for turning a Starlette `Request` into a dict of keyword arguments for a handler method.

**At startup** (`__init__`): Inspects the handler's type hints and builds a list of `ResolvedParam` objects, each containing:
- `name` -- the parameter name
- `binding_type` -- `PathVar`, `QueryParam`, `Body`, `Header`, or `Cookie`
- `inner_type` -- the type argument `T` (e.g., `str`, `int`, a Pydantic model)
- `default` -- the default value if provided in the signature
- `validate` -- `True` if the parameter was wrapped in `Valid[...]`

**At request time** (`resolve(request)`): For each `ResolvedParam`:
1. Calls the appropriate private resolver method (`_resolve_path_var`, `_resolve_query_param`, `_resolve_body`, `_resolve_header`, `_resolve_cookie`).
2. If `param.validate` is `True`, calls `_run_validation(value, param)` on the resolved value.
3. Returns the assembled kwargs dict.

**Valid[T] handling in `_inspect()`**: When a `Valid` origin is detected:
1. Sets `validate = True`.
2. Extracts the inner type argument.
3. If the inner type is itself a binding type (e.g., `Body`, `QueryParam`), uses that as the `binding_type`.
4. If the inner type is a plain type (e.g., a Pydantic model), implies `Body` as the binding type.

**Validation in `_resolve_body()`**: When `validate=True` and the inner type is a `BaseModel`:
1. Wraps `model_validate_json(body_bytes)` in a `try/except`.
2. On `PydanticValidationError`, extracts the error list, builds a detail string, and raises `ValidationException`.

Source file: `src/pyfly/web/adapters/starlette/resolver.py`

---

## WebServerPort

The `WebServerPort` protocol (`src/pyfly/web/ports/outbound.py`) defines the contract for any web server adapter:

```python
@runtime_checkable
class WebServerPort(Protocol):
    def create_app(self, **kwargs: Any) -> Any:
        """Create and return a web application instance."""
        ...
```

This protocol ensures that the application layer never depends directly on Starlette. In theory, you could implement a Flask adapter, a Django adapter, or any other ASGI/WSGI adapter by implementing this port.

The `WebAutoConfiguration` class registers the `StarletteWebAdapter` (which implements this protocol) as a bean when Starlette is available and no user-provided `WebServerPort` bean already exists.

Source file: `src/pyfly/web/ports/outbound.py`

---

## Module Exports Reference

The `pyfly.web` package (`src/pyfly/web/__init__.py`) re-exports the following symbols for convenience:

### Framework-Agnostic Exports

| Symbol                   | Source Module                            | Description                              |
|--------------------------|------------------------------------------|------------------------------------------|
| `Body`                   | `pyfly.web.params`                       | Request body binding type                |
| `PathVar`                | `pyfly.web.params`                       | Path variable binding type               |
| `QueryParam`             | `pyfly.web.params`                       | Query parameter binding type             |
| `Header`                 | `pyfly.web.params`                       | HTTP header binding type                 |
| `Cookie`                 | `pyfly.web.params`                       | Cookie binding type                      |
| `Valid`                  | `pyfly.web.params`                       | Validation marker type                   |
| `WebFilter`              | `pyfly.web.ports.filter`                 | Filter protocol                          |
| `OncePerRequestFilter`   | `pyfly.web.filters`                      | Abstract filter base class               |
| `get_mapping`            | `pyfly.web.mappings`                     | GET method decorator                     |
| `post_mapping`           | `pyfly.web.mappings`                     | POST method decorator                    |
| `put_mapping`            | `pyfly.web.mappings`                     | PUT method decorator                     |
| `patch_mapping`          | `pyfly.web.mappings`                     | PATCH method decorator                   |
| `delete_mapping`         | `pyfly.web.mappings`                     | DELETE method decorator                  |
| `request_mapping`        | `pyfly.web.mappings`                     | Class-level base path decorator          |
| `exception_handler`      | `pyfly.web.exception_handler`            | Exception handler decorator              |
| `CORSConfig`             | `pyfly.web.cors`                         | CORS configuration dataclass             |
| `SecurityHeadersConfig`  | `pyfly.web.security_headers`             | Security headers configuration           |

### Starlette Adapter Exports

These symbols are **not** in `pyfly.web` — import them directly from the adapter package:

| Symbol                     | Source Module                                          | Description                        |
|----------------------------|--------------------------------------------------------|------------------------------------|
| `create_app`               | `pyfly.web.adapters.starlette.app`                     | Application factory function       |
| `ControllerRegistrar`      | `pyfly.web.adapters.starlette.controller`              | Route collection engine            |
| `handle_return_value`      | `pyfly.web.adapters.starlette.response`                | Return value to Response converter |

### Additional pyfly.web Exports

| Symbol             | Source Module                   | Description                    |
|--------------------|---------------------------------|--------------------------------|
| `File`             | `pyfly.web.params`              | File upload binding type       |
| `UploadedFile`     | `pyfly.web.params`              | Uploaded file wrapper          |
| `sse_mapping`      | `pyfly.web.sse`                 | SSE endpoint decorator         |
| `controller_advice`| `pyfly.container.stereotypes`   | Controller advice stereotype   |

---

## Complete CRUD Example

The following example demonstrates a full CRUD controller with all HTTP methods, `Valid[T]` validation, exception handling, custom filters, and proper status codes.

```python
from uuid import UUID
from pydantic import BaseModel, Field

from pyfly.container import rest_controller, service, component
from pyfly.container.ordering import order
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import (
    request_mapping, get_mapping, post_mapping, put_mapping, patch_mapping,
    delete_mapping, exception_handler, Body, PathVar, QueryParam, Valid,
    OncePerRequestFilter,
)
from pyfly.web.ports.filter import CallNext


# --- Request/Response Models ---

class CreateProductRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    price: float = Field(..., gt=0)
    category: str

class UpdateProductRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    price: float = Field(..., gt=0)
    category: str

class PatchProductRequest(BaseModel):
    name: str | None = None
    price: float | None = Field(None, gt=0)
    category: str | None = None

class ProductResponse(BaseModel):
    id: str
    name: str
    price: float
    category: str


# --- Custom WebFilter ---

@component
@order(10)
class ApiKeyFilter(OncePerRequestFilter):
    """Validates API key on all /api/* endpoints."""

    url_patterns = ["/api/*"]
    exclude_patterns = ["/api/health"]

    async def do_filter(self, request, call_next: CallNext):
        api_key = request.headers.get("x-api-key")
        if not api_key:
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"error": {"message": "Missing API key", "code": "MISSING_API_KEY"}},
                status_code=401,
            )
        return await call_next(request)


# --- Service Layer ---

@service
class ProductService:
    async def find_all(self, category: str | None = None) -> list[dict]:
        # Business logic here
        ...

    async def find_by_id(self, product_id: str) -> dict:
        # Raises ResourceNotFoundException if not found
        ...

    async def create(self, data: CreateProductRequest) -> dict:
        ...

    async def replace(self, product_id: str, data: UpdateProductRequest) -> dict:
        ...

    async def patch(self, product_id: str, data: PatchProductRequest) -> dict:
        ...

    async def delete(self, product_id: str) -> None:
        ...


# --- Controller ---

@rest_controller
@request_mapping("/api/products")
class ProductController:

    def __init__(self, product_service: ProductService) -> None:
        self._service = product_service

    # LIST all products with optional category filter
    @get_mapping("/")
    async def list_products(
        self,
        category: QueryParam[str] = None,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
    ) -> list[dict]:
        return await self._service.find_all(category=category)

    # GET a single product by ID
    @get_mapping("/{product_id}")
    async def get_product(self, product_id: PathVar[str]) -> dict:
        return await self._service.find_by_id(product_id)

    # CREATE a new product (201 Created) with structured validation
    @post_mapping("/", status_code=201)
    async def create_product(self, body: Valid[CreateProductRequest]) -> dict:
        return await self._service.create(body)

    # REPLACE a product (full update) with structured validation
    @put_mapping("/{product_id}")
    async def replace_product(
        self,
        product_id: PathVar[str],
        body: Valid[UpdateProductRequest],
    ) -> dict:
        return await self._service.replace(product_id, body)

    # PATCH a product (partial update) with structured validation
    @patch_mapping("/{product_id}")
    async def update_product(
        self,
        product_id: PathVar[str],
        body: Valid[PatchProductRequest],
    ) -> dict:
        return await self._service.patch(product_id, body)

    # DELETE a product (204 No Content)
    @delete_mapping("/{product_id}", status_code=204)
    async def delete_product(self, product_id: PathVar[str]) -> None:
        await self._service.delete(product_id)

    # --- Exception Handlers ---

    @exception_handler(ResourceNotFoundException)
    async def handle_not_found(self, exc: ResourceNotFoundException):
        return 404, {
            "error": {
                "message": str(exc),
                "code": exc.code or "NOT_FOUND",
            }
        }

    @exception_handler(ValueError)
    async def handle_value_error(self, exc: ValueError):
        return 400, {
            "error": {
                "message": str(exc),
                "code": "BAD_REQUEST",
            }
        }
```

**Running the application:**

```python
from pyfly.web import CORSConfig
from pyfly.web.adapters.starlette import create_app

app = create_app(
    title="Product Service",
    version="1.0.0",
    description="CRUD API for product management",
    context=application_context,
    docs_enabled=True,
    actuator_enabled=True,
    cors=CORSConfig(
        allowed_origins=["http://localhost:3000"],
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    ),
)

# Run with: uvicorn main:app --reload
```

Or via configuration:

```yaml
# application.yml
pyfly:
  web:
    adapter: auto
    port: 8080
    debug: false
    docs:
      enabled: true
    actuator:
      enabled: true
```

This will expose:
- `GET    /api/products/`             -- list products
- `GET    /api/products/{product_id}` -- get a product
- `POST   /api/products/`             -- create a product (with Valid[T] validation)
- `PUT    /api/products/{product_id}` -- replace a product (with Valid[T] validation)
- `PATCH  /api/products/{product_id}` -- partially update a product (with Valid[T] validation)
- `DELETE /api/products/{product_id}` -- delete a product
- `GET    /docs`                       -- Swagger UI
- `GET    /redoc`                      -- ReDoc
- `GET    /openapi.json`               -- OpenAPI 3.1 spec

The WebFilter chain executes in this order for every request:
1. `RequestContextFilter` (order: `HIGHEST_PRECEDENCE`) -- initializes request-scoped context
2. `CorrelationFilter` (order: `HIGHEST_PRECEDENCE + 50`) -- stamps W3C/correlation headers
3. `TransactionIdFilter` (order: `HIGHEST_PRECEDENCE + 100`) -- injects/propagates transaction ID
4. `RequestLoggingFilter` (order: `HIGHEST_PRECEDENCE + 200`) -- logs request/response
5. `SecurityHeadersFilter` (order: `HIGHEST_PRECEDENCE + 300`) -- adds security headers
6. `ApiKeyFilter` (order: `10`) -- validates API key (custom, only for `/api/*`)
7. Route handler -- the controller method

---

## Adapters

- [Starlette Adapter](../adapters/starlette.md) — Setup, configuration reference, and adapter-specific features for the Starlette / Uvicorn backend
