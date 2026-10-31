# Authorization policies

`pyfly.security` provides a pure authorization policy evaluator for applications that need
permission constraints, human approval requirements and execution ceilings. It requires only
PyFly's base dependencies and can be registered as an application-owned native DI bean.

## Evaluate policy layers

```python
from pyfly.security import (
    PolicyContext, PolicyEvaluator, PolicyLimits, PolicyOutcome, PolicySpec,
)

policy = PolicySpec.model_validate({
    "schema_version": 1,
    "allowed_principals": ["operator:alice"],
    "allowed_actions": ["run", "deploy"],
    "allowed_target_kinds": ["architecture"],
    "approval_required_actions": ["deploy"],
    "min_approvals": 2,
    "approval_ttl_seconds": 900,
    "limits": {"max_requests": 10, "budget_microusd": 1000000},
})
context = PolicyContext(
    principal_id="operator:alice",  # resolved by the trusted host
    action="deploy",
    target_kind="architecture",
    limits=PolicyLimits(max_requests=5),
)
decision = PolicyEvaluator().evaluate([policy], context)
assert decision.outcome == PolicyOutcome.APPROVAL_REQUIRED
assert decision.min_approvals == 2
assert decision.approval_ttl_seconds == 900
assert decision.effective_limits.max_requests == 5
```

Supply every required policy layer. `None` allow sets are unrestricted; an empty allow set
denies every value. Selectors match exact, case-sensitive strings. Every layer must permit
the trusted principal, action and target kind. A single denial overrides every approval or
allow result. An empty policy sequence returns `deny` with `policy_missing`; invalid policy
or context values raise Pydantic `ValidationError` and must block host admission.

The evaluator is order-independent and does not execute expressions, regular expressions,
callbacks or rule actions. String identifiers contain no whitespace or unprintable characters.
Principal identifiers are at most 512 characters; action and target-kind identifiers are at
most 128 characters. Each selector set contains at most 256 entries.

## Public API

All value models are frozen Pydantic models with unknown fields forbidden. Their nested
collections are immutable. They support `model_validate`, `model_validate_json`,
`model_dump(mode="json")`, `model_dump_json`, and `model_json_schema`. Numeric constraints
accept integers only: booleans, numeric strings, floats, negative numbers and excess values
are rejected. The only supported `schema_version` is the integer `1`.

| Type | Fields or method |
| --- | --- |
| `PolicySpec` | `schema_version=1`, `allowed_principals=None`, `allowed_actions=None`, `allowed_target_kinds=None`, `approval_required_actions=frozenset()`, `min_approvals=1`, `approval_ttl_seconds=3600`, `limits=PolicyLimits()` |
| `PolicyContext` | Required `principal_id`, `action`, `target_kind`; `limits=PolicyLimits()` supplies the host's existing ceilings |
| `PolicyLimits` | Optional `max_run_seconds`, `max_requests`, `max_tokens`, `budget_microusd`, all defaulting to `None` |
| `PolicyDecision` | `outcome`, `reason_codes`, `effective_limits`, `min_approvals`, `approval_ttl_seconds` |
| `PolicyOutcome` | `ALLOW="allow"`, `DENY="deny"`, `APPROVAL_REQUIRED="approval_required"` |
| `PolicyEvaluator` | `evaluate(policies: Sequence[PolicySpec], context: PolicyContext) -> PolicyDecision` |

Reason codes are stable values: `policy_missing`, `principal_not_allowed`, `action_not_allowed`,
`target_kind_not_allowed`, and `approval_required`. A simple allow has no reason codes. Denial
reasons are deduplicated in principal/action/target order. A denied or allowed decision has
`min_approvals=0` and `approval_ttl_seconds=None`.

### Execution ceilings

| Limit | Inclusive range |
| --- | --- |
| `max_run_seconds` | 0–86,400 |
| `max_requests` | 0–1,000,000 |
| `max_tokens` | 0–1,000,000,000 |
| `budget_microusd` | 0–1,000,000,000,000 |

For each measure, the effective ceiling is the minimum of the host baseline and all defined
policy limits. `None` adds no further ceiling. Zero remains zero and forbids consuming any
positive amount of that measure; hosts must not replace it using truthiness or a default.
An `allow` decision still carries ceilings the host must enforce before dispatching an effect.

For policies requiring approval for the exact requested action, the effective minimum reviewer
count is the maximum `min_approvals` (1–5), and approval validity is the minimum
`approval_ttl_seconds` (300–86,400). Policies with no approval requirement for that action do
not contribute approval constraints. Adding a layer cannot increase an existing ceiling,
reduce required approvals or lengthen their validity.

## Native dependency injection

```python
from pyfly.container import bean, configuration
from pyfly.security import PolicyEvaluator

@configuration
class GovernanceConfiguration:
    @bean
    def policy_evaluator(self) -> PolicyEvaluator:
        return PolicyEvaluator()
```

Inject `PolicyEvaluator` into the application service that owns admission. Separate application
contexts own their own beans. There is no process-global policy installation and no replacement
of the existing boolean `PermissionEvaluator` or method-security expression behavior.

## Host enforcement and durable evidence

The evaluator computes constraints. Applications authenticate actors, select current immutable
policy revisions and resource revisions, and persist decisions bound to the exact principal,
action, request, resource revisions, policy revisions and correlation ID. Resolve identities
from trusted authentication, such as `SecurityContextHolder`; never accept a caller's supplied
principal claims or an `approved` boolean as evidence.

An `approval_required` result is an unmet obligation. The host persists an approval request,
validates distinct authorized reviewers and separation-of-duties rules, enforces expiry and
revocation, and atomically consumes exact-request approval evidence. Reevaluate current
permissions and constraints on resume. Approval never overrides a denial, a stale resource
revision or a budget breach.

Enforce effective limits at actual run/deployment and external-effect admission boundaries.
Reserve cost and tokens atomically in durable host storage before dispatch, together with the
decision and approval consumption. Concurrent workers and restarts must share those records.
PyFly Data's application-owned units of work can own this transaction. The evaluator does not
persist approvals, reserve spend, call providers or reset counters. Agentic's in-memory
`BudgetGate` and post-call `UsageTracker` remain useful accounting tools; they do not establish
durable concurrent budget admission. Unknown prices remain unknown, and configured-rate
reservations must remain distinguishable from actual provider billing.
