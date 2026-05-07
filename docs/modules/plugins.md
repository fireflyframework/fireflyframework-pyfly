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
and sorts extensions by priority within each extension point.
