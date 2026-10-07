<span class="eyebrow">Apéndice F</span>

# Feature Flags en Lumen {.chtitle}

La API de monederos de Lumen puede ofrecer una novedad sin alterar las reglas de saldo ni del libro mayor. La ruta opcional `/api/v1/wallets/rollout/offer` muestra una compuerta en tiempo de ejecución: la oferta existente permanece disponible durante el despliegue de la nueva. El código y las pruebas de comportamiento están en `samples/lumen`. La [guía de Feature Flags](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags.md) contiene la referencia de API y configuración; el [contrato Firefly](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags-contract.md) define el formato compartido con LaraFly.

## F.1. Empezar con el interruptor apagado

Instala el PyFly local con el extra `feature-flags` y ejecuta las pruebas:

```bash
cd samples/lumen
uv sync --extra dev
uv run pytest tests/test_feature_flags.py -q
uv run pyfly run --server uvicorn
```

El `pyfly.yaml` del ejemplo activa el subsistema y establece `wallet-offer: false`. La abreviatura booleana se admite en configuración y en sustituciones de pruebas; un archivo, una fuente HTTP o el almacén exige la definición completa. `FeatureFlags` es una fachada inyectable cuando el subsistema está activo. Al desactivarlo no se registra ese bean; una compuerta cerrada sin alternativa lanza `FeatureFlagDisabledException`.

La ruta utiliza `@feature_flag("wallet-offer", fallback="legacy_offer")`. El decorador conserva el mapeo y la firma. Con el valor inicial devuelve `{"offer":"standard"}`; al activar el flag devuelve `{"offer":"new"}`. La alternativa es un método del mismo controlador y no modifica el agregado del monedero. La prueba ejecuta la compuerta real y restaura el proveedor previo al salir de cada `override_flags`. Puedes consultar `GET /api/v1/wallets/rollout/offer`.

Sin alternativa, una ruta cerrada responde con `pyfly.feature-flags.web.disabled-status` (404 por defecto; también 403 o 503). `default=True` abre deliberadamente una compuerta si el flag falta, está desactivado o falla. El valor por defecto del llamador también rige cuando una compuerta por variante no obtiene ninguna variante: `DISABLED` no obliga siempre a devolver `false`.

## F.2. Definir y dirigir un flag

Una definición transportable declara `state`, `variants` del mismo tipo, `defaultVariant` y, opcionalmente, `targeting` en JSON Logic. El estado es `ENABLED` o `DISABLED`. Una variante ausente, un tipo incompatible o un error de evaluación devuelve el valor tipado por defecto del llamador; `details()` revela el motivo y el código de error. `get_string`, `get_int`, `get_float`, `get_object` e `is_enabled` fijan el tipo; `variant()` devuelve el nombre de la variante. Cada método de evaluación tiene una versión asíncrona.

```yaml
wallet-experiment:
  state: ENABLED
  variants: {control: standard, treatment: new}
  defaultVariant: control
  targeting: {fractional: [[control, 50], [treatment, 50]]}
  metadata: {kind: experiment, owner: wallet, expires: "2026-12-31"}
```

`fractional-v2` calcula el grupo con la clave del flag y un `targetingKey` estable; el mismo usuario permanece en el mismo grupo en PyFly y LaraFly. Una petición anónima sin clave estable recibe la variante predeterminada. La prueba del ejemplo lee **el mismo fixture de conformidad** que ambos frameworks y comprueba el valor y la variante de Alice mediante el proveedor OpenFeature real. Para un permiso, utiliza atributos fiables como `tenant` o `plan` en JSON Logic. Un evaluador compartido `$evaluators` se referencia mediante `{"$ref": "name"}`; una referencia inexistente o cíclica produce `PARSE_ERROR` solo en el flag afectado. El contrato recoge la validación y los límites de expansión.

El contexto ambiental de PyFly incluye el ID del usuario autenticado, los roles sin `ROLE_`, la aplicación y los perfiles. El inquilino procede del atributo del principal; la cabecera `X-Tenant-Id` solo se usa al activar `context.trust-tenant-header` tras un límite de confianza. `context=` prevalece sobre los atributos ambientales y `targeting_key=` sobre la clave del contexto. Las claves de texto o enteros decimales se normalizan a texto; un valor inválido conserva la clave de menor precedencia. Un bean `EvaluationContextContributor` puede aportar, por ejemplo, el plan. Sus métodos deben ser baratos e idempotentes: se ejecutan al preparar la petición y otra vez en cada llamada a la fachada.

## F.3. Elegir una fuente y cambiarla con seguridad

La precedencia, de menor a mayor, es configuración, archivo observado, HTTP consultado periódicamente, almacén consultado periódicamente y sustituciones de pruebas. Una capa superior sustituye la definición completa del flag; los nombres de evaluadores y metadatos del documento se fusionan. Reemplaza los archivos de forma atómica: el observador detecta mtime y tamaño. Si falla la actualización se conserva el último documento correcto y la fuente pasa a `STALE`; si nunca se cargó aparece `DOWN`. La configuración o el archivo inválidos impiden el arranque; un fallo de HTTP o almacén mantiene la última composición válida. `FeatureFlagsChanged` anuncia los cambios efectivos.

Utiliza el almacén de base de datos para operaciones compartidas. Una escritura actualiza la fila del flag y añade una fila de auditoría en una misma transacción; el ID de cambio es la revisión de consulta. `expectedVersion` rechaza escrituras obsoletas con `conflict`. El proceso escritor actualiza tras el commit y los demás en su siguiente intervalo. Dentro de una transacción externa, la actualización y `FeatureFlagUpdated` esperan al **commit exterior**; un rollback los descarta. Algunos conflictos nativos abortan esa transacción: repite toda la operación del llamador con una vista nueva. El Capítulo 5 explica las migraciones y el 18 el despliegue. El almacén de memoria es local al proceso.

Con almacén escribible y `management.writes: true` se permiten `put`, `delete`, `enable`, `disable` y `default-variant`. `FlagManagement.evaluate` es una vista previa con contexto explícito: ignora el principal del operador y no registra métricas ni exposición. Un `put` exitoso puede devolver `{key, refreshPending: true}` si todavía no es visible por una transacción externa o una actualización pendiente; consulta GET hasta verlo. El recibo por sí solo no prueba durabilidad. El panel `/admin#flags`, las rutas `GET/POST /actuator/flags/{key}` y `pyfly flags` comparten el servicio. Una clave ausente en GET devuelve 404/`unknown-flag`; un POST mal formado devuelve 400/`bad-request`. Un cliente HTTP no puede elegir el canal fiable de auditoría `admin`. La seguridad de administración es independiente de la seguridad de la aplicación; configúrala antes de exponer escrituras. Consulta el Capítulo 15 y los comandos ejecutables del Apéndice D.

## F.4. Compartir el documento y medir el despliegue

Un servicio puede activar el endpoint de sincronización compatible con flagd en `server.path` (por defecto `/feature-flags/flagd.json`). Utiliza un token bearer; `server.allow-anonymous` es false por defecto y una ruta vacía o raíz no es válida. La fuente HTTP remota envía `If-None-Match`; un 304 mantiene el documento cacheado. Configura intervalos positivos y un timeout. Si la aplicación aporta su propio proveedor OpenFeature, la fachada y la compuerta permanecen, pero la composición, el almacén y el endpoint de Firefly se desactivan.

Cada evaluación que no sea vista previa incrementa `feature_flag_evaluations_total{flag,variant,reason}`. `events.evaluations: true` activa los eventos de exposición `FeatureFlagEvaluated`. La vista previa los suprime. El presupuesto inspecciona como máximo 10.000 apariciones de valores; si se excede, se omite la exposición y continúan la evaluación y la métrica. `metadata.expires` registra deuda en salud y administración, pero no desactiva el flag. Consulta el Capítulo 15 para telemetría, el 16 para sustituciones de pruebas y el 14 para identidad fiable.

## F.5. Verificar el despliegue de Lumen

Esta prueba procede literalmente de `samples/lumen/tests/test_feature_flags.py`. Ejecuta el controlador y el proveedor reales y comprueba la restauración al terminar cada sustitución:

::: listing samples/lumen/tests/test_feature_flags.py | Listado F.1 — Definiciones apagada, encendida y ausente
@pytest.mark.asyncio
async def test_wallet_offer_route_method_falls_back_and_activates() -> None:
    controller = WalletController(None, None)  # this route does not dispatch commands or queries
    with override_flags({"wallet-offer": False}):
        assert await controller.wallet_offer() == {"offer": "standard"}
    with override_flags({"wallet-offer": True}):
        assert await controller.wallet_offer() == {"offer": "new"}
    with override_flags({}):
        assert await controller.wallet_offer() == {"offer": "standard"}
:::

El mismo archivo de pruebas lee `firefly-vectors.json` para comprobar un experimento compartido, ejecuta la ruta con ASGI, usa un almacén SQLite real para commit/conflicto/rollback y comprueba que la vista previa no registra exposición mientras que una evaluación normal sí. La suite `tests/feature_flags` del framework añade la matriz de bases de datos y estados HTTP. El ejemplo separa las pruebas del monedero de su despliegue opcional. Salir de `override_flags` restaura el proveedor y el registro anteriores. Una vista previa no es un registro de exposición.

## F.6. Configuración y diagnóstico

`pyfly.feature-flags.enabled` vale false por defecto. `sources.file`, `sources.http` y `sources.store` tienen `enabled` y un `refresh-interval` positivo; HTTP añade URL, token y timeout, y el almacén añade driver y datasource. `context.tenant-attribute` vale `tenant`, `context.trust-tenant-header` false, `web.disabled-status` 404, y `management.writes` y `events.evaluations` false. `server` incluye enabled, path, token y `allow-anonymous`; `openfeature.domain` aísla aplicaciones dentro de un proceso. La [referencia de configuración](https://github.com/fireflyframework/fireflyframework-pyfly/blob/v26.10.01/docs/modules/feature-flags.md#configuration-reference) enumera las claves exactas.

Si todos reciben la variante por defecto, comprueba el targeting key estable y el estado del flag. Si un cambio parece perdido, revisa el estado y la precedencia de las fuentes antes de reescribirlo. Un `conflict` requiere una versión nueva; un aborto transaccional nativo exige repetir toda la operación. Un flag caducado sigue evaluándose: elimínalo o desactívalo de forma explícita. En `pyfly.yaml`, pon entre comillas las claves de variante `"on"` y `"off"` para evitar sorpresas de YAML 1.1; cita las claves y fechas ambiguas en archivos transportables.
