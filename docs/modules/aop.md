# Aspect-Oriented Programming (AOP) Guide

Add cross-cutting concerns like logging, security, and performance monitoring
to your application without modifying business logic, using the PyFly AOP
module.

---

## Table of Contents

1. [Introduction](#introduction)
   - [What Is AOP?](#what-is-aop)
   - [When to Use AOP](#when-to-use-aop)
2. [The @aspect Decorator](#the-aspect-decorator)
3. [Advice Types](#advice-types)
   - [@before](#before)
   - [@after_returning](#after_returning)
   - [@after_throwing](#after_throwing)
   - [@after](#after)
   - [@around](#around)
4. [JoinPoint](#joinpoint)
   - [Attributes](#attributes)
   - [Using proceed() in @around Advice](#using-proceed-in-around-advice)
5. [Pointcut Expressions](#pointcut-expressions)
   - [Pattern Syntax](#pattern-syntax)
   - [Wildcards](#wildcards)
   - [Qualified Name Construction](#qualified-name-construction)
   - [Examples](#examples)
6. [matches_pointcut()](#matches_pointcut)
7. [AspectRegistry and AdviceBinding](#aspectregistry-and-advicebinding)
   - [AdviceBinding Dataclass](#advicebinding-dataclass)
   - [AspectRegistry Methods](#aspectregistry-methods)
8. [AspectBeanPostProcessor](#aspectbeanpostprocessor)
9. [weave_bean()](#weave_bean)
   - [Async Methods](#async-methods)
   - [Sync Methods](#sync-methods)
   - [The Advice Chain](#the-advice-chain)
10. [Auto-Configuration](#auto-configuration)
11. [Ordering Aspects](#ordering-aspects)
12. [Complete Examples](#complete-examples)
    - [Logging Aspect](#logging-aspect)
    - [Performance Monitoring Aspect](#performance-monitoring-aspect)
    - [Audit Trail Aspect](#audit-trail-aspect)
    - [Putting It All Together](#putting-it-all-together)

---

## Introduction

### What Is AOP?

Aspect-Oriented Programming (AOP) is a programming paradigm that separates
**cross-cutting concerns** from **business logic**. Cross-cutting concerns are
behaviors that affect multiple parts of an application but do not belong to any
single module:

- Logging method calls and their results
- Enforcing security and authorization checks
- Measuring execution time and reporting metrics
- Auditing data changes
- Handling transactions

Without AOP, these concerns end up scattered across every service method,
creating code duplication and tangling business logic with infrastructure code.
AOP lets you define these behaviors once, in a single place (an **aspect**),
and apply them declaratively to the methods that need them.

### When to Use AOP

AOP is best for behaviors that:

- Apply uniformly across many methods or classes
- Are orthogonal to business logic (logging, metrics, security)
- Should be easy to enable/disable without touching business code

Avoid using AOP for business rules specific to a single method -- that logic
belongs in the method itself.

PyFly's AOP implementation is available from a single import:

```python
from pyfly.aop import (
    aspect,
    before,
    after_returning,
    after_throwing,
    after,
    around,
    JoinPoint,
    AspectRegistry,
    AdviceBinding,
    AspectBeanPostProcessor,
    weave_bean,
    matches_pointcut,
)
```

---

## The @aspect Decorator

`@aspect` marks a class as a PyFly aspect. It sets metadata that the framework
uses for automatic discovery and registration:

```python
from pyfly.aop import aspect

@aspect
class LoggingAspect:
    ...
```

The decorator sets the following attributes on the class:

| Attribute | Value |
|---|---|
| `__pyfly_aspect__` | `True` |
| `__pyfly_injectable__` | `True` |
| `__pyfly_stereotype__` | `"aspect"` |
| `__pyfly_scope__` | `Scope.SINGLETON` |

Because `__pyfly_injectable__` is set, aspects are automatically registered in
the PyFly dependency injection container as singletons. They can receive
injected dependencies just like any other bean.

Note that `@aspect` is applied directly to the class (not as a call with
arguments):

```python
# Correct
@aspect
class MyAspect: ...

# Incorrect -- @aspect does not take arguments
@aspect()
class MyAspect: ...
```

---

## Advice Types

Advice is the action taken by an aspect at a particular join point. PyFly
supports five advice types, each implemented as a decorator factory that takes
a pointcut expression string.

### @before

Runs **before** the target method executes. Receives a `JoinPoint` with the
method name, arguments, and target object:

```python
from pyfly.aop import aspect, before, JoinPoint

@aspect
class SecurityAspect:
    @before("service.*.create_*")
    def check_permissions(self, jp: JoinPoint):
        user = jp.kwargs.get("current_user")
        if not user or not user.has_permission("write"):
            raise PermissionError(f"User cannot call {jp.method_name}")
```

`@before` advice handlers must be **synchronous** — the weaver calls them
without `await`. They cannot modify the arguments or prevent execution (unless
the handler raises an exception). They are ideal for validation, logging, and
security checks.

### @after_returning

Runs **after** the target method returns successfully. The `JoinPoint`'s
`return_value` attribute contains the result:

```python
@aspect
class AuditAspect:
    @after_returning("service.OrderService.*")
    def audit_success(self, jp: JoinPoint):
        print(
            f"[AUDIT] {jp.method_name} completed successfully. "
            f"Result: {jp.return_value}"
        )
```

This advice runs only when the method succeeds. If the method raises an
exception, `@after_returning` is skipped. Handlers must be **synchronous**.

### @after_throwing

Runs **after** the target method raises an exception. The `JoinPoint`'s
`exception` attribute contains the caught exception:

```python
@aspect
class ErrorTrackingAspect:
    @after_throwing("**.*.process_*")
    def track_error(self, jp: JoinPoint):
        print(
            f"[ERROR] {jp.method_name} raised {type(jp.exception).__name__}: "
            f"{jp.exception}"
        )
        # Send to error tracking service
        self.error_tracker.capture(jp.exception)
```

The original exception is always re-raised after the advice runs. You cannot
suppress it from `@after_throwing`. Handlers must be **synchronous**.

### @after

Runs **after** the target method, regardless of whether it succeeded or raised
an exception. Analogous to a `finally` block:

```python
@aspect
class ResourceCleanupAspect:
    @after("service.*.execute_*")
    def cleanup(self, jp: JoinPoint):
        print(f"[CLEANUP] {jp.method_name} finished (success or failure)")
```

`@after` always runs, even if `@after_returning` or `@after_throwing` advice
also executed. You can inspect both `jp.return_value` and `jp.exception` to
determine the outcome. Handlers must be **synchronous**.

### @around

The most powerful advice type. Wraps the entire method execution, giving you
control over whether and how the method is called. The `JoinPoint`'s
`proceed()` callable invokes the next advice in the chain or the original
method:

```python
import time
from pyfly.aop import aspect, around, JoinPoint

@aspect
class TimingAspect:
    @around("service.*.*")
    async def measure_time(self, jp: JoinPoint):
        start = time.perf_counter()
        try:
            result = await jp.proceed()
            return result
        finally:
            elapsed = time.perf_counter() - start
            print(f"[TIMING] {jp.method_name} took {elapsed:.3f}s")
```

Key rules for `@around` advice:

- You **must** call `jp.proceed()` to execute the target method (or the next
  around advice in the chain). If you do not call `proceed()`, the target method
  never runs.
- You **must** return the result from `proceed()` (or a substitute value).
- `@around` is supported for **both async and sync methods**. For async methods,
  `jp.proceed` is an async callable and you must `await` it. For sync methods,
  `jp.proceed` is a sync callable — do not `await` it.
- If there are multiple `@around` advices, they chain: the first around's
  `proceed()` calls the second around, whose `proceed()` calls the third, and
  so on until the original method is reached.
- Around advice handlers on async joinpoints can be either sync or async — if
  the handler returns an awaitable, the weaver awaits it. On sync joinpoints the
  handler must be synchronous.

---

## JoinPoint

`JoinPoint` is a dataclass that represents the point in execution where advice
is applied. It carries all the context needed by advice methods.

### Attributes

```python
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

@dataclass
class JoinPoint:
    target: Any                           # The bean instance being intercepted
    method_name: str                      # Name of the method being called
    args: tuple                           # Positional arguments
    kwargs: dict[str, Any]                # Keyword arguments
    return_value: Any = None              # Set after successful execution
    exception: Exception | None = None    # Set after an exception
    proceed: Callable[..., Any] | None = None  # Set in @around advice
```

| Attribute | Available in | Description |
|---|---|---|
| `target` | All advice types | The object whose method is being intercepted. |
| `method_name` | All advice types | The name of the method (e.g., `"create_order"`). |
| `args` | All advice types | Positional arguments passed to the method. |
| `kwargs` | All advice types | Keyword arguments passed to the method. |
| `return_value` | `@after_returning`, `@after` | The value returned by the method. `None` until the method completes. |
| `exception` | `@after_throwing`, `@after` | The exception raised by the method. `None` if no exception. |
| `proceed` | `@around` only | An async callable that invokes the next advice or the original method. |

### Using proceed() in @around Advice

In `@around` advice, `jp.proceed()` is a callable that takes no arguments. It
invokes either the next `@around` advice in the chain or the original method
(if this is the innermost around). For **async** joinpoints, `proceed` is async
and must be `await`ed; for **sync** joinpoints it is a plain callable:

```python
# Async method joinpoint — must await proceed()
@around("service.*.*")
async def my_around(self, jp: JoinPoint):
    # Pre-processing
    print(f"Before {jp.method_name}")

    # Call the target (or next around)
    result = await jp.proceed()

    # Post-processing
    print(f"After {jp.method_name}, got: {result}")

    return result  # Must return the result to the caller

# Sync method joinpoint — call proceed() without await
@around("repository.*.*")
def my_sync_around(self, jp: JoinPoint):
    result = jp.proceed()
    return result
```

You can also modify the result:

```python
@around("service.*.get_*")
async def add_metadata(self, jp: JoinPoint):
    result = await jp.proceed()
    if isinstance(result, dict):
        result["_served_by"] = "node-1"
    return result
```

Or implement retry logic:

```python
import asyncio

@around("service.*.call_external")
async def retry_on_failure(self, jp: JoinPoint):
    for attempt in range(3):
        try:
            return await jp.proceed()
        except ConnectionError:
            if attempt == 2:
                raise
            await asyncio.sleep(1)
```

---

## Pointcut Expressions

Pointcut expressions define which methods an advice applies to. They use a
dot-separated pattern that is matched against the fully qualified name of each
bean method.

### Pattern Syntax

A pointcut pattern is a dot-separated string where each segment can be:

| Token | Meaning |
|---|---|
| `*` | Matches exactly one segment (does not cross dots) |
| `**` | Matches one or more segments (crosses dots) |
| Literal | Matches the exact text |
| Partial glob | Supports `*` and `?` within a segment (e.g., `get_*`, `*Service`) |

### Wildcards

- **Single segment** (`*`): Matches one name component.
  `service.*.create` matches `service.OrderService.create` but not
  `service.order.OrderService.create`.

- **Multi-segment** (`**`): Matches one or more name components across dots.
  `**.*Service.*` matches `a.b.c.OrderService.create`.

- **Partial glob** (`get_*`): Matches within a segment using fnmatch-style
  rules. `mymod.MyClass.get_*` matches `mymod.MyClass.get_order` and
  `mymod.MyClass.get_user`. The `?` wildcard matches a single character
  within a segment.

### Qualified Name Construction

When `AspectBeanPostProcessor` weaves advice, it constructs the qualified name
for each bean method as follows:

- If the bean has a `__pyfly_stereotype__` (e.g., `"component"`, `"service"`):
  the prefix is `"{stereotype}.{ClassName}"`
- Otherwise: the prefix is `"{module}.{ClassName}"`

The method name is appended: `"{prefix}.{method_name}"`

This means a `@service` bean `OrderService` with method `create` has the
qualified name `service.OrderService.create`. A pointcut of `service.*.*`
matches all public methods on all service beans.

### Examples

```python
from pyfly.aop import matches_pointcut

# Exact match
matches_pointcut("service.OrderService.create", "service.OrderService.create")
# True

# Single-segment wildcard for class name
matches_pointcut("service.*.create", "service.OrderService.create")
# True

# Single-segment wildcard -- does NOT cross dots
matches_pointcut("*.my_method", "a.b.MyClass.my_method")
# False (*.my_method expects exactly two segments)

# Multi-segment wildcard
matches_pointcut("**.*Service.*", "a.b.c.OrderService.create")
# True

# Partial glob in method name
matches_pointcut("mymod.MyClass.get_*", "mymod.MyClass.get_order")
# True

# Match all methods on all service classes
matches_pointcut("service.*.*", "service.UserService.delete")
# True
```

---

## matches_pointcut()

The `matches_pointcut(pattern, qualified_name)` function is the core matching
engine. It converts the pattern to a regular expression and performs a full
match:

```python
from pyfly.aop import matches_pointcut

result = matches_pointcut("service.*.create_*", "service.OrderService.create_order")
# True
```

Internally, the function:

1. Splits the pattern on `.` into segments.
2. Converts each segment to a regex fragment:
   - `**` becomes `(?:[^.]+\.)*[^.]+` (one or more dot-separated segments)
   - `*` becomes `[^.]+` (one segment, no dots)
   - Partial globs: `*` within text becomes `[^.]*`, `?` becomes `[^.]`,
     other characters are regex-escaped
3. Joins segments with `\.` and compiles a `re.Pattern` for full matching.
4. Returns `True` if `regex.fullmatch(qualified_name)` succeeds.

---

## AspectRegistry and AdviceBinding

### AdviceBinding Dataclass

Each piece of advice is stored as an `AdviceBinding`:

```python
from pyfly.aop import AdviceBinding

@dataclass
class AdviceBinding:
    advice_type: str     # "before", "after_returning", "after_throwing", "after", "around"
    pointcut: str        # The pointcut expression string
    handler: Any         # The bound method on the aspect instance
    aspect_order: int    # Numeric ordering from @order decorator
```

### AspectRegistry Methods

`AspectRegistry` collects aspect instances and provides advice lookups:

```python
from pyfly.aop import AspectRegistry

registry = AspectRegistry()
```

| Method | Description |
|---|---|
| `register(aspect_instance)` | Extract all advice methods from the aspect and store as bindings. Keeps bindings sorted by `aspect_order`. |
| `get_all_bindings()` | Return all registered bindings, sorted by order. |
| `get_matching(qualified_name)` | Return bindings whose pointcut matches the given qualified name. |

**Registration process:**

When `register()` is called, it:

1. Inspects all methods on the aspect instance using `inspect.getmembers()`.
2. Checks each method for `__pyfly_advice_type__` and `__pyfly_pointcut__`
   attributes. It checks both the bound method and the underlying unbound
   function on the class, since bound methods may not propagate all custom
   attributes.
3. Creates an `AdviceBinding` for each advice method, including the aspect's
   order value from `get_order()` (defaults to `0` if no `@order` decorator).
4. Re-sorts all bindings by `aspect_order` to maintain global ordering.

```python
registry = AspectRegistry()
registry.register(logging_aspect)
registry.register(security_aspect)

# Query for a specific method
bindings = registry.get_matching("service.OrderService.create")
for b in bindings:
    print(f"  {b.advice_type}: {b.pointcut} (order={b.aspect_order})")
```

---

## AspectBeanPostProcessor

`AspectBeanPostProcessor` is the glue that integrates AOP into the PyFly
container lifecycle. It implements the two-phase bean post-processing pattern:

**Phase 1 -- before_init(bean, bean_name):**

If the bean's class has `__pyfly_aspect__ = True`, the bean is registered in
an internal `AspectRegistry`. The `AspectRegistry` is created lazily on the
first aspect bean encountered. The aspect's class is recorded in a set to
avoid re-weaving it in phase 2.

**Phase 2 -- after_init(bean, bean_name):**

If the registry exists and the bean is **not** an aspect, the post-processor
calls `weave_bean()` to wrap the bean's methods with matching advice chains.
The qualified prefix is derived from the bean's `__pyfly_stereotype__` or
module path.

```python
from pyfly.aop import AspectBeanPostProcessor

# Typically registered with the ApplicationContext
context.register_post_processor(AspectBeanPostProcessor())
```

This means aspects must be initialized before the beans they advise. PyFly's
container processes beans in order, and aspects (as singletons) are typically
initialized early.

> **Note:** As of v0.2.0-M5, `AspectBeanPostProcessor` is automatically registered as a container bean via `AopAutoConfiguration`. You no longer need to manually create or register it — the framework handles this during context startup.

---

## weave_bean()

`weave_bean(bean, qualified_prefix, registry)` is the function that actually
wraps bean methods with advice. It iterates over all public methods (names not
starting with `_`) on the bean, builds a qualified name, queries the registry
for matching bindings, and replaces the method on the bean instance with a
wrapped version via `setattr`.

### Async Methods

For async (coroutine) methods, the wrapper:

1. Creates a `JoinPoint` with the target, method name, args, and kwargs.
2. Executes all `@before` bindings (synchronous calls with the `JoinPoint`).
3. If `@around` bindings exist, builds a proceed chain:
   - The innermost callable invokes the original method.
   - Each around advice wraps the next, setting `jp.proceed` before calling
     the handler.
   - Around bindings are processed in reverse order so the first binding is
     the outermost wrapper.
4. If no `@around` bindings, calls the original method directly with `await`.
5. On success: sets `jp.return_value`, runs all `@after_returning` bindings.
6. On exception: sets `jp.exception`, runs all `@after_throwing` bindings,
   then re-raises.
7. In `finally`: runs all `@after` bindings (always executed).

### Sync Methods

For sync methods, the wrapper follows the same pattern as async methods and
supports all five advice types, including `@around`. The `@before`,
`@after_returning`, `@after_throwing`, and `@after` advice work identically.
For `@around` on sync methods, `jp.proceed` is a plain synchronous callable
(not async) — call it as `result = jp.proceed()` without `await`.

### The Advice Chain

When multiple advice bindings match a method, they execute in this order:

```
@before (all, in aspect_order)
    |
    v
@around chain (outermost first, each calls proceed())
    |
    v
  [original method]
    |
    v  (on success)
@after_returning (all, in aspect_order)
    |
    v  (on exception)
@after_throwing (all, in aspect_order)
    |
    v  (always)
@after (all, in aspect_order)
```

---

## Auto-Configuration

The `AopAutoConfiguration` class automatically registers an `AspectBeanPostProcessor` bean in the DI container. This is the only unconditional auto-configuration in PyFly — AOP support is always active because `@aspect` classes rely on the post-processor to discover and weave advice at startup.

### AopAutoConfiguration

**Conditions:** None (always active).

| Bean | Type | Description |
|------|------|-------------|
| `aspect_post_processor` | `AspectBeanPostProcessor` | Discovers `@aspect` beans and weaves advice into matching target beans |

### How It Works

During context startup, the `AspectBeanPostProcessor` (now auto-registered as a container bean):

1. Scans all beans for the `@aspect` decorator
2. Registers aspect beans with the `AspectRegistry`
3. For each non-aspect bean, checks if any pointcut expression matches
4. Wraps matching methods with the advice chain via `weave_bean()`

### Deduplication

If you manually register an `AspectBeanPostProcessor` (e.g., in tests or custom configurations), the framework detects the duplicate at the type level and ensures only one instance processes beans. This prevents double-weaving.

**Source:** `src/pyfly/aop/auto_configuration.py`

---

## Ordering Aspects

When multiple aspects provide advice for the same method, the order matters.
Use the `@order` decorator from `pyfly.container.ordering` to control
execution priority:

```python
from pyfly.aop import aspect
from pyfly.container.ordering import order, HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE

@aspect
@order(-100)  # Runs before aspects with higher order values
class SecurityAspect:
    @before("service.*.*")
    def check_auth(self, jp: JoinPoint):
        ...

@aspect
@order(0)  # Default order
class LoggingAspect:
    @before("service.*.*")
    def log_call(self, jp: JoinPoint):
        ...

@aspect
@order(100)  # Runs after aspects with lower order values
class MetricsAspect:
    @after("service.*.*")
    def record_metrics(self, jp: JoinPoint):
        ...
```

**Ordering rules:**

| Constant | Value | Description |
|---|---|---|
| `HIGHEST_PRECEDENCE` | `-(2^31)` | Runs first |
| Default (no `@order`) | `0` | Normal priority |
| `LOWEST_PRECEDENCE` | `2^31 - 1` | Runs last |

Lower numeric values = higher priority (runs earlier). The `AspectRegistry`
keeps all bindings sorted by `aspect_order` after each registration.

---

## Complete Examples

### Logging Aspect

A comprehensive logging aspect that logs method entry, exit, and exceptions:

```python
import logging
from pyfly.aop import aspect, before, after_returning, after_throwing, JoinPoint
from pyfly.container.ordering import order

logger = logging.getLogger("audit")


@aspect
@order(-50)
class LoggingAspect:
    @before("service.*.*")
    def log_entry(self, jp: JoinPoint):
        logger.info(
            "Entering %s.%s with args=%s, kwargs=%s",
            type(jp.target).__name__,
            jp.method_name,
            jp.args,
            jp.kwargs,
        )

    @after_returning("service.*.*")
    def log_return(self, jp: JoinPoint):
        logger.info(
            "Exiting %s.%s with result=%s",
            type(jp.target).__name__,
            jp.method_name,
            jp.return_value,
        )

    @after_throwing("service.*.*")
    def log_exception(self, jp: JoinPoint):
        logger.error(
            "Exception in %s.%s: %s",
            type(jp.target).__name__,
            jp.method_name,
            jp.exception,
            exc_info=jp.exception,
        )
```

### Performance Monitoring Aspect

An `@around` aspect that measures execution time and reports slow methods:

```python
import time
from pyfly.aop import aspect, around, JoinPoint
from pyfly.container.ordering import order


@aspect
@order(50)
class PerformanceAspect:
    SLOW_THRESHOLD = 1.0  # seconds

    @around("service.*.*")
    async def measure_execution_time(self, jp: JoinPoint):
        start = time.perf_counter()
        try:
            result = await jp.proceed()
            return result
        finally:
            elapsed = time.perf_counter() - start
            method_fqn = f"{type(jp.target).__name__}.{jp.method_name}"

            if elapsed > self.SLOW_THRESHOLD:
                print(f"[SLOW] {method_fqn} took {elapsed:.3f}s")
            else:
                print(f"[PERF] {method_fqn} took {elapsed:.3f}s")
```

### Audit Trail Aspect

An aspect that records data mutations for compliance:

```python
from datetime import datetime, timezone
from pyfly.aop import aspect, after_returning, JoinPoint
from pyfly.container.ordering import order


@aspect
@order(100)
class AuditTrailAspect:
    def __init__(self, audit_repository=None):
        self.audit_repository = audit_repository

    @after_returning("service.*.create_*")
    def audit_create(self, jp: JoinPoint):
        self._record("CREATE", jp)

    @after_returning("service.*.update_*")
    def audit_update(self, jp: JoinPoint):
        self._record("UPDATE", jp)

    @after_returning("service.*.delete_*")
    def audit_delete(self, jp: JoinPoint):
        self._record("DELETE", jp)

    def _record(self, action: str, jp: JoinPoint):
        entry = {
            "action": action,
            "target_class": type(jp.target).__name__,
            "method": jp.method_name,
            "args": str(jp.args),
            "result": str(jp.return_value),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.audit_repository:
            self.audit_repository.save(entry)
        print(f"[AUDIT] {entry}")
```

### Putting It All Together

Here is how the aspects work with a target service bean, using both the
manual API and the automatic post-processor:

**Manual weaving (for understanding and testing):**

```python
from pyfly.aop import AspectRegistry, weave_bean


# 1. Define a service
class OrderService:
    async def create_order(self, item: str, quantity: int) -> dict:
        return {"id": "ord_123", "item": item, "quantity": quantity}

    async def get_order(self, order_id: str) -> dict:
        return {"id": order_id, "item": "widget", "quantity": 5}


# 2. Create aspects
logging_aspect = LoggingAspect()
perf_aspect = PerformanceAspect()
audit_aspect = AuditTrailAspect()

# 3. Register aspects
registry = AspectRegistry()
registry.register(logging_aspect)
registry.register(perf_aspect)
registry.register(audit_aspect)

# 4. Weave the service bean
order_service = OrderService()
weave_bean(order_service, "service.OrderService", registry)

# 5. Call the method -- all aspects fire automatically
result = await order_service.create_order("widget", 10)
# Output:
# Entering OrderService.create_order with args=('widget', 10), kwargs={}
# [PERF] OrderService.create_order took 0.001s
# Exiting OrderService.create_order with result={'id': 'ord_123', ...}
# [AUDIT] {'action': 'CREATE', 'target_class': 'OrderService', ...}
```

**Automatic weaving (production usage):**

In a real PyFly application, you simply define your aspects with `@aspect` and
your services with `@service`. The `AspectBeanPostProcessor` handles discovery,
registration, and weaving automatically during container startup:

```python
from pyfly.aop import aspect, before, around, after_returning, JoinPoint
from pyfly.container import service
from pyfly.container.ordering import order


@aspect
@order(-50)
class LoggingAspect:
    @before("service.*.*")
    def log_entry(self, jp: JoinPoint):
        print(f"-> {jp.method_name}({jp.args})")

    @after_returning("service.*.*")
    def log_exit(self, jp: JoinPoint):
        print(f"<- {jp.method_name} = {jp.return_value}")


@aspect
@order(0)
class TimingAspect:
    @around("service.*.*")
    async def time_it(self, jp: JoinPoint):
        import time
        start = time.perf_counter()
        result = await jp.proceed()
        elapsed = time.perf_counter() - start
        print(f"   [{jp.method_name}: {elapsed:.3f}s]")
        return result


@service
class OrderService:
    async def create_order(self, data: dict) -> dict:
        # This method is automatically wrapped by both aspects at startup.
        # No AOP-related code needed here.
        return {"id": "123", **data}
```

When `OrderService.create_order` is called, the output will be:

```
-> create_order(({'item': 'widget'},))
   [create_order: 0.001s]
<- create_order = {'id': '123', 'item': 'widget'}
```
