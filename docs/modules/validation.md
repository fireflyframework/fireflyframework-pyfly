# Validation Guide

PyFly integrates deeply with [Pydantic](https://docs.pydantic.dev/) to provide
declarative input validation throughout your application. This guide covers the
validation helpers, decorators, the `Valid[T]` annotation for structured 422
error responses, and their integration with the web layer.

---

## Table of Contents

1. [Introduction](#introduction)
2. [validate_model()](#validate_model)
   - [Basic Usage](#basic-usage)
   - [Field Error Handling](#field-error-handling)
3. [@validate_input Decorator](#validate_input-decorator)
   - [Parameters](#parameters)
   - [How It Works](#how-it-works)
   - [Passthrough Behavior](#passthrough-behavior)
4. [@validator Decorator](#validator-decorator)
   - [Predicate Functions](#predicate-functions)
   - [Lambda Predicates](#lambda-predicates)
   - [Stacking Multiple Validators](#stacking-multiple-validators)
5. [Valid\[T\] Annotation](#validt-annotation)
   - [Why Valid\[T\]?](#why-validt)
   - [Usage Patterns](#usage-patterns)
   - [How It Works Internally](#how-it-works-internally)
   - [Structured 422 Error Response](#structured-422-error-response)
   - [Body\[T\] vs Valid\[T\]](#bodyt-vs-validt)
   - [Integration Examples](#integration-examples)
6. [ValidationException](#validationexception)
7. [Integration with the Web Layer](#integration-with-the-web-layer)
   - [Body\[T\] Automatic Validation](#bodyt-automatic-validation)
   - [Valid\[T\] Explicit Validation](#validt-explicit-validation)
   - [Automatic 422 Responses](#automatic-422-responses)
8. [Domain Validators](#domain-validators)
9. [Complete Example](#complete-example)

---

## Introduction

Validation is the first line of defense against invalid data entering your system.
PyFly provides four complementary validation mechanisms:

| Mechanism         | Purpose                                                        | Module                         |
|-------------------|----------------------------------------------------------------|--------------------------------|
| `validate_model()` | Validate a raw dict against a Pydantic model                 | `pyfly.validation.helpers`     |
| `@validate_input` | Decorator for automatic parameter validation                   | `pyfly.validation.decorators`  |
| `@validator`      | Decorator for custom predicate-based validation                | `pyfly.validation.decorators`  |
| `Valid[T]`        | Type annotation for explicit validation with structured errors | `pyfly.web.params`             |

All four raise `ValidationException` on failure, which the web layer automatically
converts to a `422 Unprocessable Entity` response with structured error details.

```python
from pyfly.validation import validate_model, validate_input, validator
from pyfly.web import Valid
```

**Source:** `src/pyfly/validation/__init__.py`, `src/pyfly/web/params.py`

---

## validate_model()

`validate_model()` is the core validation function. It takes a Pydantic model class
and a plain Python dictionary, validates the dictionary against the model's schema,
and returns a fully constructed model instance on success.

### Basic Usage

```python
from pydantic import BaseModel
from pyfly.validation import validate_model


class CreateUserRequest(BaseModel):
    name: str
    email: str
    age: int


# Valid data -- returns a CreateUserRequest instance
user = validate_model(CreateUserRequest, {
    "name": "Alice",
    "email": "alice@example.com",
    "age": 30,
})
print(user.name)   # "Alice"
print(user.email)  # "alice@example.com"
print(user.age)    # 30
```

**Parameters:**

| Parameter | Type               | Description                                 |
|-----------|--------------------|---------------------------------------------|
| `model`   | `type[T]`          | A Pydantic `BaseModel` subclass             |
| `data`    | `dict[str, Any]`   | The raw dictionary to validate              |

**Returns:** An instance of `T` (the validated model).

**Raises:** `ValidationException` when validation fails.

Internally, the function delegates to Pydantic's `model_validate()` method:

```python
def validate_model(model: type[T], data: dict[str, Any]) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors
        )
        raise ValidationException(
            f"Validation failed: {detail}",
            code="VALIDATION_ERROR",
            context={"errors": errors},
        ) from exc
```

**Source:** `src/pyfly/validation/helpers.py`

### Field Error Handling

When validation fails, `validate_model()` constructs a `ValidationException` with:

- A human-readable `message` that joins all field errors with semicolons.
- An error `code` of `"VALIDATION_ERROR"`.
- A `context` dict containing the raw Pydantic error list under the key `"errors"`.

```python
from pyfly.kernel.exceptions import ValidationException

try:
    validate_model(CreateUserRequest, {
        "name": "",
        "email": "not-an-email",
        # "age" is missing entirely
    })
except ValidationException as exc:
    print(exc)
    # "Validation failed: age: Field required"

    print(exc.code)
    # "VALIDATION_ERROR"

    print(exc.context["errors"])
    # [{"type": "missing", "loc": ["age"], "msg": "Field required", ...}]
```

The `context["errors"]` list contains Pydantic's native error dictionaries. Each
error has these fields:

| Field   | Description                                       |
|---------|---------------------------------------------------|
| `type`  | Error type identifier (e.g. `"missing"`, `"string_type"`) |
| `loc`   | Location path as a list (e.g. `["age"]` or `["address", "zip"]`) |
| `msg`   | Human-readable error message                      |
| `input` | The input value that failed validation            |

For nested models, the `loc` path reflects the nesting:

```python
class Address(BaseModel):
    zip_code: str

class Order(BaseModel):
    shipping: Address

try:
    validate_model(Order, {"shipping": {"zip_code": 12345}})
except ValidationException as exc:
    for error in exc.context["errors"]:
        print(error["loc"])  # ["shipping", "zip_code"]
        print(error["msg"])  # "Input should be a valid string"
```

---

## @validate_input Decorator

The `@validate_input` decorator validates a specific keyword argument of an async
function against a Pydantic model. If the argument is a raw dict, the decorator
converts it to a validated model instance before the function executes.

```python
from pydantic import BaseModel
from pyfly.validation import validate_input


class OrderRequest(BaseModel):
    product_id: str
    quantity: int


@validate_input(model=OrderRequest, param="order_data")
async def create_order(order_data: OrderRequest) -> dict:
    return {
        "product_id": order_data.product_id,
        "quantity": order_data.quantity,
        "status": "created",
    }


# Call with a raw dict -- it gets validated and converted automatically
result = await create_order(order_data={"product_id": "SKU-42", "quantity": 3})
# result == {"product_id": "SKU-42", "quantity": 3, "status": "created"}
```

### Parameters

| Parameter | Type              | Description                                         |
|-----------|-------------------|-----------------------------------------------------|
| `model`   | `type[BaseModel]` | The Pydantic model class to validate against        |
| `param`   | `str`             | The name of the keyword argument to validate        |

### How It Works

The decorator wraps the function with logic that:

1. Looks up `kwargs[param]` (the named keyword argument).
2. If the value is `None`, the function is called as-is (no validation occurs).
3. If the value is a `dict`, it is passed through `validate_model(model, value)`.
   On success, the validated model instance replaces the dict in `kwargs`.
4. If the value is already an instance of the model, it passes through untouched.
5. The decorated function is then awaited with the (potentially replaced) kwargs.

The implementation:

```python
@functools.wraps(func)
async def wrapper(*args: Any, **kwargs: Any) -> Any:
    value = kwargs.get(param)
    if value is not None and isinstance(value, dict):
        kwargs[param] = validate_model(model, value)
    return await func(*args, **kwargs)
```

### Passthrough Behavior

If the argument is already a model instance, no validation occurs -- it passes
through directly:

```python
# This also works -- the model instance passes through
order = OrderRequest(product_id="SKU-42", quantity=3)
result = await create_order(order_data=order)
```

This is useful when calling the function from tests or other service methods where
you have already constructed a validated model.

**Source:** `src/pyfly/validation/decorators.py`

---

## @validator Decorator

The `@validator` decorator applies a custom predicate function to the arguments of
an async function. If the predicate returns `False`, a `ValidationException` is
raised with the specified message.

```python
from pyfly.validation import validator


@validator(
    predicate=lambda self, amount: amount > 0,
    message="Amount must be positive",
)
async def process_payment(self, amount: float) -> dict:
    return {"amount": amount, "status": "processed"}
```

### Predicate Functions

The predicate receives the **same positional and keyword arguments** as the decorated
function. It should return `True` if the arguments are valid and `False` otherwise.

```python
def positive_quantity(self, product_id: str, quantity: int) -> bool:
    """Validate that quantity is positive."""
    return quantity > 0


@validator(predicate=positive_quantity, message="Quantity must be positive")
async def add_to_cart(self, product_id: str, quantity: int) -> dict:
    return {"product_id": product_id, "quantity": quantity}
```

**Parameters:**

| Parameter   | Type                      | Default              | Description                    |
|-------------|---------------------------|----------------------|--------------------------------|
| `predicate` | `Callable[..., bool]`     | required             | Validation function            |
| `message`   | `str`                     | `"Validation failed"` | Error message on failure      |

When validation fails, the decorator raises:

```python
ValidationException(message, code="VALIDATION_ERROR")
```

### Lambda Predicates

For simple checks, lambda predicates keep the code concise:

```python
@validator(
    predicate=lambda self, start, end: start < end,
    message="Start date must be before end date",
)
async def create_booking(self, start: str, end: str) -> dict:
    return {"start": start, "end": end}
```

You can also combine multiple conditions:

```python
@validator(
    predicate=lambda self, amount, currency: (
        amount > 0 and amount <= 999999 and currency in ("USD", "EUR", "GBP")
    ),
    message="Invalid amount or unsupported currency",
)
async def initiate_transfer(self, amount: float, currency: str) -> dict:
    return {"amount": amount, "currency": currency}
```

### Stacking Multiple Validators

Multiple `@validator` decorators can be stacked. They execute from bottom to top
(the innermost decorator runs first):

```python
@validator(
    predicate=lambda self, price, qty: price * qty <= 10000,
    message="Order total must not exceed $10,000",
)
@validator(
    predicate=lambda self, price, qty: qty > 0,
    message="Quantity must be positive",
)
async def place_order(self, price: float, qty: int) -> dict:
    return {"total": price * qty}
```

**Source:** `src/pyfly/validation/decorators.py`

---

## Valid[T] Annotation

### Why Valid[T]?

When you annotate a parameter with `Body[T]`, PyFly calls Pydantic's
`model_validate_json()` to parse and validate the request body. If the payload is
invalid, Pydantic raises a raw `ValidationError` -- which propagates to the global
exception handler as-is rather than as a structured `ValidationException` with
a consistent error code and context.

`Valid[T]` solves this. It is a Generic marker type (imported from `pyfly.web.params`
or `pyfly.web`) that explicitly marks a controller parameter for Pydantic validation
**with structured error handling**. When the `ParameterResolver` encounters `Valid[T]`,
it catches Pydantic's `ValidationError` and converts it to PyFly's
`ValidationException` with `code="VALIDATION_ERROR"` and
`context={"errors": [...]}`.

This ensures that **all** validation failures -- whether they originate from body
parsing, query parameter coercion, or header resolution -- produce the same
structured 422 response format.

### Usage Patterns

`Valid[T]` supports three usage patterns:

```python
from pyfly.web import Valid, Body, QueryParam, Header
from pyfly.web import rest_controller, request_mapping, post_mapping, get_mapping
```

**1. Standalone -- `Valid[T]` implies `Body[T]` with structured validation:**

When `Valid` wraps a Pydantic model directly (no inner binding type), the resolver
defaults to `Body[T]` for resolution and enables structured error handling:

```python
@post_mapping("/", status_code=201)
async def create(self, user: Valid[CreateUserRequest]) -> dict:
    # `user` is a validated CreateUserRequest instance
    # Validation errors produce structured 422 responses
    return await self._service.create(user)
```

**2. Wrapping `Body[T]` -- explicit validation marker:**

Wrapping `Body[T]` in `Valid` makes the validation intent explicit and enables
structured 422 errors:

```python
@post_mapping("/", status_code=201)
async def create(self, user: Valid[Body[CreateUserRequest]]) -> dict:
    return await self._service.create(user)
```

**3. Wrapping other binding types -- validate after resolution:**

`Valid` can wrap `QueryParam`, `Header`, or `Cookie` to apply Pydantic validation
to non-body parameters:

```python
@get_mapping("/search")
async def search(self, page: Valid[QueryParam[int]]) -> list:
    return await self._service.search(page=page)

@get_mapping("/protected")
async def protected(self, x_api_key: Valid[Header[str]]) -> dict:
    return {"key_length": len(x_api_key)}
```

### How It Works Internally

The `ParameterResolver` in `src/pyfly/web/adapters/starlette/resolver.py` processes
`Valid[T]` through the following steps:

1. **`_inspect()` detects `Valid` as the origin type** -- using `get_origin(hint)`.
2. **Peels the `Valid` layer** to find the inner type via `get_args(hint)`.
3. **Checks if the inner type is a binding type** (`Body`, `QueryParam`, `Header`,
   `Cookie`, `PathVar`):
   - If yes (e.g. `Valid[Body[T]]`), uses that binding type for resolution.
   - If no (e.g. `Valid[T]` standalone), defaults to `Body[T]`.
4. **Sets `validate=True`** on the `ResolvedParam` dataclass.
5. **At request time**, `resolve()` calls `_resolve_one()` for normal parameter
   resolution, then checks `param.validate`.
6. **For body params with `validate=True`**: `_resolve_body()` wraps the
   `model_validate_json()` call in a try/except that catches Pydantic's
   `ValidationError` and converts it to a `ValidationException`.
7. **For dict values**: `_run_validation()` calls `validate_model()` from
   `pyfly.validation.helpers` for model validation.
8. **For `BaseModel` instances**: `_run_validation()` recognizes the value is
   already validated and passes it through.

The key dataclass used by the resolver:

```python
@dataclass
class ResolvedParam:
    """Metadata for a single resolved parameter."""

    name: str
    binding_type: type
    inner_type: type
    default: Any = _MISSING
    validate: bool = False
```

The `validate` field is `False` by default and only set to `True` when the resolver
encounters a `Valid[...]` annotation.

### Structured 422 Error Response

When `Valid[T]` validation fails, the resulting `ValidationException` is caught by
the global exception handler and converted to a structured 422 response:

```json
{
    "error": {
        "message": "Validation failed: name: Field required; age: Input should be greater than 0",
        "code": "VALIDATION_ERROR",
        "status": 422,
        "path": "/api/users",
        "timestamp": "2026-01-15T10:30:00Z",
        "transaction_id": "tx-abc-123",
        "context": {
            "errors": [
                {"type": "missing", "loc": ["name"], "msg": "Field required"},
                {"type": "greater_than", "loc": ["age"], "msg": "Input should be greater than 0"}
            ]
        }
    }
}
```

The response body follows the same RFC 7807-inspired format used by all PyFly error
responses. The `context.errors` array contains Pydantic's native error dictionaries,
giving API consumers fine-grained, machine-parseable error information for each
invalid field.

### Body[T] vs Valid[T]

| Feature | `Body[T]` | `Valid[T]` |
|---------|-----------|------------|
| Pydantic validation | Yes (via `model_validate_json`) | Yes (via `model_validate_json`) |
| Error type on failure | Raw Pydantic `ValidationError` | PyFly `ValidationException` |
| Error format | Pydantic native | Structured with `code` + `context` |
| Error code in response | Pydantic error propagation | `"VALIDATION_ERROR"` |
| `context.errors` field | Not guaranteed | Always present with field-level details |
| Works with `QueryParam` | N/A | Yes: `Valid[QueryParam[T]]` |
| Works with `Header` | N/A | Yes: `Valid[Header[T]]` |
| Works with `Cookie` | N/A | Yes: `Valid[Cookie[T]]` |

**When to use which:**

- Use `Body[T]` when you want simple body parsing and are fine with Pydantic's
  default error propagation.
- Use `Valid[T]` (or `Valid[Body[T]]`) when you need consistent, structured 422
  error responses with `code="VALIDATION_ERROR"` and a `context.errors` array.
- Use `Valid[QueryParam[T]]` or `Valid[Header[T]]` when you need to validate
  non-body parameters with the same structured error format.

### Integration Examples

A complete controller using `Valid[T]` with Pydantic field constraints:

```python
from pydantic import BaseModel, Field
from pyfly.container import rest_controller, service
from pyfly.web import request_mapping, post_mapping, get_mapping, Valid, QueryParam


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[\w.-]+@[\w.-]+\.\w+$")
    age: int = Field(gt=0, le=150)


@service
class UserService:
    async def create(self, user: CreateUserRequest) -> dict:
        return {
            "id": "usr-001",
            "name": user.name,
            "email": user.email,
            "age": user.age,
        }

    async def search(self, page: int, size: int) -> list:
        return [{"id": "usr-001", "name": "Alice"}]


@rest_controller
@request_mapping("/api/users")
class UserController:
    def __init__(self, user_service: UserService) -> None:
        self._service = user_service

    @post_mapping("", status_code=201)
    async def create(self, user: Valid[CreateUserRequest]) -> dict:
        """Valid[T] validates the body and produces structured 422 errors."""
        return await self._service.create(user)

    @get_mapping("/search")
    async def search(
        self,
        page: Valid[QueryParam[int]],
        size: QueryParam[int] = 20,
    ) -> list:
        """Valid[QueryParam[int]] validates the query parameter."""
        return await self._service.search(page=page, size=size)
```

Sending an invalid request to the `create` endpoint:

```bash
curl -X POST http://localhost:8080/api/users \
  -H "Content-Type: application/json" \
  -d '{"email": "bad", "age": -5}'
```

Produces the following structured 422 response:

```json
{
    "error": {
        "message": "Validation failed: name: Field required; email: String should match pattern '^[\\w.-]+@[\\w.-]+\\.\\w+$'; age: Input should be greater than 0",
        "code": "VALIDATION_ERROR",
        "transaction_id": "a1b2c3d4-...",
        "timestamp": "2026-01-15T10:30:00Z",
        "status": 422,
        "path": "/api/users",
        "context": {
            "errors": [
                {
                    "type": "missing",
                    "loc": ["name"],
                    "msg": "Field required"
                },
                {
                    "type": "string_pattern_mismatch",
                    "loc": ["email"],
                    "msg": "String should match pattern '^[\\w.-]+@[\\w.-]+\\.\\w+$'"
                },
                {
                    "type": "greater_than",
                    "loc": ["age"],
                    "msg": "Input should be greater than 0"
                }
            ]
        }
    }
}
```

**Source:** `src/pyfly/web/params.py`, `src/pyfly/web/adapters/starlette/resolver.py`

---

## ValidationException

All validation mechanisms in PyFly raise `ValidationException` on failure. This
exception is part of PyFly's structured exception hierarchy:

```
PyFlyException
  -> BusinessException
       -> ValidationException
```

**Constructor:**

```python
ValidationException(
    message: str,
    code: str | None = None,
    context: dict | None = None,
)
```

| Field     | Type           | Description                                     |
|-----------|----------------|-------------------------------------------------|
| `message` | `str`          | Human-readable error description                |
| `code`    | `str \| None`   | Machine-readable error code (e.g. `"VALIDATION_ERROR"`) |
| `context` | `dict`         | Structured error details (e.g. Pydantic errors) |

When raised by `validate_model()` or `Valid[T]`, the context contains Pydantic's
error list:

```python
{
    "errors": [
        {"type": "missing", "loc": ["field_name"], "msg": "Field required", ...},
        ...
    ]
}
```

When raised by `@validator`, the context is empty by default (since the predicate
only returns True/False, no field-level details are available).

**Source:** `src/pyfly/kernel/exceptions.py`

---

## Integration with the Web Layer

### Body[T] Automatic Validation

When you annotate a controller handler parameter with `Body[T]` where `T` is a
Pydantic `BaseModel`, PyFly automatically:

1. Reads the JSON request body.
2. Validates the JSON against the model `T` using `model_validate_json()`.
3. Passes the validated model instance to your handler.

```python
from pydantic import BaseModel, Field
from pyfly.container import rest_controller
from pyfly.web import request_mapping, post_mapping, Body


class CreateOrderRequest(BaseModel):
    customer_id: str
    items: list[dict] = Field(min_length=1)
    notes: str = ""


@rest_controller
@request_mapping("/api/orders")
class OrderController:

    @post_mapping("", status_code=201)
    async def create_order(self, body: Body[CreateOrderRequest]) -> dict:
        # `body` is a validated CreateOrderRequest instance
        return {
            "customer_id": body.customer_id,
            "item_count": len(body.items),
            "status": "created",
        }
```

Note that with plain `Body[T]`, Pydantic's `ValidationError` propagates directly.
If you need structured 422 errors with `code="VALIDATION_ERROR"` and a
`context.errors` array, use `Valid[T]` instead (see the next section).

### Valid[T] Explicit Validation

When you annotate a parameter with `Valid[T]`, PyFly adds a structured error
handling layer on top of the normal parameter resolution. This applies to any
binding type:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, post_mapping, get_mapping
from pyfly.web import Valid, Body, QueryParam, Header


@rest_controller
@request_mapping("/api/orders")
class OrderController:

    @post_mapping("", status_code=201)
    async def create(self, body: Valid[CreateOrderRequest]) -> dict:
        """Standalone Valid[T] -- implies Body[T] + structured errors."""
        return {"status": "created"}

    @post_mapping("/explicit", status_code=201)
    async def create_explicit(self, body: Valid[Body[CreateOrderRequest]]) -> dict:
        """Explicit Valid[Body[T]] -- same behavior, more readable intent."""
        return {"status": "created"}

    @get_mapping("/search")
    async def search(self, page: Valid[QueryParam[int]]) -> list:
        """Valid[QueryParam[T]] -- validates query parameters."""
        return []
```

All three patterns produce the same structured 422 error response when validation
fails.

### Automatic 422 Responses

When validation fails -- whether from `Body[T]`, `Valid[T]`, `validate_model()`,
`@validate_input`, or `@validator` -- the global exception handler catches the
resulting `ValidationException` and returns a structured `422 Unprocessable Entity`
response:

```json
{
    "error": {
        "message": "Validation failed: customer_id: Field required; items: List should have at least 1 item after validation, not 0",
        "code": "VALIDATION_ERROR",
        "transaction_id": "tx-abc-123",
        "timestamp": "2026-01-15T10:30:00Z",
        "status": 422,
        "path": "/api/orders",
        "context": {
            "errors": [
                {
                    "type": "missing",
                    "loc": ["customer_id"],
                    "msg": "Field required"
                },
                {
                    "type": "too_short",
                    "loc": ["items"],
                    "msg": "List should have at least 1 item after validation, not 0"
                }
            ]
        }
    }
}
```

This mapping is defined in the global exception handler at
`src/pyfly/web/adapters/starlette/errors.py`:

```python
_STATUS_MAP: dict[type, int] = {
    ValidationException: 422,
    # ... other exceptions
}
```

The same 422 response is produced whether the validation failure comes from
`Valid[T]`, `Body[T]`, `validate_model()`, `@validate_input`, or `@validator`.

---

## Domain Validators

Beyond the Pydantic helpers, `pyfly.validation` ships a set of domain-specific
validators (a port of `org.fireflyframework.validators`) for common financial
and identity data. Each `is_valid_*` function returns `True`/`False`; each
`valid_*` factory returns the value on success and raises `ValueError` on
failure, so it plugs straight into a Pydantic `field_validator`.

```python
from pyfly.validation import is_valid_iban, is_valid_currency_code, valid_credit_card
from pydantic import BaseModel, field_validator

is_valid_iban("DE89 3704 0044 0532 0130 00")   # True (format + length + mod-97)
is_valid_currency_code("USD")                   # True (active ISO 4217 code)
is_valid_currency_code("XYZ")                   # False

class Payment(BaseModel):
    card_number: str
    _check_card = field_validator("card_number")(valid_credit_card)
```

| Function | Checks |
|----------|--------|
| `is_valid_iban` | Format, **country-specific total length** (ISO 13616), and mod-97 checksum. |
| `is_valid_bic` | SWIFT/BIC format (8 or 11 chars). |
| `is_valid_currency_code` | Membership in the active **ISO 4217** code set (e.g. `XYZ` rejected). |
| `is_valid_credit_card` | Length 12–19, a **recognised card scheme** (Visa, Mastercard, Amex, Discover, Diners, JCB), *and* Luhn checksum. |
| `is_valid_cvv` | 3–4 digits. |
| `is_valid_phone_number` | E.164 format. |
| `is_valid_amount` | Non-negative number with bounded precision (`allow_zero`, `max_digits`). |
| `is_valid_account_number` | Alphanumeric, 6–34 chars. |
| `is_valid_tax_id` / `is_valid_national_id` | Generic alphanumeric formats. |
| `is_valid_pin` | All digits, configurable `length` (default 4). |
| `is_strong_password` | Lower + upper + digit + symbol, configurable `min_length`. |
| `is_valid_date` / `is_valid_datetime` | ISO-8601 date / datetime (datetime accepts trailing `Z`). |
| `is_valid_sort_code` | UK sort code (6 digits, optional `-`). |
| `is_valid_interest_rate` | Percentage within `[min_pct, max_pct]` (default 0–100). |

A matching `valid_*` factory exists for each (e.g. `valid_iban`,
`valid_currency_code`, `valid_credit_card`), each raising `ValueError` with a
descriptive message on invalid input.

**Source:** `src/pyfly/validation/domain.py`

---

## Complete Example

The following example demonstrates a complete order validation workflow using nested
Pydantic models, `Valid[T]` for structured 422 errors, custom validators, and full
web integration.

```python
"""order_service/controllers.py"""

from pydantic import BaseModel, Field
from pyfly.container import rest_controller, service
from pyfly.web import (
    request_mapping,
    post_mapping,
    get_mapping,
    Valid,
    Body,
    PathVar,
    QueryParam,
)
from pyfly.validation import validate_model, validate_input, validator
from pyfly.kernel.exceptions import ValidationException, ResourceNotFoundException


# =========================================================================
# Pydantic Models
# =========================================================================

class Address(BaseModel):
    street: str
    city: str
    state: str = Field(min_length=2, max_length=2)
    zip_code: str = Field(pattern=r"^\d{5}(-\d{4})?$")


class OrderItem(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, description="Must be at least 1")
    unit_price: float = Field(gt=0)


class CreateOrderRequest(BaseModel):
    customer_id: str
    shipping_address: Address
    items: list[OrderItem] = Field(min_length=1)
    notes: str = ""


# =========================================================================
# Service Layer
# =========================================================================

@service
class OrderService:
    """Order service with layered validation."""

    @validate_input(model=CreateOrderRequest, param="order_data")
    @validator(
        predicate=lambda self, order_data: (
            sum(item.quantity * item.unit_price
                for item in order_data.items) <= 10000
        ),
        message="Order total must not exceed $10,000",
    )
    async def create_order(self, order_data: CreateOrderRequest) -> dict:
        """Create a new order after validation."""
        total = sum(
            item.quantity * item.unit_price for item in order_data.items
        )
        return {
            "order_id": "ord-001",
            "customer_id": order_data.customer_id,
            "total": round(total, 2),
            "item_count": len(order_data.items),
            "shipping_city": order_data.shipping_address.city,
            "status": "created",
        }

    async def search_orders(self, page: int, size: int) -> list:
        """Search orders with pagination."""
        return [
            {"order_id": "ord-001", "customer_id": "cust-42", "total": 59.98},
        ]


# =========================================================================
# Controller Layer
# =========================================================================

@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, order_service: OrderService) -> None:
        self._service = order_service

    @post_mapping("", status_code=201)
    async def create(self, body: Valid[CreateOrderRequest]) -> dict:
        """Valid[T] validates the JSON body with structured 422 errors."""
        return await self._service.create_order(order_data=body)

    @get_mapping("/search")
    async def search(
        self,
        page: Valid[QueryParam[int]],
        size: QueryParam[int] = 20,
    ) -> list:
        """Valid[QueryParam[int]] validates the page query parameter."""
        return await self._service.search_orders(page=page, size=size)


# =========================================================================
# Manual Validation Example
# =========================================================================

async def manual_validation_demo():
    """Shows validate_model() used directly."""

    # Successful validation
    data = {
        "customer_id": "cust-42",
        "shipping_address": {
            "street": "123 Main St",
            "city": "Springfield",
            "state": "IL",
            "zip_code": "62701",
        },
        "items": [
            {"product_id": "SKU-001", "quantity": 2, "unit_price": 29.99},
            {"product_id": "SKU-002", "quantity": 1, "unit_price": 49.99},
        ],
    }
    order = validate_model(CreateOrderRequest, data)
    print(f"Validated: {order.customer_id}, {len(order.items)} items")

    # Failed validation -- invalid zip code, empty items
    try:
        validate_model(CreateOrderRequest, {
            "customer_id": "cust-99",
            "shipping_address": {
                "street": "456 Oak Ave",
                "city": "Portland",
                "state": "OR",
                "zip_code": "INVALID",
            },
            "items": [],
        })
    except ValidationException as exc:
        print(f"Error: {exc}")
        print(f"Code: {exc.code}")
        for error in exc.context["errors"]:
            loc = ".".join(str(part) for part in error["loc"])
            print(f"  {loc}: {error['msg']}")
```

**Testing the endpoint with `curl`:**

```bash
# Successful creation with Valid[T]
curl -X POST http://localhost:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "cust-42",
    "shipping_address": {
      "street": "123 Main St",
      "city": "Springfield",
      "state": "IL",
      "zip_code": "62701"
    },
    "items": [
      {"product_id": "SKU-001", "quantity": 2, "unit_price": 29.99}
    ]
  }'
# HTTP 201
# {"order_id": "ord-001", "customer_id": "cust-42", "total": 59.98, ...}

# Validation failure -- missing required fields (structured 422 via Valid[T])
curl -X POST http://localhost:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{"items": []}'
# HTTP 422
# {
#   "error": {
#     "message": "Validation failed: customer_id: Field required; shipping_address: Field required; items: List should have at least 1 item after validation, not 0",
#     "code": "VALIDATION_ERROR",
#     "transaction_id": "...",
#     "timestamp": "...",
#     "status": 422,
#     "path": "/api/orders",
#     "context": {
#       "errors": [
#         {"type": "missing", "loc": ["customer_id"], "msg": "Field required"},
#         {"type": "missing", "loc": ["shipping_address"], "msg": "Field required"},
#         {"type": "too_short", "loc": ["items"], "msg": "List should have at least 1 item after validation, not 0"}
#       ]
#     }
#   }
# }

# Search with validated query parameter
curl "http://localhost:8080/api/orders/search?page=1&size=20"
# HTTP 200
# [{"order_id": "ord-001", "customer_id": "cust-42", "total": 59.98}]
```
