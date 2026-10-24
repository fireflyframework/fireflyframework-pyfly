<span class="eyebrow">Capítulo 20</span>

# Conocimiento y Razonamiento {.chtitle}

El asistente de Lumen tiene interfaz tipada y un límite de aprobación. Ahora necesita evidencia. Los datos de entrenamiento de un modelo no demuestran cuál es la política vigente. Recuperaremos una revisión autorizada, conservaremos su identidad y pediremos una respuesta limitada por esa evidencia. La generación aumentada con recuperación, o RAG, aporta contexto mediante búsqueda y lo explica mediante generación. La aplicación controla alcance, relevancia y validación final.

El ejemplo `samples/agentic-book/src/lumen_agentic/knowledge.py` usa un almacén vectorial real en memoria envuelto por `TenantScopedVectorStore`. Los vectores de dos dimensiones son fixtures para obtener un orden determinista. No son embeddings semánticos ni miden calidad de búsqueda. Así podemos revisar corrección técnica y calidad del modelo por separado.

## 20.1. Preparar documentos antes de responder

Parte de un registro de origen con ID, revisión, tenant/workspace confiables, tipo de contenido y procedencia. Carga mediante el adaptador adecuado, normaliza entradas binarias y preserva la identidad al dividir. `pyfly.agentic.content` ofrece fragmentación textual y Markdown, división de documentos, mosaicos de imágenes, compresión y procesamiento por lotes. Los conversores binarios tienen dependencias opcionales; instalar el núcleo no valida todos los formatos.

Divide respetando significado. Separar una regla de su excepción puede producir una respuesta convincente e incorrecta. La fragmentación Markdown conserva contexto de encabezados; un fragmentador textual sirve para contenido sencillo. Limita solapamiento y guarda posiciones o secciones. Prueba tablas, ficheros vacíos, párrafos extensos, formatos no admitidos y fallos de conversión. No indexes un éxito vacío cuando la conversión falló.

Los identificadores deben ser estables al repetir una ingesta de la misma revisión. Una revisión nueva requiere reemplazar fragmentos obsoletos o conservar versiones con filtros explícitos. Reutilizar IDs sin disciplina puede mezclar políticas antiguas y nuevas. Guarda procedencia suficiente para reconstruir por qué una respuesta citó un pasaje determinado.

::: figure art/figures/20-agentic-knowledge-es.svg | Figura 20.1 — La evidencia mantiene identidad de origen y alcance confiable hasta la respuesta

## 20.2. Modelo y dimensiones definen un contrato

Un adaptador de embeddings transforma contenido en vectores. `EmbeddingProtocol` y `BaseEmbedder` establecen la interfaz; adaptadores y registros implementan proveedores. Usa el mismo modelo, dimensiones y normalización al indexar y consultar. Cambiar de modelo normalmente exige volver a generar embeddings o mantener un índice paralelo versionado. Dos listas de floats no pertenecen necesariamente al mismo espacio vectorial.

Una evaluación real necesita documentos relevantes e irrelevantes, consultas multilingües y redacción ambigua. Mide recuperación antes de juzgar la calidad de la prosa. Una caché reduce trabajo repetido, pero su clave debe incluir modelo y configuración efectiva. Un embedding cacheado de otro modelo no es intercambiable con el actual.

En este libro el documento tiene `[1.0, 0.0]` y la consulta usa el mismo vector. El orden por coseno es transparente. Solo demostramos recuperación de un registro en el alcance correcto; no comprensión semántica entre español e inglés, extracción de facturas ni rendimiento de proveedores.

## 20.3. Aplicar alcance en cada operación

La API básica acepta un namespace. El envoltorio exige `tenant_id` y `workspace_id` como argumentos nombrados en lecturas, escrituras y borrados. Deriva el namespace canónico y añade metadatos sin modificar el documento original. Lumen obtiene esos identificadores de la identidad autenticada; el modelo no puede elegir su tenant a partir del prompt.

::: listing samples/agentic-book/src/lumen_agentic/knowledge.py | Listado 20.1 — Política ficticia con alcance y revisión explícitos
async def build_knowledge() -> TenantScopedVectorStore:
    store = TenantScopedVectorStore(InMemoryVectorStore())
    await store.initialise()
    await store.upsert(
        [
            VectorDocument(
                id="lumen-review-v1",
                text="Review requires a human decision.",
                embedding=[1.0, 0.0],
                metadata={"revision": "1", "source": "fictional-handbook"},
            )
        ],
        tenant_id="lumen",
        workspace_id="help",
    )
    return store
:::

La prueba recupera el documento para `lumen/help`, recibe una lista vacía para otro tenant y comprueba que omitir el alcance causa `TypeError`. Este caso negativo importa tanto como obtener un documento esperado. Un adaptador persistente necesita pruebas reales de concurrencia, borrado, dimensiones y filtros.

Los adaptadores incluyen memoria, SQLite vectorial, PostgreSQL/pgvector, Chroma, Qdrant y Pinecone. Elige según requisitos y despliegue e instala el extra `agentic-*` correspondiente. Los filtros, índices y garantías de consistencia varían. Una prueba en memoria no demuestra persistencia ni aislamiento entre procesos. Inicializa y cierra clientes propios dentro del ciclo de aplicación del capítulo 21.

## 20.4. Recuperar, limitar y validar

Genera el embedding de consulta, recupera un top-k limitado, aplica autorización y revisión y construye evidencia con IDs. Si no queda evidencia relevante, explica la insuficiencia o escala a un humano. No sustituyas evidencia ausente por una respuesta inventada. La similitud ordena resultados; no representa la probabilidad de que una política sea aplicable.

Separa instrucciones y contenido recuperado. Un documento puede contener instrucciones hostiles o accidentales; trátalas como evidencia no confiable. Los permisos de herramientas y la autorización permanecen fuera del texto. Solicita ID y revisión en la salida y verifica que cada cita pertenezca al conjunto autorizado. Un esquema solo confirma que el identificador tiene la forma requerida.

Los validadores deterministas comprueban evidencia obligatoria, estados permitidos e invariantes. Las revisiones de calidad y bucles de refinamiento pueden mejorar contenido, pero añaden llamadas y fallos. Limita iteraciones y presupuesto. Un modelo que juzga a otro también falla; usa revisión humana para excepciones relevantes. Registra por qué se rechazó una respuesta para distinguir evidencia ausente y error transitorio del proveedor.

## 20.5. Prompts y skills versionan comportamiento

Las plantillas separan instrucciones estables y variables. Proporciona únicamente evidencia y contexto necesarios; versiona la plantilla junto al esquema y conjunto de evaluación. Un cambio puede alterar herramientas, rechazos, citas y coste aunque no cambie la firma Python. Trátalo como un cambio de aplicación y repite las pruebas de esas propiedades.

El subsistema de skills agrupa instrucciones reutilizables y recursos. Selecciona skills confiables para el rol de asistencia, inspecciona sus requisitos y evita cargar instrucciones arbitrarias del usuario como comportamiento privilegiado. Una skill describe un proceso, pero no concede permisos que la aplicación no otorga. Las guías nativas de prompts y skills documentan cargadores, composición y registros completos.

La memoria conversacional del capítulo 19 aporta contexto, pero no sustituye la política. Recupera la revisión vigente y evita que un resumen antiguo la anule. Compresión y resúmenes ayudan a respetar límites; conserva citas exactas y decisiones pendientes fuera de representaciones con pérdida. Prueba conversaciones que atraviesan una revisión de política.

## 20.6. Elegir razonamiento por una necesidad medible

`pyfly.agentic.reasoning` incluye patrones de descomposición, planificación, reflexión, búsqueda y verificación. Pueden dividir una petición compleja, pero más pasos no garantizan mayor corrección. Empieza con recuperación y una salida tipada; añade patrones cuando un caso de evaluación justifique el trabajo adicional.

Ante una pregunta sobre varias políticas, descompón en subpreguntas limitadas, recupera evidencia autorizada para cada una y sintetiza después de validar. Fija máximos de pasos y llamadas. Los modos de salida estructurada dependen del proveedor: `reasoning_output_mode` del runtime y opciones explícitas del patrón controlan la estrategia. Verifica la ruta elegida con el proveedor en una prueba independiente.

No muestres trazas privadas de razonamiento como explicaciones públicas. Explica evidencia, política aplicada y resultado verificable. Las herramientas de explicabilidad registran estructura y reportes sin convertir razonamiento interno en fuente de verdad. Mantén telemetría de contenido opcional y una retención acorde con identidad y privacidad del capítulo 14.

## 20.7. Medir evidencia y prosa por separado

Prepara casos ficticios: pregunta normal, evidencia ausente, revisión obsoleta, documento ajeno, ambigüedad e instrucciones hostiles dentro de un documento. Mide recuperación, validez de citas, esquema, escalado y permisos por separado. Una puntuación agregada puede esconder un fallo grave en una categoría pequeña.

Amplía la prueba del ejemplo con dos workspaces del mismo tenant y un borrado seguido de búsqueda. Sustituye después los vectores fijos por un adaptador real en una prueba con credenciales explícitas. Registra modelo, dimensiones, revisión de datos y configuración del backend. La aceptación de producción necesita esa evidencia y evaluación humana representativa.

El capítulo 21 une estos componentes con controles operativos. Recuperar demuestra qué contexto recibió el modelo; validar comprueba propiedades seleccionadas de la salida; los servicios de dominio deciden qué operaciones de negocio están permitidas. Mantener esas responsabilidades explícitas facilita investigar y corregir fallos.

## 20.8. Confirmar hechos junto a los datos de negocio

Instala `pyfly[agentic-relational]` para memoria relacional nativa. El companion
selecciona ese extra y crea un archivo SQLite temporal por prueba.
`native_memory(path)` inicia un `ApplicationContext` real; PyFly Data posee el
motor, el pool y el gestor transaccional, y Agentic toma prestado el `DataSource`
principal. Una configuración mínima de desarrollo local es:

```yaml
pyfly:
  agentic:
    enabled: true
    memory-backend: relational
  data:
    relational:
      url: sqlite+aiosqlite:///lumen.db
      ddl-auto: create
```

Usa `memory-datasource` para elegir una fuente registrada por nombre.
`ddl-auto: create` es una decisión explícita de arranque local; en producción,
`none` o `validate` requieren migraciones ordinarias. Las URL y los ajustes del
pool pertenecen a `pyfly.data.relational`, sin otra configuración de conexión
en Agentic.

::: listing samples/agentic-book/src/lumen_agentic/native_memory.py | Listado 20.2 — Una unidad nativa confirma o revierte la fila y el hecho
async def record_review(
    memory: MemoryManager,
    datasource: DataSource,
    request_id: str,
    *,
    abort: bool = False,
) -> None:
    async with TransactionTemplate(datasource).transaction() as unit:
        assert unit is not None
        await unit.resource.execute(
            text("INSERT INTO review_requests (id) VALUES (:id)"),
            {"id": request_id},
        )
        await memory.async_set_fact(f"review:{request_id}", {"status": "pending"})
        if abort:
            raise ValueError("cancel fictional review")
:::

`TransactionTemplate` participa en la misma unidad de la fuente de datos que el
adaptador de memoria. Las pruebas verifican una fila confirmada y un diccionario
con sus tipos, la sustitución de un hecho y la reversión de ambas escrituras con
`abort=True`. Es SQL real en SQLite sin peticiones al modelo. Ejecuta
`uv run pytest -q` desde el companion para reproducirlo, o
`uv run python -m lumen_agentic.native_memory` para mostrar la fila confirmada,
su hecho y el hecho revertido ausente desde una base temporal local. El esquema es ficticio:
no autoriza ni mueve fondos.

`RelationalMemoryStore`, `PostgreSQLStore` y `MongoDBStore` toman prestados recursos
Data. Cerrarlos no destruye el motor ni el cliente Mongo de la aplicación.
PostgreSQL conserva `firefly_memory.memory_entries` y MongoDB conserva
`firefly_memory.entries`, salvo configuración explícita. Las restricciones
históricas de ID global requieren una migración explícita a la identidad compuesta
namespace/ID. El arranque rechaza ese esquema sin borrar índices, reescribir filas
ni mover datos. Haz copia y migra deliberadamente; cambiar la configuración no
realiza una migración destructiva.

## 20.9. Separar vectores y cifrado del contenido

`PgVectorVectorStore` toma prestada una fuente relacional nativa o su gestor y usa
su unidad de trabajo. Inicialízalo en el bucle de la aplicación antes de las
transacciones de negocio. `create_schema=None` sigue `ddl-auto`; el arranque
explícito y la validación de producción son decisiones separadas. Los argumentos
antiguos de URL y pool se rechazan con instrucciones de migración a Data.
Instala `pyfly[agentic-vectorstores-pgvector]` para el adaptador y sus dependencias
PostgreSQL nativas. Los documentos vectoriales conservan su identidad global;
se diferencia de la identidad compuesta namespace/entrada de la memoria.

El cifrado es opcional con `encryption-enabled` y un secreto `encryption-key`, o
un `EncryptionProvider` inyectado; instala `pyfly[agentic-security]` para AES.
El envoltorio de memoria propia cifra solo `MemoryEntry.content` y conserva los
tipos JSON. Claves, namespaces, metadatos y marcas temporales siguen en texto
plano, igual que las conversaciones del proceso. No pongas secretos en esos campos.
Una clave ausente, un texto cifrado inválido o un valor JSON no compatible provoca
un error, sin guardar ni devolver texto plano silenciosamente. Los gestores de
memoria aportados por el usuario son prestados y requieren su propia política
explícita. Valida rotación, recuperación y control de acceso por separado de la
prueba transaccional de SQLite.
