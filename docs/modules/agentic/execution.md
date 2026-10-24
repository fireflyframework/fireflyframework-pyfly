# Secure Script Execution

Use these capability snippets inside your application’s [native runtime](native-runtime.md).
Installation is through [PyFly Agentic extras](getting-started/installation.md).

Copyright 2026 Firefly Software Foundation. Licensed under the Apache License 2.0.

`pyfly.agentic.execution` runs **untrusted or model-generated Python**
in a sandbox, on *your* infrastructure. It is built around a single
`ExecutionEnvironment` abstraction with tiered, deny-by-default backends, and a
`generate → validate → execute → capture` loop that reuses PyFly Agentic's existing
guard and audit machinery.

> **Why not just use the provider's code tool?** pydantic-ai's built-in code
> execution runs server-side in the *model provider's* sandbox — you get zero
> control over what executes on your own infrastructure. Use it for "let the
> model run throwaway analysis", but for self-hosted, audited execution use this
> subsystem. (Historical note: `mcp-run-python` was archived by its maintainers
> as unsafe for untrusted code, and `RestrictedPython` is explicitly *not* a
> sandbox — PyFly Agentic uses neither as a boundary.)

The Monty backend requires the extra:

```bash
python -m pip install -e ".[agentic,agentic-script-execution]"
```

---

## Quick start

```python
from pyfly.agentic.execution import MontyEnvironment, SecureScriptRunner

runner = SecureScriptRunner(MontyEnvironment())

result = await runner.execute("a = 2\nb = 3\na * b")
assert result.success and result.output == 6

# Dangerous code is rejected by the static pre-screen — the sandbox never runs it:
bad = await runner.execute("import os\nos.system('rm -rf /')")
assert bad.success is False
assert bad.error_type == "CodeSafetyError"
```

`ExecutionResult` carries `success`, `output` (the script's final expression),
`stdout`/`stderr`, and on failure `error` / `error_type` / `violations`.

---

## The execution environment

```python
class ExecutionEnvironment(Protocol):
    @property
    def capabilities(self) -> frozenset[Capability]: ...
    async def run_code(self, code, *, inputs=None,
                       external_functions=None, limits=None) -> ExecutionResult: ...
```

Environments are **deny-by-default**: the only host access guest code gets is
the set of `external_functions` you hand to that specific call.

### `MontyEnvironment` (tiers 0–1, default)

[Monty](https://pypi.org/project/pydantic-monty/) 0.0.23 executes a Python
*subset* in a local Rust worker. PyFly grants no guest filesystem, network or
environment capabilities and mounts no host directories. Only explicitly
registered external functions can access host services. Third-party Python
packages are unavailable inside the guest.

Each `run_code` owns an `AsyncMonty(max_processes=1, min_processes=0)` pool and a
fresh session. Completion, errors and cancellation close the session and worker
pool; variables and registered functions do not persist into the next run.
Worker startup is part of execution cost; do not assume in-process or
microsecond startup. The SDK locates the binary installed by
`agentic-script-execution` (`pydantic-monty-runtime`), honoring its `MONTY_BIN`
override. Keep the runtime dependency installed on a supported platform.

| Tier | What | Use |
|---|---|---|
| 0 | Monty, no external functions | Pure computation. |
| 1 | Monty + allow-listed `external_functions` | PyFly Agentic **Code Mode** — scripts call your tools. |

> Higher tiers — full-CPython Docker (gVisor/Kata) and cloud micro-VMs
> (E2B/Modal) for third-party-package, network, or GPU workloads — slot behind
> the same `ExecutionEnvironment` interface as later additions.

---

## Resource limits

```python
from pyfly.agentic.execution import ExecutionLimits

await env.run_code(code, limits=ExecutionLimits(
    timeout_seconds=5.0,        # wall-clock cap — the reliable runaway-loop guard
    max_memory_bytes=64 * 1024 * 1024,
    max_recursion_depth=500,
))
```

`MontyEnvironment` applies a **30-second wall-clock default**; per-call limits
are merged over it field-by-field. The parent deadline includes worker/session
setup, guest execution and host-tool waits. Monty's native VM-duration control
also receives the time budget. Memory and recursion controls apply to the guest,
not to allocations or recursion inside host callbacks.

Monty 0.0.23 removed allocation-count limits. The retained compatibility field
`ExecutionLimits.max_allocations` must be `None`; any other value raises
`ValueError` before workers start. Replace old allocation-count budgets with
memory and time limits.

Async callbacks run on the application's owner loop. Sync callbacks run in a
worker thread so they cannot block that loop. Host tools must apply their own
I/O deadlines, authorization and idempotency. Cancellation closes the sandbox
but cannot undo a completed effect or forcibly stop a running Python thread.

---

## Static safety pre-screen

`analyze_code` is an AST check applied *before* code reaches the interpreter. It
is **defense in depth, not the boundary** — the sandbox is the boundary — but it
gives fast, legible rejections and a second layer of protection.

```python
from pyfly.agentic.execution import analyze_code, SafetyPolicy

report = analyze_code("().__class__.__bases__[0].__subclasses__()")
assert not report.safe  # dunder-walk escape attempt

# Tighten or relax the policy:
policy = SafetyPolicy(allowed_modules=frozenset({"math", "statistics"}))
```

It rejects dangerous imports (`os`, `subprocess`, `socket`, …), dynamic-execution
builtins (`eval`, `exec`, `__import__`, `open`, …), and dunder attribute walks.

---

## Code Mode: tools as external functions

Instead of one tool call per step, let the model emit a *script* that
orchestrates several tools at once — executed in the sandbox with those tools
registered as external functions. This cuts model round-trips while keeping every
host call audited and capability-gated.

```python
from pyfly.agentic.execution import toolkit_external_functions

funcs = toolkit_external_functions(my_toolkit)          # {tool_name: callable}
result = await runner.execute(
    "totals = fetch_orders(customer_id)\nsummarize(totals)",
    inputs={"customer_id": 42},
    external_functions=funcs,
)
```

Positional arguments in the script are mapped onto each tool's declared parameter
names; async tools are awaited automatically on the caller's event loop. The
guest writes `lookup_policy(wallet_id)`, even when the host function is async;
no guest `await` is required. Host-tool exceptions return as failed execution
results, and cleanup still closes the run's session and worker pool.

`toolkit_external_functions` supports explicit `ParameterSpec` metadata and
signatures inferred from `@agent_tool` handlers. Positional and keyword arguments
retain the handler's binding rules and defaults. Each bridged call goes through
`tool.execute`, preserving tool guards and call listeners; the bridge does not
invoke the decorated handler directly.

### Run a deterministic mixed-tool example

Save this as `monty_demo.py` and run `python monty_demo.py` in an environment
with `pyfly[agentic,agentic-script-execution]` installed. It uses no model,
credentials, files or network services:

```python
import asyncio

from pyfly.agentic.execution import (
    ExecutionLimits, MontyEnvironment, SecureScriptRunner,
)

async def lookup_policy(wallet_id: int) -> dict:
    await asyncio.sleep(0)
    return {"wallet_id": wallet_id, "review": "human"}

def summarize(policy: dict) -> str:
    return f"Wallet {policy['wallet_id']}: {policy['review']} review"

async def main() -> None:
    runner = SecureScriptRunner(MontyEnvironment())
    limits = ExecutionLimits(
        timeout_seconds=5.0,
        max_memory_bytes=64 * 1024 * 1024,
        max_recursion_depth=100,
    )
    pure = await runner.execute("sum([2, 3, 5])", limits=limits)
    assert pure.success and pure.output == 10
    result = await runner.execute(
        "policy = lookup_policy(wallet_id)\nsummarize(policy)",
        inputs={"wallet_id": 42},
        external_functions={
            "lookup_policy": lookup_policy,
            "summarize": summarize,
        },
        limits=limits,
    )
    assert result.success, result.error
    assert result.output == "Wallet 42: human review"
    print(result.output)

asyncio.run(main())
```

The first execution grants computation only. The second grants exactly the two
functions in its mapping. They cannot authorize a wallet transfer: real tool
implementations must enforce the application's identity, tenant and domain
rules. Each execution starts a fresh session and releases its worker; the
`MontyEnvironment` object does not retain guest variables between these calls.

---

## Redacting captured output

Captured `stdout` can be scrubbed before results re-enter a model's context —
wire the security layer's `OutputGuard` here:

```python
from pyfly.agentic.security import default_output_guard

runner = SecureScriptRunner(
    MontyEnvironment(),
    output_scrubber=lambda s: default_output_guard.scan(s).sanitised_output or s,
)
```

---

## API reference

| Symbol | Purpose |
|---|---|
| `ExecutionEnvironment` | Protocol over sandbox backends. |
| `MontyEnvironment` | Default deny-by-default Monty backend (tiers 0–1). |
| `SecureScriptRunner` | validate → execute → capture orchestrator. |
| `analyze_code` / `SafetyPolicy` / `SafetyReport` / `CodeViolation` | Static AST pre-screen. |
| `ExecutionLimits` | Per-run resource ceilings. |
| `ExecutionResult` | Outcome (`success`, `output`, `stdout`, `stderr`, `error`, `violations`). |
| `Capability` | Capabilities an environment grants (`COMPUTE`, `EXTERNAL_FUNCTIONS`, …). |
| `toolkit_external_functions` | Build a Code Mode external-function map from tools. |

Exceptions live in `pyfly.agentic.exceptions`: `ExecutionError` (base),
`CodeSafetyError`, `SandboxUnavailableError`.

## Monty interpreter compatibility

The native `agentic-script-execution` extra pins **`pydantic-monty==0.0.23`**
and installs its `pydantic-monty-runtime` binary dependency. The adapter uses an
owned worker pool and a fresh session per run; it closes both after success,
failure or cancellation. Guest scripts keep direct calls to registered sync and
async tools without guest-side `await`. Binary discovery follows the SDK's
`MONTY_BIN`/installed-binary locator. A missing binary fails explicitly; no
CPython or remote-service fallback is enabled.

Monty 0.0.23 removed `max_allocations`. Remove any non-`None` value from
`ExecutionLimits`; PyFly raises `ValueError` before starting workers rather than
silently ignoring it. Use wall-clock, memory and recursion budgets. The
[execution guide](execution.md) describes callback scheduling and lifecycle.
