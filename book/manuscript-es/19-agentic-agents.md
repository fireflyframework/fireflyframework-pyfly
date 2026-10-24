<span class="eyebrow">Capítulo 19</span>

# Agentes y Herramientas en Lumen {.chtitle}

El equipo de soporte de Lumen necesita explicar la política de revisión de wallets. El asistente consulta un manual ficticio, devuelve una respuesta tipada y puede solicitar revisión humana. No decide si se mueve dinero. El agregado y los manejadores de comandos de los capítulos 6 y 7 conservan esa responsabilidad. Esta parte final añade las capacidades nativas de `pyfly.agentic` a la estructura conocida: la configuración crea servicios, el contenedor los inyecta y la aplicación administra su ciclo de vida.

El ejemplo completo está en `samples/agentic-book`. Utiliza el `TestModel` real de Pydantic AI para producir respuestas deterministas sin credenciales ni solicitudes a proveedores. Permite repetir las pruebas con Python 3.12 y 3.13; no demuestra la precisión, el coste o la latencia de un modelo de producción. Los datos, identificadores de política y efectos son ficticios.

Esta versión utiliza Pydantic AI 2.59.0 y adapta la ejecución opcional en
sandbox a Monty 0.0.23. La evaluación opcional conserva Ragas 0.2.6, el adaptador
compatible LangChain community 0.3 y datasets >=5.0.1. Resuelve el grafo completo
de la aplicación: versiones posteriores de Ragas y datasets tienen conflictos
de API o dependencias descritos en las
[notas de dependencias](https://fireflyframework.github.io/fireflyframework-pyfly/docs/dependency-updates/).

La versión de mantenimiento v26.10.11 conserva estas API y el mínimo de
SQLAlchemy 2.0.50. El lock del repositorio selecciona 2.1.6; cada aplicación
conserva y prueba su propio lock de dependencias. Las actualizaciones automáticas
del proyecto cambian el lockfile; modificar requisitos exige revisar compatibilidad
por separado.

## 19.1. Activar el módulo de forma explícita

Instala el núcleo opcional desde el wheel oficial de v26.10.11, seleccionando
`pyfly[agentic]`, como indica la
[guía de instalación](https://fireflyframework.github.io/fireflyframework-pyfly/docs/modules/agentic/getting-started/installation/).
PyFly se distribuye mediante GitHub Releases. Una instalación base no necesita
SDK de inteligencia artificial. Activa la integración administrada en el árbol efectivo de configuración:

```yaml
pyfly:
  agentic:
    enabled: true
    observability-enabled: false
    instrumentation-include-content: false
```

Mantén el módulo desactivado en perfiles que no lo utilicen. Si se activa sin sus dependencias opcionales, el arranque falla con una indicación de instalación. Los adaptadores de proveedores e infraestructura utilizan extras `agentic-*`; consulta la tabla del módulo antes de elegirlos. Los extras existentes mantienen su significado, por lo que un servicio puede combinar `web` con `agentic`.

El prefijo de configuración es `pyfly.agentic` y las variables empiezan por `PYFLY_AGENTIC_`, como `PYFLY_AGENTIC_DEFAULT_MODEL`. El enlace nativo utiliza la configuración efectiva, los perfiles y los placeholders del capítulo 3. El modelo tipado es `AgenticConfig`, de `pyfly.agentic.config`. Las opciones explícitas del agente prevalecen sobre los valores de la aplicación. Conserva los nombres de credenciales definidos por cada proveedor y evita incluir secretos en YAML, prompts o ejemplos.

El ejemplo pasa `TestModel` explícitamente. Un perfil real emplearía un identificador admitido por el proveedor y sus credenciales, elegidos según su documentación actual. Esa configuración necesita una validación independiente. Cambiar de modelo puede alterar esquemas, herramientas, aprobaciones, límites y fallos aunque la firma Python no cambie.

::: figure art/figures/19-agentic-runtime-es.svg | Figura 19.1 — Cada aplicación administra su asistente, registro y recursos

## 19.2. Construir una vez e inyectar el servicio

`AgenticRuntime` administra la configuración, los registros y la contabilidad de una aplicación. El `AgentFactory` inyectable crea agentes asociados a ese runtime y los registra por defecto. Devuelve desde un `@bean` singleton un agente creado por la fábrica o con el runtime de su aplicación explícito; el arranque rechaza agentes sin propietario o de otra aplicación. El servicio y el registro comparten así el mismo objeto. No retengas agentes de alcance de petición en un registro de larga duración.

::: listing samples/agentic-book/src/lumen_agentic/assistance.py | Listado 19.1 — Una respuesta tipada define el contrato del servicio
class AssistanceReply(BaseModel):
    summary: str
    policy_id: str
    needs_human: bool
:::

`build_assistant(factory)` configura ese tipo de salida, una herramienta de lectura y los argumentos deterministas del modelo. `AssistanceConfiguration.lumen_assistant` lo devuelve desde un método `@bean`. `AssistanceService` recibe un `Agent` en el constructor y expone `explain(question) -> AssistanceReply`. Los controladores no necesitan construir modelos. Si hay varios beans de agentes, utiliza los calificadores del capítulo 2.

En una prueba o tarea de consola puedes construir el propietario explícitamente:

```python
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.runtime import AgenticRuntime

async def explain_offline():
    async with AgenticRuntime(
        config=AgenticConfig(observability_enabled=False)
    ) as runtime:
        assistant = build_assistant(runtime.agent_factory)
        result = await assistant.run("Explain wallet review policy")
        return result.output
```

En este esquema se importa `build_assistant` del ejemplo. Una aplicación administrada usa el arranque y cierre de PyFly; no crees otro runtime por petición. `runtime.activate()` selecciona el propietario de operaciones anidadas al orquestar manualmente. El agente lleva su propietario durante la ejecución. El capítulo 21 amplía las reglas de cancelación y limpieza.

## 19.3. Las herramientas expresan capacidades

La descripción y los parámetros tipados indican al modelo qué puede solicitar. No autorizan acceso. La aplicación debe obtener identidad y tenant de la autenticación, validar argumentos y aplicar políticas de dominio en cada invocación. Separa claramente las lecturas y las escrituras.

::: listing samples/agentic-book/src/lumen_agentic/assistance.py | Listado 19.2 — Una capacidad de lectura pequeña y determinista
@agent_tool("lookup_policy", auto_register=False)
async def lookup_policy() -> str:
    """Read the fictional Lumen policy; never change wallet state."""
    return "lumen-review-v1: Review requires a human decision."
:::

`auto_register=False` evita registrar la herramienta al importar el módulo: la fábrica la recibe explícitamente. El agente sí queda registrado en su aplicación. `ToolKit` agrupa herramientas y los combinadores filtran, renombran o envuelven conjuntos. `AgentTool`, `ToolBuilder`, guardas, caché y composición secuencial, condicional o con alternativas cubren capacidades mayores. Una guarda ayuda a controlar el acceso, pero el comando subyacente sigue necesitando autorización e idempotencia. No repitas ciegamente una escritura cuyo resultado se desconoce.

La salida estructurada valida forma, no veracidad. Lumen debe comprobar que `policy_id` corresponde a una revisión recuperada y autorizada y que `needs_human` respeta sus reglas. Una respuesta puede cumplir el esquema y ser incorrecta. El capítulo 20 conecta el contrato con evidencia recuperada y validación de salida.

## 19.4. Conversaciones y contexto limitado

Asocia un `MemoryManager` cuando necesites historial y pasa un `conversation_id` estable a `run`. Derívalo de un contexto autenticado y limítalo al usuario y tenant. No expongas historiales por identificadores adivinables. Memoria de trabajo, conversación y estado persistido tienen contratos diferentes: persistir memoria de trabajo no hace durables automáticamente todas las conversaciones.

`message_history` permite administrar explícitamente la conversación. No lo combines con la inyección automática de `conversation_id`. La compresión y los resúmenes reducen el prompt, pero descartan información. Conserva identificadores exactos, aprobaciones y recibos fuera de un resumen generado. Prueba por separado conversaciones nuevas y continuadas.

Las opciones de modelo pueden establecerse por agente y por invocación. Temperatura, límites de salida y controles específicos de razonamiento dependen del proveedor. Los valores explícitos prevalecen sobre los predeterminados y una opción nullable puede borrar deliberadamente un valor heredado. No asumas equivalencia entre proveedores; consulta los campos y mapeos de la referencia nativa.

## 19.5. El streaming también tiene ciclo de vida

La propiedad de una operación incluye la iteración y su terminación anticipada. Espera `run_stream` para obtener su administrador de contexto asíncrono y consume dentro de `async with`. La prueba reconstruye la respuesta con los fragmentos:

```python
async with await assistant.run_stream("Explain the status") as stream:
    fragments = [part async for part in stream.stream_text(delta=True)]
text = "".join(fragments)
```

Este esquema necesita un agente de salida textual; el asistente tipado devuelve `AssistanceReply`. Una desconexión del navegador debe abandonar el contexto y propagar cancelación para liberar recursos y capacidad. El texto parcial es provisional, no un comando aprobado. Ejecuta efectos a través del servicio de dominio después de validarlos. La correlación del capítulo 15 enlaza petición, herramientas y telemetría.

## 19.6. Pausar ante una decisión humana

El segundo agente envuelve `request_review` con `requires_approval=True`. Su efecto ficticio añade un elemento a una lista. La primera ejecución devuelve solicitudes diferidas y deja la lista vacía. Un operador autorizado aprueba el identificador concreto de llamada; la aplicación reanuda con `DeferredToolResults` y `all_messages()` del resultado pausado.

Usa `is_deferred(result)` antes de interpretar `result.output` como respuesta final. Las aprobaciones son datos de control. Persiste identificadores, mensajes, decisión autenticada y caducidad en un almacén de la aplicación. Al reanudar con historial explícito, omite `conversation_id`: ambos mecanismos de historial son incompatibles. Prueba también la denegación. El ejemplo demuestra un efecto tras aprobar dentro de esa ejecución; no implementa una bandeja durable de aprobaciones.

La aprobación identifica quién permitió una llamada propuesta. No sustituye la autorización del comando ni garantiza pagos exactamente una vez. Revalida permisos y estado actual, usa una clave de idempotencia y guarda un recibo durable. Los workflows transaccionales del capítulo 12 y los journals del capítulo 21 protegen límites diferentes.

## 19.7. Ejecutar y revisar el límite

Desde `samples/agentic-book`, ejecuta `uv sync --extra dev --python 3.12` y `uv run pytest -q`. Repite con un entorno Python 3.13 aislado para validar ambos intérpretes. Las pruebas usan runtime real, identidad del registro, resultados de herramientas en el historial, salida tipada, streaming y efecto diferido sin llamar a proveedores reales.

Como ejercicio, añade un identificador de política inválido a la salida determinista y recházalo en el servicio. Después crea una evaluación independiente con un modelo real. Antes de continuar debes poder identificar el propietario del agente, la herramienta que produce efectos y por qué un esquema válido no autoriza esos efectos. El siguiente capítulo aporta la evidencia que necesita el asistente.

## 19.8. Probar un modelo local con Ollama

PyFly Agentic permite utilizar un modelo local mediante su fábrica tipada. Este
ejemplo requiere activación explícita y apunta al servidor Ollama de
`127.0.0.1:11434`, en el endpoint compatible con OpenAI `/v1`. El selector de
ejemplo es `qwen2.5:3b`, que debe estar instalado en ese servidor. Comprueba
disponibilidad, herramientas y salida estructurada en el modelo concreto.

::: listing samples/agentic-book/src/lumen_agentic/local_ollama.py | Listado 19.3 — Construir el modelo local y asociarlo al runtime nativo
async def build_local_assistant(factory: AgentFactory, model_name: str = "qwen2.5:3b") -> Agent:
    spec = ModelSpec(
        provider="ollama",
        model=model_name,
        base_url="http://127.0.0.1:11434/v1",
    )
    # A synthetic local token prevents inheriting a cloud credential from the environment.
    model = ModelFactory().build_with_secret(spec, "local-no-auth")
    factory.runtime.own(model.client)
    return factory.create(
        "lumen-local-assistant",
        model=model,
        instructions="Explain fictional Lumen policy. Never authorize a payment.",
        model_settings={"max_tokens": 128},
    )

:::

Desde el ejemplo ejecuta `uv run python -m lumen_agentic.local_ollama` solamente
cuando quieras invocar el servidor. El comando realiza una petición real local;
es independiente de `pytest`, cuyo fixture prohíbe peticiones a modelos reales.
La séptima prueba construye el adaptador sin invocarlo. El token sintético no
contiene credenciales cloud y el runtime administra y cierra el cliente HTTP
creado. No transfieras la propiedad de un cliente externo compartido sin decidir
explícitamente su ciclo de vida.

`ModelSpec` fija proveedor, nombre con tag y endpoint antes de construir el agente.
Un modelo explícito prevalece sobre los valores configurados. Una respuesta local
demuestra esa ruta; evalúa precisión, herramientas y autorización por separado.

## 19.9. Esperar la memoria en el bucle de la aplicación

Los recursos de base de datos de una aplicación iniciada pertenecen a su bucle de
eventos. En servicios asíncronos usa `await memory.async_set_fact(key, value)`,
`await memory.async_get_fact(key)` y `await memory.async_get_working_context()`.
`WorkingMemory` ofrece `async_set`, `async_get`, `async_items` y las operaciones
asíncronas correspondientes. También funcionan con el fixture en memoria: el
servicio conserva su interfaz cuando cambia la persistencia.

Los métodos síncronos de compatibilidad pueden enviar trabajo desde otro hilo al
bucle propietario activo; una llamada síncrona con base de datos en ese mismo bucle
se rechaza. Del mismo modo, usa `await assistant.run(...)` en el bucle propietario
de una aplicación iniciada. `run_sync` no puede bloquear ese bucle esperando su
propio trabajo. El contexto de streaming del Capítulo 21 también debe consumirse
en su bucle propietario.

El almacén persiste hechos de trabajo. El historial de conversación permanece en
el proceso, incluso con un almacén relacional o MongoDB. Reiniciar un worker no
restaura sus conversaciones. Guarda aprobaciones duraderas y recibos de negocio
en el modelo Data de la aplicación. El Capítulo 20 demuestra una transacción real
de SQLite con una fila de negocio y un hecho asíncrono.


## 19.10. Ejecutar scripts limitados con Code Mode

Instala `pyfly[agentic,agentic-script-execution]` desde el wheel de v26.10.11.
Este extra opcional instala `pydantic-monty==0.0.23` y el binario de
`pydantic-monty-runtime`. El SDK localiza el binario instalado y admite
`MONTY_BIN` para indicar un ejecutable compatible. Si falta el runtime,
devuelve instrucciones de instalación; no selecciona otro backend.

Monty ejecuta un subconjunto de Python en un worker Rust local. PyFly no monta
el sistema de archivos ni concede al script acceso a red, entorno o paquetes
de terceros. El script solo llega a servicios del host mediante las funciones
externas suministradas para esa ejecución. `SecureScriptRunner` comprueba el
código antes de ejecutarlo; ese análisis estático complementa el sandbox.

Para exponer herramientas nativas, pasa `toolkit_external_functions(my_toolkit)`
como mapa de funciones externas. Admite metadatos explícitos `ParameterSpec` y
firmas inferidas de funciones `@agent_tool`, conservando valores predeterminados
y argumentos por nombre. Cada llamada utiliza `tool.execute`, por lo que se
mantienen las guardas y los listeners de la herramienta.

Guarda este ejemplo como `monty_demo.py` y ejecuta `python monty_demo.py` en
el entorno instalado. No necesita modelo ni credenciales de proveedor. La
primera llamada realiza cálculo puro. La segunda utiliza Code Mode: el script
combina una lectura asíncrona con un formateador síncrono.

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

La salida es `Wallet 42: human review`. Aunque `lookup_policy` sea asíncrona en
el host, el script la llama directamente, sin `await`. Las herramientas
asíncronas se esperan en el bucle de la aplicación; las síncronas se ejecutan
fuera de ese bucle, en un hilo. El adaptador pausa y reanuda el script alrededor
de cada llamada al host.

Cada ejecución posee un pool `AsyncMonty(max_processes=1, min_processes=0)` y
una sesión nueva. Tanto el éxito como el fallo y la cancelación cierran ambos.
Reutilizar `MontyEnvironment` no conserva variables, módulos importados ni
funciones registradas en ejecuciones anteriores. Arrancar el worker tiene un
coste: este backend no es un intérprete dentro del proceso con arranque de
microsegundos.

El límite de tiempo real predeterminado es 30 segundos; el ejemplo establece
cinco para el arranque, la VM y la espera de herramientas del host en conjunto.
También se aplican los límites nativos de tiempo de VM, memoria y recursión del
script. Monty 0.0.23 eliminó el conteo de asignaciones: deja
`max_allocations=None`. Cualquier otro valor provoca `ValueError` antes de
arrancar workers. Sustituye un presupuesto antiguo de asignaciones por límites
de memoria y tiempo. Los límites del script no limitan la memoria del host.

Las herramientas conservan autoridad de negocio. Autentica la identidad,
comprueba tenant y fija plazos de E/S. Cancelar no revierte efectos externos ni
detiene por la fuerza una función síncrona que ya esté ejecutándose en su hilo.
Mantén escrituras idempotentes y recibos duraderos. Como ejercicio, sustituye
el formateador por una herramienta que falle, inspecciona `success`,
`error_type` y `error`, y comprueba que la siguiente llamada inicia un estado
del script nuevo. La
[guía de ejecución](https://fireflyframework.github.io/fireflyframework-pyfly/docs/modules/agentic/execution/)
explica el filtrado de salida y la integración con `ToolKit`.
