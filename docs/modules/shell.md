# Shell Guide

PyFly's shell module provides a Spring Shell-inspired CLI application framework
with full dependency injection integration. It follows the hexagonal architecture
pattern: a `ShellRunnerPort` protocol defines the contract for command execution,
while pluggable adapters (Click, and in the future Typer, etc.) supply the
implementation. You write CLI commands as methods on `@shell_component` classes,
and the framework infers CLI parameters from type hints, registers them with the
adapter, and wires everything through the DI container at startup.

The module also provides `CommandLineRunner` and `ApplicationRunner` protocols
for one-shot post-startup tasks — the Python equivalent of Spring Boot's runner
interfaces.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The @shell_component Stereotype](#the-shell_component-stereotype)
3. [The @shell_method Decorator](#the-shell_method-decorator)
4. [Parameter Inference](#parameter-inference)
   - [Inference Rules](#inference-rules)
   - [PEP 604 and Optional Handling](#pep-604-and-optional-handling)
   - [The MISSING Sentinel](#the-missing-sentinel)
5. [Explicit Overrides: @shell_option and @shell_argument](#explicit-overrides-shell_option-and-shell_argument)
   - [@shell_option](#shell_option)
   - [@shell_argument](#shell_argument)
   - [@shell_method_availability](#shell_method_availability)
   - [Override Merge Behavior](#override-merge-behavior)
6. [Data Models](#data-models)
   - [ShellParam](#shellparam)
   - [CommandResult](#commandresult)
7. [ShellRunnerPort Protocol](#shellrunnerport-protocol)
8. [Adapters](#adapters)
   - [ClickShellAdapter](#clickshelladapter)
9. [CommandLineRunner and ApplicationRunner](#commandlinerunner-and-applicationrunner)
   - [CommandLineRunner](#commandlinerunner)
   - [ApplicationRunner](#applicationrunner)
   - [Execution Order](#execution-order)
10. [ApplicationArguments](#applicationarguments)
11. [Auto-Configuration](#auto-configuration)
12. [How Wiring Works](#how-wiring-works)
    - [Step 1: Command Registration](#step-1-command-registration)
    - [Step 2: Runner Invocation](#step-2-runner-invocation)
13. [Configuration Reference](#configuration-reference)
14. [Complete Example: DevOps Toolkit CLI](#complete-example-devops-toolkit-cli)
15. [Testing](#testing)
    - [Unit Testing Commands Directly](#unit-testing-commands-directly)
    - [Testing Through the Adapter](#testing-through-the-adapter)
    - [Integration Testing with ApplicationContext](#integration-testing-with-applicationcontext)
16. [Spring Shell Comparison](#spring-shell-comparison)

---

## Architecture Overview

PyFly Shell is built on the same two concepts as every other PyFly module:

* **Port** — `ShellRunnerPort` is a `Protocol` that defines command registration,
  single-shot execution, and interactive REPL operations. Your application code
  depends only on this abstraction.
* **Adapter** — `ClickShellAdapter` implements the port using the
  [Click](https://click.palletsprojects.com) library. You can swap adapters
  without changing any command code.

```
┌──────────────────────────────────────────────────────────────┐
│                       YOUR APPLICATION                       │
│                                                              │
│  @shell_component                                            │
│  class DbCommands:                                           │
│      @shell_method(group="db")                               │
│      def migrate(self, target: str = "head") -> str: ...     │
│                                                              │
│  @service  # implements CommandLineRunner                    │
│  class Seeder:                                               │
│      async def run(self, args: list[str]) -> None: ...       │
│                                                              │
└──────────────────────────────────┬───────────────────────────┘
                                   │ depends on
┌──────────────────────────────────┴───────────────────────────┐
│              ShellRunnerPort  (Python Protocol)              │
│                                                              │
│  register_command(key, handler, *, help_text, group, params) │
│  run(args) -> int                                            │
│  run_interactive() -> None                                   │
│                                                              │
│              CommandLineRunner / ApplicationRunner           │
│  run(args: list[str]) -> None                                │
│  run(args: ApplicationArguments) -> None                     │
│                                                              │
└──────────────────────────────────┬───────────────────────────┘
                                   │ implements
┌──────────────────────────────────┴───────────────────────────┐
│              ClickShellAdapter  (Click 8.1+)                 │
│                                                              │
│  Converts ShellParam → click.Option / click.Argument         │
│  Wraps async handlers for synchronous Click dispatch         │
│  Supports grouped sub-commands (e.g. "db migrate")           │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**Wiring lifecycle:** During `ApplicationContext.start()`, the framework:

1. Evaluates auto-configuration conditions and registers `ClickShellAdapter` as
   the `ShellRunnerPort` bean (if `pyfly.shell.enabled=true`).
2. Scans all `@shell_component` beans for `@shell_method` methods.
3. Calls `infer_params()` on each method to build `ShellParam` descriptors from
   type hints, merging any explicit `@shell_option` / `@shell_argument` metadata.
4. Registers each command with the `ShellRunnerPort` adapter.
5. After publishing `ApplicationReadyEvent`, discovers and invokes any
   `CommandLineRunner` / `ApplicationRunner` beans.

---

## The @shell_component Stereotype

`@shell_component` marks a class as a DI-managed shell component. It behaves
identically to `@component` (singleton scope, constructor injection, lifecycle
hooks) but signals that the class contains CLI command methods.

```python
from pyfly.shell import shell_component, shell_method

@shell_component
class GreetingCommands:
    def __init__(self, greeting_service: GreetingService) -> None:
        self._service = greeting_service

    @shell_method(help="Say hello to someone")
    def greet(self, name: str) -> str:
        return self._service.greet(name)
```

The decorator sets the following metadata on the class:

| Attribute | Value |
|-----------|-------|
| `__pyfly_stereotype__` | `"shell_component"` |
| `__pyfly_scope__` | `Scope.SINGLETON` |
| `__pyfly_injectable__` | `True` |

Shell components have full access to all DI features: constructor injection,
`Autowired()` fields, `@post_construct` / `@pre_destroy` lifecycle hooks,
`@order` for prioritization, and `Optional[T]` / `list[T]` injection.

---

## The @shell_method Decorator

`@shell_method` marks a method as a CLI command. The `ApplicationContext` scans
`@shell_component` beans at startup and registers every `@shell_method` with the
`ShellRunnerPort` adapter.

```python
@shell_method(key="say-hello", help="Greet a user", group="greetings")
def say_hello(self, name: str, loud: bool = False) -> str:
    msg = f"Hello, {name}!"
    return msg.upper() if loud else msg
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `key` | `str` | `""` | The command name in the CLI. Empty means the method name with underscores replaced by hyphens (e.g. `say_hello` → `say-hello`). |
| `help` | `str` | `""` | Help text displayed in the shell's `--help` output. |
| `group` | `str` | `""` | Logical command group. Commands in a group are registered as sub-commands (e.g. `db migrate`, `db rollback`). |

### Metadata Attributes

The decorator sets the following attributes on the wrapped function:

| Attribute | Value |
|-----------|-------|
| `__pyfly_shell_method__` | `True` |
| `__pyfly_shell_key__` | The command name (kebab-case) |
| `__pyfly_shell_help__` | The help text string |
| `__pyfly_shell_group__` | The group name string |

During startup, `ApplicationContext._wire_shell_commands()` scans for methods
carrying `__pyfly_shell_method__ = True` and registers them with the adapter.

### Async Commands

Both sync and async methods are supported. Async handlers are automatically
wrapped for the adapter:

```python
@shell_method(help="Fetch remote data")
async def fetch(self, url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return f"Status: {resp.status_code}"
```

---

## Parameter Inference

PyFly infers CLI parameters from a method's type hints and defaults using
`infer_params()`. This eliminates the need for manual Click/argparse boilerplate
in most cases.

### Inference Rules

| Signature Pattern | CLI Mapping | is_option | is_flag | Default |
|-------------------|-------------|-----------|---------|---------|
| `name: str` | Positional argument | `False` | `False` | `MISSING` |
| `count: int` | Positional argument | `False` | `False` | `MISSING` |
| `count: int = 3` | `--count` option | `True` | `False` | `3` |
| `verbose: bool = False` | `--verbose` flag | `True` | `True` | `False` |
| `query: str \| None = None` | `--query` option | `True` | `False` | `None` |
| `query: Optional[str] = None` | `--query` option | `True` | `False` | `None` |

**The general rules are:**

1. **No default + non-bool type** → positional argument.
2. **Has a default** → named `--option`.
3. **`bool` type** → always a `--flag` (defaults to `False` if no default given).
4. **Optional / union with `None`** → named `--option` with `None` default.

### PEP 604 and Optional Handling

`infer_params()` uses `_unwrap_optional()` internally to handle both legacy and
modern union syntax:

- `typing.Optional[str]` → unwraps to `str`, marks as optional
- `typing.Union[str, None]` → unwraps to `str`, marks as optional
- `str | None` (PEP 604, Python 3.10+) → unwraps to `str`, marks as optional
- `str | int` (non-optional union) → left as-is, not unwrapped

### The MISSING Sentinel

`MISSING` is a module-level sentinel that distinguishes "no default was
provided" from an explicit `None` default. This is necessary because `None` is a
valid default value for optional parameters.

```python
from pyfly.shell.result import MISSING

# MISSING is a singleton instance of _MissingSentinel
repr(MISSING)  # "MISSING"

# Usage in ShellParam
param = ShellParam(name="name", param_type=str, is_option=False)
param.default is MISSING  # True — no default provided
```

### Skipping `self` and `return`

The `self` parameter and the return type annotation are automatically excluded
from inference. Only user-facing parameters are converted to `ShellParam`
descriptors.

---

## Explicit Overrides: @shell_option and @shell_argument

While parameter inference handles most cases, you can attach explicit metadata
when you need help text, custom defaults, or different parameter kinds.

### @shell_option

Attach option metadata to a shell command method:

```python
@shell_method()
@shell_option("--verbose", is_flag=True, help="Enable verbose output")
@shell_option("--env", help="Target environment", default="staging")
def deploy(verbose: bool = False, env: str = "staging") -> str:
    return f"Deploying to {env}"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Option name (e.g. `--verbose`). Leading dashes are stripped and hyphens converted to underscores to match the function parameter name. |
| `type` | `type \| None` | `None` | Expected value type. `None` means infer from the function signature. |
| `is_flag` | `bool` | `False` | If `True`, the option is a boolean flag (no value expected). |
| `help` | `str` | `""` | Help text for this option. |
| `default` | `Any` | `None` | Default value when the option is not supplied. |
| `choices` | `list[str] \| None` | `None` | Restrict accepted values to a fixed set. Flows through to the resolved `ShellParam.choices`. |

The decorator stores metadata in a list at `func.__pyfly_shell_options__`.

Both `type` and `choices` are honored by `infer_params()`: an explicit `type=`
overrides the type inferred from the signature, and `choices=` is carried onto
the resulting `ShellParam`.

### @shell_argument

Attach positional argument metadata to a shell command method:

```python
@shell_method()
@shell_argument("service", help="Service to deploy")
def deploy(service: str) -> str:
    return f"Deploying {service}"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | The argument name (must match a function parameter). |
| `type` | `type \| None` | `None` | Expected value type. `None` means infer from the function signature. |
| `help` | `str` | `""` | Help text for this argument. |
| `required` | `bool` | `True` | Whether the argument must be supplied. |
| `default` | `Any` | `None` | Default value when the argument is not supplied. |

The decorator stores metadata in a list at `func.__pyfly_shell_arguments__`.

### @shell_method_availability

Link a shell command to a conditional availability checker:

```python
from pyfly.shell import shell_component, shell_method, shell_method_availability

@shell_component
class AdminCommands:
    def __init__(self, security: SecurityContext) -> None:
        self._security = security

    def admin_available(self) -> str:
        """Return '' if available, or a reason string if not."""
        if self._security.has_role("ADMIN"):
            return ""
        return "Requires ADMIN role"

    @shell_method(key="reset-db", help="Reset the database")
    @shell_method_availability("admin_available")
    async def reset_db(self) -> str:
        return "Database reset!"
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `checker` | `str` | Name of a method on the same `@shell_component` that returns `""` (available) or a non-empty reason string (unavailable). |

When unavailable, the command is hidden from help output and blocked from
execution. The checker name is stored at `func.__pyfly_shell_availability__`.

### Override Merge Behavior

When `infer_params()` processes a method, it:

1. Builds a `{param_name: metadata}` lookup from `@shell_option` entries
   (normalising `--kebab-case` to `snake_case`).
2. Builds a separate lookup from `@shell_argument` entries.
3. For each function parameter, checks for an explicit override first.
4. If found, the override's `help`, `default`, `is_flag`, and `choices` values
   take precedence over the inferred defaults. An explicit `type=` on the
   override is honored too: `param_type` becomes the override's `type` when set,
   otherwise it falls back to the type inferred from the function signature. This
   applies to both `@shell_option` and `@shell_argument` (note that
   `@shell_argument` carries no `is_flag` / `choices`, so only its `help`,
   `default`, and `type` flow onto the resolved `ShellParam`).
5. If no override exists, the standard inference rules apply.

This means you only need `@shell_option` / `@shell_argument` when the inference
defaults are insufficient — typically to add help text, override the value type,
or constrain choices.

---

## Data Models

### ShellParam

`ShellParam` is a frozen dataclass that describes a single parameter for a shell
command. It is the intermediate representation between your Python type hints and
the adapter's native parameter types (e.g. `click.Option`, `click.Argument`).

```python
from pyfly.shell import ShellParam
from pyfly.shell.result import MISSING

param = ShellParam(
    name="count",
    param_type=int,
    is_option=True,
    default=3,
    help_text="Number of retries",
)
```

#### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | *required* | The parameter name (matches the Python function parameter). |
| `param_type` | `type` | *required* | The Python type (`str`, `int`, `float`, `bool`). |
| `is_option` | `bool` | *required* | `True` for named `--options`, `False` for positional arguments. |
| `default` | `Any` | `MISSING` | The default value. `MISSING` means the parameter is required. |
| `help_text` | `str` | `""` | Help text displayed in `--help` output. |
| `choices` | `list[str] \| None` | `None` | Restrict accepted values to a fixed set. |
| `is_flag` | `bool` | `False` | If `True`, the option is a boolean flag (no value expected). |

Because the dataclass is frozen, `ShellParam` instances are immutable and safe
to use as dict keys or in sets.

### CommandResult

`CommandResult` is a mutable dataclass that wraps the output and exit code from
a command invocation:

```python
from pyfly.shell import CommandResult

result = CommandResult(output="Migrated to head", exit_code=0)
result.is_success  # True

failed = CommandResult(output="Connection refused", exit_code=1)
failed.is_success  # False
```

#### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `output` | `str` | `""` | The text output produced by the command. |
| `exit_code` | `int` | `0` | The exit code. `0` means success; non-zero means failure. |

#### Properties

| Property | Return Type | Description |
|----------|-------------|-------------|
| `is_success` | `bool` | Returns `True` when `exit_code == 0`. |

---

## ShellRunnerPort Protocol

The port is defined as a `@runtime_checkable` `Protocol`, so you can use
`isinstance()` checks at runtime and depend on it for type hints everywhere.

```python
from pyfly.shell import ShellRunnerPort

class ShellRunnerPort(Protocol):
    def register_command(
        self,
        key: str,
        handler: Callable[..., Any],
        *,
        help_text: str = "",
        group: str = "",
        params: list[ShellParam] | None = None,
    ) -> None: ...

    async def run(self, args: list[str] | None = None) -> int: ...

    async def run_interactive(self) -> None: ...
```

### Method Reference

| Method | Return Type | Description |
|--------|-------------|-------------|
| `register_command(key, handler, *, help_text, group, params)` | `None` | Register a command. `key` is the command name (kebab-case). `handler` is the callable. `group` nests the command under a sub-group (e.g. `group="db"` → `db <key>`). `params` is a list of `ShellParam` descriptors for the command's CLI parameters. |
| `run(args)` | `int` | Execute the shell with the given argument list and return the exit code. Pass `None` or `[]` for no arguments. |
| `run_interactive()` | `None` | Start an interactive REPL loop. Reads input lines, tokenises them, and dispatches to the appropriate command. Exits on `EOF` or `Ctrl+C`. |

---

## Adapters

### ClickShellAdapter

The default adapter, backed by [Click](https://click.palletsprojects.com) 8.1+.
It translates `ShellParam` descriptors into native Click parameters and manages
a `click.Group` as the root command.

**Install:** `uv add "pyfly[shell]"` (this pulls in `click`).

#### Constructor

```python
from pyfly.shell.adapters.click_adapter import ClickShellAdapter

adapter = ClickShellAdapter(name="myapp", help_text="My CLI application")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"app"` | The root command name. |
| `help_text` | `str` | `""` | Help text for the root command. |

#### Type Mapping

Python types are mapped to Click parameter types:

| Python Type | Click Type |
|-------------|------------|
| `str` | `click.STRING` |
| `int` | `click.INT` |
| `float` | `click.FLOAT` |
| `bool` | `click.BOOL` |

Unknown types fall back to `click.STRING`.

#### ShellParam → Click Parameter Conversion

Each `ShellParam` is converted by `_build_click_param()`:

| ShellParam State | Click Type | Behavior |
|------------------|------------|----------|
| `is_flag=True` | `click.Option` with `is_flag=True` | `--verbose` toggles a boolean |
| `is_option=True` | `click.Option` | `--count 5` with type validation |
| `is_option=False` | `click.Argument` | Positional, required unless default provided |

Option names are auto-generated from the parameter name: underscores become
hyphens (e.g. `max_retries` → `--max-retries`).

#### Async Handler Wrapping

Click is a synchronous library, so async command handlers need special treatment.
`_wrap_handler()` wraps async coroutine functions:

- **No running event loop** — Uses `asyncio.run()` to create a new loop.
- **Running event loop** (e.g. inside `pytest-asyncio`) — Dispatches to a
  `ThreadPoolExecutor` to avoid blocking the existing loop.

Sync handlers are passed through unchanged.

#### Grouped Sub-Commands

Commands with a `group` parameter are nested under a `click.Group`:

```python
adapter.register_command("migrate", handler, group="db")
adapter.register_command("rollback", handler, group="db")

# CLI usage:
#   myapp db migrate
#   myapp db rollback
```

Multiple commands can share the same group. The sub-group is created lazily on
first use and added to the root `click.Group`.

#### Invocation Modes

| Method | Return Type | Description |
|--------|-------------|-------------|
| `invoke(args)` | `tuple[int, str]` | Synchronous invocation. Returns `(exit_code, output)`. Catches `SystemExit` (from `--help`), `UsageError`, and general exceptions. |
| `run(args)` | `int` | Async wrapper around `invoke()`. Returns the exit code only. |
| `run_interactive()` | `None` | REPL loop: `input("> ")` → split → `invoke()` → print output. Exits on `EOF` or `Ctrl+C`. |

---

## CommandLineRunner and ApplicationRunner

These protocols provide **post-startup hooks** — beans that implement them are
invoked automatically after the `ApplicationContext` has fully started and
`ApplicationReadyEvent` has been published. This mirrors Spring Boot's
`CommandLineRunner` and `ApplicationRunner` interfaces.

### CommandLineRunner

Receives raw CLI arguments as `list[str]`:

```python
from pyfly.container import service

@service
class DatabaseSeeder:
    """Seed the database after startup if --seed is passed."""

    def __init__(self, repo: ItemRepository) -> None:
        self._repo = repo

    async def run(self, args: list[str]) -> None:
        if "--seed" in args:
            await self._repo.save(Item(name="Default Item"))
```

#### Protocol Definition

```python
@runtime_checkable
class CommandLineRunner(Protocol):
    async def run(self, args: list[str]) -> None: ...
```

Any bean with an `async def run(self, args: list[str]) -> None` method
structurally satisfies this protocol.

### ApplicationRunner

Receives parsed `ApplicationArguments` for richer CLI argument introspection:

```python
from pyfly.container import service
from pyfly.shell import ApplicationArguments

@service
class ConfigPrinter:
    """Print configuration summary if --show-config is passed."""

    async def run(self, args: ApplicationArguments) -> None:
        if args.contains_option("show-config"):
            print(f"Config sources: {args.non_option_args}")
```

#### Protocol Definition

```python
@runtime_checkable
class ApplicationRunner(Protocol):
    async def run(self, args: ApplicationArguments) -> None: ...
```

The `ApplicationContext` determines which protocol a bean satisfies by inspecting
the type hint of the first parameter in `run()`. If it is `ApplicationArguments`,
the raw CLI args are parsed via `ApplicationArguments.from_args()` before
invocation.

### Execution Order

Runners are invoked in `@order` priority (lowest value first, default is 0). If
multiple runners have the same order, their execution order is unspecified.

**Important:** `ShellRunnerPort` adapters are explicitly excluded from runner
detection. Because both `ShellRunnerPort` and `CommandLineRunner` define
`async def run(...)`, Python's structural typing would otherwise cause the
adapter to falsely match `CommandLineRunner`. The framework uses an
`isinstance(instance, ShellRunnerPort)` guard to prevent this.

---

## ApplicationArguments

`ApplicationArguments` is a dataclass that provides a parsed view of raw CLI
tokens, separating option arguments (`--key=value`, `--flag`) from non-option
arguments (everything else).

```python
from pyfly.shell import ApplicationArguments

args = ApplicationArguments.from_args(["serve", "--port=8080", "--debug", "extra"])

args.source_args      # ["serve", "--port=8080", "--debug", "extra"]
args.option_args      # ["--port=8080", "--debug"]
args.non_option_args  # ["serve", "extra"]
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source_args` | `list[str]` | `[]` | The original, unmodified argument list. |
| `option_args` | `list[str]` | `[]` | Arguments starting with `--`. |
| `non_option_args` | `list[str]` | `[]` | Arguments not starting with `--`. |

### Class Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `from_args(args)` | `ApplicationArguments` | Parse a raw `list[str]` into option and non-option groups. |

### Instance Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `contains_option(name)` | `bool` | Check if `--name` or `--name=value` is present in `option_args`. |
| `get_option_values(name)` | `list[str]` | Extract all values for `--name=value` options. Returns an empty list if the option is not present or has no `=value` suffix. |

---

## Auto-Configuration

Shell auto-configuration is activated when two conditions are met:

1. `pyfly.shell.enabled` is set to `true` in configuration.
2. No user-provided `ShellRunnerPort` bean exists in the container.

```yaml
# pyfly.yaml
pyfly:
  shell:
    enabled: true
```

The `ShellAutoConfiguration` class is discovered via the `pyfly.auto_configuration`
entry-point group in `pyproject.toml`:

```toml
[project.entry-points."pyfly.auto_configuration"]
shell = "pyfly.shell.auto_configuration:ShellAutoConfiguration"
```

### Conditions and Beans

| Condition | Effect |
|-----------|--------|
| `@conditional_on_property("pyfly.shell.enabled", having_value="true")` | Only activates when shell is explicitly enabled. |
| `@conditional_on_missing_bean(ShellRunnerPort)` | Skipped if the user has already registered a `ShellRunnerPort` bean. |

| Bean | Type | Adapter |
|------|------|---------|
| `shell_runner` | `ShellRunnerPort` | `ClickShellAdapter()` |

### Overriding the Default Adapter

Register your own `ShellRunnerPort` bean and the auto-configuration is silently
skipped:

```python
from pyfly.container import configuration, bean
from pyfly.shell import ShellRunnerPort

@configuration
class MyShellConfig:
    @bean
    def shell_runner(self) -> ShellRunnerPort:
        return MyCustomShellAdapter()
```

---

## How Wiring Works

### Step 1: Command Registration

After auto-configuration, `ApplicationContext._wire_shell_commands()` runs during
step 6 of the startup sequence (after `@post_construct`, scheduled tasks, and
async method wiring):

1. Iterates all registered beans looking for `__pyfly_stereotype__ == "shell_component"`.
2. For each shell component, iterates public attributes looking for
   `__pyfly_shell_method__ == True`.
3. On the first match, lazily resolves `ShellRunnerPort` from the container. If
   no `ShellRunnerPort` is registered, wiring is skipped silently.
4. Calls `infer_params(method)` to build `ShellParam` descriptors.
5. Reads `__pyfly_shell_key__`, `__pyfly_shell_help__`, and
   `__pyfly_shell_group__` from the method.
6. Calls `runner.register_command(key, method, help_text=..., group=..., params=...)`.
7. Logs the total count of wired commands.

### Step 2: Runner Invocation

After `ApplicationReadyEvent` is published, `ApplicationContext._invoke_runners()`
runs:

1. Scans all registered beans for `CommandLineRunner` or `ApplicationRunner`
   conformance (via `isinstance()` with `@runtime_checkable` protocols).
2. Excludes `ShellRunnerPort` instances to prevent structural typing false
   matches.
3. Sorts runners by `@order` priority (lowest first).
4. For each runner, inspects the type hint of `run()`'s first parameter:
   - If it is `ApplicationArguments` → calls `runner.run(ApplicationArguments.from_args(sys.argv[1:]))`.
   - Otherwise → calls `runner.run(sys.argv[1:])`.
5. Awaits the result if the method is a coroutine.

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.shell.enabled` | `bool` | `false` | Enable the shell subsystem and auto-configuration. When `false`, no `ShellRunnerPort` is registered and `@shell_method` methods are not wired. |

---

## Complete Example: DevOps Toolkit CLI

A realistic multi-command CLI application with DI-wired services, grouped
commands, async handlers, and a startup runner.

```python
# app.py
from pyfly.core import pyfly_application

@pyfly_application(scan_packages=["devops_toolkit"])
class DevOpsApp:
    pass
```

```yaml
# pyfly.yaml
pyfly:
  shell:
    enabled: true
  data:
    relational:
      enabled: true
      url: sqlite+aiosqlite:///devops.db
```

```python
# services/deployment_service.py
from pyfly.container import service

@service
class DeploymentService:
    def __init__(self, repo: DeploymentRepository) -> None:
        self._repo = repo

    async def deploy(self, service_name: str, env: str, force: bool) -> str:
        deployment = Deployment(service=service_name, env=env, forced=force)
        await self._repo.save(deployment)
        return f"Deployed {service_name} to {env}" + (" (forced)" if force else "")

    async def rollback(self, service_name: str, steps: int) -> str:
        history = await self._repo.find_recent(service_name, limit=steps)
        for entry in reversed(history):
            await self._repo.mark_rolled_back(entry.id)
        return f"Rolled back {len(history)} deployment(s) for {service_name}"

    async def list_deployments(self, env: str | None, limit: int) -> list[str]:
        deployments = await self._repo.find_by_env(env, limit=limit)
        return [f"{d.service} → {d.env} ({d.created_at})" for d in deployments]
```

```python
# commands/deploy_commands.py
from pyfly.shell import shell_component, shell_method, shell_option, shell_argument

@shell_component
class DeployCommands:
    def __init__(self, service: DeploymentService) -> None:
        self._service = service

    @shell_method(group="deploy", help="Deploy a service to an environment")
    @shell_argument("service_name", help="Name of the service to deploy")
    @shell_option("--env", help="Target environment")
    @shell_option("--force", is_flag=True, help="Force deployment even if checks fail")
    async def push(self, service_name: str, env: str = "staging", force: bool = False) -> str:
        return await self._service.deploy(service_name, env, force)

    @shell_method(group="deploy", help="Rollback recent deployments")
    @shell_argument("service_name", help="Name of the service to rollback")
    async def rollback(self, service_name: str, steps: int = 1) -> str:
        return await self._service.rollback(service_name, steps)

    @shell_method(group="deploy", help="List recent deployments")
    @shell_option("--env", help="Filter by environment")
    async def ls(self, env: str | None = None, limit: int = 10) -> str:
        items = await self._service.list_deployments(env, limit)
        return "\n".join(items) if items else "No deployments found"
```

```python
# runners/health_checker.py
from pyfly.container import service, order
from pyfly.shell import ApplicationArguments

@service
@order(10)
class HealthChecker:
    """Check service health after startup if --health-check is passed."""

    async def run(self, args: ApplicationArguments) -> None:
        if args.contains_option("health-check"):
            # Perform connectivity checks...
            print("All health checks passed")
```

### CLI Usage

```bash
# Deploy a service
python -m devops_toolkit deploy push order-service --env production --force

# Rollback
python -m devops_toolkit deploy rollback order-service --steps 2

# List deployments for staging
python -m devops_toolkit deploy ls --env staging --limit 5

# Run with startup health check
python -m devops_toolkit --health-check

# Interactive REPL
python -m devops_toolkit --interactive
> deploy push payment-service --env staging
Deployed payment-service to staging
> deploy ls --env staging
payment-service → staging (2026-02-17 14:30:00)
> ^C
```

---

## Testing

### Unit Testing Commands Directly

Shell commands are ordinary methods on DI-managed classes. The simplest way to
test them is to instantiate the class with mock dependencies:

```python
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_deploy_command():
    mock_service = AsyncMock(spec=DeploymentService)
    mock_service.deploy.return_value = "Deployed api to staging"

    commands = DeployCommands(service=mock_service)
    result = await commands.push("api", env="staging", force=False)

    assert result == "Deployed api to staging"
    mock_service.deploy.assert_called_once_with("api", "staging", False)
```

### Testing Through the Adapter

Test the full CLI parameter parsing by registering commands with a
`ClickShellAdapter`:

```python
from pyfly.shell import ShellParam
from pyfly.shell.adapters.click_adapter import ClickShellAdapter
from pyfly.shell.param_inference import infer_params


def test_deploy_via_adapter():
    adapter = ClickShellAdapter()
    captured = {}

    def deploy(service_name: str, env: str = "staging", force: bool = False) -> None:
        captured.update(service_name=service_name, env=env, force=force)

    adapter.register_command(
        "deploy",
        deploy,
        params=infer_params(deploy),
    )

    exit_code, _ = adapter.invoke(["deploy", "api", "--env", "production", "--force"])
    assert exit_code == 0
    assert captured == {"service_name": "api", "env": "production", "force": True}


def test_deploy_defaults():
    adapter = ClickShellAdapter()
    captured = {}

    def deploy(service_name: str, env: str = "staging", force: bool = False) -> None:
        captured.update(service_name=service_name, env=env, force=force)

    adapter.register_command("deploy", deploy, params=infer_params(deploy))

    exit_code, _ = adapter.invoke(["deploy", "api"])
    assert exit_code == 0
    assert captured == {"service_name": "api", "env": "staging", "force": False}
```

### Integration Testing with ApplicationContext

Test the full wiring pipeline — auto-configuration, command registration, and
runner invocation:

```python
import pytest
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.shell import ShellRunnerPort


@pytest.mark.asyncio
async def test_shell_auto_configuration():
    config = Config({"pyfly": {"shell": {"enabled": True}}})
    ctx = ApplicationContext(config)
    await ctx.start()
    try:
        runner = ctx.get_bean(ShellRunnerPort)
        assert runner is not None
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_shell_disabled_by_default():
    config = Config({"pyfly": {}})
    ctx = ApplicationContext(config)
    await ctx.start()
    try:
        with pytest.raises(KeyError):
            ctx.get_bean(ShellRunnerPort)
    finally:
        await ctx.stop()
```

---

## Spring Shell Comparison

| Spring Shell (Java) | PyFly Shell (Python) |
|---------------------|---------------------|
| `@ShellComponent` | `@shell_component` |
| `@ShellMethod` | `@shell_method` |
| `@ShellOption` | `@shell_option` |
| `@ShellMethodAvailability` | `@shell_method_availability(checker)` |
| `CommandLineRunner` | `CommandLineRunner` protocol |
| `ApplicationRunner` | `ApplicationRunner` protocol |
| `ApplicationArguments` | `ApplicationArguments` dataclass |
| JLine (terminal) | Click (CLI framework) |
| `spring.shell.*` config | `pyfly.shell.*` config |

---

## Adapters

- [Click Adapter](../adapters/click.md) — Setup, configuration reference, and adapter-specific features for the Click CLI backend
