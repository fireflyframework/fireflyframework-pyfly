# Rule Engine

`pyfly.rule_engine` is a YAML-based business rules engine: parse rules
into an AST, evaluate them against a context dict, take actions.

## Defining rules in YAML

```yaml
id: order-rules
name: Order processing rules
rules:
  - id: high-value
    priority: 10
    when:
      op: ge
      field: order.amount
      value: 1000
    then:
      - { type: set, target: flags.high_value, value: true }
      - { type: log, value: "marking order as high-value" }
  - id: blocked-region
    when:
      op: in
      field: order.shipping_country
      value: ["XX", "YY"]
    then:
      - { type: set, target: flags.blocked, value: true }
```

## Evaluating

```python
from pyfly.rule_engine import RuleSetLoader, RuleSetEvaluator

ruleset = RuleSetLoader.from_yaml(yaml_text)
evaluator = RuleSetEvaluator()
ctx = {"order": {"amount": 5000, "shipping_country": "US"}, "flags": {}}
results = evaluator.evaluate(ruleset, ctx)
print(ctx["flags"])  # {"high_value": True}
```

## Operators

Comparison: `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `in`, `not_in`, `regex`.
Logical: `and`, `or`, `not` (with `conditions: [...]`).

Action types: `set` (write context path), `increment`, `log`. Subclass
`RuleEvaluator._execute_action` to support `call`, `calculate`, etc.
