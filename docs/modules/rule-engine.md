# Rule Engine

`pyfly.rule_engine` is a YAML-based business-rules engine: parse rules into an AST,
evaluate them in priority order against a context dict, and execute typed actions.
The engine is pure Python — no JVM, no external server, no forward-chaining loop.

---

## Model

Every object is an immutable (frozen) dataclass.  Fields shown with their defaults.

### `Condition`

```python
@dataclass
class Condition:
    operator: str                  # leaf operator OR "and" / "or" / "not"
    field: str | None = None       # dot-notation path into ctx, e.g. "order.amount"
    value: Any = None              # comparand for leaf operators
    children: list[Condition] = [] # sub-conditions for compound operators
```

### `Action`

```python
@dataclass
class Action:
    type: str                      # "set" | "increment" | "log" | "call" | "calculate"
    target: str | None = None      # context path to write (required for set/increment)
    value: Any = None              # value to write or log message
    expression: str | None = None  # optional expression string (for custom handlers)
    arguments: dict[str, Any] = {} # extra key/value arguments for custom handlers
```

### `Rule`

```python
@dataclass
class Rule:
    id: str                        # unique within a RuleSet
    description: str = ""
    when: Condition | None = None  # condition; None means "always match"
    then: list[Action] = []        # actions when condition is True
    otherwise: list[Action] = []   # actions when condition is False
    priority: int = 0              # higher priority = evaluated first
    enabled: bool = True           # disabled rules are skipped entirely
```

### `RuleSet`

```python
@dataclass
class RuleSet:
    id: str
    name: str = ""
    version: int = 1
    rules: list[Rule] = []

    def sorted_rules(self) -> list[Rule]: ...  # descending priority order
```

---

## Operator reference

### Leaf operators (None-safe)

All leaf operators are **None-safe**: if the field is absent or evaluates to `None`
the result is `False` (not an exception) unless the operator is specifically
designed to test for absence (`exists`, `is_null`, `is_empty`).

| Operator | Semantics |
|---|---|
| `eq` | `actual == value` |
| `ne` | `actual != value` |
| `gt` | `actual > value`; `None` → `False` |
| `ge` | `actual >= value`; `None` → `False` |
| `lt` | `actual < value`; `None` → `False` |
| `le` | `actual <= value`; `None` → `False` |
| `in` | `actual in value` (value must be a list); `None` → `False` |
| `not_in` | `actual not in value`; `None` → `False` |
| `regex` | `re.search(value, str(actual))`; coerces both sides to str |
| `between` | `value[0] <= actual <= value[1]`; value must be `[lo, hi]`; `None` → `False` |
| `contains` | For strings: `value in actual`; for lists/collections: `value in actual`; `None` → `False` |
| `not_contains` | Inverse of `contains`; `None` → `False` |
| `starts_with` | `str(actual).startswith(str(value))`; `None` → `False` |
| `ends_with` | `str(actual).endswith(str(value))`; `None` → `False` |
| `exists` | `True` if field is present **and** not `None`; `value` is ignored |
| `is_null` | `True` if field is absent **or** `None`; `value` is ignored |
| `is_empty` | `True` if `None`, `""`, `[]`, or `{}`; `value` is ignored |

### Compound operators

| Operator | Semantics |
|---|---|
| `and` | All `children` conditions must be `True` (short-circuits) |
| `or` | At least one `children` condition must be `True` (short-circuits) |
| `not` | Negates **exactly one** child; providing 0 or 2+ children raises `ValueError` |

In YAML compound conditions use `conditions:` (or `children:`) instead of `field:`/`value:`:

```yaml
op: and
conditions:
  - { op: ge, field: order.amount, value: 1000 }
  - { op: in, field: order.region, value: ["US", "CA"] }
```

---

## Loading

### `RuleSetLoader`

```python
from pyfly.rule_engine import RuleSetLoader

rs = RuleSetLoader.from_yaml(yaml_text)   # parse a YAML string
rs = RuleSetLoader.from_json(json_text)   # parse a JSON string
rs = RuleSetLoader.from_dict(data_dict)   # parse a plain dict
```

**YAML example**

```yaml
id: order-processing
name: Order Processing Rules
version: 1
rules:
  - id: high-value
    description: Flag high-value orders
    priority: 20
    when:
      op: between
      field: order.amount
      value: [5000, 999999]
    then:
      - { type: set, target: flags.high_value, value: true }
      - { type: increment, target: score, value: 10 }
    otherwise:
      - { type: set, target: flags.high_value, value: false }

  - id: blocked-region
    priority: 15
    when:
      op: in
      field: order.region
      value: ["RU", "KP", "IR"]
    then:
      - { type: set, target: flags.blocked, value: true }
    otherwise:
      - { type: set, target: flags.blocked, value: false }

  - id: fraud-pattern
    priority: 10
    when:
      op: regex
      field: order.email
      value: ".*@temp.*\\..*"
    then:
      - { type: set, target: flags.fraud_suspected, value: true }
      - type: call
        target: fraud-audit
        arguments: { event: fraud_pattern_matched }
    otherwise:
      - { type: set, target: flags.fraud_suspected, value: false }
```

### Validation

Use `validate_ruleset` (function) or `RuleSetValidator` (class) before evaluating
untrusted or user-supplied rule documents.

```python
from pyfly.rule_engine import validate_ruleset
from pyfly.rule_engine.validation import RuleSetValidator, RuleValidationError

# returns a list of human-readable strings; empty = valid
issues = validate_ruleset(rs)

# OO interface
issues = RuleSetValidator.check(rs)

# raises RuleValidationError if any issues found
RuleSetValidator.assert_valid(rs)
```

`RuleValidationError` carries `.ruleset_id` and `.issues` (list of strings).

**What the validator catches**

- Duplicate rule `id` values within a `RuleSet`.
- Unknown leaf operator (not in the supported set).
- `and`/`or` compound with no children.
- `not` compound with a child count other than 1.
- `set` or `increment` action missing `target`.
- `between` condition whose `value` is not a 2-element sequence.
- Unknown `action.type` (not one of `set`, `increment`, `log`, `call`, `calculate`).

---

## Fluent builder

Build rule objects in Python without writing YAML.

```python
from pyfly.rule_engine.builder import (
    field, all_of, any_of, not_,
    set_action, increment_action, log_action,
    rule, ruleset,
)

# --- conditions ---
cond = field("order.amount").ge(1000)
cond = field("order.region").in_(["US", "CA"])
cond = field("order.email").regex(r".*@temp.*\..*")
cond = all_of(field("customer.tier").eq("gold"), field("order.total").ge(500))
cond = any_of(field("flags.vip").eq(True), field("order.total").ge(2000))
cond = not_(field("flags.blocked").eq(True))

# --- actions ---
a1 = set_action("flags.high_value", True)
a2 = increment_action("score", 10)  # 'by' defaults to 1
a3 = log_action("High-value order detected")

# --- single rule ---
my_rule = (
    rule("high-value")
    .describe("Flag high-value orders")
    .priority(20)
    .when(field("order.amount").between(5000, 999999))
    .then(set_action("flags.high_value", True), increment_action("score", 10))
    .otherwise(set_action("flags.high_value", False))
    .build()
)

# --- ruleset ---
my_ruleset = (
    ruleset("order-processing", name="Order Processing Rules", version=1)
    .add(my_rule)
    .build()
)
```

**`_FieldBuilder` operator methods** (all return a `Condition`):
`eq`, `ne`, `gt`, `ge`, `lt`, `le`, `in_`, `not_in`, `regex`, `between`,
`contains`, `not_contains`, `starts_with`, `ends_with`, `exists`, `is_null`, `is_empty`.

**`RuleBuilder` chain methods**: `describe(text)`, `priority(n)`, `enabled(flag)`,
`when(condition)`, `then(*actions)`, `otherwise(*actions)`, `build()` → `Rule`.

**`RuleSetBuilder` chain methods**: `add(*rules)`, `build()` → `RuleSet`.

---

## Evaluation

### `RuleEvaluator` — single-rule evaluator

```python
from pyfly.rule_engine import RuleEvaluator

# default: only set / increment / log action types supported
evaluator = RuleEvaluator()

# with custom handler(s) merged on top of built-ins
evaluator = RuleEvaluator(action_handlers={"call": my_handler})
```

`RuleEvaluator.evaluate(rule, ctx)` → `EvaluationResult`

### `EvaluationResult`

```python
@dataclass
class EvaluationResult:
    rule_id: str
    matched: bool
    actions_executed: list[Action] = []  # successfully executed actions
    error: str | None = None             # semicolon-joined errors from isolated failures
```

### `EvaluationMode`

```python
from pyfly.rule_engine import EvaluationMode

EvaluationMode.ALL          # evaluate every enabled rule; default
EvaluationMode.FIRST_MATCH  # stop after the first rule whose condition matched
```

### `RuleSetEvaluator` — whole-ruleset evaluator

```python
from pyfly.rule_engine import RuleSetEvaluator, EvaluationMode

evaluator = RuleSetEvaluator(
    rule_evaluator=RuleEvaluator(),      # defaults to vanilla RuleEvaluator
    mode=EvaluationMode.ALL,             # default
)

results: list[EvaluationResult] = evaluator.evaluate(ruleset, ctx)
```

**`ALL` mode** — every enabled rule is evaluated in descending `priority` order.
All matching rules execute their actions against the **shared** `ctx` dict, so
later rules (lower priority) observe mutations made by earlier ones.

**`FIRST_MATCH` mode** — rules are evaluated in descending priority order and
evaluation **stops immediately** after the first rule whose condition is `True`.
The returned list contains every rule evaluated *up to and including* the first
match; rules with lower priority are never evaluated and their actions never fire.
Shared-context semantics are identical for the subset of rules that *are* evaluated.

**Action isolation** — within a single rule, each action is executed in its own
`try/except`.  If an action raises (e.g. an unregistered type), the error is
recorded in `EvaluationResult.error` and sibling actions still execute.

---

## Action handlers

### Built-in handlers

| Type | Behaviour |
|---|---|
| `set` | Write `action.value` to the dot-notation `action.target` in `ctx` |
| `increment` | Add `action.value` (default 1) to the numeric value at `action.target` |
| `log` | `logging.info("rule action: %s", action.value or action.target)` |

### Custom handlers via `ActionHandler` protocol

```python
from pyfly.rule_engine.ports.outbound import ActionHandler
from pyfly.rule_engine.dsl import Action
from typing import Any

class AuditHandler:
    def handle(self, action: Action, ctx: dict[str, Any]) -> None:
        # action.target, action.value, action.expression, action.arguments available
        record_audit_event(action.arguments.get("event"), ctx.get("order_id"))

# register at RuleEvaluator construction time
evaluator = RuleEvaluator(action_handlers={"call": AuditHandler().handle})
```

Any callable `(action: Action, ctx: dict[str, Any]) -> None` satisfies the
`ActionHandler` protocol — a method reference or a plain function both work.

Custom handlers are **additive**: built-in `set`/`increment`/`log` remain
available unless you explicitly override them with the same key.

Any action type *not* present in the final handler registry raises
`NotImplementedError` at evaluation time (the error is isolated to that action
by the action-isolation guarantee).

The `call` and `calculate` action types appear in the YAML DSL and pass validation
(`validate_ruleset` accepts them) but are **not** in the default handler registry
by design — they are extension points that application code wires up via custom
handlers.

---

## Service and port

### `RuleEnginePort` (protocol)

```python
from pyfly.rule_engine.ports.outbound import RuleEnginePort

class RuleEnginePort(Protocol):
    def evaluate(self, ruleset: RuleSet, ctx: dict[str, Any]) -> list[EvaluationResult]: ...
```

Application code should depend on `RuleEnginePort` rather than
`RuleEngineService` directly to keep the dependency injectable.

### `RuleSetRepository` (protocol) + `InMemoryRuleSetRepository`

```python
from pyfly.rule_engine import RuleSetRepository, InMemoryRuleSetRepository

class RuleSetRepository(Protocol):
    async def save(self, ruleset: RuleSet) -> None: ...
    async def get(self, ruleset_id: str) -> RuleSet | None: ...
    async def list(self) -> list[RuleSet]: ...
    async def delete(self, ruleset_id: str) -> bool: ...
```

`InMemoryRuleSetRepository` is the default adapter — backed by an in-memory
dict with an `asyncio.Lock`, suitable for tests and single-process deployments.

### `RuleEngineService`

```python
from pyfly.rule_engine import RuleEngineService, RuleSetNotFoundError

svc = RuleEngineService(
    repository=InMemoryRuleSetRepository(),
    evaluator=RuleSetEvaluator(),   # optional; defaults to ALL mode
    metrics=recorder,               # optional MetricsRecorder
)

# synchronous — satisfies RuleEnginePort
results = svc.evaluate(ruleset, ctx)

# async — load by id from repo then evaluate
results = await svc.evaluate_by_name("order-processing", ctx)  # raises RuleSetNotFoundError if absent

# async repository passthrough
await svc.save_ruleset(rs)
rs = await svc.get_ruleset("order-processing")   # None if not found
rulesets = await svc.list_rulesets()
```

`RuleSetNotFoundError` (a `KeyError` subclass) is raised by `evaluate_by_name`
when the repository returns `None` for the given ID.

---

## Metrics

When a `MetricsRecorder` is passed to `RuleEngineService`, four counters are
created on construction and incremented after every `evaluate` /
`evaluate_by_name` call.  All counters carry a `ruleset` label set to the
evaluated `RuleSet.id`.

| Counter | Incremented |
|---|---|
| `pyfly_rule_evaluations_total` | Once per `evaluate` / `evaluate_by_name` call |
| `pyfly_rules_matched_total` | For each `EvaluationResult` where `matched is True` |
| `pyfly_rule_actions_fired_total` | By the number of successfully-executed actions across all results |
| `pyfly_rule_errors_total` | For each `EvaluationResult` with a non-`None` `error` field |

Omitting the `metrics` argument (or passing `None`) disables all instrumentation
with no other effect on behaviour.

---

## Auto-configuration

When `pyfly.rule-engine.enabled=true` the `RuleEngineAutoConfiguration` bean
registers `InMemoryRuleSetRepository`, `RuleEvaluator`, `RuleSetEvaluator`, and
`RuleEngineService` in the application container.

| Property | Values | Default |
|---|---|---|
| `pyfly.rule-engine.enabled` | `true` / `false` | (disabled) |
| `pyfly.rule-engine.mode` | `all` / `first-match` | `all` |

Example `application.yaml`:

```yaml
pyfly:
  rule-engine:
    enabled: true
    mode: first-match
```

---

## Out of scope / by design

- **Stateful forward-chaining** is not implemented.  Each `evaluate` call is a
  single pass over the sorted rule list; the engine does not re-evaluate rules
  after actions mutate the context.  This is intentional — the simpler semantics
  are sufficient for the majority of business-rule use cases and avoid the
  complexity (and non-termination risk) of a Rete-style engine.
- **`call` and `calculate` action types** are defined in the DSL and pass
  validation, but they are **not handled by default**.  They are explicit
  extension points: wire them up by injecting a custom `ActionHandler` via
  `RuleEvaluator(action_handlers={"call": ...})`.
