<span class="eyebrow">Capítulo 21</span>

# Workflows Agénticos y Operación {.chtitle}

La asistencia de Lumen tiene dos etapas de modelo: localizar política y redactar orientación. Más adelante puede incorporar comprobaciones paralelas, validación y una decisión humana. `pyfly.agentic` ofrece pipelines y un DSL Python para estas estructuras. Ambos viven dentro del límite operativo de la aplicación: configuración, presupuestos, cuotas, recursos y telemetría pertenecen a su runtime.

Estas APIs son distintas de las sagas y workflows transaccionales del capítulo 12. Un journal agéntico puede reutilizar resultados de llamadas al modelo. No convierte una transferencia en una operación durable o exactamente una vez. Los comandos monetarios mantienen autorización, idempotencia y recibos del servicio transaccional.

## 21.1. Elegir una estructura de orquestación

Un pipeline explicita etapas, dependencias y transiciones. El DAG por puertos conecta entradas y salidas; el modo de estado utiliza un modelo tipado y actualizaciones. `PipelineBuilder` construye ambos. Puedes combinar pasos de agentes, razonamiento, embeddings, recuperación y funciones con ramas, distribución, reducción y lotes. Los reducers determinan cómo combinar actualizaciones concurrentes; elígelos deliberadamente si varios nodos escriben el mismo campo.

El modo de estado admite ciclos con límite de recursión, routing y envíos dinámicos. `Pause` establece una pausa de control; checkpointers y logs de auditoría aportan estado de reinicio y evidencia. Define qué se persiste, cómo evoluciona su esquema y qué identifica una ejecución. Una llamada externa puede completarse antes de guardar el checkpoint.

El DSL Python resulta útil cuando el control de flujo ordinario explica mejor el proceso. `workflow`, `Workflow`, `agent`, `parallel`, `pipeline`, `phase`, `human`, `stream` y `subworkflow` proporcionan sus primitivas. Mantén determinista el orden de llamadas respecto al journal. Renumerarlas o saltarlas condicionalmente puede cambiar el significado de un journal indexado por secuencia.

::: figure art/figures/21-agentic-operations-es.svg | Figura 21.1 — El runtime limita llamadas; los efectos durables conservan sus propios recibos

## 21.2. Ejecutar el workflow mínimo útil

El ejemplo construye `Workflow` directamente para no registrar nada globalmente al importar el módulo. Una aplicación administrada registra los workflows elegidos durante el arranque. Las dos etapas ejecutan agentes nativos reales con `TestModel`:

::: listing samples/agentic-book/src/lumen_agentic/operations.py | Listado 21.1 — Dos fases con llamadas reales y deterministas sin red
def assistance_workflow() -> Workflow:
    async def explain(question: str) -> str:
        with phase("retrieve"):
            policy = await agent(
                f"Retrieve policy for {question}",
                model=TestModel(
                    custom_output_text="Review requires a human decision.",
                ),
            )
        with phase("draft"):
            return await agent(
                f"Draft guidance from: {policy}",
                model=TestModel(
                    custom_output_text="Read the policy; request human review.",
                ),
            )

    return Workflow("lumen-assistance", explain)
:::

El ejemplo enseña orquestación, no calidad RAG: la primera etapa devuelve una política fija. En la aplicación completa sustitúyela por el servicio de recuperación autorizado del capítulo 20 y pasa sus identificadores al agente tipado. Conserva alcance y validación en esa interfaz.

`ManagedAgentRunner`, el runner predeterminado, mantiene la pila de agentes nativa. `DefaultAgentRunner` utiliza directamente Pydantic AI y tiene otro límite de integración. Los protocolos permiten pruebas y adaptadores propios. Elige conscientemente; no asumas que todos los runners aplican idéntico middleware o instrumentación.

## 21.3. Limitar cada ejecución con presupuesto

`WorkflowBudget` limita agentes concurrentes y llamadas totales; puede limitar tokens, coste USD y tiempo. Las dos etapas fijan `max_concurrent_agents=1` y `max_agents_total=2`. Si el máximo total baja a uno, se produce `WorkflowBudgetError` antes de la segunda llamada. Esa prueba limita trabajo descontrolado de forma ejecutable.

Los contadores de tokens y coste se actualizan al completar llamadas. El techo no garantiza que una petición en curso no supere su estimación. Configura también límites del proveedor y margen conservador. Define qué ocurre ante un precio desconocido; tratarlo como gratuito invalida una garantía de gasto. Registra supuestos de precios al evaluar modelos nuevos.

Presupuestos, guardas de coste y cuotas tienen responsabilidades distintas. Un presupuesto limita una orquestación. El ledger del runtime mide uso de la aplicación. Las cuotas aplican una política por sujeto y ventana, posiblemente mediante un backend compartido entre workers. Una cuota en memoria no demuestra un límite de toda la flota. Prueba rechazo, concurrencia, liberación por cancelación y reinicio con la implementación elegida.

## 21.4. Reanudar mediante journals y conocer sus límites

Pasa un `Journal` a `workflow.run`. Reutilizarlo devuelve resultados ya completados en lugar de repetir llamadas al modelo. El ejemplo ejecuta dos veces y verifica que las dos entradas producen la misma respuesta. La prueba de presupuesto usa otro journal para que la caché no oculte la segunda llamada.

`FileJournalBackend` persiste en ficheros con una clave estable. Esa persistencia es de mejor esfuerzo: un fallo puede registrarse en logs sin propagarse como error de commit. Valida esa conducta antes de utilizarlo como evidencia normativa. Si necesitas una decisión durable, el almacenamiento debe ofrecer garantías explícitas de escritura. Guarda versión de workflow y esquema con la identidad de ejecución y rechaza reanudaciones incompatibles.

La reanudación vuelve a ejecutar el Python que rodea las llamadas cacheadas. Un email o cambio de saldo situado entre llamadas puede repetirse después de un fallo. Usa comandos idempotentes con clave durable, outbox u otro protocolo de efectos. Las ramas paralelas también necesitan identidades estables y una política de fallo deliberada. Un resultado de modelo reproducido no es un recibo transaccional.

`human` pausa mediante `WorkflowInterrupt`; proporciona la decisión registrada y vuelve a ejecutar con el mismo journal. Esa pausa del workflow difiere de la aprobación diferida de herramientas del capítulo 19. Ambas necesitan operador autenticado, vinculación con la solicitud exacta, caducidad y auditoría.

## 21.5. Administrar ciclo de vida y cancelación

Crea un `AgenticRuntime` por aplicación. La autoconfiguración aporta runtime y servicios cuando están activados y respeta los beans proporcionados por el usuario. Un programa manual puede usar `async with AgenticRuntime(config=...)`; `runtime.activate()` selecciona propietario para pipelines y workflows anidados. La selección debe cubrir iteración y trabajo de fondo, no solo el arranque.

Al cerrar, deja de aceptar trabajo, drena o cancela operaciones, cierra streams, libera capacidad y clientes propios y detén bucles administrados. Los clientes externos mantienen propietario externo salvo transferencia explícita. Los forks de un almacén compartido no deben cerrarlo varias veces. Prueba por separado reinicio y fallo parcial de arranque.

No llames a puentes síncronos bloqueantes desde el event loop propietario de la operación. Consume los streams en su event loop propietario; iniciarlos desde otro bucle se rechaza. Una llamada sin streaming desde otro hilo puede utilizar el puente hacia un bucle propietario activo. Prefiere métodos async en HTTP. Deja que la cancelación se propague después de limpiar recursos; ocultarla puede mantener consumo tras finalizar una petición aparentemente. El contexto del stream del capítulo 19 forma parte de esa disciplina.

## 21.6. Observar sin recopilar todo

Utiliza los proveedores de trazas y métricas del capítulo 15. El scope del framework es `pyfly.agentic` y las trazas nativas del modelo usan `pydantic-ai`; evita instalar otro SDK global o handlers raíz duplicados por agente. Correlaciona petición, aplicación, workflow, agente, herramienta y revisión de política. No uses prompts completos ni entradas arbitrarias como etiquetas sin límite.

`instrumentation_include_content` requiere activación explícita. Tokens, duraciones y clasificación del resultado suelen bastar para operación; recopilar contenido necesita retención y acceso deliberados. Registros de uso, decisiones de cuota, eventos de workflow, auditoría y explicabilidad responden a preguntas distintas. Define cuál sirve para diagnóstico y cuál constituye el registro autorizado de negocio.

Evaluaciones y experimentos aportan casos, métricas, runners, comparaciones y almacenamiento. Los adaptadores opcionales tienen restricciones propias. El laboratorio compara latencia y coste con entradas representativas y configuración equivalente. Registra revisión del dataset, modelo, configuración, versiones de validadores, tamaño de muestra y distribución de fallos. Un resultado determinista sin red no es un benchmark de proveedor real.

Reutiliza los proveedores de trazas y métricas de la aplicación. Para contextos aislados, configura `pyfly.observability.tracing.register-global` y `pyfly.observability.metrics.register-global` como `false`; su propietario debe drenar y cerrar los exportadores cuando termine el runtime. Un flush del SDK no demuestra aceptación por el colector remoto. Las trazas del modelo usan `gen_ai.usage.*` y las envolventes de invocación usan `gen_ai.aggregated_usage.*`; no sumes ambos totales. Las peticiones con precio también emiten `operation.cost`. Conserva las etiquetas de métricas al reenviarlas.

Los streams deben cerrar su contexto async aunque fallen el arranque, la memoria o el registro final de uso. Por defecto, las trazas conservan tipo y estado del error sin mensajes ni stack traces. El contenido sigue desactivado, pero esquemas, descripciones de herramientas y metadatos propios necesitan una política de privacidad. Una traza chat puede quedar UNSET tras un fallo parcial mientras la invocación y el run indican ERROR; revisa el resultado envolvente.

Los programas sin servidor pueden seleccionar Agentic y observabilidad con `ApplicationContext(config, auto_configurations=[...])`, obtener runtime y proveedores como beans y cerrar con `await context.stop()`. Usa `async with runtime.scope() as operation` para limitar la vida de agentes y modelos por operación; la aplicación conserva proveedores, memoria y presupuestos acumulados. Configura `pyfly.observability.metrics.exporter=none` para mantener un proveedor DI sin exportación ni endpoints OTLP heredados.

La aplicación puede inyectar el bean nativo `ModelFactory` con un bean `ModelClientPolicy` para reutilizar el puerto HTTP streaming de DI y fijar reintentos y tiempos del SDK. Registra cada modelo como recurso del scope de operación. La política admite Chat/Responses compatibles con OpenAI y Ollama; otros proveedores rechazan combinaciones no soportadas. Los reintentos del transporte y de cada llamada Pydantic AI siguen siendo independientes. Los mapas de headers OTLP por contexto evitan cambios globales del entorno. Las trazas web y de pipeline usan el mismo proveedor DI privado.

## 21.7. Validar el recorrido del servicio

Ejecuta las pruebas del capítulo 19. Demuestran registro nativo, salida tipada, lectura de herramientas, streaming, aprobación, aislamiento vectorial, replay y presupuesto de llamadas. La suite del framework cubre aislamiento entre aplicaciones y extremos del ciclo de vida; la aceptación productiva requiere también proveedores y persistencia reales.

Despliega primero un perfil controlado con presupuestos conservadores, telemetría de contenido desactivada, identidad confiable y escalado a operadores. Prueba credenciales ausentes, almacén caído, cuota agotada, timeout, desconexión de stream y aprobación duplicada. Comprueba que el fallo conserva una respuesta útil sin autorizar acciones silenciosamente.

La referencia completa vive en `docs/modules/agentic/`; esta parte enseña un recorrido práctico. Los modelos de dominio, comandos, eventos, identidad y despliegue de Lumen siguen vigentes. El asistente explica evidencia y coordina trabajo acotado mientras los servicios de negocio conservan autoridad sobre dinero y efectos durables.

## 21.8. Reutilizar HTTP, eventos y plugins nativos

`HttpTool` recibe el `HttpClientPort` de la aplicación. Configura el transporte
en `pyfly.client`; los antiguos campos de pool HTTP de Agentic se rechazan en
lugar de crear otro pool. La herramienta sigue necesitando guardas de host y
listas permitidas explícitas. Los clientes SDK de proveedores tienen su propio
contrato de integración y propiedad; el transporte nativo de herramientas no
reconfigura todos los SDK.

Las ejecuciones reales de agentes, herramientas y razonamiento publican objetos
`AgentEvent` esperados mediante el `ApplicationEventPublisher` de su aplicación.
Los suscriptores trabajan con el contexto transaccional nativo, incluidos los
listeners por fase. La publicación es de mejor esfuerzo: los fallos de listeners
o de exportación EDA se registran; estos eventos operativos no son recibos de
negocio duraderos. La exportación EDA requiere publicador y destino explícitos.
Guarda comandos autorizados y registros outbox en la transacción de dominio.

Los plugins gestionados usan el `Container` de la aplicación para resolver
dependencias y administrar arranque/cierre. `plugin-auto-discover` controla el
descubrimiento por entry points; el companion lo desactiva para ser reproducible.
Inyecta dependencias mediante el contenedor en vez de construir otro cliente HTTP,
fuente de datos o runtime global dentro del plugin. Al cerrar, Agentic drena sus
operaciones antes de que PyFly Data libere los recursos prestados.

## 21.9. Inspeccionar la vista Agentic nativa

Con admin habilitado, **Agentic** aparece en la barra lateral y la paleta de
comandos cuando el contenedor tiene un `AgenticRuntime`. La pantalla permite
buscar agentes, herramientas y definiciones de workflows registrados; sus diálogos
muestran campos seguros del inventario. **Refresh** obtiene otra instantánea.
Presenta uso retenido, peticiones, tokens, coste estimado, latencia y cuotas.
Los workflows son definiciones, no historial de ejecución. Un coste cero puede
significar que no se conoce el precio del modelo.

::: figure art/screenshots/21-agentic-admin.png | Figura 21.2 — Consola nativa con el fixture TestModel sin red; etiquetas y valores son ilustrativos

Los GET protegidos son `/admin/api/agentic`, `/admin/api/agentic/agents/{name}` y
`/admin/api/agentic/tools/{name}`. Activa `pyfly.admin.require_auth` y
`allowed_roles` según el Capítulo 15; un runtime o elemento ausente devuelve 404.
La vista no ejecuta agentes, modifica registros ni borra datos. Omite prompts,
instrucciones, mensajes, IDs de correlación, argumentos y valores por defecto de
herramientas, credenciales/configuración de proveedores y claves originales de
modelo en cuotas. Protege este inventario operativo mediante acceso autorizado.

El panel actual usa la identidad Python del brand kit canónico de Firefly:
Manrope local, carbón, papel y ámbar. La captura muestra la consola real y su
marca pyfly trazada. Se admiten ambos temas, foco de teclado y navegación móvil.
Las nueve pruebas sin modelos externos del companion validan su recorrido de
runtime y SQLite; la aceptación del navegador admin y los backends reales
PostgreSQL/MongoDB/pgvector pertenecen a pruebas separadas del framework.
