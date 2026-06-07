# Testing Guide

This guide covers the testing utilities that PyFly provides out of the box: a base
test case class, a test container factory, and event assertion helpers. Together,
these tools make it straightforward to write unit tests, integration tests, and
event-driven tests for PyFly applications.

---

## Table of Contents

1. [Introduction](#introduction)
   - [Testing Pyramid](#testing-pyramid)
2. [PyFlyTestCase](#pyflytestcase)
   - [Setup and Teardown](#setup-and-teardown)
   - [Available Fixtures](#available-fixtures)
3. [create_test_container()](#create_test_container)
   - [Basic Usage](#basic-usage)
   - [Injecting Mocks and Fakes](#injecting-mocks-and-fakes)
   - [Resolving Services in Tests](#resolving-services-in-tests)
4. [Event Assertions](#event-assertions)
   - [assert_event_published()](#assert_event_published)
   - [assert_no_events_published()](#assert_no_events_published)
5. [mock_bean()](#mock_bean)
6. [Test Slices](#test-slices)
   - [@WebTest](#webtest)
   - [@DataTest](#datatest)
   - [@ServiceTest](#servicetest)
   - [get_test_slice()](#get_test_slice)
   - [Functional Slices (web_slice / service_slice / data_slice)](#functional-slices-web_slice--service_slice--data_slice)
7. [PyFlyTestClient](#pyflytestclient)
   - [TestResponse](#testresponse)
   - [Fluent Assertion Methods](#fluent-assertion-methods)
8. [Testcontainers (Docker-backed integration tests)](#testcontainers-docker-backed-integration-tests)
   - [Installation](#installation)
   - [Container Factories](#container-factories)
   - [Wiring Connections into Config](#wiring-connections-into-config)
   - [Graceful Skip Without Docker](#graceful-skip-without-docker)
   - [Runnable Example](#runnable-example)
9. [Testing Patterns](#testing-patterns)
   - [Unit Testing Services](#unit-testing-services)
   - [Integration Testing with In-Memory Adapters](#integration-testing-with-in-memory-adapters)
   - [Testing Controllers](#testing-controllers)
   - [Testing Event Handlers](#testing-event-handlers)
10. [Complete Example](#complete-example)

---

## Introduction

PyFly adopts a "testing is a first-class citizen" philosophy. The framework provides
dedicated testing modules so you never have to build boilerplate setup from scratch.

```python
from pyfly.testing import (
    PyFlyTestCase,
    create_test_container,
    assert_event_published,
    assert_no_events_published,
    mock_bean,
    WebTest, DataTest, ServiceTest,
    get_test_slice,
    PyFlyTestClient,
    TestResponse,
)
```

**Source:** `src/pyfly/testing/__init__.py`

### Testing Pyramid

PyFly encourages the standard testing pyramid, where the number of tests decreases
as the scope and cost of each test increases:

```
          /\
         /  \       End-to-end tests (few)
        /    \      - Full application stack with HTTP client
       /------\
      /        \    Integration tests (some)
     /          \   - Real components, in-memory adapters
    /------------\
   /              \ Unit tests (many)
  /                \ - Mocked dependencies, fast execution
 /------------------\
```

| Level       | Dependencies          | Speed  | PyFly Tools                              |
|-------------|-----------------------|--------|------------------------------------------|
| Unit        | Mocked                | Fast   | `create_test_container()`, `unittest.mock` |
| Integration | In-memory adapters    | Medium | `PyFlyTestCase`, `InMemoryEventBus`      |
| E2E         | Full stack            | Slow   | `create_app()` + `httpx.AsyncClient`     |

---

## PyFlyTestCase

`PyFlyTestCase` is a base class for integration tests that need PyFly framework
infrastructure. It pre-configures an `ApplicationContext` and an `InMemoryEventBus`
so your tests can focus on behavior rather than setup.

```python
from pyfly.testing import PyFlyTestCase


class TestOrderWorkflow(PyFlyTestCase):

    async def test_full_order_lifecycle(self):
        await self.setup()

        # self.context is a fully initialized ApplicationContext
        # self.event_bus is an InMemoryEventBus ready for subscriptions

        # ... test logic ...

        await self.teardown()
```

### Setup and Teardown

Both methods are `async` and must be awaited. Call `setup()` at the beginning of
each test and `teardown()` at the end to ensure clean state between tests.

| Method       | What It Does                                                    |
|-------------|------------------------------------------------------------------|
| `setup()`   | 1. Creates an `ApplicationContext` with an empty `Config({})`.   |
|             | 2. Creates a fresh `InMemoryEventBus` instance.                  |
|             | 3. Calls `await context.start()` to initialize the context.     |
|             | 4. Registers every `mock_bean(...)` descriptor's `AsyncMock` into the context's container (keyed on its bean type), so DI-resolved collaborators receive the mock. |
| `teardown()` | 1. Calls `await context.stop()` to clean up resources.         |

The internal implementation:

```python
class PyFlyTestCase:
    context: ApplicationContext
    event_bus: InMemoryEventBus

    async def setup(self) -> None:
        self.context = ApplicationContext(Config({}))
        self.event_bus = InMemoryEventBus()
        await self.context.start()
        self._install_mock_beans()

    def _install_mock_beans(self) -> None:
        """Register each mock_bean descriptor's AsyncMock into the container."""
        container = self.context.container
        for klass in type(self).__mro__:
            for attr_name, attr in list(vars(klass).items()):
                if isinstance(attr, MockBeanDescriptor):
                    mock_instance = getattr(self, attr_name)
                    container.register(attr.bean_type)
                    container._registrations[attr.bean_type].instance = mock_instance

    async def teardown(self) -> None:
        await self.context.stop()
```

Because `_install_mock_beans()` walks the full MRO, `mock_bean(...)` class
attributes declared on a `PyFlyTestCase` (or any base class) are wired into the
test context. A subsequent `self.context.get_bean(BeanType)` — and any
DI-resolved collaborator that depends on `BeanType` — receives the same
per-instance `AsyncMock` you configured in the test.

### Available Fixtures

After calling `setup()`, the following attributes are available:

| Attribute      | Type                  | Description                              |
|---------------|-----------------------|------------------------------------------|
| `self.context`  | `ApplicationContext` | Pre-configured application context with empty config |
| `self.event_bus` | `InMemoryEventBus` | In-memory event bus for publishing and subscribing |

The `InMemoryEventBus` supports wildcard pattern subscriptions (e.g., `"order.*"`
matches `"order.created"` and `"order.shipped"`), making it easy to capture events
in tests.

**Source:** `src/pyfly/testing/fixtures.py`

---

## create_test_container()

`create_test_container()` creates a pre-configured DI `Container` for testing. It
accepts an optional `overrides` dictionary that maps interface types to test
implementations, enabling you to substitute real services with fakes or mocks.

### Basic Usage

```python
from pyfly.testing import create_test_container

# Create a container with no overrides
container = create_test_container()
```

### Injecting Mocks and Fakes

The `overrides` parameter maps interface types to test implementations. Each override
is registered as a `SINGLETON` and bound to the interface.

```python
from pyfly.testing import create_test_container


class FakeOrderRepository:
    """In-memory order repository for testing."""

    def __init__(self):
        self.orders: dict[str, dict] = {}

    async def save(self, order: dict) -> None:
        self.orders[order["id"]] = order

    async def find_by_id(self, order_id: str) -> dict | None:
        return self.orders.get(order_id)


# Create container with the fake repository
container = create_test_container(overrides={
    OrderRepository: FakeOrderRepository,
})
```

**`create_test_container()` Parameters:**

| Parameter   | Type                       | Default | Description                                |
|------------|----------------------------|---------|--------------------------------------------|
| `overrides` | `dict[type, type] \| None` | `None`  | Interface-to-implementation mappings       |

**How overrides work internally:**

For each `(interface, impl)` pair in the overrides dictionary:

1. The implementation is registered with `container.register(impl, scope=Scope.SINGLETON)`.
2. If `interface != impl`, a binding is created with `container.bind(interface, impl)`.

This means you can `resolve()` by the interface type and receive the test implementation:

```python
repo = container.resolve(OrderRepository)
# Returns a FakeOrderRepository instance (singleton)
```

The full implementation:

```python
def create_test_container(
    overrides: dict[type, type] | None = None,
) -> Container:
    container = Container()
    if overrides:
        for interface, impl in overrides.items():
            container.register(impl, scope=Scope.SINGLETON)
            if interface != impl:
                container.bind(interface, impl)
    return container
```

**Source:** `src/pyfly/testing/containers.py`

### Resolving Services in Tests

Once the container is configured with overrides, register your service classes and
use `resolve()` to get instances with their dependencies injected:

```python
from pyfly.testing import create_test_container
from pyfly.container import Scope

container = create_test_container(overrides={
    OrderRepository: FakeOrderRepository,
})

# Also register the service that depends on the repository
container.register(OrderService, scope=Scope.SINGLETON)

# Resolve -- OrderService gets FakeOrderRepository injected via constructor
service = container.resolve(OrderService)

# The resolved service uses the fake repository
result = await service.create_order({"id": "ord-1", "customer": "Alice"})
```

---

## Event Assertions

PyFly provides two assertion helpers for verifying event-driven behavior. They work
with lists of `EventEnvelope` objects, which is the standard event wrapper in PyFly's
EDA module.

An `EventEnvelope` contains:

| Field        | Type              | Description                    |
|-------------|-------------------|--------------------------------|
| `event_type` | `str`            | Event type identifier          |
| `payload`    | `dict[str, Any]` | Event data                     |
| `destination` | `str`           | Target destination/topic       |
| `event_id`   | `str`            | Auto-generated UUID            |
| `timestamp`  | `datetime`       | Auto-generated UTC timestamp   |
| `headers`    | `dict[str, str]` | Optional metadata headers      |

### assert_event_published()

Asserts that an event of a given type was published. Optionally verifies that the
event payload contains specific key-value pairs.

```python
from pyfly.testing import assert_event_published
from pyfly.eda.types import EventEnvelope

events = [
    EventEnvelope(
        event_type="order.created",
        payload={"order_id": "ord-123", "customer_id": "cust-42", "total": 99.99},
        destination="orders",
    ),
]

# Assert just the event type
event = assert_event_published(events, "order.created")

# Assert event type AND payload contents
event = assert_event_published(
    events,
    "order.created",
    payload_contains={"order_id": "ord-123", "customer_id": "cust-42"},
)
```

**Parameters:**

| Parameter          | Type                     | Default | Description                            |
|-------------------|--------------------------|---------|----------------------------------------|
| `events`          | `list[EventEnvelope]`    | required | List of captured event envelopes      |
| `event_type`      | `str`                    | required | Expected event type string            |
| `payload_contains` | `dict[str, Any] \| None` | `None`  | Key-value pairs the payload must contain |

**Returns:** The matching `EventEnvelope` instance.

**Raises `AssertionError` when:**

- No event with the given type is found. The error message lists all published event
  types for debugging:
  ```
  Expected event 'order.created' to be published. Published events: ['user.updated']
  ```
- A matching event is found but its payload is missing an expected key:
  ```
  Expected key 'order_id' in event payload
  ```
- A matching event is found but a payload value does not match:
  ```
  Expected payload['order_id'] == 'ord-123', got 'ord-999'
  ```

**How matching works internally:**

1. Filters `events` for those where `event.event_type == event_type`.
2. Takes the **first** match (if there are multiple events of the same type).
3. If `payload_contains` is provided, iterates over each key-value pair and asserts
   presence and equality against `event.payload`.

### assert_no_events_published()

Asserts that no events were published at all. Use this to verify that a failed
operation did not produce side effects.

```python
from pyfly.testing import assert_no_events_published

events: list[EventEnvelope] = []

# Passes -- no events in the list
assert_no_events_published(events)
```

If events are present, the assertion fails with a descriptive message:

```python
events.append(EventEnvelope(
    event_type="order.created",
    payload={"order_id": "ord-123"},
    destination="orders",
))
assert_no_events_published(events)
# AssertionError: Expected no events to be published. Got: ['order.created']
```

**Parameters:**

| Parameter | Type                  | Description                          |
|----------|-----------------------|--------------------------------------|
| `events` | `list[EventEnvelope]` | List of captured event envelopes     |

**Source:** `src/pyfly/testing/assertions.py`

---

## mock_bean()

`mock_bean()` is a Python descriptor that provides fresh `AsyncMock` instances for each test. It replaces the manual boilerplate of creating `AsyncMock(spec=...)` fixtures.

```python
from pyfly.testing import mock_bean, PyFlyTestCase


class TestOrderService(PyFlyTestCase):
    order_repo = mock_bean(OrderRepository)
    event_publisher = mock_bean(EventPublisher)

    async def test_create_order(self):
        self.order_repo.save.return_value = Order(id="1")

        service = OrderService(self.order_repo, self.event_publisher)
        result = await service.create_order({"customer": "Alice"})

        self.order_repo.save.assert_called_once()
```

### How It Works

`mock_bean()` returns a `MockBeanDescriptor` — a Python descriptor that lazily creates an `AsyncMock(spec=bean_type)` per test instance. Each test method gets a fresh mock because the mock is attached to the instance, not the class.

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `bean_type` | `type` | The type to mock (used as the `spec` argument to `AsyncMock`) |

**Returns:** A descriptor that provides an `AsyncMock` instance when accessed on an instance.

**Key behaviors:**

- Each test instance gets its own mock (no state leakage between tests)
- The mock has the `spec` of the given type, so typos in method names are caught
- The `bean_type` property on the descriptor is available for introspection

```python
# Access the underlying type
TestOrderService.order_repo.bean_type  # OrderRepository
```

### Wiring into the ApplicationContext

When a `mock_bean(...)` descriptor is declared on a `PyFlyTestCase` subclass,
`setup()`'s `_install_mock_beans()` step registers the materialized `AsyncMock`
into the test context's container, keyed on the descriptor's `bean_type`. This
means the mock is not just a convenient attribute — it is the bean the context
resolves:

```python
from pyfly.testing import mock_bean, PyFlyTestCase


class TestOrderWorkflow(PyFlyTestCase):
    order_repo = mock_bean(OrderRepository)

    async def test_context_resolves_the_mock(self):
        await self.setup()

        self.order_repo.find_by_id.return_value = {"id": "1"}

        # The context returns the same mock you configured above,
        # and any bean that depends on OrderRepository receives it.
        resolved = self.context.get_bean(OrderRepository)
        assert resolved is self.order_repo

        await self.teardown()
```

Earlier releases created the mock but never wired it into the context, so
`context.get_bean(...)` and DI-resolved collaborators could not see it. That gap
is now closed: the per-instance `AsyncMock` is registered during `setup()`.

**Source:** `src/pyfly/testing/mock.py`, `src/pyfly/testing/fixtures.py`

---

## Test Slices

Test slices are class decorators that mark test classes for focused testing of specific application layers. They mirror Spring Boot's `@WebMvcTest`, `@DataJpaTest`, and `@SpringBootTest` annotations.

```python
from pyfly.testing import WebTest, DataTest, ServiceTest, get_test_slice
```

### @WebTest

Marks a test class as a web-layer test. Use this for testing controllers and filters in isolation, with the service layer mocked.

```python
from pyfly.testing import WebTest, mock_bean


@WebTest
class TestUserController:
    user_service = mock_bean(UserService)

    async def test_get_users_returns_200(self):
        self.user_service.list_users.return_value = [{"id": "1", "name": "Alice"}]
        # ... test controller with mocked service
```

### @DataTest

Marks a test class as a data-layer test. Use this for testing repositories with in-memory backends.

```python
from pyfly.testing import DataTest


@DataTest
class TestUserRepository:
    async def test_save_and_find(self):
        # ... test repository operations
```

### @ServiceTest

Marks a test class as a service-layer test. Use this for testing services with mocked repositories.

```python
from pyfly.testing import ServiceTest, mock_bean


@ServiceTest
class TestOrderService:
    order_repo = mock_bean(OrderRepository)

    async def test_place_order(self):
        # ... test service logic
```

### get_test_slice()

Inspect which slice a test class belongs to:

```python
from pyfly.testing import get_test_slice, WebTest, DataTest

@WebTest
class MyWebTest: ...

@DataTest
class MyDataTest: ...

class PlainTest: ...

get_test_slice(MyWebTest)    # "web"
get_test_slice(MyDataTest)   # "data"
get_test_slice(PlainTest)    # None
```

| Decorator | Slice Value | Purpose |
|---|---|---|
| `@WebTest` | `"web"` | Controllers and filters |
| `@DataTest` | `"data"` | Repositories and queries |
| `@ServiceTest` | `"service"` | Services and business logic |

**Source:** `src/pyfly/testing/slices.py`

### Functional Slices (web_slice / service_slice / data_slice)

While the `@WebTest`/`@DataTest`/`@ServiceTest` decorators *mark* a test class,
the **functional slices** actually *build and start* a minimal `ApplicationContext`
containing only the beans you pass (plus the collaborators you supply via
`overrides`). They are the explicit-builder equivalents of Spring Boot's
`@WebMvcTest` / `@DataJpaTest` / `@SpringBootTest` slices: each slice registers a
focused set of beans, starts a real context, and — for the web slice — wraps it in
a `PyFlyTestClient` so routes, filters, and error handlers are wired exactly as in
production via `create_app(context=...)`.

```python
from pyfly.testing import web_slice, service_slice, data_slice, slice_context
```

| Helper | Yields | Use for |
|---|---|---|
| `web_slice(*controllers, config=None, overrides=None)` | `(context, client)` | Controllers + a `PyFlyTestClient` |
| `service_slice(*beans, config=None, overrides=None)` | `context` | Services and business logic |
| `data_slice(*beans, config=None, overrides=None)` | `context` | Repositories and queries |
| `slice_context(*beans, config=None, overrides=None)` | `context` | Generic minimal context |

`service_slice` and `data_slice` are intent-named aliases of `slice_context`. Each
helper is a coroutine, so you `await` it to get an async context manager — the
context is started on `__aenter__` and stopped on `__aexit__`:

```python
async with await web_slice(...) as (context, client):
    ...   # context started; stopped automatically on exit
```

**Overrides.** The `overrides` parameter maps an interface/dependency type to a
replacement. The value can be either a **class** (registered as a `SINGLETON` and
bound to the interface) or a **pre-built instance/mock** (installed directly under
the interface type):

```python
overrides={WidgetService: FakeWidgetService}       # a replacement class
overrides={WidgetService: FakeWidgetService()}      # a pre-built instance/fake
```

#### Web slice

```python
from pyfly.container.stereotypes import rest_controller
from pyfly.web.mappings import get_mapping, request_mapping
from pyfly.testing import web_slice


class WidgetService:
    def names(self) -> list[str]:
        return ["real-widget"]


class FakeWidgetService:
    def names(self) -> list[str]:
        return ["fake-widget"]


@rest_controller
@request_mapping("/api/widgets")
class WidgetController:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    @get_mapping("/")
    async def list_widgets(self) -> dict:
        return {"widgets": self._service.names()}


async def test_widgets_endpoint():
    # Real dependency: pass WidgetController and WidgetService.
    async with await web_slice(WidgetController, WidgetService) as (ctx, client):
        assert ctx.get_bean(WidgetController) is not None
        client.get("/api/widgets/").assert_status(200)

    # Swap the collaborator for a fake instance via overrides.
    async with await web_slice(
        WidgetController, overrides={WidgetService: FakeWidgetService()}
    ) as (_ctx, client):
        assert client.get("/api/widgets/").json() == {"widgets": ["fake-widget"]}
```

#### Service / data slices

`service_slice` and `data_slice` yield the started context directly (no client).
Resolve beans with `ctx.get_bean(...)`:

```python
from pyfly.container.stereotypes import service
from pyfly.testing import service_slice, data_slice


@service
class GreetingService:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    def greet(self) -> str:
        return f"hello {self._service.names()[0]}"


async def test_service_slice():
    # Override the collaborator with a replacement class.
    async with await service_slice(
        GreetingService, overrides={WidgetService: FakeWidgetService}
    ) as ctx:
        assert ctx.get_bean(GreetingService).greet() == "hello fake-widget"


async def test_data_slice():
    async with await data_slice(WidgetService) as ctx:
        repo = ctx.get_bean(WidgetService)
        assert repo.names() == ["real-widget"]
```

#### Fail-fast on missing collaborators

A slice never silently pulls in unrelated infrastructure. After the context
starts, each slice bean is resolved immediately, so a missing collaborator surfaces
at build time (matching Spring slice startup) rather than on first use:

```python
import pytest
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.testing import web_slice


async def test_missing_collaborator_fails_loudly():
    # WidgetController needs WidgetService, which is neither passed nor overridden.
    with pytest.raises((NoSuchBeanError, BeanCreationException)):
        async with await web_slice(WidgetController):
            pass
```

**Source:** `src/pyfly/testing/slice_context.py`

---

## PyFlyTestClient

`PyFlyTestClient` is a test HTTP client that wraps Starlette's `TestClient` with fluent assertion methods. It makes controller tests more readable by chaining assertions directly on the response.

```python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from pyfly.testing import PyFlyTestClient


def get_users(request):
    return JSONResponse([{"id": "1", "name": "Alice"}])

app = Starlette(routes=[Route("/api/users", get_users)])
client = PyFlyTestClient(app)

# Fluent assertion chaining
client.get("/api/users") \
    .assert_status(200) \
    .assert_json_path("$[0].name", value="Alice") \
    .assert_header("content-type", value="application/json")
```

### Creating a Client

```python
from pyfly.testing import PyFlyTestClient

# Pass any ASGI application
client = PyFlyTestClient(app)
```

The client wraps Starlette's `TestClient` with `raise_server_exceptions=False`, so server errors return as HTTP 500 responses instead of raising exceptions in test code.

### HTTP Methods

| Method | Description |
|---|---|
| `client.get(url, **kwargs)` | Send GET request |
| `client.post(url, **kwargs)` | Send POST request |
| `client.put(url, **kwargs)` | Send PUT request |
| `client.delete(url, **kwargs)` | Send DELETE request |
| `client.patch(url, **kwargs)` | Send PATCH request |

All methods return a `TestResponse` instance. Pass any keyword arguments supported by Starlette's `TestClient` (e.g., `json=`, `headers=`, `cookies=`).

### TestResponse

`TestResponse` wraps the HTTP response with fluent assertion methods. All assert methods return `self` for chaining.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `status_code` | `int` | HTTP status code |
| `headers` | `dict[str, str]` | Response headers (lowercase keys) |
| `body` | `bytes` | Raw response body |

**Methods:**

| Method | Description |
|---|---|
| `json()` | Parse and return body as JSON (cached) |

### Fluent Assertion Methods

#### assert_status(expected)

Assert the response status code:

```python
client.get("/api/users").assert_status(200)
client.post("/api/orders", json={}).assert_status(422)
```

#### assert_json_path(path, *, value=..., exists=True)

Assert a JSON path exists (or not) and optionally matches a value. Uses [JSONPath](https://goessner.net/articles/JsonPath/) syntax via `jsonpath_ng`:

```python
response = client.get("/api/users/1")

# Assert path exists
response.assert_json_path("$.name")

# Assert path has specific value
response.assert_json_path("$.name", value="Alice")

# Assert path does NOT exist
response.assert_json_path("$.deleted_at", exists=False)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | JSONPath expression |
| `value` | `Any` | `...` (unset) | Expected value (only checked if provided) |
| `exists` | `bool` | `True` | Whether the path should exist |

#### assert_header(name, *, value=None, exists=True)

Assert a response header exists and optionally matches a value:

```python
response.assert_header("content-type", value="application/json")
response.assert_header("x-custom", exists=False)
```

#### assert_body_contains(text)

Assert the response body contains a text substring:

```python
client.get("/health").assert_body_contains("UP")
```

### Complete Test Example

```python
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

from pyfly.testing import PyFlyTestClient


def list_items(request):
    return JSONResponse(
        [{"id": "1", "name": "Widget", "price": 9.99}],
        headers={"X-Total-Count": "1"},
    )

def create_item(request):
    return JSONResponse({"id": "2", "name": "Gadget"}, status_code=201)


app = Starlette(routes=[
    Route("/api/items", list_items, methods=["GET"]),
    Route("/api/items", create_item, methods=["POST"]),
])


class TestItemsAPI:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        self.client = PyFlyTestClient(app)

    def test_list_items(self):
        self.client.get("/api/items") \
            .assert_status(200) \
            .assert_json_path("$[0].name", value="Widget") \
            .assert_json_path("$[0].price", value=9.99) \
            .assert_header("x-total-count", value="1")

    def test_create_item(self):
        self.client.post("/api/items", json={"name": "Gadget"}) \
            .assert_status(201) \
            .assert_json_path("$.name", value="Gadget")
```

**Source:** `src/pyfly/testing/client.py`

---

## Testcontainers (Docker-backed integration tests)

When in-memory adapters are not enough, PyFly's Testcontainers helpers spin up a **real**
Postgres, MySQL, Redis, MongoDB, or Kafka in Docker for the duration of a test, then wire
the container's connection details straight into pyfly config keys. This is the equivalent
of Spring Boot's `@Testcontainers` plus `@ServiceConnection`.

```python
from pyfly.testing import (
    postgres_container, mysql_container, redis_container,
    mongodb_container, kafka_container,
    pyfly_config, pyfly_config_for,
    is_docker_available, requires_docker,
)
```

### Installation

The helpers require the optional `testcontainers` extra and a running Docker daemon:

```bash
pip install 'pyfly[testcontainers]'
```

The extra pulls in `testcontainers>=4.0.0`. The container factories also need the backing
`testcontainers` submodule for the engine you use (e.g. `testcontainers.postgres`); if a
factory is called without it installed, it raises a `RuntimeError` whose message points you
back at `pip install 'pyfly[testcontainers]'`.

### Container Factories

Each factory returns an unstarted `testcontainers` container — start it with a `with`
block (it is stopped automatically on exit). Pass a custom image as the first argument and
any extra keyword arguments through to the underlying container.

| Factory | Default image | Returns |
|---|---|---|
| `postgres_container(image="postgres:16-alpine", **kwargs)` | `postgres:16-alpine` | `PostgresContainer` |
| `mysql_container(image="mysql:8", **kwargs)` | `mysql:8` | `MySqlContainer` |
| `redis_container(image="redis:7-alpine", **kwargs)` | `redis:7-alpine` | `RedisContainer` |
| `mongodb_container(image="mongo:7", **kwargs)` | `mongo:7` | `MongoDbContainer` |
| `kafka_container(image="confluentinc/cp-kafka:7.6.0", **kwargs)` | `confluentinc/cp-kafka:7.6.0` | `KafkaContainer` |

```python
with postgres_container() as pg:
    ...  # pg is started here; stopped when the block exits
```

### Wiring Connections into Config

Once a container is started, map it to pyfly config without copying URLs by hand — the
`@ServiceConnection`-style helpers do it for you.

**`pyfly_config_for(container)`** returns a flat dict of dotted config overrides for a
single started container, and raises `ValueError` for an unmapped container type:

| Container | Config keys produced |
|---|---|
| Postgres | `pyfly.data.relational.url` (rewritten to the `postgresql+asyncpg://` async driver) |
| MySQL | `pyfly.data.relational.url` (rewritten to the `mysql+aiomysql://` async driver) |
| Redis | `pyfly.cache.redis.url` **and** `pyfly.session.redis.url` (both `redis://host:port/0`) |
| MongoDB | `pyfly.data.document.uri` |
| Kafka | `pyfly.eda.kafka.bootstrap-servers` |

The Postgres/MySQL mappings deliberately swap the container's sync driver URL for pyfly's
async driver, so the resulting URL is ready to hand to the reactive data layer.

**`pyfly_config(*containers, base=None)`** is the one-call setup for an integration
`ApplicationContext`: it merges `pyfly_config_for(...)` for every started container (plus an
optional `base` dict of flat overrides) and returns a nested `Config`:

```python
from pyfly.testing import pyfly_config

with postgres_container() as pg, redis_container() as redis:
    config = pyfly_config(
        pg,
        redis,
        base={"pyfly.data.enabled": True},
    )
    config.get("pyfly.data.relational.url")  # postgresql+asyncpg://...
    config.get("pyfly.cache.redis.url")      # redis://127.0.0.1:.../0
```

### Graceful Skip Without Docker

So a test suite stays green on machines without Docker (e.g. some CI runners), guard the
integration tests:

- **`is_docker_available() -> bool`** — `True` only when the `docker` package is importable
  *and* a daemon answers `ping()`. Any connectivity failure returns `False`.
- **`@requires_docker`** — a `pytest` decorator that attaches a `skipif` mark; the test is
  skipped (not failed) wherever Docker is unavailable.

```python
from pyfly.testing import is_docker_available, requires_docker

# Decorator form — skips cleanly when Docker is down or the extra is missing
@requires_docker
def test_against_real_postgres():
    ...

# Programmatic form — branch on availability yourself
if is_docker_available():
    ...
```

### Runnable Example

A complete integration test: start Postgres and Redis, build the pyfly `Config` from them,
boot an `ApplicationContext`, and exercise the wired beans — skipped automatically when
Docker is not present.

```python
"""tests/integration/test_orders_integration.py"""

from pyfly.context import ApplicationContext
from pyfly.testing import (
    postgres_container,
    redis_container,
    pyfly_config,
    requires_docker,
)


@requires_docker
async def test_orders_persist_against_real_infra():
    # Spin up real Docker-backed services for the test's lifetime.
    with postgres_container() as pg, redis_container() as redis:
        # Wire their connection details straight into pyfly config keys.
        config = pyfly_config(
            pg,
            redis,
            base={
                "pyfly.data.enabled": True,
                "pyfly.cache.enabled": True,
                "pyfly.cache.provider": "redis",
            },
        )

        # The async Postgres URL and Redis URL are now in the config.
        assert config.get("pyfly.data.relational.url").startswith(
            "postgresql+asyncpg://"
        )
        assert config.get("pyfly.cache.redis.url").startswith("redis://")

        # Boot an ApplicationContext bound to the real infrastructure.
        context = ApplicationContext(config)
        await context.start()
        try:
            # ... resolve repositories/services and assert real round-trips ...
            ...
        finally:
            await context.stop()
```

Run only the Docker-backed tests (and let them skip if Docker is absent):

```bash
pytest tests/integration -v
```

**Source:** `src/pyfly/testing/testcontainers.py`

---

## Testing Patterns

### Unit Testing Services

Unit tests mock all external dependencies and test a single class in isolation.
Use `unittest.mock.AsyncMock` for async dependencies.

```python
import pytest
from unittest.mock import AsyncMock
from pyfly.kernel.exceptions import ResourceNotFoundException


class TestOrderService:
    """Pure unit tests -- all dependencies are mocked."""

    @pytest.fixture
    def mock_repo(self):
        repo = AsyncMock()
        repo.find_by_id = AsyncMock(return_value=None)
        repo.save = AsyncMock()
        return repo

    @pytest.fixture
    def service(self, mock_repo):
        return OrderService(order_repository=mock_repo)

    async def test_create_order_saves_to_repository(self, service, mock_repo):
        result = await service.create_order({
            "customer_id": "cust-42",
            "items": [{"product": "Widget", "qty": 1}],
        })

        mock_repo.save.assert_called_once()
        assert result["status"] == "created"

    async def test_create_order_returns_order_id(self, service):
        result = await service.create_order({
            "customer_id": "cust-42",
            "items": [{"product": "Widget", "qty": 1}],
        })
        assert "order_id" in result

    async def test_get_order_raises_not_found(self, service, mock_repo):
        mock_repo.find_by_id.return_value = None

        with pytest.raises(ResourceNotFoundException):
            await service.get_order("nonexistent-id")
```

### Integration Testing with In-Memory Adapters

Integration tests use PyFly's in-memory adapters (such as `InMemoryEventBus`) and
fake repositories to test multiple components working together.

```python
import pytest
from pyfly.testing import PyFlyTestCase, assert_event_published
from pyfly.eda.types import EventEnvelope


class TestOrderEventIntegration(PyFlyTestCase):
    """Tests that verify event publishing and subscription."""

    async def test_order_created_event_is_published_and_received(self):
        await self.setup()

        # Set up an event capture list
        captured_events: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured_events.append(envelope)

        # Subscribe to order events using wildcard pattern
        self.event_bus.subscribe("order.*", capture)

        # Publish an event (simulating what the service would do)
        await self.event_bus.publish(
            destination="orders",
            event_type="order.created",
            payload={"order_id": "ord-123", "total": 59.99},
        )

        # Verify the event was published correctly
        event = assert_event_published(
            captured_events,
            "order.created",
            payload_contains={"order_id": "ord-123"},
        )
        assert event.payload["total"] == 59.99

        await self.teardown()

    async def test_wildcard_pattern_receives_multiple_event_types(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        self.event_bus.subscribe("order.*", capture)

        await self.event_bus.publish("orders", "order.created", {"id": "1"})
        await self.event_bus.publish("orders", "order.shipped", {"id": "1"})
        await self.event_bus.publish("users", "user.created", {"id": "u1"})

        # Only order.* events should be captured
        assert len(captured) == 2
        assert_event_published(captured, "order.created")
        assert_event_published(captured, "order.shipped")

        await self.teardown()
```

### Testing Controllers

Test controllers by creating a Starlette test client with `create_app()` and
`httpx.AsyncClient`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from pyfly.web.adapters.starlette import create_app
from pyfly.testing import create_test_container
from pyfly.container import Scope


class TestOrderController:
    """HTTP-level tests for the order controller."""

    @pytest.fixture
    async def client(self):
        # Set up the DI container with test overrides
        container = create_test_container(overrides={
            OrderRepository: FakeOrderRepository,
        })
        container.register(OrderService, scope=Scope.SINGLETON)
        container.register(OrderController, scope=Scope.SINGLETON)

        app = create_app(title="Test", version="0.0.1")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_create_order_returns_201(self, client):
        response = await client.post("/api/orders", json={
            "customer_id": "cust-42",
            "items": [{"product_id": "SKU-001", "quantity": 2}],
        })
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "created"

    async def test_create_order_invalid_body_returns_422(self, client):
        response = await client.post("/api/orders", json={})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"

    async def test_get_order_not_found_returns_404(self, client):
        response = await client.get("/api/orders/nonexistent")
        assert response.status_code == 404
```

### Testing Event Handlers

Test event handlers by directly invoking them with constructed `EventEnvelope`
objects:

```python
import pytest
from unittest.mock import AsyncMock
from pyfly.eda.types import EventEnvelope


class TestOrderCreatedHandler:
    """Tests for the order.created event handler."""

    @pytest.fixture
    def notification_service(self):
        return AsyncMock()

    @pytest.fixture
    def handler(self, notification_service):
        return OrderCreatedHandler(
            notification_service=notification_service,
        )

    async def test_sends_confirmation_notification(self, handler, notification_service):
        envelope = EventEnvelope(
            event_type="order.created",
            payload={"order_id": "ord-123", "customer_id": "cust-42"},
            destination="orders",
        )

        await handler.handle(envelope)

        notification_service.send_confirmation.assert_called_once_with(
            customer_id="cust-42",
            order_id="ord-123",
        )

    async def test_handles_missing_customer_id_gracefully(self, handler):
        envelope = EventEnvelope(
            event_type="order.created",
            payload={"order_id": "ord-123"},  # no customer_id
            destination="orders",
        )

        # Should not raise -- handler should handle missing data gracefully
        await handler.handle(envelope)
```

---

## Complete Example

The following example puts everything together: a service under test with a fake
repository, DI container setup, and event assertions.

```python
"""tests/test_order_service.py"""

import pytest
from unittest.mock import AsyncMock

from pyfly.testing import (
    PyFlyTestCase,
    create_test_container,
    assert_event_published,
    assert_no_events_published,
)
from pyfly.container import Scope
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.exceptions import ResourceNotFoundException, ValidationException


# =========================================================================
# Test Doubles
# =========================================================================

class OrderRepository:
    """Interface for the order repository (used for type binding)."""
    pass


class FakeOrderRepository:
    """In-memory order repository for testing."""

    def __init__(self):
        self.orders: dict[str, dict] = {}

    async def save(self, order: dict) -> None:
        self.orders[order["id"]] = order

    async def find_by_id(self, order_id: str) -> dict | None:
        return self.orders.get(order_id)

    async def find_all(self) -> list[dict]:
        return list(self.orders.values())


# =========================================================================
# Service Under Test
# =========================================================================

class OrderService:
    def __init__(self, order_repository: OrderRepository) -> None:
        self._repo = order_repository

    async def create_order(self, data: dict) -> dict:
        if "customer_id" not in data:
            raise ValidationException("customer_id is required")
        order = {
            "id": "ord-001",
            "customer_id": data["customer_id"],
            "status": "created",
        }
        await self._repo.save(order)
        return order

    async def get_order(self, order_id: str) -> dict:
        order = await self._repo.find_by_id(order_id)
        if order is None:
            raise ResourceNotFoundException(
                f"Order {order_id} not found",
                code="ORDER_NOT_FOUND",
            )
        return order


# =========================================================================
# Unit Tests -- fast, isolated, mocked dependencies
# =========================================================================

class TestOrderServiceUnit:

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=FakeOrderRepository)

    @pytest.fixture
    def service(self, mock_repo):
        return OrderService(order_repository=mock_repo)

    async def test_create_order_success(self, service, mock_repo):
        result = await service.create_order({"customer_id": "cust-42"})

        assert result["status"] == "created"
        assert result["customer_id"] == "cust-42"
        mock_repo.save.assert_called_once()

    async def test_create_order_missing_customer_raises(self, service):
        with pytest.raises(ValidationException, match="customer_id is required"):
            await service.create_order({})

    async def test_get_order_not_found_raises(self, service, mock_repo):
        mock_repo.find_by_id.return_value = None

        with pytest.raises(ResourceNotFoundException):
            await service.get_order("nonexistent")


# =========================================================================
# Integration Tests -- real components, in-memory adapters
# =========================================================================

class TestOrderServiceIntegration:

    @pytest.fixture
    def container(self):
        container = create_test_container(overrides={
            OrderRepository: FakeOrderRepository,
        })
        container.register(OrderService, scope=Scope.SINGLETON)
        return container

    @pytest.fixture
    def service(self, container) -> OrderService:
        return container.resolve(OrderService)

    @pytest.fixture
    def repo(self, container) -> FakeOrderRepository:
        return container.resolve(OrderRepository)

    async def test_create_and_retrieve_order(self, service, repo):
        # Create an order
        created = await service.create_order({"customer_id": "cust-42"})
        assert created["status"] == "created"

        # Retrieve it from the repository
        fetched = await service.get_order(created["id"])
        assert fetched["customer_id"] == "cust-42"

        # Verify it was persisted in the fake repository
        assert created["id"] in repo.orders

    async def test_repository_is_shared_singleton(self, container):
        repo1 = container.resolve(OrderRepository)
        repo2 = container.resolve(OrderRepository)
        assert repo1 is repo2  # Same singleton instance


# =========================================================================
# Event Tests -- verify event publishing behavior
# =========================================================================

class TestOrderEvents(PyFlyTestCase):

    async def test_order_created_event(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        self.event_bus.subscribe("order.*", capture)

        # Simulate publishing an order created event
        await self.event_bus.publish(
            destination="orders",
            event_type="order.created",
            payload={
                "order_id": "ord-001",
                "customer_id": "cust-42",
                "total": 149.97,
            },
        )

        # Assert the event with payload matching
        event = assert_event_published(
            captured,
            "order.created",
            payload_contains={
                "order_id": "ord-001",
                "customer_id": "cust-42",
            },
        )
        assert event.payload["total"] == 149.97
        assert event.destination == "orders"

        await self.teardown()

    async def test_no_events_when_validation_fails(self):
        await self.setup()

        captured: list[EventEnvelope] = []

        async def capture(envelope: EventEnvelope) -> None:
            captured.append(envelope)

        self.event_bus.subscribe("order.*", capture)

        # Validation failure should not produce events
        # (no publish call was made)
        assert_no_events_published(captured)

        await self.teardown()
```

Run the tests:

```bash
# Run all tests with verbose output
pytest tests/test_order_service.py -v

# Run only unit tests
pytest tests/test_order_service.py::TestOrderServiceUnit -v

# Run only integration tests
pytest tests/test_order_service.py::TestOrderServiceIntegration -v

# Run with async support (if using pytest-asyncio)
pytest tests/test_order_service.py -v --asyncio-mode=auto
```
