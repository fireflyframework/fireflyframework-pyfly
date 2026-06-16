<span class="eyebrow">Capítulo 3</span>

# Configuración, perfiles y secretos {.chtitle}

::: figure art/openers/ch03.svg | &nbsp;

Lumen ya tiene servicios cableados: un `WalletService` respaldado por un repositorio, un publicador de eventos y un ciclo de vida de inyección de dependencias completo. El problema es que cada entorno donde se ejecuta Lumen —tu portátil, un clúster de staging compartido, un despliegue de producción reforzado— necesita ajustes diferentes: puertos distintos, URL de base de datos distintas, verbosidad de log distinta, secretos distintos. Codificar esas diferencias a fuego en el código es frágil; repartirlas entre una docena de llamadas a `os.environ` resulta ilegible e imposible de auditar.

Este capítulo muestra cómo PyFly resuelve eso con un único `pyfly.yaml`, un sistema de precedencia de cuatro capas, ficheros de superposición específicos por entorno y clases de configuración fuertemente tipadas. Al terminar, Lumen tendrá una historia de configuración limpia que escala desde `pyfly run` en tu portátil hasta un despliegue de producción en contenedores, sin tocar una línea de lógica de negocio.

---

## pyfly.yaml: tu única fuente de ajustes

Toda aplicación no trivial tiene al menos dos audiencias para su configuración: un desarrollador que quiere logs detallados y una base de datos local relajada, y un sistema de producción que exige logs JSON estructurados, un pool de conexiones real y ningún modo de depuración. La solución ingenua —`if os.getenv("ENV") == "prod":` repartido entre una docena de ficheros— se vuelve rápidamente imposible de auditar. La respuesta de PyFly es un único fichero YAML (o TOML) canónico que contiene todo lo que tu aplicación sabe de sí misma, con mecanismos separados para lo que cambia entre entornos.

PyFly autodescubre este fichero en la raíz de tu proyecto. El framework comprueba los candidatos en orden —`pyfly.yaml`, `pyfly.toml`, `config/pyfly.yaml`, `config/pyfly.toml`— y carga el primero que encuentra.

!!! note "Nuevo término: autodescubrimiento"
    "Autodescubrimiento" simplemente significa que no tienes que decirle a PyFly dónde está el fichero de configuración. Dejas `pyfly.yaml` en la raíz de tu proyecto y el framework lo encuentra al arrancar. Sin argumento de ruta, sin llamada de registro.

Veamos el `pyfly.yaml` base de Lumen una sección cada vez, y luego leámoslo en su conjunto.

**Paso 1 — Identifica el servicio.** El primer bloque nombra la aplicación y le da una versión. Estos dos valores fluyen hacia el banner de arranque, el endpoint de salud y los metadatos de trazas, de modo que cada parte del sistema reporta la misma identidad:

```yaml
pyfly:
  app:
    name: lumen
    version: 1.0.0
```

**Paso 2 — Elige el puerto de escucha.** La API de negocio de PyFly escucha en `pyfly.server.port`. El valor por defecto es `8080`, así que esta línea es técnicamente redundante: se escribe por claridad. (Si has leído material más antiguo de PyFly que mencionaba `pyfly.web.port`, ten en cuenta que esa clave se eliminó en la v26.06.102; usa ahora `pyfly.server.port`.)

```yaml
  server:
    port: 8080
```

**Paso 3 — Activa las características de dominio que usa Lumen.** Los bloques restantes activan la observabilidad, CQRS, el motor transaccional, event sourcing, la caché, el bus de eventos en memoria y la capa de datos relacional. Cada bloque es una característica; activas lo que necesitas y dejas el resto en los valores por defecto del framework.

Aquí está el fichero completo. Fíjate en que `pyfly:` es la única clave de nivel superior: todo lo que Lumen le dice al framework vive bajo ella:

::: listing pyfly.yaml | Listado 3.1 — Fichero de configuración base de Lumen
pyfly:
  app:
    name: lumen
    version: 1.0.0
  banner:
    mode: console
  server:
    # App on 8080; actuator + admin default to the management port 9090.
    port: 8080
  observability:
    metrics:
      enabled: true
    tracing:
      enabled: false
  cqrs:
    enabled: true
  transactional:
    enabled: true
    persistence:
      provider: in-memory
  eventsourcing:
    enabled: true
  cache:
    provider: in-memory
  # Event-Driven Architecture: the in-memory bus (no broker needed).
  # Setting the provider registers the EventPublisher bean that the
  # wallet command handlers publish domain events through and that the
  # @event_listener audit projection auto-subscribes to.
  eda:
    provider: memory
  # Relational data layer (SQLAlchemy + SQLite via aiosqlite). The
  # framework creates the schema on startup (ddl-auto=create) and backs
  # the WalletRepository (a framework Repository[WalletEntity, str]) that
  # the command handlers persist through inside @transactional().
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

!!! note "Ejecútalo"
    Desde la raíz del proyecto Lumen, arranca la aplicación y observa qué fichero carga el framework:

    ```bash
    cd samples/lumen
    uv sync
    uv run pyfly run
    ```

    El banner de arranque imprime la versión del framework y, a continuación, PyFly registra cada
    fuente de configuración que ha fusionado (una línea `loaded_config` por capa) antes de que la
    aplicación se enlace al puerto `8080`:

    ```
    :: PyFly Framework :: (v26.06.110) (Python 3.12.13)
    Copyright 2026 Firefly Software Foundation. | Apache License 2.0
    no_active_profiles  message=No active profiles set, falling back to default
    loaded_config  source=pyfly-defaults.yaml (framework defaults)
    loaded_config  source=.../samples/lumen/pyfly.yaml
    Uvicorn running on http://0.0.0.0:8080
    ```

    Déjala en marcha; pronto la golpearás con `curl`. Pulsa `Ctrl+C` para detenerla.

Tres cosas merecen mención. Primera, la clave de nivel superior `pyfly:` está reservada exclusivamente para los ajustes del framework: servidor web, observabilidad, CQRS, EDA, acceso a datos y perfiles viven todos ahí. Tus propias claves de aplicación van bajo un nombre de nivel superior diferente (como `lumen:`). Segunda, `pyfly.app.name` y `pyfly.app.version` identifican el servicio en todas partes: el banner de arranque, los endpoints de salud y los metadatos de trazas leen estos valores. Tercera, el bloque `pyfly.data.relational.*` configura la capa SQLAlchemy/aiosqlite; `url`, `ddl-auto` y `enabled` son sus tres claves centrales.

Las claves anidadas se corresponden directamente con el acceso por notación de puntos a través de `Config.get()`. Para leer un valor anidado, unes la ruta de la clave con puntos: `pyfly.server.port` recorre `pyfly:`, luego `server:` y luego `port:`:

```python
config.get("pyfly.app.name")              # "lumen"
config.get("pyfly.server.port")           # 8080
config.get("pyfly.data.relational.url")   # "sqlite+aiosqlite:///./lumen.db"
config.get("pyfly.eda.provider")          # "memory"
```

`Config.get()` usa **coincidencia relajada de segmentos**: `ddl-auto` y `ddl_auto` se resuelven a la misma clave. Tu YAML puede usar kebab-case (el estilo YAML convencional) y tu código Python puede usar snake_case; no hace falta recordar qué forma usaste en el fichero.

PyFly usa `PyYAML` (`yaml.safe_load`) para el análisis de YAML; los tipos nativos de YAML se conservan. El entero `8080` en YAML llega como un `int` de Python, sin necesidad de analizar cadenas.

!!! note "Lo que acaba de pasar"
    Escribiste un único fichero YAML y PyFly lo convirtió en un objeto de configuración tipado y consultable. El bloque `pyfly:` le dijo al framework qué características activar (capa de datos, CQRS, bus de eventos); `Config.get("…")` vuelve a leer cualquier valor por su ruta con puntos; y la coincidencia relajada significa que nunca tienes que recordar si escribiste `ddl-auto` o `ddl_auto`. Ese único objeto es la fuente de la verdad sobre la que se construye el resto de este capítulo.

!!! tip "Consejo"
    También puedes usar TOML si tu equipo prefiere una sintaxis estilo INI con tipado estricto. Renombra el fichero a `pyfly.toml` y usa la sintaxis de tablas de TOML —`[pyfly.web]`, `[pyfly.data.relational]`— en lugar del anidamiento de YAML. Cada característica descrita en este capítulo funciona de forma idéntica con ambos formatos.

---

## Cómo se organiza la configuración por capas

Un único fichero funciona bien con un solo entorno. Los proyectos reales tienen tres o cuatro —desarrollo, test, staging, producción— y las diferencias entre ellos suelen ser pequeñas: una URL de base de datos aquí, un nivel de log allá. Duplicar el fichero entero para cada entorno es una carga de mantenimiento; la primera vez que alguien actualice el puerto en un fichero y olvide los demás, tendrás un bug de deriva de configuración.

PyFly evita esto superponiendo cuatro fuentes de configuración, cada una fusionada en profundidad sobre la anterior. Las capas posteriores siempre ganan:

::: figure art/figures/03-config.svg | Figura 3.1 — Precedencia de configuración (las capas posteriores ganan).

**Capa 1 — Valores por defecto del framework.** El `pyfly-defaults.yaml` empaquetado dentro del paquete `pyfly.resources` proporciona un valor por defecto sensato para cada clave que lee el framework. Nunca editas este fichero: se carga vía `importlib.resources` y funciona correctamente en distribuciones empaquetadas. El framework siempre parte de una línea base completa y funcional.

**Capa 2 — Fichero de configuración del usuario.** Tu `pyfly.yaml` (o `pyfly.toml`). Incluye solo las claves que difieren de los valores por defecto del framework. En el Listado 3.1, `pyfly.server.port: 8080` coincide con el valor por defecto: se incluye por claridad, no por necesidad.

**Capa 3 — Ficheros de superposición de perfil.** Para cada perfil activo, PyFly busca un fichero llamado `pyfly-{profile}.yaml` junto al fichero base y lo fusiona en profundidad. Las superposiciones de perfil contienen solo las claves que cambian.

**Capa 4 — Variables de entorno.** Se comprueban en el **momento de lectura** en cada llamada a `Config.get()`, no se fijan al arrancar. Esto significa que una variable de entorno establecida después de que la aplicación arranque sigue ganando: el comportamiento correcto para despliegues en contenedores donde los secretos se inyectan en tiempo de ejecución. Las variables de entorno siempre anulan todo lo demás.

### Fusión en profundidad, no reemplazo

Las capas se combinan mediante una fusión en profundidad recursiva (`Config._deep_merge()`). Los diccionarios anidados se fusionan clave por clave; los valores escalares se reemplazan. La distinción importa en la práctica: sin fusión en profundidad, una superposición de producción que cambiara solo `pyfly.server.port` borraría la clave `host` que está junto a ella en la misma sección `server:`, y el bloque `web.docs` no relacionado en una sección hermana. Con la fusión en profundidad, escribes solo lo que pretendes cambiar.

Para concretarlo, considera un fichero base y una superposición de producción:

```yaml
# pyfly.yaml (base)
pyfly:
  server:
    port: 8080
    host: "0.0.0.0"
  web:
    docs:
      enabled: true
```

```yaml
# pyfly-prod.yaml (overlay)
pyfly:
  server:
    port: 443
```

Tras la fusión, la configuración efectiva es:

```yaml
pyfly:
  server:
    port: 443         # overridden by prod overlay
    host: "0.0.0.0"   # preserved from base
  web:
    docs:
      enabled: true   # preserved from base (sibling section untouched)
```

Solo las claves que difieren aparecen en la superposición. Todo lo demás se conserva de la capa inferior.

!!! spring "Equivalencia con Spring"
    Este modelo de cuatro capas se corresponde directamente con la jerarquía de configuración de Spring Boot: `application.yaml` (valores por defecto embebidos en el jar) → tu `application.yaml` → `application-{profile}.yaml` → variables de entorno. El comportamiento de fusión en profundidad, la regla de que la variable de entorno siempre gana y el paso temprano de resolución de perfiles son todas decisiones deliberadas de equivalencia.

---

## Perfiles

El sistema de capas proporciona el mecanismo para variar la configuración entre entornos. Los perfiles proporcionan el vocabulario para nombrar esos entornos y activarlos limpiamente, sin ninguna lógica `if/else` en el código de tu aplicación.

Un **perfil** es una variante de entorno con nombre: `dev`, `test`, `staging`, `prod`. Activar un perfil carga un fichero de superposición y puede incluir o excluir beans condicionalmente.

!!! note "Nuevo término: fichero de superposición"
    Una "superposición" es un pequeño fichero YAML que contiene *solo las claves que cambian* para un entorno. PyFly lo fusiona sobre el `pyfly.yaml` base. Nunca repites la configuración completa: simplemente declaras las diferencias, y la fusión en profundidad de la sección anterior rellena todo lo demás.

### Activar perfiles

PyFly debe saber qué perfiles están activos *antes* de cargar la configuración completa, porque necesita saber qué ficheros de superposición fusionar. Esta **resolución temprana de perfiles** sigue un orden de prioridad deliberado:

1. **Variable de entorno `PYFLY_PROFILES_ACTIVE`** — máxima prioridad; separada por comas para múltiples perfiles.
2. **`pyfly.profiles.active` en el fichero de configuración base** — alternativa cuando la variable de entorno no está establecida.
3. **Pasada programáticamente** — vía `Config.from_file("pyfly.yaml", active_profiles=["prod"])`.

En producción, anula con una variable de entorno: sin cambio de código, sin edición de fichero:

```bash
PYFLY_PROFILES_ACTIVE=prod uv run pyfly run
```

### Ficheros de superposición de perfil

Para cada perfil activo `{name}`, PyFly busca `pyfly-{name}.yaml` junto al fichero base. Añadiremos tres superposiciones a Lumen, cada una conteniendo solo las claves que difieren de la base. Constrúyelas una a una.

**Paso 1 — La superposición de desarrollo.** Crea `pyfly-dev.yaml` junto a `pyfly.yaml`. Dev quiere el bucle de retroalimentación más ruidoso posible: trazas completas, cada consulta SQL volcada a la terminal y los internos del framework visibles en `DEBUG`.

::: listing pyfly-dev.yaml | Listado 3.2 — Superposición de desarrollo: logging detallado, modo de depuración
pyfly:
  web:
    debug: true
  data:
    relational:
      echo: true
  logging:
    level:
      root: "DEBUG"
:::

Tres claves cubren todo lo que el entorno de desarrollo necesita: modo de depuración para trazas detalladas, eco de SQL para que cada consulta aparezca en la terminal y nivel de log `DEBUG` para que los internos del framework sean visibles. Todo lo demás llega sin cambios desde el fichero base. Fíjate en que `echo` vive bajo `pyfly.data.relational.*`, de forma coherente con la estructura del fichero base.

**Paso 2 — La superposición de test.** Crea `pyfly-test.yaml`. El entorno de test quiere lo opuesto a dev: silencio. Silencia el banner para que la salida de las pruebas siga siendo legible, desactiva la persistencia real (las pruebas unitarias simulan el repositorio) y eleva el umbral de log para que las pruebas que pasan no impriman nada.

::: listing pyfly-test.yaml | Listado 3.3 — Superposición de test: SQLite en memoria, banner silenciado
pyfly:
  banner:
    mode: "OFF"
  data:
    relational:
      enabled: false
  logging:
    level:
      root: "WARNING"
:::

La superposición de test silencia el banner de arranque para que la salida de las pruebas se mantenga limpia, desactiva la persistencia de datos (las pruebas unitarias simulan la capa de repositorio) y eleva el umbral de log a `WARNING` para que las pruebas que pasan no produzcan ruido.

**Paso 3 — La superposición de producción.** Crea `pyfly-prod.yaml`. Producción cambia muchos interruptores a la vez: una URL real de PostgreSQL, logs JSON estructurados, los docs interactivos desactivados y un banner silencioso.

::: listing pyfly-prod.yaml | Listado 3.4 — Superposición de producción: base de datos real, logging JSON, docs desactivados
pyfly:
  server:
    port: 443
  web:
    debug: false
    docs:
      enabled: false
  data:
    relational:
      enabled: true
      url: "postgresql+asyncpg://prod-db:5432/lumen"
  logging:
    level:
      root: "WARNING"
    format: "json"
  banner:
    mode: "OFF"
:::

La superposición de producción toma varias decisiones deliberadas. Desactiva los docs interactivos de la API: no quieres una interfaz de Swagger en vivo en un endpoint de producción. Cambia el logging a formato `json` para que agregadores como Datadog o CloudWatch puedan analizar campos estructurados en lugar de raspar texto legible por humanos. Apunta `pyfly.data.relational.url` a la instancia real de PostgreSQL. Establece `pyfly.server.port: 443`, aunque en la práctica anularás esto con `PYFLY_SERVER_PORT` desde tu pipeline de despliegue para que ningún detalle de topología entre en el repositorio.

!!! note "Ejecútalo"
    Con los tres ficheros de superposición en su sitio, activa el perfil dev y observa cómo ocurre la fusión. La variable de entorno `PYFLY_PROFILES_ACTIVE` le dice a PyFly qué superposiciones cargar antes de que lea el resto de la configuración:

    ```bash
    PYFLY_PROFILES_ACTIVE=dev uv run pyfly run
    ```

    El log de arranque ahora lista la superposición dev entre las fuentes fusionadas y, como dev establece `pyfly.data.relational.echo: true`, cada sentencia SQL aparece en la terminal en cuanto la aplicación toca la base de datos:

    ```
    active_profiles  profiles=['dev']
    loaded_config  source=pyfly-defaults.yaml (framework defaults)
    loaded_config  source=.../samples/lumen/pyfly.yaml
    loaded_config  source=.../samples/lumen/pyfly-dev.yaml (profile: dev)
    INFO   sqlalchemy.engine.Engine  BEGIN (implicit)
    ```

    Detén la aplicación, ejecútala de nuevo *sin* la variable de entorno y la superposición dev desaparece de la lista de fuentes: los valores por defecto más silenciosos del fichero base toman el control. Ese es todo el mecanismo de perfiles en un experimento: una variable de entorno introduce y retira una capa entera.

!!! tip "Consejo"
    Múltiples perfiles se separan por comas en la variable de entorno y se aplican en orden, de modo que el último perfil gana en los conflictos: `PYFLY_PROFILES_ACTIVE=prod,metrics` aplica primero `pyfly-prod.yaml` y luego `pyfly-metrics.yaml`. Usa esto para componer aspectos transversales: un perfil `metrics` puede activar el raspado de Prometheus sin duplicar toda tu configuración de prod.

### Beans con ámbito de perfil

A veces la diferencia entre entornos no es un valor sino si un componente existe siquiera. Un cargador de semillas que rellena monederos de prueba nunca debe ejecutarse en producción. Un registrador de auditoría detallado que graba cada campo de la petición es útil en desarrollo pero un riesgo de cumplimiento en prod.

El parámetro `profile` de cualquier estereotipo controla cuándo participa un bean en el contenedor. La expresión admite negación y OR separado por comas:

```python
from pyfly.container import service


@service(profile="dev")
class DevSeedLoader:
    """Seeds the database with test wallets — only in dev."""
    ...


@service(profile="!prod")
class VerboseAuditLogger:
    """Detailed audit logging — active everywhere except prod."""
    ...
```

!!! note "Nuevo término: bean"
    Un "bean" es cualquier objeto que el contenedor de inyección de dependencias crea y gestiona por ti: un `@service`, `@repository`, `@command_handler`, etc. (el término viene directamente de Spring). "Con ámbito de perfil" significa que el contenedor solo crea el bean cuando un perfil que coincide está activo.

`Environment.accepts_profiles()` evalúa las expresiones de perfil durante la primera pasada de `ApplicationContext.start()`. Los beans cuya expresión no coincide con el conjunto activo se eliminan antes de que tenga lugar cualquier resolución: nunca se instancian, nunca se cablean, nunca están presentes en el contenedor. El resultado es un contenedor estructuralmente diferente por entorno, sin ninguna sentencia `if` en el código de tu aplicación.

!!! note "Lo que acaba de pasar"
    Los perfiles te dieron dos herramientas independientes. Los *ficheros de superposición* (`pyfly-{name}.yaml`) cambian los *valores* de configuración por entorno. El parámetro `profile=` de un estereotipo cambia qué *beans existen* por entorno. Ambos están gobernados por el mismo conjunto de perfiles activos —establecido una sola vez vía `PYFLY_PROFILES_ACTIVE`—, de modo que una única variable de entorno remodela tanto tus ajustes como tu grafo de objetos, con cero lógica condicional en tu código de negocio.

---

## Ajustes con tipos seguros mediante @config_properties

Las búsquedas por clave de cadena como `config.get("pyfly.data.relational.url")` funcionan para lecturas ocasionales, pero no escalan. Cada llamada es una lectura aislada sin información de tipos: debes acordarte de llamar a `float()` sobre el resultado, y una errata en una clave aflora en la primera petición en producción, no al arrancar. Para cualquier cosa más allá de un puñado de valores dispersos, el enfoque correcto es agrupar los ajustes relacionados en una clase Python tipada que se rellena una vez al arrancar y se inyecta donde haga falta.

`@config_properties` resuelve exactamente esto enlazando una sección de configuración a una dataclass de Python tipada.

!!! note "Nuevo término: enlace (binding)"
    "Enlazar" (binding) significa copiar valores del árbol de configuración a los campos de un objeto tipado. Una `@dataclass` de Python es una clase cuyos campos se declaran con anotaciones de tipo (`url: str`, `pool_size: int`); tras el enlace, lees `props.pool_size` y obtienes un `int` de verdad, no una cadena que tienes que convertir tú mismo.

### Declarar una clase de propiedades

Decora una `@dataclass` con `@config_properties(prefix="...")`. El `prefix` identifica la sección de configuración que se va a enlazar; los nombres de los campos deben coincidir con las claves bajo esa sección (kebab/snake intercambiables).

**Paso 1 — Escribe la clase.** Aquí está la propia `RelationalProperties` del framework, que enlaza el bloque `pyfly.data.relational.*`:

::: listing pyfly/config/properties/data.py | Listado 3.5 — RelationalProperties: ajustes tipados para la capa de datos
from dataclasses import dataclass

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.data.relational")
@dataclass
class RelationalProperties:
    """Typed binding for pyfly.data.relational.*"""

    enabled: bool = False
    url: str = "sqlite+aiosqlite:///pyfly.db"
    echo: bool = False
    pool_size: int = 5
:::

El decorador establece `__pyfly_config_prefix__` en la clase y la marca como un bean inyectable. Los tipos de los campos deben ser `int`, `float`, `bool` o `str` para la coerción automática; los tipos más complejos se dejan tal cual.

Fíjate en que cada campo lleva un valor por defecto que coincide con el `pyfly-defaults.yaml` integrado del framework. Esto es intencionado: la clase es autodocumentada y puede construirse y usarse en pruebas unitarias sin ningún fichero YAML en disco; basta con instanciar `RelationalProperties()` y obtienes los valores por defecto de desarrollo.

**Paso 2 — Aplica el patrón a tus propios ajustes.** El mismo decorador funciona para la configuración a nivel de aplicación. Así sería una clase `WalletProperties` para las reglas de negocio de Lumen:

```python
from dataclasses import dataclass
from pyfly.core import config_properties


@config_properties(prefix="lumen.wallet")
@dataclass
class WalletProperties:
    daily_transfer_limit: float = 10_000.0
    default_currency: str = "USD"
```

**Paso 3 — Añade el YAML correspondiente.** Pon el bloque bajo la clave de nivel superior `lumen:` (fuera de `pyfly:`, ya que este es el espacio de nombres propio de tu aplicación, no el del framework) y el framework lo enlaza automáticamente, sin necesidad de ningún registro especial:

```yaml
lumen:
  wallet:
    daily-transfer-limit: 10000.0
    default-currency: USD
```

### Enlazar e inyectar

Llama a `config.bind(PropertiesClass)` para producir una instancia tipada y rellenada. `Config` está registrado como un bean singleton, así que puedes inyectarlo en cualquier servicio y enlazar desde ahí:

::: listing lumen/wallet_service.py | Listado 3.6 — Inyectar RelationalProperties vía config.bind()
from pyfly.container import service
from pyfly.core import Config
from pyfly.config.properties import RelationalProperties
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.eda import EventPublisher


@service
class WalletService:
    def __init__(
        self,
        repo: WalletRepository,
        events: EventPublisher,
        config: Config,
    ) -> None:
        self.repo = repo
        self.events = events
        self.db: RelationalProperties = config.bind(
            RelationalProperties
        )

    async def transfer(
        self, from_id: str, to_id: str, amount: float
    ) -> dict:
        if not self.db.enabled:
            raise RuntimeError("Relational layer not enabled")
        # ... perform transfer using self.db.url for diagnostics ...
        return {"from": from_id, "to": to_id, "amount": amount}
:::

Cuando el contenedor de inyección de dependencias arranca Lumen, `WalletService.__init__` recibe el singleton `Config` compartido e inmediatamente llama a `config.bind(RelationalProperties)`. Esa llamada resuelve los valores enlazados una sola vez, al arrancar, y los almacena en `self.db`: una dataclass de Python sencilla con tipos reales. A partir de ese momento, `transfer()` lee `self.db.enabled` como un `bool`, con autocompletado completo del IDE y sin código de análisis en ninguna parte.

`config.bind()` funciona en cinco pasos:

1. Lee `__pyfly_config_prefix__` de la clase.
2. Llama a `effective_section(prefix)` — una copia resuelta del subárbol con los marcadores `${...}` expandidos, las anulaciones de variables de entorno aplicadas y las claves solo de entorno inyectadas.
3. Hace coincidir las claves de la sección con los campos de la dataclass mediante búsqueda relajada (kebab/snake intercambiables).
4. Aplica coerción de tipos a los campos cuyos valores llegaron como cadenas (por ejemplo, desde variables de entorno).
5. Construye la dataclass con los kwargs recopilados; los campos ausentes de la configuración usan los valores por defecto de la dataclass.

El detalle crítico en el paso 2 es que `effective_section()` aplica la pila completa de cuatro capas —valores por defecto, fichero, superposición de perfil, variables de entorno— antes de que se construya la dataclass. Para cuando `bind()` termina, `RelationalProperties` refleja lo que sea que diga la superposición de producción o una variable de entorno en tiempo de ejecución, no solo el YAML base.

!!! note "Lo que acaba de pasar"
    Reemplazaste las búsquedas dispersas de cadena `config.get("…")` por un único objeto tipado. `config.bind(RelationalProperties)` lee toda la sección `pyfly.data.relational.*` una sola vez, aplica la precedencia de cuatro capas, fuerza las cadenas a los tipos correctos y te devuelve una dataclass sencilla. A partir de entonces tu servicio lee `self.db.enabled` como un `bool` con autocompletado del IDE, y una errata en el nombre de un campo se detecta al arrancar, no en el momento de la petición en producción.

!!! note "Ejecútalo"
    Puedes demostrar que la capa de variables de entorno alcanza una propiedad enlazada con una pequeña comprobación. El `pyfly.yaml` base establece `pyfly.data.relational.enabled: true`; aquí lo anulamos desde el entorno. Desde la raíz del proyecto Lumen:

    ```bash
    PYFLY_DATA_RELATIONAL_ENABLED=false uv run python -c "
    from pyfly.core import Config
    from pyfly.config.properties import RelationalProperties
    db = Config.from_file('pyfly.yaml').bind(RelationalProperties)
    print('enabled =', db.enabled, type(db.enabled).__name__)
    "
    ```

    Salida esperada: la variable de entorno gana sobre el YAML y la cadena `"false"` se fuerza a un `bool`:

    ```
    enabled = False bool
    ```

### Inyectar valores individuales con Value

Para ajustes aislados que no justifican una clase de propiedades completa, PyFly proporciona un descriptor `Value`. Decláralo como un campo a nivel de clase y el contenedor de inyección de dependencias lo resuelve en el momento de creación del bean, exactamente como el `@Value("${...}")` de Spring Boot:

```python
from pyfly.container import service
from pyfly.core import Value


@service
class WalletService:
    # Resolved from pyfly.app.name in the merged config.
    app_name: str = Value("${pyfly.app.name}")
    # Falls back to 10000 when the key is absent.
    transfer_limit: float = Value(
        "${lumen.wallet.daily-transfer-limit:10000}"
    )
```

`Value("${key}")` lanza `KeyError` al arrancar cuando la clave falta y no se proporciona un valor por defecto: una garantía de fallo rápido que mantiene los bugs de configuración faltante fuera de producción. `Value("${key:default}")` usa el valor por defecto delimitado por dos puntos cuando la clave está ausente.

### Coerción de tipos

Los tipos nativos de YAML llegan correctamente tipados: los enteros, booleanos y flotantes no necesitan coerción. Cuando un valor llega de una variable de entorno (siempre una cadena) y el campo de destino tiene un tipo no de cadena, `bind()` aplica la coerción automáticamente:

| Tipo de destino | Regla de coerción |
|---|---|
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `value.lower() in ("true", "1", "yes")` |
| `str` | no se necesita coerción |

La ruta de dataclass de `bind()` trata `"true"`, `"1"` y `"yes"` como `True`; la ruta de anulación de `get()`/variable de entorno en tiempo de lectura admite además `"on"`.

Llamar a `bind()` sobre una clase no decorada con `@config_properties` lanza `ValueError` inmediatamente: una señal clara de fallo rápido al arrancar en lugar de un bug silencioso de valor incorrecto en el momento de la petición.

!!! spring "Equivalencia con Spring"
    `@config_properties` es la respuesta de PyFly al `@ConfigurationProperties` de Spring Boot. El modelo mental es idéntico: anota un POJO (aquí, una dataclass) con un prefijo y el framework le enlaza la sección de configuración correspondiente con coerción de tipos completa. `Value("${...}")` se corresponde con el `@Value("${...}")` de Spring: misma sintaxis de expresión, misma garantía de fallo rápido ante valores faltantes. La combinación de `pyfly.yaml` + superposiciones de perfil + `@config_properties` + `Value` se corresponde con `application.yaml` + `application-{profile}.yaml` + `@ConfigurationProperties` + `@Value`: los mismos conceptos, con modismos pythónicos.

---

## Variables de entorno y secretos

Los ficheros son el hogar adecuado para la configuración que varía por entorno pero que es seguro versionar: puertos, niveles de log, nombres de host de bases de datos. Son el hogar equivocado para los secretos: las contraseñas, las claves de API, los tokens de firma y las credenciales de bases de datos nunca deben entrar en el control de versiones. La cuarta capa de la pila de configuración existe específicamente para recibir estos valores en el momento del despliegue, desde un gestor de secretos o un pipeline de CI/CD, sin que ninguno de ellos toque el sistema de ficheros.

Las variables de entorno son la cuarta capa y la de mayor prioridad. PyFly las comprueba en cada llamada a `Config.get()` —en el momento de lectura, no al arrancar—, de modo que siempre ganan, incluso cuando se establecen después de que el proceso empiece.

### Convención de nombres

Cada clave de configuración con notación de puntos se corresponde con una variable de entorno con prefijo `PYFLY_` mediante una transformación mecánica de tres pasos:

1. Elimina el prefijo `pyfly.` (si está presente).
2. Reemplaza los puntos (`.`) y guiones (`-`) por guiones bajos (`_`).
3. Pon el resultado en mayúsculas y antepón `PYFLY_`.

| Clave de configuración | Variable de entorno |
|---|---|
| `pyfly.app.name` | `PYFLY_APP_NAME` |
| `pyfly.server.port` | `PYFLY_SERVER_PORT` |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` |
| `pyfly.web.debug` | `PYFLY_WEB_DEBUG` |
| `pyfly.data.relational.url` | `PYFLY_DATA_RELATIONAL_URL` |
| `pyfly.data.relational.pool-size` | `PYFLY_DATA_RELATIONAL_POOL_SIZE` |
| `pyfly.logging.level.root` | `PYFLY_LOGGING_LEVEL_ROOT` |
| `pyfly.eda.provider` | `PYFLY_EDA_PROVIDER` |
| `pyfly.profiles.active` | `PYFLY_PROFILES_ACTIVE` |

Para las claves específicas de la aplicación que no empiezan por `pyfly.`, la ruta completa con puntos se transforma de la misma manera (sin eliminar prefijo):

```
lumen.wallet.daily-transfer-limit
  →  PYFLY_LUMEN_WALLET_DAILY_TRANSFER_LIMIT
```

La regla es consistente: cuando necesitas decirle a un operador de Kubernetes qué variable de entorno controla un ajuste dado, la respuesta es siempre "aplica la transformación de tres pasos" en lugar de rebuscar en el código fuente del framework.

### Las variables de entorno siempre ganan

Activar el perfil de producción y anular la URL de la base de datos para una instancia de contenedor concreta es un único comando:

```bash
PYFLY_PROFILES_ACTIVE=prod \
  PYFLY_DATA_RELATIONAL_URL="postgresql+asyncpg://rds-prod:5432/lumen" \
  PYFLY_SERVER_PORT=8080 \
  uv run pyfly run
```

Aquí, `PYFLY_SERVER_PORT=8080` anula el `port: 443` de la superposición de prod. La pila de precedencia se resuelve así:

1. Valores por defecto del framework → `port: 8080`
2. Configuración base → `port: 8080` (sin cambios)
3. Superposición de prod → `port: 443`
4. Variable de entorno → `port: 8080` (gana)

El puerto efectivo es `8080`. Este es un patrón útil durante una migración por fases: mantén `port: 443` en la superposición como el valor por defecto de producción previsto y luego usa una variable de entorno temporal para mantener el servicio en `8080` para un experimento de división de tráfico. Cuando el experimento termina, elimina la variable de entorno y la superposición toma el control: sin necesidad de editar ficheros.

### Mantener los secretos fuera de los ficheros

Los ficheros de configuración nunca deben contener credenciales, claves de API o secretos de firma. El `pyfly-defaults.yaml` se distribuye con un secreto JWT de marcador de posición (`"change-me-in-production"`) que existe solo para mantener el framework ejecutable nada más sacarlo de la caja. Reemplázalo antes de pasar a producción:

```bash
PYFLY_SECURITY_JWT_SECRET="$(vault kv get -field=jwt_secret secret/lumen)"
```

!!! warning "Nunca versiones secretos"
    No pongas contraseñas, claves de API, credenciales de bases de datos ni secretos JWT en `pyfly.yaml`, `pyfly-prod.yaml` ni en ningún fichero que entre en el control de versiones. Usa variables de entorno provenientes de un gestor de secretos (HashiCorp Vault, AWS Secrets Manager, Kubernetes Secrets o similar). La capa de variables de entorno existe precisamente para recibir estos valores en el momento del despliegue, no en el momento del desarrollo.

### Una nota sobre las claves solo de entorno

`Config.bind()` también gestiona los valores que existen *solo* como variables de entorno, sin entrada correspondiente en ningún fichero YAML. `effective_section()` inyecta estas claves solo de entorno en la sección enlazada para que `bind()` vea el mismo valor que vería `get()`. Añade un nuevo campo a una clase `@config_properties`, establécelo exclusivamente vía una variable de entorno en tu pipeline de despliegue, y se rellena correctamente incluso cuando los ficheros YAML aún no se han actualizado:

```bash
# No YAML entry for pyfly.data.relational.echo?
# Set it exclusively via env var — bind() still picks it up.
PYFLY_DATA_RELATIONAL_ECHO=true uv run pyfly run
```

Esta es una escotilla de escape práctica durante despliegues incrementales: el equipo que despliega puede inyectar un nuevo valor antes de que el fichero YAML se actualice y se revise, y la aplicación lo toma sin un cambio de código.

!!! warning "Nombres de campo con varias palabras e inyección solo de entorno"
    La inyección solo de entorno trata cada guion bajo en un nombre `PYFLY_*` como un separador de ruta, de modo que `PYFLY_DATA_RELATIONAL_POOL_SIZE` se lee como la ruta anidada `pool` → `size`, no como el campo plano `pool_size`. Para un campo de una sola palabra como `echo` esto es inequívoco y `bind()` lo inyecta limpiamente. Para un campo de varias palabras como `pool_size`, dale a la clave un hogar real en tu YAML (aunque sea solo `pool-size: 5`) para que la variable de entorno anule una hoja existente en lugar de depender de la inyección solo de entorno. La lectura en tiempo de lectura `config.get("pyfly.data.relational.pool-size")` siempre devuelve el valor de entorno de todos modos, porque `get()` mapea la clave completa con puntos en un solo paso.

---

## Lo que construiste {.recap}

Lumen ahora tiene una historia de configuración limpia a través de tres entornos. Un `pyfly.yaml` contiene la línea base compartida —`pyfly.app.name`, `pyfly.eda.provider`, `pyfly.data.relational.*` y cualquier otra perilla del framework que use Lumen—. `pyfly-dev.yaml`, `pyfly-test.yaml` y `pyfly-prod.yaml` contienen solo los deltas por entorno. Activar un perfil requiere una única variable de entorno (`PYFLY_PROFILES_ACTIVE=prod`). Los ajustes tipados viven en dataclasses `@config_properties`, enlazadas al arrancar con coerción de tipos completa, de modo que los servicios leen campos tipados en lugar de llamar a `float(os.environ.get(...))` en código de servicio disperso. Los valores individuales se inyectan limpiamente vía `Value("${key}")`, que falla rápido al arrancar cuando la clave falta. Los secretos se quedan en las variables de entorno, nunca en los ficheros.

La pila de cuatro capas —valores por defecto → fichero → superposición de perfil → variables de entorno— te da un único modelo mental que funciona desde `pyfly run` en tu portátil hasta un contenedor blindado con secretos inyectados en el momento del despliegue, sin tocar una línea de lógica de negocio.

---

## Puertos de aplicación y de gestión

PyFly separa el puerto de **aplicación** del puerto de **gestión**, reflejando
los `server.port` / `management.server.port` de Spring Boot. Nada más sacarlo de la caja, la
API de negocio escucha en `pyfly.server.port` (**8080**) mientras que los endpoints
del actuator (`/actuator/*`) y el panel de administración (`/admin`) se sirven en un
`pyfly.management.server.port` dedicado (**9090**). Esto mantiene las comprobaciones de salud,
el raspado de Prometheus y la consola de administración fuera del puerto público: expones solo
el 8080 a internet y accedes al 9090 desde dentro del clúster.

| Clave | Variable de entorno | Por defecto | Propósito |
|---|---|---|---|
| `pyfly.server.port` | `PYFLY_SERVER_PORT` | `8080` | Puerto HTTP de la aplicación |
| `pyfly.server.host` | `PYFLY_SERVER_HOST` | `0.0.0.0` | Dirección de enlace de la aplicación |
| `pyfly.management.server.port` | `PYFLY_MANAGEMENT_SERVER_PORT` | `9090` | Puerto de gestión (actuator + admin) |
| `pyfly.management.server.address` | `PYFLY_MANAGEMENT_SERVER_ADDRESS` | host de la app | Dirección de enlace de gestión |

El puerto de gestión es un segundo escuchador **en proceso** —no procesos de worker
adicionales—, que comparte el mismo bucle de eventos y los mismos beans, así que funciona con cualquier adaptador de
servidor (Granian, Uvicorn, Hypercorn). Dos valores cambian la topología: establece
`pyfly.management.server.port` **igual** al puerto de la app para servir todo en un
único puerto (el comportamiento previo a la `v26.06.102`), o establécelo en **`-1`** para desactivar por completo los
endpoints web de gestión.

!!! warning "El puerto de gestión está abierto por defecto"
    Por equivalencia con Spring Boot, el puerto de gestión (9090) está **abierto y
    sin autenticación** por defecto: está pensado para vivir en una red interna
    protegida por aislamiento de red, no por los filtros de login de la app. Antes de
    exponerlo a cualquier lugar alcanzable, asegúralo con
    `pyfly.management.security.enabled: true`, que aplica los filtros de seguridad,
    sesión y CSRF de la app también al puerto de gestión. Por defecto, solo los
    endpoints del actuator `health` e `info` se exponen por HTTP; amplíalos con
    `pyfly.management.endpoints.web.exposure.include` (por ejemplo
    `"health,info,metrics,prometheus"`).

!!! note "Ejecútalo"
    Arranca Lumen y golpea el puerto de gestión directamente. El endpoint de salud vive en el
    9090, no en el 8080 de la aplicación:

    ```bash
    uv run pyfly run        # in one terminal
    curl http://localhost:9090/actuator/health   # in another
    ```

    Salida esperada: un pequeño documento JSON que informa de que el servicio está activo:

    ```json
    {"status":"UP"}
    ```

    La misma ruta en el puerto de la app (`curl http://localhost:8080/actuator/health`)
    no responderá, porque el actuator escucha solo en el puerto de gestión.

!!! spring "Equivalencia con Spring"
    `pyfly.server.port` ≡ `server.port` de Spring, `pyfly.server.host` ≡
    `server.address`, y `pyfly.management.server.port` ≡
    `management.server.port`. Establecer un puerto de gestión distinto ejecuta el actuator
    en su propio conector, exactamente como hace Spring Boot. `pyfly.management.security.enabled`
    y `pyfly.management.endpoints.web.exposure.include` reflejan los ajustes de seguridad de
    gestión de Spring y `management.endpoints.web.exposure.include`.

## Pruébalo tú mismo {.exercises}

1. **Añade una superposición de staging.** Crea `pyfly-staging.yaml` con una URL de PostgreSQL para una base de datos de pruebas compartida bajo `pyfly.data.relational.url`, `pyfly.data.relational.enabled: true` y logging en `INFO`. Actívala con `PYFLY_PROFILES_ACTIVE=staging uv run pyfly run` y verifica desde el log de arranque que la fuente de staging se cargó. Compara la configuración efectiva con la que produciría la superposición de prod.

2. **Enlaza una nueva propiedad tipada y úsala.** Añade un campo `max_wallets_per_owner: int = 5` a una nueva clase `WalletProperties` decorada con `@config_properties(prefix="lumen.wallet")`, y una clave correspondiente `lumen.wallet.max-wallets-per-owner: 5` en `pyfly.yaml` (fuera del bloque `pyfly:`). Inyecta `Config` en `WalletService`, llama a `config.bind(WalletProperties)` y añade una guarda en `open_wallet` que lance `ValueError` cuando el propietario ya posee el número máximo de monederos. Escribe una prueba rápida que anule el límite a `1` estableciendo `PYFLY_LUMEN_WALLET_MAX_WALLETS_PER_OWNER=1` y verificando que el error se dispara en el segundo monedero.

3. **Anula un valor vía una variable de entorno y observa la precedencia.** Establece `PYFLY_SERVER_PORT=9090` antes de arrancar Lumen. Comprueba el log de arranque y confirma que el servidor se enlaza al `9090`, no al `8080` de `pyfly.yaml`. Luego desestablece la variable de entorno y reinicia: el puerto debería revertir a `8080`. Este ejercicio hace concreta la naturaleza en tiempo de lectura de la resolución de variables de entorno: la variable de entorno siempre gana, y eliminarla restaura inmediatamente el valor del fichero sin ningún cambio de código.
