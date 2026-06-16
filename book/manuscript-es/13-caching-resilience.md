<span class="eyebrow">Capítulo 13</span>

# Caché y resiliencia {.chtitle}

::: figure art/openers/ch13.svg | &nbsp;

En el Capítulo 11 dividiste Lumen en servicios independientes y enseñaste a su
manejador (handler) de monederos a llamar a un `AccountService` descendente a
través de HTTP. En el Capítulo 12 añadiste una `DepositSaga` para coordinar
operaciones de varios pasos a través de las fronteras de servicio, con
transacciones de compensación listas para dispararse cuando cualquier paso
fallara.

Esos dos capítulos introdujeron una nueva clase de problema: la latencia y la
propagación de fallos. Cada salto HTTP a `AccountService` es un viaje de ida y
vuelta que podría ser lento en una red congestionada, y cada llamada a la
propia base de datos de Lumen compite con participantes de saga concurrentes.
En un sistema distribuido, los fallos no son eventos excepcionales: son
mantenimiento programado. `AccountService` se actualizará en plena carga de
tráfico. Redis tendrá un tropiezo. Una pasarela de pago se disparará a tiempos
de respuesta de tres segundos durante el pico de liquidación.

Sin protección, Lumen propaga esos fallos hacia arriba. Un `AccountService`
lento atasca corrutinas, bloqueando lecturas de monederos para usuarios no
relacionados. Una breve caída de Redis borra los saldos en caché y envía cada
petición directamente a la base de datos, multiplicando la carga en el peor
momento posible.

Este capítulo hace que Lumen sea **rápido** y **tolerante a fallos**. La primera
mitad cubre la capa de caché declarativa de PyFly — **`@cacheable`**,
**`@cache_put`** y **`@cache_evict`** — y muestra cómo respaldarlas con un
`InMemoryCache` en proceso para desarrollo y un `RedisCacheAdapter` compartido
con conmutación por error (failover) automática para producción. La segunda
mitad incorpora el conjunto de herramientas de resiliencia: un **limitador de
tasa** de cubo de tokens que limita el tráfico entrante, un **bulkhead** de
semáforo que aísla la concurrencia, un **limitador de tiempo** que cancela
corrutinas colgadas, un **fallback** que degrada con elegancia, y patrones de
**reintento** y **cortacircuitos** (circuit breaker) que protegen las llamadas
salientes. Una sección de cierre muestra cómo apilarlos todos en el orden
correcto.

Al final del capítulo, cada ruta caliente de Lumen estará en caché y cada
dependencia saliente estará envuelta en una valla de resiliencia.

!!! note "Lo que construirás, en términos sencillos"
    Este capítulo introduce mucho vocabulario — *caché*, *cubo de tokens*,
    *bulkhead*, *cortacircuitos*. No dejes que la jerga te intimide. Cada una
    de estas es una herramienta pequeña y autocontenida que atornillas a una
    función con un único decorador. Presentaremos cada herramienta de una en
    una, la integraremos en Lumen paso a paso, la ejecutaremos y observaremos
    qué cambia. Al final tendrás una lista de comprobación mental: *¿es esta
    lectura caliente? cachéala; ¿va esta llamada por la red? ponle una valla.*
    La versión de PyFly usada a lo largo del libro es **v26.6.110** — cada
    comando y clave de configuración de abajo coincide con esa versión.

---

## Cachear la ruta de lectura

### ¿Por qué cachear las lecturas de monedero?

!!! note "Nuevo término: caché"
    Una *caché* es una pequeña y rápida zona de almacenamiento donde guardas la
    respuesta a una pregunta costosa para poder devolverla al instante la
    próxima vez que alguien la formule. La primera vez que Lumen calcula el
    saldo del monedero `w-001` almacena el resultado en la caché; las lecturas
    posteriores devuelven esa copia almacenada sin volver a ejecutar la consulta
    a la base de datos. La contrapartida — y siempre hay una contrapartida — es
    que la copia almacenada puede estar ligeramente desactualizada. El resto de
    esta sección trata de mantener esa obsolescencia dentro de límites
    aceptables.

La operación más frecuente de Lumen es la consulta de saldo: "¿cuál es el saldo
actual del monedero `w-001`?". Bajo carga normal esa consulta llega a la réplica
de lectura. Bajo carga intensa compite con comandos de depósito, participantes
de saga y escrituras de instantáneas. Un saldo en caché cuesta una búsqueda en
Redis — un único viaje de ida y vuelta de red colocalizado — comparado con una
consulta SQL completa que la réplica de lectura debe además analizar,
planificar y ejecutar.

La economía es convincente, pero la caché introduce una preocupación de
corrección: el saldo en caché puede ir por detrás del saldo confirmado hasta el
límite del TTL. Para Lumen, un saldo obsoleto de cinco segundos es una
contrapartida aceptable para el tráfico de consultas normal. Cuando un depósito
se completa, el manejador invalida la entrada de caché inmediatamente, de modo
que la siguiente lectura de saldo refleja el cambio. Las actualizaciones que
pasan por la saga usan `@cache_put` para refrescar el valor en caché como efecto
secundario de la escritura, eliminando cualquier ventana de obsolescencia
visible.

::: figure art/figures/13-cache.svg | Figura 13.1 — Los decoradores de caché se sitúan delante de la capa de servicio. En un acierto el cuerpo de la función nunca se ejecuta; en un fallo se ejecuta y el resultado se almacena.

### La abstracción de caché

La capa de caché de PyFly sigue el principio hexagonal que has visto a lo largo
del libro: la lógica de negocio depende de un protocolo **`CacheAdapter`**, no
de ningún backend específico. Las implementaciones concretas — `InMemoryCache`
para desarrollo y `RedisCacheAdapter` para producción — se conectan a través del
contenedor de inyección de dependencias. Cambiar de backend no requiere ningún
cambio en la lógica de negocio.

El protocolo `CacheAdapter` define el contrato completo:

| Método | Devuelve | Descripción |
|---|---|---|
| `get(key)` | `Any \| None` | Devuelve el valor en caché, o `None` si está ausente o ha expirado. |
| `put(key, value, ttl=None)` | `None` | Almacena un valor; `ttl` es un `timedelta` o `None` para que no expire. |
| `evict(key)` | `bool` | Elimina una clave; devuelve `True` si existía. |
| `exists(key)` | `bool` | Comprueba la presencia sin obtener el valor. |
| `clear()` | `None` | Vacía toda la caché. |
| `start()` | `None` | Se llama una vez al arrancar la aplicación. |
| `stop()` | `None` | Se llama una vez al apagar la aplicación. |

Tanto `InMemoryCache` como `RedisCacheAdapter` implementan este contrato.
`InMemoryCache` almacena las entradas en un `OrderedDict` con expiración de TTL
perezosa y acotación LRU opcional; es ideal para el desarrollo en un único
proceso y para las suites de pruebas porque no tiene dependencias externas.
`RedisCacheAdapter` envuelve un cliente `redis.asyncio.Redis`, serializa los
valores a JSON antes de almacenarlos y delega la gestión del TTL en el propio
Redis — las claves expiradas desaparecen del lado del servidor sin sobrecarga
de limpieza alguna por tu parte.

### Configurar un backend de caché

Conectaremos dos backends: uno en proceso para desarrollo y uno compartido
respaldado por Redis para producción. Tómalos de uno en uno.

**Paso 1 — Elige un backend de desarrollo.** Para desarrollo, lo único que
necesitas es una sola importación:

::: listing lumen/cache/config_dev.py | Listado 13.1 — InMemoryCache para desarrollo
from pyfly.cache.adapters.memory import InMemoryCache

wallet_cache = InMemoryCache(max_size=1000)
:::

`max_size=1000` acota la ventana de expulsión LRU: una vez que la caché contiene
1000 entradas, la entrada usada menos recientemente se descarta para hacer
sitio. Pasa `None` (el valor por defecto) para dejar la caché sin límite y
depender por completo de los TTL.

!!! note "Nuevo término: LRU y TTL"
    *LRU* significa *least-recently-used* (la usada menos recientemente) —
    cuando la caché está llena, la entrada que nadie ha tocado durante más
    tiempo es la que se expulsa para hacer sitio. *TTL* significa *time-to-live*
    (tiempo de vida) — cuánto tiempo permanece válida una entrada antes de
    expirar por sí sola. `InMemoryCache` admite ambos: `max_size` limita cuántas
    entradas contiene (LRU); cada `put` puede llevar un TTL que va envejeciendo
    la entrada hasta sacarla.

**Paso 2 — Elige un backend de producción.** Para producción, apunta
`RedisCacheAdapter` a un cliente `redis.asyncio.Redis` y envuélvelo con un
fallback en memoria para que un tropiezo de Redis nunca tumbe a Lumen:

::: listing lumen/cache/config_prod.py | Listado 13.2 — RedisCacheAdapter para producción
import redis.asyncio as aioredis

from pyfly.cache import CacheAdapter, CacheManager
from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.container import bean, configuration


@configuration
class CacheConfig:

    @bean
    def wallet_cache(self) -> CacheAdapter:
        client = aioredis.from_url("redis://localhost:6379/0")
        primary = RedisCacheAdapter(client)
        fallback = InMemoryCache(max_size=500)
        return CacheManager(primary=primary, fallback=fallback)
:::

**Cómo funciona:** `CacheManager` envuelve un backend primario de Redis y un
fallback en memoria. Cada escritura va a ambas cachés, manteniendo el fallback
caliente. En las lecturas, el gestor prueba primero con Redis; si Redis lanza
una excepción registra un `WARNING` y recurre al almacén en proceso de forma
silenciosa. Cuando Redis se recupera, las nuevas escrituras lo repueblan
inmediatamente — sin intervención manual. El método `@bean` le indica al
contenedor de inyección de dependencias de PyFly que cree un singleton y lo
inyecte allá donde `CacheAdapter` se declare como dependencia.

**Lo que acaba de pasar.** Ahora tienes una interfaz `CacheAdapter` y dos formas
de satisfacerla. En desarrollo le entregas al contenedor de inyección de
dependencias un `InMemoryCache`; en producción le entregas un `CacheManager` que
pone a Redis por delante y recurre silenciosamente a la memoria cuando Redis es
inalcanzable. Cada manejador en el resto de este capítulo pide `cache:
CacheAdapter` en su constructor y nunca sabe ni le importa cuál de los dos
recibió — esa es la recompensa hexagonal.

!!! tip "Autoconfiguración"
    No tienes por qué escribir la clase `@configuration` en absoluto. Añade lo
    siguiente a `pyfly.yaml` y la `CacheAutoConfiguration` de PyFly construye un
    bean `CacheAdapter` por ti al arrancar:

    ```yaml
    pyfly:
      cache:
        enabled: true        # required to switch the subsystem on
        provider: redis      # redis | postgres | memory | auto
        redis:
          url: redis://localhost:6379/0
        max-size: 1000       # used by the memory provider
    ```

    Con `provider: redis` (o `auto`, que detecta un `redis.asyncio` instalado)
    la autoconfiguración conecta un `RedisCacheAdapter` apuntado a
    `pyfly.cache.redis.url`. Registra ese único adaptador como el bean
    `CacheAdapter` — **no** añade la capa de failover en memoria. Cuando quieras
    Redis *más* el fallback en proceso transparente que se muestra en el Listado
    13.2, declara tú mismo el `CacheManager` en una clase `@configuration` como
    arriba. La autoconfiguración también se retira por completo si ya has
    definido tu propio bean `CacheAdapter` (`@conditional_on_missing_bean`), de
    modo que los dos enfoques nunca chocan.

### @cacheable — saltarse la ejecución en un acierto

**`@cacheable`** es el decorador más común. En la primera llamada ejecuta el
cuerpo de la función y almacena el valor de retorno. En cada llamada posterior
con la misma clave devuelve el valor almacenado *sin ejecutar en absoluto el
cuerpo de la función*.

El `GetBalanceHandler` de Lumen encaja de forma natural: las lecturas de saldo
son frecuentes, baratas de cachear y toleran unos pocos segundos de
obsolescencia. Le añadiremos caché en tres pequeños pasos.

**Paso 1 — Acepta la caché.** Añade un parámetro `cache: CacheAdapter` al
constructor. El contenedor de inyección de dependencias de PyFly ve el tipo e
inyecta el backend que hayas conectado en la sección anterior.

**Paso 2 — Mueve el trabajo real a un método privado.** Renombra el cuerpo que
llega a la base de datos a `_fetch`. Esta es la función que la caché envolverá.

**Paso 3 — Envuélvelo en el momento de la construcción.** Dentro de `__init__`,
establece `self.do_handle = cacheable(...)(self._fetch)`. Envolvemos dentro de
`__init__` (en lugar de como un decorador `@cacheable` sobre el método) por una
razón: el argumento `backend=cache` solo existe una vez que `cache` ha sido
inyectado, y eso no ocurre hasta que se ejecuta `__init__`.

El manejador recibe `CacheAdapter` a través de su constructor — inyectado por
PyFly — y envuelve `do_handle` en el momento de la construcción:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listado 13.3 — @cacheable en GetBalanceHandler
from datetime import timedelta

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cacheable
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    """Return a cached :class:`BalanceDto`; bypass the DB on a hit."""

    def __init__(
        self,
        repository: WalletRepository,
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        # Wrap do_handle at construction time so `cache` is in scope.
        self.do_handle = cacheable(
            backend=cache,
            key="wallet:balance:{query.wallet_id}",
            ttl=timedelta(seconds=5),
        )(self._fetch)

    async def _fetch(
        self, query: GetBalance
    ) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

!!! note "La plantilla de clave y `self`"
    La plantilla `key` `"wallet:balance:{query.wallet_id}"` usa la sintaxis
    `str.format` de Python. PyFly enlaza los argumentos reales de la llamada con
    `inspect.signature(func).bind(*args, **kwargs)` y después llama a
    `key.format(**bound.arguments)`. Como `_fetch` se envuelve dentro de
    `__init__`, el primer argumento posicional es `query` — de modo que
    `{query.wallet_id}` se expande al id del monedero. Llamar con
    `GetBalance(wallet_id="wlt-001")` produce la clave de caché
    `"wallet:balance:wlt-001"`. La función de mapeo `entity_to_balance_dto` pasa
    por `Mapper.project` contra la interfaz `BalanceView` marcada con
    `@projection`, copiando solo los campos que la vista de saldo declara y
    calculando `balance` a partir de `balance_minor`.

**`ttl=timedelta(seconds=5)`** significa que la entrada de caché expira cinco
segundos después de escribirse. Tras la expiración, la siguiente llamada vuelve
a ejecutar el cuerpo de la función y refresca la entrada. Un TTL de `None` (el
valor por defecto) significa que la entrada nunca expira — apropiado solo para
datos verdaderamente inmutables.

**Caché de nulos:** Cuando la función devuelve `None`, PyFly aun así almacena la
entrada y registra que la clave *existe*. Una llamada posterior encuentra la
clave y devuelve `None` sin tocar la base de datos. Esto previene ataques de
penetración de caché en los que un adversario inunda con peticiones de claves
inexistentes, cada una de las cuales de otro modo se filtraría hasta la base de
datos.

**`condition` y `unless`:** Tanto `@cache` como `@cacheable` aceptan predicados
opcionales. `condition` es un invocable con la misma firma que la función
decorada; si devuelve `False`, se omite la caché para esa llamada. `unless` es
un invocable que recibe el *resultado*; si devuelve `True`, el resultado se
devuelve pero no se almacena. Ambos son de solo palabra clave:

```python
cacheable(
    backend=cache,
    key="wallet:balance:{query.wallet_id}",
    ttl=timedelta(seconds=5),
    condition=lambda query: not query.wallet_id.startswith("test-"),
    unless=lambda result: result is None,
)(self._fetch)
```

#### Ejecútalo — demuestra que la segunda lectura se salta la base de datos

La forma más limpia de *ver* un acierto de caché es una prueba unitaria que
cuente cuántas veces se llama al repositorio. Usa un `InMemoryCache` real (sin
necesidad de Redis) y un repositorio de prueba minúsculo:

::: listing tests/cache/test_get_balance_cache.py | Listado 13.3a — Una prueba que demuestra que la segunda lectura es un acierto
from datetime import timedelta

import pytest

from pyfly.cache import cacheable
from pyfly.cache.adapters.memory import InMemoryCache


class _CountingRepo:
    """Stub repository that records how many times it is queried."""

    def __init__(self) -> None:
        self.calls = 0

    async def find_by_id(self, wallet_id: str) -> dict:
        self.calls += 1
        return {"wallet_id": wallet_id, "balance_minor": 500}


@pytest.mark.asyncio
async def test_second_read_is_a_cache_hit() -> None:
    repo = _CountingRepo()
    cache = InMemoryCache(max_size=10)

    fetch = cacheable(
        backend=cache,
        key="wallet:balance:{wallet_id}",
        ttl=timedelta(seconds=5),
    )(repo.find_by_id)

    first = await fetch("wlt-001")   # miss -> runs the repo
    second = await fetch("wlt-001")  # hit  -> repo NOT called again

    assert first == second
    assert repo.calls == 1           # the body ran exactly once
:::

Ejecuta solo esta prueba:

```console
$ uv run --extra dev pytest tests/cache/test_get_balance_cache.py -q
.                                                                        [100%]
1 passed in 0.04s
```

El único `.` y el `1 passed` lo confirman: la segunda llamada devolvió el valor
en caché y `repo.calls` se quedó en `1`, de modo que la base de datos se tocó
exactamente una vez en dos lecturas. Eso es un acierto de caché, demostrado en
lugar de afirmado en prosa.

**Lo que acaba de pasar.** Envolviste una función async sencilla con `cacheable`,
la respaldaste con un `InMemoryCache` y confirmaste que claves idénticas
cortocircuitan el cuerpo. En `GetBalanceHandler` la función envuelta es `_fetch`
y el backend es el `CacheAdapter` inyectado, pero la mecánica es exactamente la
que acabas de ejecutar.

!!! spring "Equivalencia con Spring"
    `@cacheable` refleja la `@Cacheable` de Spring. La plantilla `key` usa la sintaxis `str.format` de Python en lugar de SpEL, pero la semántica — saltarse en acierto, almacenar en fallo, `condition`, `unless` — es idéntica. `@cache` es un alias de más bajo nivel que se comporta igual; usa el nombre que mejor se lea en tu base de código.

### @cache_put — ejecutar siempre, almacenar siempre

`@cacheable` es para lecturas: cortocircuita la función cuando la caché ya
contiene un valor. **`@cache_put`** es para escrituras: *siempre* ejecuta la
función y *siempre* almacena el resultado. Úsalo cuando la función es la fuente
de la verdad — un manejador de comandos que modifica el monedero y debe mantener
la caché actualizada.

`DepositFundsHandler` es el ejemplo canónico. Después de que un depósito tiene
éxito, el nuevo saldo debe ser visible para la siguiente lectura sin esperar a
que el TTL expire. El cableado refleja lo que hiciste para `@cacheable`, con un
detalle crítico que vigilar:

**Paso 1 — Acepta la caché** en el constructor, exactamente como antes.

**Paso 2 — Mueve la lógica de depósito a `_deposit`** y conserva su decorador
`@transactional()` para que la escritura siga confirmándose como una unidad de
trabajo.

**Paso 3 — Envuelve con `cache_put`, reutilizando la *misma* forma de clave.**
El manejador de depósitos debe escribir en la mismísima ranura de caché que el
lector de saldos consulta. `@cacheable` usa `"wallet:balance:{query.wallet_id}"`;
aquí el argumento se llama `command`, así que la plantilla es
`"wallet:balance:{command.wallet_id}"`. Distintos nombres de parámetro, pero
ambos resuelven a `wallet:balance:wlt-001`.

Envolver `do_handle` con `@cache_put` refresca la entrada de caché de forma
atómica con la escritura:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 13.4 — @cache_put refresca la caché en un depósito
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cache_put
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance
    in minor units and refreshes the cached balance entry."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory
        # Wrap at construction time so `cache` is in scope.
        self.do_handle = cache_put(
            backend=cache,
            key="wallet:balance:{command.wallet_id}",
            ttl=timedelta(seconds=5),
        )(self._deposit)

    @transactional()
    async def _deposit(self, command: DepositFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

**Cómo funciona:** `@cache_put` espera (await) a la función envuelta, después
llama a `backend.put(resolved_key, result, ttl=ttl)`. Como la función siempre se
ejecuta, el valor en caché tras un comando `DepositFunds` es el saldo recién
confirmado — no una instantánea obsoleta anterior al depósito. `_deposit` se
ejecuta dentro de `@transactional()`, de modo que la secuencia `find_by_id →
to_aggregate → mutate → upsert` se confirma como una unidad de trabajo antes de
que se refresque la caché. La siguiente lectura `@cacheable` en
`GetBalanceHandler` recoge este valor fresco sin tocar la base de datos.

!!! note "La clave de caché debe coincidir"
    La clave `@cache_put` `"wallet:balance:{command.wallet_id}"` debe coincidir con la clave `@cacheable` `"wallet:balance:{query.wallet_id}"` cuando ambas resuelven al mismo id de monedero. Claves que no coinciden significan que el depósito escribe en una ranura de caché distinta de la que consulta la lectura de saldo — la obsolescencia regresa.

| Decorador | ¿Se ejecuta la función? | En un acierto |
|---|---|---|
| `@cacheable` / `@cache` | Solo en un fallo | Devuelve el valor en caché |
| `@cache_put` | Siempre | Reemplaza el valor en caché con el resultado fresco |

### @cache_evict — eliminar tras un borrado

Cuando cierras un monedero o reviertes una transacción, la entrada de caché
asociada debe eliminarse. **`@cache_evict`** ejecuta primero el cuerpo de la
función y después elimina la clave indicada — o vacía toda la caché cuando
`all_entries=True`.

::: listing lumen/core/services/wallets/close_wallet_handler.py | Listado 13.5 — @cache_evict tras eliminar un monedero
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.cache import CacheAdapter, cache_evict
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional


@command_handler
@service
class CloseWalletHandler(CommandHandler["CloseWallet", None]):
    """Close a wallet and evict its cached balance entry."""

    def __init__(
        self,
        repository: WalletRepository,
        cache: CacheAdapter,
    ) -> None:
        super().__init__()
        self._repository = repository
        self.do_handle = cache_evict(
            backend=cache,
            key="wallet:balance:{command.wallet_id}",
        )(self._close)

    @transactional()
    async def _close(self, command) -> None:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is not None:
            await self._repository.delete(entity)
:::

Para vaciar de golpe cada saldo en caché — útil para un reinicio administrativo
— pasa `all_entries=True`:

```python
self.do_handle = cache_evict(
    backend=cache,
    all_entries=True,
)(self._reset_all)
```

**Cómo funciona:** El cuerpo de la función se ejecuta primero —
`repository.delete(entity)` elimina la fila antes de la expulsión, de modo que
un fallo no descarta prematuramente la entrada de caché. Después, o bien
`backend.evict(resolved_key)` elimina una clave o bien `backend.clear()` vacía
todo. Con `CacheManager`, la expulsión se propaga tanto a la caché primaria como
a la de fallback, de modo que no queda ninguna entrada obsoleta en ninguno de
los dos niveles.

`all_entries=True` es un instrumento contundente reservado para reinicios
administrativos. En el funcionamiento normal, prefiere la expulsión dirigida por
clave.

### Estrategia de invalidación

Una estrategia coherente empareja cada operación con el decorador correcto:

| Operación | Decorador | Razón |
|---|---|---|
| Consulta `GetBalance` | `@cacheable` | Salta la BD en acierto; el TTL de 5 s acota la obsolescencia |
| Comando `DepositFunds` | `@cache_put` | Refresca la entrada de caché de forma atómica con la escritura |
| Comando `WithdrawFunds` | `@cache_put` | Igual — mantén caliente el saldo posterior a la retirada |
| Cerrar monedero | `@cache_evict` | Elimina la entrada; la siguiente lectura la reconstruye desde la BD |
| Truncado de admin | `@cache_evict(all_entries=True)` | Reinicio masivo; un vaciado completo de la caché es lo correcto |

!!! warning "Requisito de asincronía"
    Los tres decoradores requieren que la función envuelta esté declarada `async`. Los adaptadores de caché son totalmente asíncronos (esperan con `await` las operaciones del backend), de modo que un objetivo síncrono fallará con un `TypeError` en el momento de la decoración — PyFly lanza el error inmediatamente para que cojas el error al arrancar en lugar de en tiempo de ejecución.

---

## Patrones de resiliencia

### Por qué importa la protección

!!! note "Nuevo término: resiliencia"
    *Resiliencia* aquí significa que el sistema sigue sirviendo las peticiones
    que *puede* servir incluso cuando una dependencia de la que depende está
    lenta, sobrecargada o caída. Las herramientas de esta sección no hacen que
    el servicio descendente sea más rápido — impiden que una dependencia enferma
    enferme a Lumen entero. Cada herramienta es, de nuevo, un decorador que
    apilas sobre la función que realiza la llamada arriesgada.

La caché hace rápido el camino feliz. Los patrones de resiliencia protegen a
Lumen cuando el camino feliz no está disponible. Sin protección, un
`AccountService` lento desencadena una cascada:

1. Las peticiones de los manejadores de monederos se acumulan, cada una
   esperando una respuesta HTTP.
2. El bucle de eventos asyncio de Lumen — de un solo hilo por defecto — procesa
   las tareas pendientes en orden; una acumulación de llamadas HTTP lentas
   retrasa cualquier otra operación.
3. La memoria y los descriptores de archivo abiertos suben a medida que las
   corrutinas se apilan.
4. Lumen se vuelve no disponible para peticiones que no tienen nada que ver con
   `AccountService`.

Cuatro patrones complementarios rompen esta cascada antes de que empiece:

::: figure art/figures/13-resilience.svg | Figura 13.2 — Cuatro capas de resiliencia guardan la llamada saliente. El limitador de tasa descarta el tráfico excedente antes de que entre en el sistema; el bulkhead limita la concurrencia; el tiempo límite cancela las operaciones lentas; el fallback proporciona una respuesta segura cuando todo lo demás falla.

| Patrón | Protege contra | ¿Fallo rápido o espera? |
|---|---|---|
| **Limitador de tasa** | Picos de tráfico que abruman al servicio descendente | Fallo rápido (rechaza el exceso) |
| **Bulkhead** | Demasiadas llamadas concurrentes que atan recursos | Fallo rápido (rechaza por encima del límite) |
| **Limitador de tiempo** | Llamadas colgadas que nunca regresan | Cancela tras el tiempo límite |
| **Fallback** | Cualquier fallo que llegue al llamante | Devuelve un valor degradado |

Los cuatro están en `pyfly.resilience`:

```python
from pyfly.resilience import (
    RateLimiter, rate_limiter,
    Bulkhead, bulkhead,
    time_limiter,
    fallback,
)
```

### Limitador de tasa — cubo de tokens

`RateLimiter` usa un **cubo de tokens**: el cubo contiene hasta `max_tokens`
tokens y se rellena a `refill_rate` tokens por segundo. Cada llamada consume un
token. Cuando el cubo está vacío, se lanza `RateLimitException` inmediatamente —
sin cola, sin espera.

::: listing lumen/resilience/rate_example.py | Listado 13.6 — Limitador de tasa de cubo de tokens en las búsquedas de cuentas
from pyfly.resilience import RateLimiter, rate_limiter

# Sustained: 20 calls/s; burst: up to 40
account_limiter = RateLimiter(max_tokens=40, refill_rate=20.0)


@rate_limiter(account_limiter)
async def fetch_account(account_id: str) -> dict:
    # This body is reached only when a token is available.
    ...
:::

**Cómo funciona:** `@rate_limiter(limiter)` llama a `await limiter.acquire()`
antes de cada invocación. `acquire()` rellena el cubo según el tiempo de reloj
transcurrido (usando `time.monotonic()`), después comprueba y decrementa
atómicamente el recuento de tokens bajo un `threading.Lock` — no un lock de
asyncio — de modo que tanto las tareas async como los llamantes síncronos
comparten el mismo recuento sin condiciones de carrera. Si quedan menos de 1.0
tokens, `RateLimitException` se propaga al llamante.

La forma de cubo de tokens permite ráfagas controladas: un servicio que
normalmente ve 10 llamadas por segundo puede absorber una ráfaga de 40 llamadas
inmediatamente (recurriendo a tokens ahorrados) y después sostiene 20 llamadas
por segundo a partir de ahí. Los limitadores de tasa de ventana fija no pueden
expresar este matiz.

#### Ejecútalo — observa cómo el cubo se vacía

Un pequeño script hace concreto el comportamiento. Crea un limitador con un cubo
diminuto, llama más allá de su capacidad y observa el rechazo:

::: listing scratch/rate_demo.py | Listado 13.6a — Vaciar el cubo de tokens a propósito
import asyncio

from pyfly.kernel.exceptions import RateLimitException
from pyfly.resilience import RateLimiter, rate_limiter

# 3 tokens, refilling slowly so the burst is what we observe.
limiter = RateLimiter(max_tokens=3, refill_rate=1.0)


@rate_limiter(limiter)
async def ping(n: int) -> str:
    return f"ok-{n}"


async def main() -> None:
    for n in range(5):
        try:
            print(await ping(n))
        except RateLimitException:
            print(f"rejected-{n}")


asyncio.run(main())
:::

Ejecútalo directamente:

```console
$ uv run python scratch/rate_demo.py
ok-0
ok-1
ok-2
rejected-3
rejected-4
```

Las primeras tres llamadas gastan cada una un token; la cuarta y la quinta
llegan con un cubo vacío y se rechazan inmediatamente con `RateLimitException` —
sin cola, sin espera. Ralentiza el bucle (o sube `refill_rate`) y los rechazos
desaparecen porque los tokens se rellenan entre llamadas.

**Lo que acaba de pasar.** No cambiaste la función `ping` en absoluto — la
decoraste. El decorador insertó un `await limiter.acquire()` antes de cada
llamada, y `acquire()` lanzó cuando el cubo estaba vacío. Esta es la forma que
adopta cada herramienta de resiliencia de este capítulo: un decorador que guarda
la llamada sin que el cuerpo de la función sepa que existe.

Múltiples funciones que comparten una instancia `RateLimiter` imponen una tasa
*global* a través de todas ellas — útil para limitar el tráfico total hacia un
servicio descendente con independencia de qué método interno inicie la llamada.

### Bulkhead — aislamiento de concurrencia

!!! note "Nuevo término: bulkhead"
    El nombre viene de la construcción naval: el casco de un barco se divide en
    compartimentos estancos (*bulkheads*, mamparos) de modo que una brecha en
    uno no inunde toda la nave. Un bulkhead de software limita cuántas llamadas
    a una dependencia pueden ejecutarse a la vez, de modo que una avalancha de
    llamadas lentas a `AccountService` no pueda consumir cada corrutina y hundir
    peticiones no relacionadas.

`Bulkhead` es un semáforo: limita el número de llamadas *en vuelo al mismo
tiempo*. Las llamadas que superen `max_concurrent` se rechazan inmediatamente
con `BulkheadException`.

::: listing lumen/resilience/bulkhead_example.py | Listado 13.7 — Bulkhead que limita las llamadas concurrentes al servicio de cuentas
from pyfly.resilience import Bulkhead, bulkhead

# At most 5 concurrent calls to AccountService
account_bulkhead = Bulkhead(max_concurrent=5)


@bulkhead(account_bulkhead)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**Cómo funciona:** El decorador adquiere un permiso (`_acquire_slot`) antes de
entrar en la función y lo libera (`_release_slot`) en un bloque `finally`, de
modo que la ranura siempre se devuelve incluso cuando la función lanza. Las
ranuras se rastrean con un único contador entero protegido por lock compartido
por las rutas de llamada async y síncrona, de modo que una instancia `Bulkhead`
decora con seguridad una mezcla de corrutinas y funciones normales.

Este comportamiento de fallo rápido es intencionado: cuando hay 5 llamadas
concurrentes en vuelo y llega una 6.ª, rechazarla inmediatamente permite al
llamante reintentar o invocar un fallback — mucho mejor que ponerla en cola
indefinidamente y provocar contrapresión en cascada.

!!! tip "Monitorizar la utilización del bulkhead"
    `account_bulkhead.available_slots` devuelve el número de permisos libres en cualquier momento. Expón esto en un endpoint de salud o aliméntalo a tu pila de observabilidad para detectar la saturación persistente antes de que se convierta en una caída.

### Limitador de tiempo — imponer una fecha límite

Un servicio descendente lento es a veces peor que uno caído: las llamadas que
bloquean indefinidamente consumen recursos sin límite. **`@time_limiter`**
cancela la corrutina si no se completa dentro de un `timedelta`:

::: listing lumen/resilience/timeout_example.py | Listado 13.8 — Fecha límite de 2 segundos en la búsqueda de cuenta
from datetime import timedelta

from pyfly.resilience import time_limiter


@time_limiter(timeout=timedelta(seconds=2))
async def fetch_account(account_id: str) -> dict:
    ...
:::

**Cómo funciona:** Internamente, `time_limiter` llama a
`asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)`. Cuando pasa
la fecha límite, `asyncio.wait_for` cancela la tarea subyacente, provocando que
cualquier `await` dentro de la función lance `asyncio.CancelledError`. El
decorador captura `TimeoutError` y lo relanza como `OperationTimeoutException`
con un mensaje descriptivo:

```
OperationTimeoutException: fetch_account exceeded timeout of 2.0s
```

Los recursos adquiridos dentro de la función con límite de tiempo deberían
guardarse con `try/finally` para que se liberen incluso en una cancelación:

```python
@time_limiter(timeout=timedelta(seconds=2))
async def fetch_account(account_id: str) -> dict:
    conn = await pool.acquire()
    try:
        return await conn.execute(query, account_id)
    finally:
        await pool.release(conn)
```

### Fallback — degradación elegante

**`@fallback`** es la red de seguridad de la capa más externa: captura
excepciones y devuelve una respuesta alternativa en lugar de propagar el error
al llamante. El endpoint de resumen de saldo de Lumen puede devolver una
respuesta degradada — el último saldo conocido, marcado como potencialmente
obsoleto — en lugar de un HTTP 500 cuando `AccountService` está caído.

Hay dos modos disponibles. El primero devuelve un **valor estático**:

::: listing lumen/resilience/fallback_static.py | Listado 13.9 — Valor de fallback estático
from pyfly.resilience import fallback


@fallback(fallback_value={"balance_minor": 0, "source": "fallback"})
async def fetch_account(account_id: str) -> dict:
    ...
:::

El segundo invoca un **método de fallback** que recibe los argumentos originales más la excepción:

::: listing lumen/resilience/fallback_method.py | Listado 13.10 — Método de fallback con datos en caché
from pyfly.cache import CacheAdapter
from pyfly.resilience import fallback


_cache: CacheAdapter  # injected elsewhere


async def account_from_cache(
    account_id: str,
    exc: Exception = None,
) -> dict:
    cached = await _cache.get(f"account:{account_id}")
    if cached:
        return {**cached, "source": "cache"}
    return {"account_id": account_id, "balance_minor": 0, "source": "fallback"}


@fallback(fallback_method=account_from_cache)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**Cómo funciona:** Cuando la función primaria lanza uno de los tipos de excepción
listados en `on` (por defecto: todas las subclases de `Exception`), el decorador
llama a `fallback_method(*args, exc=exc, **kwargs)`. El argumento de palabra
clave `exc` lleva la excepción capturada para que el fallback pueda registrarla,
inspeccionar su tipo o devolver valores distintos para distintos modos de fallo.
Si el método de fallback devuelve una corrutina, PyFly la espera con await
automáticamente. Acota el filtro de excepciones con
`on=(OperationTimeoutException, CircuitBreakerException)` para dejar que los
errores de programación se propaguen con normalidad.

!!! warning "Firma del método de fallback"
    El método de fallback debe aceptar `exc` como argumento de palabra clave. PyFly pasa la excepción capturada como `exc=<exception>`. Si la firma de tu método de fallback no incluye `exc`, verás un `TypeError` con un mensaje claro en el primer fallo — no en el momento de la decoración.

---

## Reintento y cortacircuitos

### @retry — reintentos acotados con backoff

Los errores de red a menudo son transitorios: se pierde un paquete, un pool de
conexiones se agota momentáneamente, un pod descendente se reinicia.
**`@retry`** reinvoca la función decorada hasta `max_attempts` veces con backoff
exponencial entre intentos.

`max_attempts` es el único argumento posicional; todos los demás parámetros son
de solo palabra clave:

::: listing lumen/resilience/retry_example.py | Listado 13.11 — Reintento con backoff exponencial
from pyfly.resilience import retry


@retry(
    max_attempts=3,
    delay=0.1,
    backoff=2.0,
    max_delay=2.0,
    exceptions=(IOError, TimeoutError),
)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**Cómo funciona:** El decorador ejecuta la función, captura las excepciones que
coinciden con `exceptions`, duerme `delay * backoff ** attempt` segundos
(limitado a `max_delay`) y vuelve a intentar. En el último intento relanza la
última excepción. La pausa usa `await asyncio.sleep(...)` para funciones async y
`time.sleep(...)` para funciones síncronas — la misma implementación maneja
ambas. El parámetro `jitter` añade aleatorización para evitar reintentos en
estampida (thundering-herd) cuando muchas instancias se reinician
simultáneamente.

| Parámetro | Por defecto | Descripción |
|---|---|---|
| `max_attempts` | `3` | Total de intentos incluyendo el primero (≥ 1). Posicional. |
| `delay` | `0.0` | Pausa base en segundos antes del primer reintento. Solo palabra clave. |
| `backoff` | `1.0` | Multiplicador aplicado a `delay` en cada intento. Solo palabra clave. |
| `max_delay` | `None` | Tope de la pausa por intento. `None` significa sin tope. Solo palabra clave. |
| `jitter` | `0.0` | Fracción de aleatorización `[0, 1]` aplicada a cada espera. Solo palabra clave. |
| `exceptions` | `(Exception,)` | Tipos de excepción que disparan un reintento; los demás se propagan inmediatamente. Solo palabra clave. |

!!! warning "La idempotencia es tu responsabilidad"
    `@retry` llamará al cuerpo de la función varias veces. Si la operación no es idempotente — si llamarla dos veces tiene un efecto distinto de llamarla una vez — puedes aplicar cambios más de una vez. Los depósitos de monedero no son seguros de reintentar ingenuamente: reintentar un depósito fallido podría abonar la misma cantidad dos veces. Envuelve las operaciones no idempotentes en una comprobación de clave de idempotencia (almacena el ID de la operación antes de ejecutar; sáltala si el ID ya existe) o limita `exceptions` a errores que sean definitivamente previos a la ejecución (errores de conexión, tiempos de espera durante la fase de petición) en lugar de la ambigüedad posterior a la ejecución.

### @circuit_breaker — fallo rápido bajo una caída sostenida

!!! note "Nuevo término: cortacircuitos"
    Tomado prestado del cableado eléctrico: un cortacircuitos *salta* (se abre)
    cuando fluye demasiada corriente, cortando el circuito antes de que el
    cableado se sobrecaliente. Un cortacircuitos de software salta tras
    demasiados fallos, cortando las llamadas a una dependencia que falla para
    que dejes de machacarla — y para que tus propios llamantes fallen rápido en
    lugar de esperar en llamadas que de todos modos están condenadas a errar.

Reintentar un servicio genuinamente no disponible amplifica la carga
precisamente en el momento en que ese servicio más necesita alivio. El patrón
cortacircuitos resuelve esto: tras un umbral de fallos consecutivos el circuito
se **abre** y las llamadas posteriores se rechazan inmediatamente — sin intentar
la llamada remota — hasta que transcurre un tiempo de espera de recuperación.

El cortacircuitos de PyFly tiene tres estados:

| Estado | Comportamiento |
|---|---|
| **CLOSED** | Funcionamiento normal. Cada llamada pasa; los fallos se cuentan. |
| **OPEN** | Todas las llamadas lanzan `CircuitBreakerException` inmediatamente, sin E/S de red. |
| **HALF_OPEN** | Tras `recovery_timeout` segundos, se admite una llamada de sondeo limitada. Si tiene éxito el circuito se cierra; si falla el circuito se vuelve a abrir. |

`@circuit_breaker` toma una **instancia** `CircuitBreaker` — no argumentos de
palabra clave. Construye el `CircuitBreaker` por separado y pásalo:

::: listing lumen/resilience/cb_example.py | Listado 13.12 — Cortacircuitos alrededor de AccountService
from pyfly.resilience import CircuitBreaker, circuit_breaker

account_cb = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    expected=(IOError, TimeoutError),
)


@circuit_breaker(account_cb)
async def fetch_account(account_id: str) -> dict:
    ...
:::

**Cómo funciona:** Antes de cada llamada, `breaker.before_call()` comprueba el
estado actual. Si está OPEN, lanza `CircuitBreakerException` inmediatamente. Si
está HALF_OPEN y el presupuesto de sondeo está agotado, también lanza. En caso
contrario la llamada procede. En caso de éxito, `breaker.on_success()` reinicia
el contador de fallos consecutivos (o, en HALF_OPEN, cierra el circuito una vez
que suficientes sondeos tienen éxito). En caso de fallo, `breaker.on_failure()`
incrementa el contador y abre el circuito cuando se alcanza `failure_threshold`.

Solo las excepciones en `expected` disparan el cortacircuitos. Las excepciones
de negocio — `ValueError`, `PermissionError` — se propagan con normalidad sin
afectar al estado del circuito.

**Parámetros del constructor de `CircuitBreaker`** (`failure_rate_threshold`,
`window_size` y `half_open_max_calls` son de solo palabra clave):

| Parámetro | Por defecto | Descripción |
|---|---|---|
| `failure_threshold` | `5` | Fallos consecutivos que hacen saltar el circuito. |
| `recovery_timeout` | `30.0` | Segundos en OPEN antes de pasar a HALF_OPEN. |
| `expected` | `(Exception,)` | Tipos de excepción que cuentan como fallos. |
| `failure_rate_threshold` | `None` | Cambia al modo de tasa por ventana cuando se establece (p. ej. `0.5`). |
| `window_size` | `10` | Tamaño de la ventana de resultados para el salto basado en tasa. |
| `half_open_max_calls` | `1` | Llamadas de sondeo requeridas para cerrar desde HALF_OPEN. |

Los parámetros `failure_rate_threshold` y `window_size` cambian del modo de
recuento consecutivo al modo de tasa por ventana, igual que la ventana
deslizante COUNT_BASED de Resilience4j. Establece `failure_rate_threshold=0.5` y
`window_size=10` para abrir el circuito cuando más de la mitad de las últimas 10
llamadas fallen.

!!! spring "Equivalencia con Spring"
    `@retry` refleja la `@Retryable` de Spring Retry (con `maxAttempts`, `backoff`, `include`). `CircuitBreaker` refleja el `CircuitBreaker` de Resilience4j (umbral de fallos, tiempo de recuperación, máquina de estados CLOSED/OPEN/HALF_OPEN, llamadas de sondeo en half-open, filtro de excepciones esperadas). PyFly no usa la biblioteca Java de Resilience4j — es una reimplementación en Python puro con la misma semántica.

### Configurar la resiliencia desde `pyfly.yaml`

Hasta ahora has construido cada `RateLimiter`, `Bulkhead` y `CircuitBreaker` en
código. Eso es perfecto para una única pasarela, pero los equipos de operaciones
suelen querer ajustar estos umbrales *sin un cambio de código* — subir un tiempo
límite, ampliar un límite de tasa — y quieren un lugar evidente donde leer la
configuración actual. PyFly v26.6.110 incluye un **`ResilienceRegistry`**
dirigido por configuración para exactamente esto, dando paridad con el modelo de
registro con nombre de Resilience4j.

**Paso 1 — Declara instancias con nombre en `pyfly.yaml`.** Cada entrada bajo
`pyfly.resilience.*` se convierte en una instancia con nombre. Los nombres son
tuyos a elegir; agrúpalos por el servicio descendente que protegen:

```yaml
pyfly:
  resilience:
    circuit-breaker:
      account-api:
        failure-threshold: 5
        recovery-timeout: 30s
        # or switch to windowed-rate mode:
        # failure-rate-threshold: 0.5
        # window-size: 10
    rate-limiter:
      account-api:
        max-tokens: 50
        refill-rate: 20.0
    bulkhead:
      account-api:
        max-concurrent: 8
    time-limiter:
      account-api:
        timeout: 2s
```

Las duraciones aceptan sufijos amigables — `30s`, `500ms`, `1m`, `2h` — o un
número desnudo leído como segundos. Las claves usan kebab-case
(`failure-threshold`); el enlazado relajado de PyFly también acepta snake_case.

**Paso 2 — Inyecta el registro y busca las instancias por nombre.** La
`ResilienceAutoConfiguration` de PyFly registra un único bean
`ResilienceRegistry` construido a partir de esas claves (siempre está activo, y
devuelve un registro vacío cuando no hay claves presentes). Pídelo en cualquier
constructor `@service`:

::: listing lumen/account/gateway_configured.py | Listado 13.12a — Obtener instancias de resiliencia del registro
from pyfly.container import service
from pyfly.resilience import (
    ResilienceRegistry,
    bulkhead,
    circuit_breaker,
    rate_limiter,
)


@service
class AccountGateway:

    def __init__(self, http_client, registry: ResilienceRegistry) -> None:
        self._http = http_client
        # Look up the named instances declared in pyfly.yaml.
        cb = registry.circuit_breaker("account-api")
        rl = registry.rate_limiter("account-api")
        bh = registry.bulkhead("account-api")

        # Wrap the real call with the config-driven instances.
        guarded = circuit_breaker(cb)(self._raw_get)
        guarded = bulkhead(bh)(guarded)
        self.get_account = rate_limiter(rl)(guarded)

    async def _raw_get(self, account_id: str) -> dict:
        resp = await self._http.get(f"/accounts/{account_id}")
        return resp.json()
:::

**Lo que acaba de pasar.** Los umbrales ahora viven en la configuración, no en
literales de Python. Un `CircuitBreaker` llamado `account-api` se materializa una
vez al arrancar y se comparte por todo lo que lo busca — de modo que los
recuentos de fallos y el estado OPEN/CLOSED son *globales* a través de todos los
llamantes de ese nombre, exactamente como una instancia compartida en código.
Buscar un nombre desconocido lanza `KeyError` con la lista de nombres
disponibles, de modo que una errata falla ruidosamente al arrancar en lugar de
crear silenciosamente una ruta sin protección.

!!! tip "El limitador de tiempo devuelve un timedelta"
    `registry.time_limiter("account-api")` devuelve el **`timedelta`** configurado, no un decorador — pásalo directamente a `time_limiter(timeout=registry.time_limiter("account-api"))`. Los otros tres accesores (`circuit_breaker`, `rate_limiter`, `bulkhead`) devuelven la instancia que pasas al decorador correspondiente.

!!! spring "Equivalencia con Spring"
    El `ResilienceRegistry` refleja el `CircuitBreakerRegistry`, el `RateLimiterRegistry` y el `BulkheadRegistry` de Resilience4j — instancias con nombre declaradas en la configuración y buscadas en tiempo de ejecución. El `resilience4j.circuitbreaker.instances.<name>.*` de Spring Boot se convierte en `pyfly.resilience.circuit-breaker.<name>.*`; los nombres de las propiedades se alinean uno a uno.

---

## Componer las capas

### Orden de los decoradores

Los decoradores de resiliencia de PyFly se componen apilándose. Python aplica
los decoradores de abajo arriba en el momento de la decoración pero los ejecuta
de arriba abajo en el momento de la llamada. El orden recomendado, del más
externo al más interno:

```
@fallback           ← 1. Catch any exception; return degraded response
@rate_limiter       ← 2. Reject excess traffic before it acquires resources
@bulkhead           ← 3. Limit concurrency of rate-limited calls
@time_limiter       ← 4. Cancel if execution takes too long
async def func()    ← 5. The actual operation
```

Este orden garantiza:

1. **Fallback** captura las excepciones de cada capa interna — incluyendo
   `RateLimitException`, `BulkheadException` y `OperationTimeoutException` — de
   modo que el llamante siempre recibe una respuesta utilizable.
2. **Limitador de tasa** descarta las peticiones excedentes antes de que
   consuman una ranura del bulkhead, evitando que una avalancha de tráfico agote
   el presupuesto de concurrencia.
3. **Bulkhead** limita cuántas llamadas permitidas por la tasa se ejecutan
   concurrentemente, protegiendo al servicio descendente de la sobrecarga.
4. **Limitador de tiempo** se aplica solo a la ejecución real; cuando se
   dispara, el bloque `finally` del bulkhead libera la ranura correctamente.

Añade `@retry` y `@circuit_breaker` en el lado más interno — envolviendo solo la
llamada de E/S real — de modo que el fallback absorba sus excepciones y el
limitador de tasa y el bulkhead contabilicen correctamente las llamadas
reintentadas:

```
@fallback
@rate_limiter
@bulkhead
@time_limiter
@circuit_breaker(account_cb)
@retry(max_attempts=2, delay=0.05, backoff=2.0, exceptions=(IOError,))
async def fetch_account(account_id: str) -> dict: ...
```

Con `@retry` por debajo de `@time_limiter`, el presupuesto de tiempo límite
cubre toda la secuencia de reintentos, no cada intento individual. Para acotar
cada intento de forma independiente, mueve `@time_limiter` por debajo de
`@retry`.

### Juntándolo todo — la pasarela de cuentas de Lumen

Aquí está el patrón completo ensamblado en una `AccountGateway` realista que los
manejadores de monederos de Lumen usan para buscar información de cuentas:

::: listing lumen/account/gateway.py | Listado 13.13 — AccountGateway con la pila de resiliencia completa
from datetime import timedelta

from pyfly.cache import CacheAdapter, cacheable
from pyfly.container import service
from pyfly.kernel.exceptions import CircuitBreakerException, OperationTimeoutException
from pyfly.resilience import (
    Bulkhead,
    CircuitBreaker,
    RateLimiter,
    bulkhead,
    circuit_breaker,
    fallback,
    rate_limiter,
    retry,
    time_limiter,
)

_limiter = RateLimiter(max_tokens=50, refill_rate=20.0)
_bh = Bulkhead(max_concurrent=8)
_cb = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    expected=(IOError, TimeoutError),
)

DEGRADED = {"status": "degraded", "balance_minor": None}


@service
class AccountGateway:

    def __init__(self, http_client, cache: CacheAdapter) -> None:
        self._http = http_client
        self._cache = cache

    @cacheable(
        backend=None,  # pass self._cache at runtime (see note below)
        key="account:{account_id}",
        ttl=timedelta(seconds=30),
    )
    @fallback(
        fallback_value=DEGRADED,
        on=(OperationTimeoutException, CircuitBreakerException, IOError),
    )
    @rate_limiter(_limiter)
    @bulkhead(_bh)
    @time_limiter(timeout=timedelta(seconds=2))
    @circuit_breaker(_cb)
    @retry(max_attempts=2, delay=0.05, backoff=2.0, exceptions=(IOError,))
    async def get_account(self, account_id: str) -> dict:
        resp = await self._http.get(f"/accounts/{account_id}")
        return resp.json()
:::

!!! note "Cablear `backend` en un método de clase"
    Como Python evalúa los decoradores del cuerpo de la clase antes de que se ejecute `__init__`, `self._cache` aún no está disponible ahí. El listado anterior pasa `backend=None` como marcador de posición para ilustrar el orden de apilamiento. En la práctica, envuelve `get_account` en `__init__` de la misma forma que en los ejemplos de manejador: `self.get_account = cacheable(backend=cache, key=..., ttl=...)(self._do_get_account)`. Como alternativa, usa una instancia `InMemoryCache` a nivel de módulo para las pruebas y cámbiala vía el contenedor de inyección de dependencias en producción.

**Cómo fluye una llamada a través de las capas:**

1. `@cacheable` comprueba la caché. En un acierto, cada capa interna se omite por
   completo.
2. En un fallo, `@fallback` se convierte en la red de seguridad más externa.
3. `@rate_limiter` comprueba el cubo de tokens; rechaza la llamada si está vacío.
4. `@bulkhead` comprueba el contador de permisos; rechaza si está a su capacidad.
5. `@time_limiter` establece una fecha límite de dos segundos para las capas de
   abajo.
6. `@circuit_breaker` rechaza inmediatamente si el circuito está OPEN.
7. `@retry` intenta la llamada HTTP hasta dos veces ante un `IOError`.
8. En caso de éxito, `@cacheable` almacena la respuesta durante 30 segundos.
9. Si un `IOError`, `OperationTimeoutException` o `CircuitBreakerException`
   escapa, `@fallback` lo captura y devuelve `DEGRADED`.

Fíjate en que `@cacheable` se sitúa *por encima* de `@fallback`. Eso significa:

- Una respuesta `DEGRADED` en caché de un ciclo de fallo anterior se devuelve
  tal cual durante hasta 30 segundos sin llegar a la red.
- Si no quieres cachear respuestas degradadas, mueve `@cacheable` por debajo de
  `@fallback`, o usa el predicado `unless`:
  `unless=lambda r: r.get("status") == "degraded"`.

#### Ejecútalo — haz que el servicio descendente falle y observa cómo la pila se degrada

No necesitas un `AccountService` en vivo para verificar la pila. Conecta las
capas de resiliencia alrededor de un cliente HTTP de prueba que siempre lance, y
verifica que el llamante aún obtiene una respuesta `DEGRADED` utilizable en lugar
de una excepción:

::: listing tests/resilience/test_gateway_stack.py | Listado 13.13a — La pila se degrada en lugar de lanzar
import pytest

from pyfly.resilience import fallback, retry

DEGRADED = {"status": "degraded", "balance_minor": None}


class _BrokenClient:
    async def get(self, path: str) -> dict:
        raise IOError("AccountService unreachable")


@fallback(fallback_value=DEGRADED, on=(IOError,))
@retry(max_attempts=2, delay=0.0, exceptions=(IOError,))
async def get_account(client: _BrokenClient, account_id: str) -> dict:
    resp = await client.get(f"/accounts/{account_id}")
    return resp


@pytest.mark.asyncio
async def test_degrades_instead_of_raising() -> None:
    result = await get_account(_BrokenClient(), "acc-1")
    assert result == DEGRADED
:::

Ejecútalo:

```console
$ uv run --extra dev pytest tests/resilience/test_gateway_stack.py -q
.                                                                        [100%]
1 passed in 0.05s
```

`@retry` intentó la llamada dos veces, ambos intentos lanzaron `IOError`, y
`@fallback` capturó la excepción final y devolvió `DEGRADED`. El llamante nunca
vio el error — exactamente el comportamiento que quieres cuando `AccountService`
está teniendo un mal día.

**Lo que acaba de pasar.** Ensamblaste una porción de la pila completa —
reintento por dentro, fallback por fuera — y demostraste que un fallo duro
aflora como una respuesta degradada pero válida. Añade `@rate_limiter`,
`@bulkhead`, `@time_limiter` y `@circuit_breaker` entre ellos en el orden
mostrado arriba y cada uno se pliega en el mismo flujo: cada excepción que lanzan
es capturada por el `@fallback` exterior, de modo que el llamante siempre recibe
una respuesta que puede usar.

---

## Lo que construiste {.recap}

Este capítulo cierra la Parte IV. En el Capítulo 11 dividiste Lumen en servicios
independientes con clientes HTTP tipados. En el Capítulo 12 añadiste
`DepositSaga` para coordinar operaciones de varios pasos con transacciones de
compensación. Aquí hiciste que todo el sistema sea rápido y tolerante a fallos.

Concretamente, aprendiste:

- **`@cacheable`** cortocircuita las lecturas de saldo en un acierto de caché; el
  TTL de cinco segundos acota la obsolescencia a una ventana aceptable. Aplicado
  a `GetBalanceHandler` envolviendo `_fetch` en el momento de la construcción —
  `_fetch` llama a `repository.find_by_id` y proyecta el `WalletEntity`
  resultante sobre `BalanceDto` vía `entity_to_balance_dto` (`Mapper.project` +
  la `BalanceView` marcada con `@projection`).
- **`@cache_put`** refresca la caché como efecto secundario de cada comando
  `DepositFunds`. `_deposit` está decorado con `@transactional()`; hace
  `find_by_id → to_aggregate → mutate → upsert` como una unidad de trabajo
  confirmada, y después actualiza la caché con el saldo devuelto. La plantilla de
  clave debe coincidir con la clave `@cacheable` para acertar en la misma ranura.
- **`@cache_evict`** elimina entradas al cerrar un monedero o en reinicios
  administrativos; `all_entries=True` vacía toda la caché en una sola llamada.
- **`CacheManager`** refleja las escrituras tanto en Redis (primario) como en
  `InMemoryCache` (fallback) y conmuta por error de forma transparente; es el
  valor por defecto correcto para cualquier despliegue de producción.
- **`RateLimiter`** + `@rate_limiter` limitan el tráfico entrante con un
  algoritmo de cubo de tokens que permite ráfagas controladas.
- **`Bulkhead`** + `@bulkhead` aíslan la concurrencia con un semáforo de fallo
  rápido que impide que una dependencia lenta consuma todos los recursos
  disponibles.
- **`@time_limiter`** impone fechas límite usando `asyncio.wait_for`,
  convirtiendo las llamadas colgadas indefinidamente en errores acotados
  `OperationTimeoutException`.
- **`@fallback`** proporciona una respuesta degradada pero funcional cuando todas
  las demás capas han fallado; el método de fallback recibe los argumentos
  originales y la excepción capturada vía el argumento de palabra clave `exc`.
- **`@retry`** toma `max_attempts` como su único argumento posicional; todos los
  demás parámetros (`delay`, `backoff`, `max_delay`, `jitter`, `exceptions`) son
  de solo palabra clave. Reinvoca operaciones un número acotado de veces con
  backoff exponencial.
- **`@circuit_breaker`** toma una **instancia** `CircuitBreaker` — no argumentos
  de palabra clave — y abre el circuito tras un umbral de fallos,
  cortocircuitando las llamadas posteriores durante la ventana de recuperación
  para que el servicio descendente tenga tiempo de recuperarse.
- **`ResilienceRegistry`** (PyFly v26.6.110) materializa instancias con nombre de
  `CircuitBreaker`, `RateLimiter`, `Bulkhead` y limitador de tiempo a partir de
  las claves de configuración `pyfly.resilience.*`, de modo que operaciones puede
  ajustar los umbrales en `pyfly.yaml` e inyectar el registro para buscar las
  instancias por nombre — paridad con el `resilience4j.*.instances.<name>.*` de
  Spring Boot.
- El **orden de los decoradores** importa: fallback el más externo, después
  limitador de tasa, bulkhead, limitador de tiempo, cortacircuitos y reintento el
  más interno — con la caché por encima del fallback para cachear incluso las
  respuestas degradadas.

Lumen es ahora un sistema multiservicio, coordinado por sagas, en caché y
resiliente. La Parte V añade las preocupaciones finales de producción:
observabilidad — métricas, trazas distribuidas y endpoints de salud — para que
puedas ver exactamente qué está haciendo Lumen en producción.

---

## Pruébalo tú mismo {.exercises}

**Ejercicio 1 — Caché condicional.** El manejador `GetBalance` se llama mucho más a menudo para los monederos activos que para los monederos de prueba. Añade `condition=lambda query: not query.wallet_id.startswith("test-")` a la llamada `cacheable(...)` dentro de `GetBalanceHandler.__init__` y verifica con una prueba unitaria que use `InMemoryCache` que las consultas para ids de monedero de prueba siempre llegan al repositorio.

**Ejercicio 2 — Cortacircuitos con umbral basado en tasa.** Reemplaza el cortacircuitos de recuento consecutivo en `AccountGateway` por uno basado en tasa: abre el circuito cuando al menos el 60 % de las últimas 20 llamadas fallen. Construye `CircuitBreaker(failure_rate_threshold=0.6, window_size=20, recovery_timeout=60.0, expected=(IOError, TimeoutError))`. Dos sutilezas guían el diseño de la prueba. Primera, en modo de tasa el cortacircuitos permanece sin saltar hasta que la ventana está *llena* — requiere una ventana completa de 20 llamadas antes de juzgar la tasa — de modo que una ráfaga de fallos por sí sola nunca lo abre. Segunda, el cortacircuitos solo reevalúa su condición de salto en un *fallo* (un éxito nunca lo abre), de modo que la llamada que cruza el umbral debe ser ella misma una llamada fallida. Escribe una prueba que dispare 8 llamadas con éxito seguidas de 12 fallidas (20 llamadas en total = una ventana completa, terminando en un fallo). Verifica que el circuito permanece `CLOSED` hasta la llamada 19 (la ventana sigue siendo parcial), y luego `OPENS` en la 20.ª llamada, cuando la ventana se llena y la tasa de fallos alcanza exactamente 12 / 20 = 0,60.

**Ejercicio 3 — Expulsar por prefijo.** Lumen a veces necesita invalidar todas las entradas de caché de un propietario de monedero dado (borrado RGPD). Añade un método `purge_owner(owner_id: str)` a un servicio de administración de monederos que llame a `backend.evict_by_prefix(f"wallet:balance:{owner_id}:")` directamente (sin un decorador), y escribe una prueba que prepoble tres claves de monedero para un propietario y una para otro, llame a `purge_owner` y verifique que solo las entradas del propietario objetivo han desaparecido.

**Ejercicio 4 — Resiliencia dirigida por configuración.** Mueve los umbrales codificados a mano de `AccountGateway` a `pyfly.yaml` bajo `pyfly.resilience.circuit-breaker.account-api`, `pyfly.resilience.rate-limiter.account-api` y `pyfly.resilience.bulkhead.account-api`. Inyecta `ResilienceRegistry` en la pasarela, busca las tres instancias por nombre y escribe una prueba que verifique que el `CircuitBreaker.failure_threshold` materializado coincide con el valor que estableciste en la configuración. Ten en cuenta que `ResilienceRegistry.from_config(...)` espera un `Config` de pyfly, no un dict plano — llama a `config.get_section("pyfly.resilience.circuit-breaker")` internamente. Construye uno en la prueba a partir de un dict anidado, p. ej. `Config({"pyfly": {"resilience": {"circuit-breaker": {"account-api": {"failure-threshold": 5}}}}})` (importa `from pyfly.core.config import Config`), y después pasa ese `Config` a `from_config`. Confirma que buscar un nombre mal escrito lanza `KeyError` con la lista de nombres disponibles.
