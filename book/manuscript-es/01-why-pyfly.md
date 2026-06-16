<span class="eyebrow">Capítulo 1</span>

# ¿Por qué PyFly? {.chtitle}

::: figure art/openers/ch01.svg | &nbsp;

Al final de este capítulo habrás instalado PyFly, generado el esqueleto del servicio de monedero **Lumen** y lo habrás ejecutado en local, con registro estructurado, un endpoint de salud en vivo y documentación interactiva de la API ya funcionando, todo ello sin una sola línea de código repetitivo.

---

## El problema de la cohesión

Imagina tu primer día en un nuevo microservicio en Python. Antes de escribir una sola línea de lógica de negocio, te enfrentas a dos semanas de decisiones de arquitectura.

¿A qué framework web recurres? FastAPI, Flask, Starlette, Django: cada uno es razonable, cada uno introduce sus propios modismos. ¿Qué ORM? ¿SQLAlchemy (síncrono o asíncrono?), Tortoise, Beanie? ¿Cómo conectas las dependencias? ¿Con dependency-injector, python-inject o un módulo de fábrica hecho a mano? ¿Cómo gestionas la configuración? ¿Con pydantic-settings, python-dotenv, dynaconf? ¿Y cómo debería organizarse el proyecto? Cada equipo inventa su propia respuesta.

Con el tiempo ensamblas una pila a medida, la unes con buenas intenciones y la pones en producción. Seis meses después, un segundo equipo empieza un nuevo servicio y toma decisiones completamente distintas. Ahora tienes dos bases de código con convenciones incompatibles, estrategias de pruebas diferentes, patrones de despliegue distintos y ningún entendimiento compartido de cómo funciona nada.

**Python te da una elección infinita. Lo que no te da es cohesión.**

::: figure art/figures/01-choice.svg | Figura 1.1 — Elección infinita, ninguna cohesión.

El problema del ensamblaje de la pila no es un fallo de habilidades: es una carencia de herramientas. Los desarrolladores de Java lo resolvieron hace años con Spring Boot: un único framework con criterio propio que toma decisiones sensatas por ti, te deja sobrescribir lo que importa y aplica un modismo coherente en cada servicio. PyFly aporta esa misma disciplina a Python.

---

## ¿Qué es PyFly?

La solución al problema de la cohesión no es prohibir la elección, sino tomar un buen conjunto de decisiones y empaquetarlas como un framework. Eso es precisamente lo que hace PyFly.

PyFly es un **framework cohesivo, de pila completa y nativamente asíncrono** para construir aplicaciones Python de nivel de producción: tanto microservicios como monolitos y bibliotecas. Toma las decisiones de la pila por ti: inyección de dependencias, enrutamiento HTTP, acceso a base de datos, mensajería, caché, seguridad, observabilidad, todo integrado, todo coherente, todo con valores por defecto listos para producción desde el primerísimo `pyfly run`.

Por dentro, PyFly delega en bibliotecas asíncronas probadas en batalla del ecosistema de Python (Starlette para HTTP, SQLAlchemy (async) para datos relacionales, structlog para el registro, Pydantic para la validación), pero tú nunca las importas directamente. Dependes de **los puertos de PyFly** (clases `Protocol` de Python) y el contenedor de inyección de dependencias conecta los adaptadores concretos en el arranque. Cambia PostgreSQL por MongoDB, o Kafka por RabbitMQ, sin tocar una sola línea de lógica de negocio.

PyFly es la **implementación oficial en Python del Firefly Framework**, una plataforma empresarial probada en batalla construida originalmente para Java (más de 40 módulos en producción). Aporta el mismo modelo de programación a Python 3.12+, no como una conversión sino como una reimplementación nativa diseñada en torno a `async/await` y las anotaciones de tipo.

Su arquitectura se asienta sobre cuatro capas (fundamento, aplicación, infraestructura e integración), cada una compuesta por módulos enfocados que encajan limpiamente entre sí.

::: figure art/figures/01-layers.svg | Figura 1.2 — Las cuatro capas de módulos de PyFly.

Cada capa sigue el mismo principio de **arquitectura hexagonal**: tu código vive en el centro y depende solo de los puertos; los adaptadores viven en los bordes y pueden intercambiarse sin perturbar el núcleo. Este diseño permite que el framework crezca contigo: empieza con un sencillo servicio REST y gradúate a CQRS, mensajería orientada a eventos u orquestación de sagas sin reescribir el fundamento que construiste el primer día.

!!! spring "Equivalencia con Spring"
    Si vienes de Spring Boot, PyFly te resultará familiar casi de inmediato. `@pyfly_application` es tu `@SpringBootApplication`. `@service`, `@rest_controller` y `@repository` son exactamente los estereotipos que conoces. La inyección por constructor a partir de las anotaciones de tipo refleja `@Autowired` sin XML ni magia de reflexión. La jerarquía de configuración de `pyfly.yaml` (valores por defecto → perfil → variables de entorno) se corresponde directamente con `application.yaml` + perfiles. Una llamada de atención de **Equivalencia con Spring** como esta aparece a lo largo del libro allí donde los conceptos se alinean lo suficiente como para ahorrarte el trabajo mental de traducción.

---

## Instalar PyFly

Para concretar estas ideas, a lo largo del libro construirás **Lumen**, una plataforma fintech de monederos. Lumen empieza de forma sencilla: una API REST que registra asientos en el libro mayor e informa de los saldos de los monederos. Para el capítulo final abarcará varios servicios que se comunican mediante eventos, con sagas que coordinan transferencias entre servicios. Empezar con un esqueleto bien estructurado te ahorra el dolor de adaptar la arquitectura más tarde, así que el generador de esqueletos de PyFly crea esa estructura por ti desde el principio.

Iremos paso a paso, sin prisa. Aquí nada presupone experiencia previa con PyFly: si sabes ejecutar un comando en una terminal, podrás seguir el ritmo.

!!! note "Término nuevo: uv"
    [uv](https://docs.astral.sh/uv/) es un gestor de paquetes y ejecutor de proyectos de Python muy rápido (piensa en `pip` + `virtualenv` + un ejecutor de tareas, todo en un único binario). PyFly lo recomienda, y cada comando de este libro que empieza con `uv run` significa simplemente "ejecuta esto dentro del entorno virtual del proyecto". Si solo has usado `pip`, no pierdes nada: uv lee el mismo `pyproject.toml` estándar.

**Paso 1 — Comprueba los prerrequisitos.** PyFly tiene como objetivo Python 3.12+ y usa uv. Confirma que ambos están instalados:

::: listing terminal | Listado 1.1 — Verificar los prerrequisitos
python --version
# Python 3.12.0 or later

uv --version
# uv 0.5.0 or later
:::

Si alguno de los comandos falta o informa de una versión más antigua, instálalo antes de continuar (las instrucciones de instalación de uv son un breve script en su sitio web). Todo lo demás que PyFly necesita, lo trae él por ti.

**Paso 2 — Genera el esqueleto del proyecto.** Un único comando genera un directorio de proyecto completo y con forma de producción:

::: listing terminal | Listado 1.2 — Generar el esqueleto del servicio de monedero Lumen
# web-api archetype, web feature
uv run pyfly new lumen --archetype web-api --features web
:::

!!! note "Términos nuevos: esqueleto (scaffold) / arquetipo"
    *Generar el esqueleto* (scaffold) es crear un esqueleto de proyecto ya hecho para empezar a partir de código que funciona en lugar de una carpeta vacía. Un *arquetipo* es la plantilla que usa la generación de esqueletos: `web-api` es la plantilla de servicio REST. La opción `--features web` añade la dependencia del servidor ASGI (la pieza que realmente acepta las peticiones HTTP). `uv run pyfly new` llama al registro de arquetipos de PyFly y escribe el directorio completo de una sola vez.

**Paso 3 — Entra en el proyecto e instala las dependencias.**

::: listing terminal | Listado 1.3 — Entrar en el proyecto y sincronizar las dependencias
cd lumen

# Install dependencies (including the pyfly CLI and ASGI server)
uv sync
:::

`uv sync` lee las dependencias declaradas en `pyproject.toml` y las instala en un entorno virtual local del proyecto, incluido el propio comando `pyfly` (disponible mediante el extra `cli`). La primera sincronización descarga los paquetes; las sincronizaciones posteriores son casi instantáneas porque uv cachea todo.

!!! tip "Pruébalo: confirma que la CLI funciona"
    Desde la raíz del proyecto, ejecuta `uv run pyfly --help`. Deberías ver la lista de comandos de PyFly (`new`, `run` y compañía). Si eso se imprime, tu cadena de herramientas está sana y estás listo para inspeccionar el proyecto generado.

!!! tip "Consejo"
    Ejecuta `pyfly new` **sin argumentos** para entrar en el modo interactivo. Te guía por la selección de arquetipo y características con navegación mediante las teclas de flecha, algo práctico cuando quieres preseleccionar extras como soporte de datos relacionales o de mensajería.

El generador de esqueletos imprime la disposición generada en forma de árbol:

```
╭──────────────── Created web-api project ─────────────────╮
│ lumen-demo/                                               │
│ ├── .env.example                                          │
│ ├── .gitignore                                            │
│ ├── Dockerfile                                            │
│ ├── README.md                                             │
│ ├── pyfly.yaml                                            │
│ ├── pyproject.toml                                        │
│ ├── src/lumen_demo/__init__.py                            │
│ ├── src/lumen_demo/app.py                                 │
│ ├── src/lumen_demo/controllers/__init__.py                │
│ ├── src/lumen_demo/controllers/health_controller.py       │
│ ├── src/lumen_demo/controllers/todo_controller.py         │
│ ├── src/lumen_demo/main.py                                │
│ ├── src/lumen_demo/models/__init__.py                     │
│ ├── src/lumen_demo/models/todo.py                         │
│ ├── src/lumen_demo/repositories/__init__.py               │
│ ├── src/lumen_demo/repositories/todo_repository.py        │
│ ├── src/lumen_demo/services/__init__.py                   │
│ ├── src/lumen_demo/services/todo_service.py               │
│ ├── tests/__init__.py                                     │
│ ├── tests/conftest.py                                     │
│ └── tests/test_todo_service.py                            │
╰───────────────────────────────────────────────────────────╯

  Next steps:
    cd lumen-demo
    uv sync --group dev
    pyfly run --reload
```

La aparente sencillez es deliberada. `pyproject.toml` te da un proyecto conforme a los estándares desde el primer día, sin la deuda heredada de `setup.py`. `pyfly.yaml` es la única fuente de verdad para cada ajuste de tiempo de ejecución, desde el nivel de registro hasta la URL de la base de datos, de modo que la configuración nunca se dispersa por una docena de archivos. La disposición `src/` evita que el paquete `lumen` sea importable accidentalmente sin una instalación explícita, detectando temprano los errores de ruta de importación. El generador de esqueletos también crea controladores, servicios y repositorios de muestra ejecutables, de modo que tengas código que funciona para estudiar de inmediato en lugar de un cascarón vacío.

---

## Dos archivos que importan

Comprender pronto la estructura de los puntos de entrada del esqueleto te ahorrará confusión más adelante. El esqueleto genera dos archivos que trabajan juntos: `app.py` declara la aplicación y `main.py` expone el `app` ASGI que importa el servidor. Cada uno tiene una responsabilidad distinta.

!!! note "Término nuevo: ASGI"
    *ASGI* (Asynchronous Server Gateway Interface) es el contrato estándar entre un servidor web de Python y una aplicación web asíncrona: el sucesor moderno y asíncrono de WSGI. No tienes que implementarlo; PyFly le entrega al servidor un objeto `app` ASGI ya hecho. Recuerda solo esto: el *servidor* (Uvicorn o Granian) le habla ASGI a tu *app*.

**Paso 1 — Abre la declaración de la aplicación.** Mira `src/lumen/app.py`:

::: listing lumen/app.py | Listado 1.4 — Declaración de la aplicación (@pyfly_application)
from pyfly.core import pyfly_application


@pyfly_application(
    name="lumen-demo",
    scan_packages=[
        "lumen_demo.controllers",
        "lumen_demo.services",
        "lumen_demo.repositories",
    ],
)
class Application:
    pass
:::

Por corto que sea, cada parámetro tiene su peso:

- **`name`** — la identidad del servicio. Aparece en cada evento de registro estructurado, en el título de OpenAPI y en las cargas útiles de las comprobaciones de salud: un identificador inequívoco cuando estás leyendo registros agregados de una docena de servicios.
- **`scan_packages`** — la instrucción que impulsa todo el sistema de inyección de dependencias. PyFly recorre cada módulo de la lista y registra en el `ApplicationContext` cualquier clase decorada con `@service`, `@rest_controller`, `@repository` o `@configuration`. Añade una nueva clase de servicio en cualquier punto de esos paquetes y se conecta automáticamente; no se requiere código de registro explícito.

El cuerpo de la clase `Application` está intencionadamente vacío. Nunca la instancias tú mismo: lo hace `PyFlyApplication` durante el arranque: carga la configuración desde `pyfly.yaml`, configura el registro estructurado, imprime el banner de arranque, escanea los paquetes, inicializa el `ApplicationContext` y registra los tiempos de arranque.

!!! note "Términos nuevos: la inyección de dependencias y el ApplicationContext"
    La *inyección de dependencias* significa que una clase declara lo que necesita en su constructor y el framework suministra esos colaboradores, en lugar de que la clase los construya ella misma. El *ApplicationContext* (a menudo simplemente "el contenedor") es el registro que contiene cada objeto conectado; PyFly llama a cada uno un *bean*, el mismo término que usa Spring. `scan_packages` es lo que lo puebla.

**Paso 2 — Abre el punto de entrada ASGI.** Ahora mira `src/lumen/main.py`:

::: listing lumen/main.py | Listado 1.5 — Punto de entrada ASGI (main.py)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from pyfly.core import PyFlyApplication
from pyfly.web.adapters.starlette import create_app

from lumen_demo.app import Application

# Bootstrap: load config, scan packages, build DI context
_pyfly = PyFlyApplication(Application)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle."""
    _pyfly._route_metadata = getattr(
        app.state, "pyfly_route_metadata", []
    )
    _pyfly._docs_enabled = getattr(
        app.state, "pyfly_docs_enabled", False
    )
    _pyfly._host = str(_pyfly.config.get("pyfly.server.host", "0.0.0.0"))
    _pyfly._port = int(_pyfly.config.get("pyfly.server.port", 8080))
    await _pyfly.startup()
    yield
    await _pyfly.shutdown()


app = create_app(
    title="lumen-demo",
    version="0.1.0",
    context=_pyfly.context,
    lifespan=_lifespan,
)
:::

Este es el archivo que descubre `pyfly run` (y cualquier servidor ASGI como Uvicorn). El objeto `app` a nivel de módulo es una aplicación Starlette: `create_app` monta cada `@rest_controller` encontrado en el contexto de inyección de dependencias y conecta el gancho del ciclo de vida (lifespan) para que el arranque y el apagado ocurran limpiamente. El decorador `@pyfly_application` por sí solo **no** crea el `app` ASGI: `main.py` es el puente explícito entre el mundo de la inyección de dependencias (`app.py`) y el mundo HTTP.

!!! note "Lo que acaba de pasar"
    Dos archivos pequeños dividen un trabajo en dos. `app.py` responde a *qué es esta aplicación y dónde vive su código* (nombre, versión, paquetes a escanear). `main.py` responde a *cómo la alcanza un servidor web*: arranca PyFly en un `ApplicationContext` y luego expone un único objeto `app` ASGI. Cuando más adelante añadas un controlador o un servicio, no editarás ninguno de los dos archivos: solo dejas la nueva clase en uno de los paquetes escaneados y PyFly la conecta en el siguiente arranque.

---

## Archivos del proyecto

Los otros dos archivos que editarás con más frecuencia son `pyproject.toml` y `pyfly.yaml`. Cada uno desempeña un papel distinto: `pyproject.toml` gestiona las dependencias, mientras que `pyfly.yaml` es dueño de cada ajuste de tiempo de ejecución.

**Paso 1 — Lee el manifiesto de dependencias.** `pyproject.toml` registra las dependencias del proyecto. Fíjate en los extras:

::: listing lumen/pyproject.toml | Listado 1.6 — pyproject.toml (secciones clave)
[project]
name = "lumen-demo"
version = "0.1.0"
description = "lumen-demo — built with PyFly"
requires-python = ">=3.12"
dependencies = [
    "pyfly[web]",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.3",
    "mypy>=1.8",
]
:::

!!! note "Término nuevo: extras"
    Un *extra* es un paquete opcional de dependencias nombrado entre corchetes: `pyfly[web]` significa "instala PyFly más su grupo opcional `web`". Los extras mantienen tu instalación ligera: incorporas un controlador de base de datos, una CLI o un servidor más rápido solo cuando realmente lo usas.

El extra `web` en `pyfly[web]` agrupa Uvicorn junto con el adaptador ASGI. El extra `cli` (añádelo cuando necesites el comando `pyfly` fuera de `uv run`) proporciona las herramientas de línea de comandos. Otros extras (`data-relational`, `granian`) son opcionales; los añades solo cuando una característica los necesita. La muestra del monedero Lumen, por ejemplo, declara `pyfly[cli,web,data-relational]` porque se conecta a SQLite.

**Paso 2 — Lee la configuración de tiempo de ejecución.** `pyfly.yaml` es donde viven todos los ajustes de tiempo de ejecución:

::: listing lumen/pyfly.yaml | Listado 1.7 — pyfly.yaml (valor por defecto del esqueleto)
pyfly:
  app:
    name: lumen-demo
    version: 0.1.0
    module: lumen_demo.main:app

  server:
    port: 8080
    type: "auto"
    event-loop: "auto"
    workers: 0

  actuator:
    endpoints:
      enabled: true

  admin:
    enabled: true

  logging:
    level:
      root: INFO
:::

La clave `module` le dice a `pyfly run` exactamente dónde vive el `app` ASGI: `lumen_demo.main:app`. La aplicación escucha en `pyfly.server.port` (por defecto `8080`); el tipo de servidor, los endpoints del actuator y el nivel de registro también tienen valores por defecto sensatos, así que solo sobrescribes lo que realmente necesitas cambiar.

!!! spring "Equivalencia con Spring"
    `pyfly.server.port` es el equivalente directo de `server.port` de Spring Boot, hasta el mismo valor por defecto `8080`. La variable de entorno de sobrescritura correspondiente es `PYFLY_SERVER_PORT` (el `SERVER_PORT` de Spring). La identidad de la aplicación sigue el mismo patrón: `pyfly.app.name` y `pyfly.app.version` reflejan `spring.application.name`.

!!! warning "Clave renombrada: usa server.port, no web.port"
    Versiones anteriores de PyFly usaban `pyfly.web.port` (y la variable de entorno `PYFLY_WEB_PORT`) para el puerto de escucha. Ambas fueron **eliminadas** en la v26.06.102 en favor de `pyfly.server.port` / `PYFLY_SERVER_PORT`. Si copias una configuración antigua y el puerto parece ignorado, casi siempre es por esto: renombra la clave bajo `server:` y la sobrescritura surte efecto.

!!! note "Servidor por defecto: Granian frente a Uvicorn"
    El servidor ASGI por defecto de PyFly es **Granian**, un servidor de alto rendimiento basado en Rust. La dependencia `pyfly[web]` del esqueleto agrupa **Uvicorn** en su lugar (instalación más ligera). Los dos son intercambiables: pasa `--server uvicorn` en la línea de comandos, o añade `pyfly[granian]` a tus dependencias para usar Granian. Por esta razón, la muestra del monedero Lumen se ejecuta con `--server uvicorn`.

---

## Tu primera ejecución

Con el proyecto en su sitio, arranca el servidor.

**Paso 1 — Arranca el servidor.** Desde la raíz del proyecto, ejecuta:

::: listing terminal | Listado 1.8 — Ejecutar el servidor con uv
uv run pyfly run --server uvicorn
:::

!!! note "Por qué `--server uvicorn`"
    El servidor por defecto de PyFly es Granian, pero el extra `pyfly[web]` trae Uvicorn (una instalación más ligera). Pasar `--server uvicorn` le dice a PyFly que use el servidor que realmente tienes. Si alguna vez ves un error sobre que Granian no está instalado, esta opción es la solución, o bien añade `pyfly[granian]` y omite la opción.

Tras un momento verás el banner ASCII de PyFly seguido de un flujo de eventos estructurados de arranque:

```
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/

:: PyFly Framework :: (v26.06.110) (Python 3.13.13)
Copyright 2026 Firefly Software Foundation. | Apache License 2.0
2026-06-07 20:34:32,442 [INFO] pyfly.core: starting_application |
    app=lumen-demo version=0.1.0 python=3.13.13 pid=72300
2026-06-07 20:34:32,442 [INFO] pyfly.core: runtime_environment |
    os=Darwin os_version=25.5.0 arch=arm64 cpus=11
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly-defaults.yaml (framework defaults)
2026-06-07 20:34:32,442 [INFO] pyfly.core: loaded_config |
    source=pyfly.yaml
2026-06-07 20:34:32,442 [INFO] pyfly.core: scanned_package |
    package=lumen_demo.controllers beans_found=1
2026-06-07 20:34:32,580 [INFO] pyfly.core: bean_summary |
    total=127 services=6 repositories=2 controllers=4
2026-06-07 20:34:32,580 [INFO] pyfly.core: mapped_endpoints | count=5
2026-06-07 20:34:32,580 [INFO] pyfly.core: request_mapping |
    method=POST path=/api/v1/wallets handler=open_wallet
2026-06-07 20:34:32,580 [INFO] pyfly.core: api_documentation |
    swagger_ui=http://0.0.0.0:8080/docs
2026-06-07 20:34:32,580 [INFO] pyfly.core: management_server |
    url=http://0.0.0.0:9090 endpoints=actuator + admin
2026-06-07 20:34:32,580 [INFO] pyfly.core: admin_dashboard |
    url=http://0.0.0.0:9090/admin
2026-06-07 20:34:32,581 [INFO] pyfly.core: server_started |
    server=uvicorn host=0.0.0.0 port=8080 workers=1
```

Lee estas líneas de registro como la historia de lo que el framework hizo en tu nombre. Cada evento tiene un nombre en lugar de un mensaje en bruto, de modo que los agregadores de registros puedan filtrar por clave:

1. **`starting_application`** — PyFly se anuncia con el nombre y la versión del servicio que declaraste, dando a cada agregador de registros un filtro limpio desde el primerísimo evento.
2. **`runtime_environment`** — el sistema operativo, la arquitectura y el número de CPU se emiten una vez en el arranque, lo que facilita correlacionar el comportamiento con el entorno más adelante.
3. **`loaded_config`** (×2) — la configuración está en capas: el framework trae `pyfly-defaults.yaml` con valores por defecto seguros para producción; tu `pyfly.yaml` sobrescribe solo lo que necesitas. Activa un perfil (p. ej., `--profile prod`) y verás una tercera entrada.
4. **`scanned_package`** — el contenedor de inyección de dependencias encontró un `@rest_controller` en el paquete de controladores web. `beans_found` crece a medida que añades servicios y repositorios.
5. **`bean_summary`** — el tamaño total del contexto de inyección de dependencias, incluidos los beans internos del framework. Incluso con 127 beans registrados, PyFly arranca en mucho menos de un segundo.
6. **`management_server`** — los endpoints del actuator y el panel de administración se sirven en un puerto de gestión *separado* (por defecto `9090`), independiente del `8080` de la aplicación.
7. **`admin_dashboard`** — la URL exacta de la interfaz de administración en vivo, `http://0.0.0.0:9090/admin`.
8. **`server_started`** — Uvicorn está aceptando conexiones en el puerto de la aplicación.

!!! note "Lo que acaba de pasar"
    Ejecutaste un solo comando y PyFly hizo el trabajo de toda una jornada de código repetitivo: cargó la configuración en capas, escaneó tus paquetes, conectó el contenedor de inyección de dependencias, mapeó tus rutas HTTP, publicó documentación interactiva de la API, levantó un servidor de gestión separado con comprobaciones de salud y un panel de administración, y empezó a aceptar peticiones, todo registrado como eventos con nombre y filtrables por máquina. Deja este servidor ejecutándose en su terminal; lo llamarás desde una segunda terminal en los siguientes pasos. Pulsa `Ctrl+C` cuando quieras detenerlo.

!!! spring "Equivalencia con Spring"
    Servir el actuator y el panel de administración en un puerto dedicado refleja el `management.server.port` de Spring Boot. En PyFly la clave es `pyfly.management.server.port` (por defecto `9090`). Iguálala a `pyfly.server.port` para un comportamiento de puerto único, o ponla a `-1` para deshabilitar por completo los endpoints de gestión. Por defecto, este puerto está **abierto y sin autenticación** (también equivalencia con Spring): bien para el desarrollo local, pero en producción asegúralo con `pyfly.management.security.enabled: true`.

Dos endpoints ya están en vivo antes de que hayas escrito una sola línea de lógica de aplicación. Abre una *segunda* terminal (deja el servidor ejecutándose en la primera) y prueba la comprobación de salud.

**Paso 2 — Comprueba el endpoint de salud en vivo.** Vive en el puerto de gestión, `9090`:

::: listing terminal | Listado 1.9 — Comprobación de salud (puerto de gestión 9090)
curl -s localhost:9090/actuator/health
:::

!!! note "Término nuevo: actuator"
    El *actuator* es la capa de operaciones incorporada de PyFly: un pequeño conjunto de endpoints HTTP que informan sobre la aplicación en ejecución: `/actuator/health`, `/actuator/info` y (cuando los expones) métricas, entorno y más. Es el mismo concepto, y la misma ruta base `/actuator`, que Spring Boot Actuator. Por defecto solo se exponen por HTTP `health` e `info`; amplía eso con `pyfly.management.endpoints.web.exposure.include`.

Deberías ver un documento JSON con un `"status": "UP"` de nivel superior y un mapa `components`, una entrada por cada indicador de salud que el framework registró:

```json
{
  "status": "UP",
  "components": {
    "cache_health_indicator": {
      "status": "UP",
      "details": {"adapter": "InMemoryCache", "latencyMs": 0.01}
    },
    "cqrs_health_indicator": {
      "status": "UP",
      "details": {"command_handlers": 3, "query_handlers": 2}
    },
    "eda_health": {
      "status": "UP",
      "details": {"adapter": "InMemoryEventBus"}
    },
    "db_health_indicator": {
      "status": "UP",
      "details": {"database": "sqlite"}
    }
  }
}
```

**Paso 3 — Abre un monedero.** Ahora llama al primer endpoint de negocio. A diferencia del actuator, este vive en el puerto de la *aplicación*, `8080`:

::: listing terminal | Listado 1.10 — Abrir un monedero (puerto de aplicación 8080)
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
:::

La respuesta es el identificador del nuevo monedero (tu UUID será distinto):

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c"}
```

!!! note "Lo que acaba de pasar"
    Ese único `curl` viajó por toda la pila que el framework construyó para ti: el cuerpo JSON se validó contra el modelo `OpenWalletRequest`, el `WalletController` lo convirtió en un comando `OpenWallet`, el bus de comandos lo despachó a su manejador (handler), se creó y se persistió una raíz de agregado `Wallet`, y el nuevo id volvió como JSON. Construirás cada una de esas capas por ti mismo en capítulos posteriores; por ahora, fíjate en que ya funciona de extremo a extremo.

**Paso 4 — Explora la documentación interactiva.** Abre `http://localhost:8080/docs` en tu navegador para la interfaz interactiva de Swagger; `http://localhost:8080/redoc` te da ReDoc; la especificación OpenAPI en bruto está en `http://localhost:8080/openapi.json`. La documentación vive en el puerto de la aplicación junto a tu API.

!!! note "Nota"
    Las tres URL de documentación se emiten en el registro de arranque (entradas `api_documentation`), así que nunca tienes que recordarlas. El Panel de Administración de PyFly (una vista en vivo de los beans, los endpoints, los indicadores de salud y los eventos de registro recientes) se abre en `http://localhost:9090/admin` en el puerto de gestión (su URL exacta es la línea `admin_dashboard` del registro de arranque).

**Paso 5 — Ejecuta las pruebas generadas.** El esqueleto trae una batería de pruebas que funciona para que puedas confirmar la cadena de herramientas de extremo a extremo. Instala las herramientas de desarrollo y luego ejecuta pytest:

::: listing terminal | Listado 1.11 — Ejecutar la batería de pruebas
uv sync --group dev
uv run --group dev pytest -q
:::

!!! note "Salida esperada"
    Deberías ver una breve sucesión de puntos (o líneas `PASSED`) y un resumen verde como `3 passed in 0.42s`; el número exacto depende del arquetipo. Cualquier resumen verde significa que PyFly, tu entorno virtual y pytest están todos conectados correctamente. (La muestra del monedero Lumen que estudias a lo largo del libro trae una batería mucho mayor, pero se ejecuta del mismo modo: `uv run --group dev pytest -q`.)

Eso completa el ciclo: instalar, generar el esqueleto, ejecutar, llamar y probar, todo antes de escribir una línea de tu propia lógica.

---

## Lo que construiste {.recap}

En menos de cinco minutos instalaste PyFly, generaste el esqueleto de un servicio con forma de producción y lo ejecutaste en local. La tabla siguiente resume lo que Lumen ya entrega (enteramente desde el framework) antes de que hayas escrito una sola línea de lógica de aplicación.

| Capacidad | Cómo la obtuviste |
|---|---|
| Registro JSON estructurado con metadatos de tiempo de ejecución | Valor por defecto del framework: emitido en cada arranque |
| Documentación interactiva de la API (Swagger UI + ReDoc) | Incorporada; siempre activa, desactívala mediante `pyfly.yaml` |
| Endpoint de salud (`/actuator/health` en el puerto 9090) | `actuator.endpoints.enabled: true` en el esqueleto |
| Configuración en capas consciente de los perfiles | `pyfly.yaml` + la opción `--profile` |
| Contenedor de inyección de dependencias con autodescubrimiento | `scan_packages` en `@pyfly_application` |
| Tiempos de arranque, resumen de beans, mapa de endpoints | Emitidos como eventos de registro estructurado en cada arranque |
| Panel de Administración (`/admin` en el puerto de gestión 9090) | Habilitado en el esqueleto por defecto |

Esa es la promesa de PyFly: decisiones sensatas tomadas por ti, convenciones coherentes en cada servicio y valores por defecto listos para producción desde la primerísima ejecución.

---

## Pruébalo tú mismo {.exercises}

1. **Renombra el servicio.** Abre `pyfly.yaml` y cambia `app.name` de `lumen-demo` a `lumen-wallet`. Actualiza también el parámetro `name` en `@pyfly_application`. Vuelve a ejecutar con `uv run pyfly run --server uvicorn` y confirma que el nuevo nombre aparece en la línea de registro `starting_application`.

2. **Explora la documentación en vivo.** Con el servidor en ejecución, abre `http://localhost:8080/docs`. Fíjate en el nombre y la versión del servicio en la parte superior de la interfaz de Swagger. Después navega a `http://localhost:8080/redoc` y compara los dos renderizadores de documentación. Comprueba la respuesta de salud en `http://localhost:9090/actuator/health` (recuerda: el actuator vive en el puerto de gestión, no en el puerto de la aplicación) y anota qué indicadores de salud ya están informando. Abre `http://localhost:9090/admin` para ver la misma información en el panel en vivo.

3. **Cambia a Granian.** Añade `pyfly[granian]` a la lista `dependencies` en `pyproject.toml`, ejecuta `uv sync` y luego arranca el servidor sin la opción `--server uvicorn`. Compara la línea de registro `server_started`: el campo `server` ahora debería decir `granian`.

4. **Asigna archivos a responsabilidades.** Mira los dos archivos de punto de entrada `app.py` y `main.py`. Escribe una descripción de una sola frase de lo que cada uno es responsable. ¿De dónde viene el contenedor de inyección de dependencias? ¿Dónde vive el objeto `app` ASGI? ¿Cuál editarías para cambiar los paquetes a escanear?

5. **Colapsa a un único puerto.** Añade `pyfly.management.server.port: 8080` bajo la sección `management:` hermana de `server` en `pyfly.yaml` (de modo que sea igual a `pyfly.server.port`). Vuelve a ejecutar el servidor y observa el registro de arranque: la línea `management_server` desaparece y `http://localhost:8080/actuator/health` ahora responde en el puerto de la aplicación. Después prueba `pyfly.management.server.port: -1`, vuelve a ejecutar y confirma que las rutas del actuator y de administración han desaparecido por completo.
