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
