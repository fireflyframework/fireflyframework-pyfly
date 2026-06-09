# Plugins

`pyfly.plugins` is a lightweight plugin / extension system inspired by
``org.fireflyframework.plugins``. Plugins declare extension points and
extensions; the manager loads them in dependency order.

## Defining a plugin

```python
from pyfly.plugins import plugin, extension, extension_point, PluginManager

@extension_point(id="formatters")
class _FormatterPoint: ...

@plugin(id="json-formatter", version="1.0.0")
class JsonFormatterPlugin:
    @extension(point="formatters", priority=10)
    class JsonFormatter:
        name = "json"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

## Driving lifecycle

```python
manager = PluginManager()
await manager.add(JsonFormatterPlugin)
await manager.start_all()
formatters = await manager.registry.get("formatters")
await manager.stop_all()
```

`PluginManager` rejects circular dependencies (`PluginResolutionError`)
and sorts extensions by priority within each extension point (highest
priority first).

`manager.add()` scans the plugin class for nested `@extension_point` classes
*before* scanning its `@extension` contributions, so the registry knows each
point's declared interface type.

## Extension-point registration & type validation

`ExtensionRegistry` tracks extension points alongside their contributions:

| Method | Description |
|--------|-------------|
| `await register_extension_point(point_id, point_type)` | Record a point id and the class that defines its interface. |
| `await has_extension_point(point_id)` | Whether a point id has been registered. |
| `await extension_point_ids()` | All registered extension-point ids. |
| `await register(point_id, instance, priority=0)` | Register an extension instance. |
| `await get(point_id)` | The registered extensions for a point, highest priority first. |

When an extension is contributed to a point whose interface type is known,
`register()` **type-checks** the instance against that declared
`@extension_point` class and raises `ValueError` if it is not an instance of
it (mirroring Java's `DefaultExtensionRegistry`). Extensions contributed to an
id with no registered point type are still accepted (lenient,
backward-compatible).

## Unloading plugins

Unloading a plugin unregisters the extensions it contributed, so they no longer
leak in the registry:

| Method | Description |
|--------|-------------|
| `await manager.remove(plugin_id)` | Run the plugin's `unload` hook, unregister its extensions, and forget it. Returns `False` if the plugin is unknown. |
| `await manager.unload_all()` | Remove every plugin in reverse dependency order. |

`start_all()` runs each plugin's `init` then `start` hooks in dependency order;
`stop_all()` runs `stop` then `unload` in reverse order.

---

## PluginState and PluginDescriptor

Every plugin tracked by `PluginManager` has a runtime `PluginDescriptor` that
records its current lifecycle state:

```python
from pyfly.plugins import PluginDescriptor, PluginState

desc: PluginDescriptor = await manager.get_plugin("my-plugin")
print(desc.state)              # PluginState.STARTED
print(desc.loaded_at)          # datetime of add()
print(desc.last_state_change)  # datetime of last transition
print(desc.failed_reason)      # str if FAILED, else None
```

`PluginState` values:

| Value | Meaning |
|-------|---------|
| `LOADED` | Plugin registered via `add()` but not yet started |
| `STARTED` | `start()` hook completed successfully |
| `STOPPED` | `stop()` hook completed successfully |
| `FAILED` | A lifecycle hook raised an exception |

---

## `@plugin(name=, author=)` — Optional Metadata

Two new optional fields are available on `@plugin`:

```python
@plugin(id="auth", version="1.0.0", name="Auth Plugin", author="security-team")
class AuthPlugin:
    ...
```

- `name` defaults to `id` when omitted (backward-compatible).
- `author` defaults to `""`.

Both fields are accessible on `Plugin` and `PluginDescriptor.plugin`.

---

## Per-Plugin Lifecycle: `start_plugin` / `stop_plugin` / `get_plugin`

Use these instead of `start_all` / `stop_all` when you want fine-grained control:

### `start_plugin(plugin_id)`

Starts the named plugin **and all its transitive dependencies**, in dependency
order. Already-STARTED plugins are skipped. On hook failure the plugin's state
is set to `FAILED` and a `PluginStartError` is raised.

```python
# Assuming C depends on B which depends on A:
await manager.start_plugin("c")
# → starts A, then B, then C
```

### `stop_plugin(plugin_id)`

Stops the named plugin **and all plugins that (transitively) depend on it**, in
reverse dependency order (dependents first). Already-STOPPED/LOADED plugins are
skipped. On hook failure the state is set to `FAILED` and a `PluginStopError`
is raised.

```python
await manager.stop_plugin("a")
# → stops C, then B, then A
```

### `get_plugin(plugin_id)` → `PluginDescriptor | None`

Returns the descriptor for the plugin, or `None` if no plugin with that id is
registered.

---

## PluginError Exception Hierarchy

All plugin exceptions extend `PyFlyException`:

```
PyFlyException
└── PluginException          # base for all plugin errors
    ├── PluginLoadError      # plugin could not be loaded/registered
    ├── PluginStartError     # start/init hook raised
    ├── PluginStopError      # stop/unload hook raised
    ├── PluginStateError     # invalid state transition or unknown plugin id
    └── PluginResolutionError  # missing dependency or cycle during topo-sort
```

Import from `pyfly.kernel.exceptions` (or `pyfly.kernel`):

```python
from pyfly.kernel.exceptions import PluginStartError, PluginStateError
```

---

## `ExtensionRegistry.get_extension`

`get()` returns a list of all extensions for a point. Use `get_extension()` when
you only expect (and want) the single highest-priority one:

```python
processor = await registry.get_extension("processors")
# raises ValueError if the point is unknown or has no extensions
```

---

## PluginsAutoConfiguration

The plugin system is auto-configured when `pyfly.plugins.enabled=true`:

```yaml
# application.yaml
pyfly:
  plugins:
    enabled: true
```

`PluginsAutoConfiguration` registers two beans:

| Bean type | Factory method |
|-----------|---------------|
| `ExtensionRegistry` | `extension_registry()` |
| `PluginManager` | `plugin_manager(registry)` |
