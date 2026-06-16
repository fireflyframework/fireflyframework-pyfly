<span class="eyebrow">Capítulo 8</span>

# Eventos de dominio y arquitectura orientada a eventos {.chtitle}

::: figure art/openers/ch08.svg | &nbsp;

El monedero (wallet) de Lumen guarda correctamente, valida con rigor y despacha cada escritura a través de un comando tipado. Pero fíjate en lo que hacen los manejadores de comandos después de `repo.add`: nada. Los eventos de dominio que la raíz de agregado `Wallet` acumula en su buffer —`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`— se vacían y se descartan. El pipeline del bus que el Capítulo 7 prometió que "publicaría eventos de dominio" todavía no tiene adónde enviarlos.

La carencia importa en la práctica. Lumen necesita un modelo de lectura del saldo que se mantenga sincronizado sin recargar el agregado en cada consulta, una notificación de bienvenida cuando se abre un monedero nuevo y un rastro de auditoría inmutable de cada movimiento financiero por cumplimiento normativo. Las tres funcionalidades dependen de saber *que algo ocurrió* —no quién lo solicitó ni cómo se gestionó.

Los **eventos de dominio** son la respuesta. En lugar de que un manejador de comandos llame directamente al servicio de notificaciones —o de acoplar el registro de auditoría al repositorio— publicas el evento: un registro conciso, con marca de tiempo e inmutable de un hecho. Cada parte interesada se suscribe de forma independiente. El manejador que guardó el monedero no necesita saber qué hace el auditor, ni siquiera que existe un auditor. Puedes añadir un nuevo suscriptor meses después sin tocar una sola línea del código de los manejadores existentes.

Este capítulo construye el lado reactivo de la arquitectura de Lumen. Conectarás `EventPublisher` a los manejadores de comandos, introducirás el puente `publish_domain_events` que vacía el buffer del agregado y reenvía cada evento al bus, y escribirás un `WalletAuditListener` que se suscribe usando `@event_listener` y mantiene dos proyecciones en memoria: un rastro de auditoría inmutable y un total de depósitos acumulado. Al final del capítulo, la ruta de escritura y la infraestructura de lectura quedarán totalmente desacopladas: cada lado evoluciona sin que el otro se entere.

Lo construiremos gradualmente, una pequeña pieza cada vez. Cada funcionalidad viene con un recorrido numerado, el comando exacto que ejecutar y la salida que deberías esperar ver. Si has seguido el hilo desde el Capítulo 7, ya tienes los manejadores de comandos del monedero y el agregado `Wallet` en su sitio; este capítulo está verificado con PyFly v26.6.110 y el ejemplo de Lumen bajo `samples/lumen`.

!!! note "Nota: la jerga, en lenguaje llano"
    En este capítulo se repite un puñado de términos. Un **evento de dominio** es un registro pequeño y congelado de un hecho de negocio que ya ocurrió —por ejemplo, "se depositaron fondos". Un **publicador** es lo que anuncia esos hechos; un **oyente** (o *suscriptor*) es código que reacciona a ellos. Un **bus** es la centralita intermedia que lleva cada anuncio desde el publicador hasta todos los oyentes interesados. Una **proyección** es un modelo de lectura que un oyente va construyendo a partir de eventos —aquí, un rastro de auditoría y un total acumulado. Un **puerto** es una interfaz (un `Protocol` de Python) de la que depende tu código para que la implementación concreta que hay detrás pueda intercambiarse sin cambiar a quien la invoca. Ten presentes estos cinco; el resto del capítulo trata mayormente de conectarlos.

---

## Dos tipos de eventos

Antes de tocar nada de código, conviene ser precisos sobre qué significa "evento" en PyFly. El framework usa la palabra para dos cosas distintas, y confundirlas lleva al bus equivocado, a la API de suscripción equivocada y a sutiles sorpresas en tiempo de ejecución.

Los **eventos de aplicación** (`pyfly.context.events`) son notificaciones del ciclo de vida del framework. `ContextRefreshedEvent` se dispara cuando el contenedor de inyección de dependencias termina de cablear; `ApplicationReadyEvent` se dispara cuando el servidor HTTP empieza a aceptar conexiones; `ContextClosedEvent` se dispara durante el apagado. El `ApplicationEventBus` los despacha a los suscriptores emparejados por el tipo de clase de Python —son fontanería de infraestructura para el arranque, deliberadamente separada de cualquier concepto de negocio.

Los **eventos de dominio** (`pyfly.eda`) son hechos a nivel de negocio: *se abrió un monedero*, *se depositaron fondos*, *se completó una transferencia*. El puerto `EventPublisher` envuelve cada carga útil en un `EventEnvelope` y la enruta por el nombre de la clase del evento de dominio —`"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`— de modo que los oyentes se suscriben a hechos de negocio con nombre y no a detalles de implementación. Los eventos de dominio son el tema de este capítulo.

La distinción determina lo que puedes hacer con cada tipo. El `ApplicationEventBus` despacha a invocables indexados por clase; `InMemoryEventBus` enruta por nombre de clase y puede intercambiarse por un adaptador respaldado por Kafka sin tocar el código de los suscriptores. La regla es sencilla: usa eventos de ciclo de vida para el arranque de infraestructura y eventos de dominio para todo lo que tenga significado de negocio.

!!! note "Nota: los eventos de aplicación siguen siendo útiles"
    Si necesitas precalentar una caché en cuanto la aplicación esté lista, `@app_event_listener` sobre `ApplicationReadyEvent` es la herramienta adecuada. Los dos sistemas conviven; puedes usar ambos en el mismo servicio.

---

## Publicar eventos

### El puerto EventPublisher

La primera pregunta que suele hacer un nuevo miembro del equipo es: "¿qué clase importo para disparar un evento?". La respuesta, deliberadamente, no es una clase: es un protocolo. `EventPublisher` es un **puerto** en el sentido de la arquitectura hexagonal: cualquier código que necesite publicar un evento depende de esta interfaz, y la implementación del bus se inyecta desde fuera. Esa decisión de diseño es lo que te permite ejecutar `InMemoryEventBus` localmente hoy e intercambiarlo por un adaptador de Kafka en producción sin tocar un solo manejador.

El protocolo expone dos métodos:

```python
from pyfly.eda import EventPublisher

class EventPublisher(Protocol):
    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None: ...

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict,
        headers: dict[str, str] | None = None,
    ) -> None: ...
```

`publish` envuelve tus datos en un `EventEnvelope` antes de la entrega —nunca construyes el sobre tú mismo. `subscribe` registra manejadores programáticamente, aunque en la práctica usarás el decorador `@event_listener` en su lugar, porque permite que el `ApplicationContext` cablee las suscripciones automáticamente en el arranque.

El bean del bus solo existe cuando `pyfly.eda.provider` está configurado. Para Lumen, `pyfly.yaml` lo establece en `memory`:

::: listing pyfly.yaml | Listado 8.0 — Activar el bus EDA en memoria en pyfly.yaml
pyfly:
  eda:
    provider: memory
  # … other keys omitted for brevity
:::

Sin esta línea el bean `EventPublisher` no se registra y cualquier manejador que declare `events: EventPublisher` en su constructor fallará al arrancar.

Encendamos ese bus antes de usarlo.

**Paso 1 — Abre el `pyfly.yaml` de Lumen.** Localiza el bloque de nivel superior `pyfly:`. Ya verás claves como `app`, `server` y `data` de los capítulos anteriores.

**Paso 2 — Añade el bloque `eda`.** Inserta las dos líneas que se muestran en el Listado 8.0 como hijo de `pyfly:`, al mismo nivel de indentación que `server:` y `data:`. La indentación es significativa en YAML, así que alinéalas con exactitud.

**Paso 3 — Guarda el archivo.** Esa es toda la configuración que necesita el bus en memoria —sin broker, sin cadena de conexión, sin dependencia adicional.

!!! note "Nota: Ejecútalo"
    Arranca la aplicación y confirma que levanta limpiamente con el bus registrado:

    ```bash
    uv run pyfly run --server uvicorn
    ```

    En el log de arranque deberías ver la aplicación levantar en el puerto de app por defecto y los endpoints de gestión en el puerto de gestión separado:

    ```text
    INFO  starting_application name=lumen version=1.0.0
    INFO  uvicorn running on http://127.0.0.1:8080
    INFO  management endpoints on http://127.0.0.1:9090
    ```

    Si en cambio el proceso termina con un error que menciona que no se pudo resolver una dependencia `EventPublisher`, el bloque `eda` falta o está mal indentado —vuelve al Paso 2.

!!! note "Nota: dónde viven la app y el panel"
    A partir de la v26.6.110, la aplicación escucha en `pyfly.server.port` (por defecto `8080`), mientras que el actuator y el panel de administración corren en un puerto de gestión **separado**, `pyfly.management.server.port` (por defecto `9090`). El puerto de gestión está abierto y sin autenticar por defecto; establece `pyfly.management.security.enabled: true` para protegerlo, o `pyfly.management.server.port: -1` para desactivar por completo los endpoints de gestión. Nada de esto afecta al bus EDA —es puramente en proceso—, pero conviene saber qué puerto es cuál cuando empieces a juguetear con la app en ejecución más adelante en el capítulo.

**Qué acaba de pasar.** Has cambiado un único interruptor de configuración y PyFly ha registrado un bean `EventPublisher` por ti. A partir de ahora, cualquier `@service` que pida `events: EventPublisher` en su constructor recibe el bus en memoria automáticamente. Todavía no se publica nada —eso viene a continuación— pero la fontanería está en su sitio.

### El EventEnvelope

Cada evento de dominio llega a sus oyentes envuelto en un **`EventEnvelope`**. Piénsalo como la capa de metadatos que transforma un diccionario de Python pelado en un hecho trazable, auditable y de primera clase. Es una dataclass congelada —inmutable una vez creada— que empareja la carga útil con el contexto que cada oyente necesita.

| Campo | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `event_type` | `str` | obligatorio | El nombre de la clase del evento de dominio, p. ej. `"FundsDeposited"`. Se usa para el enrutamiento. |
| `payload` | `dict[str, Any]` | obligatorio | Los datos del evento. |
| `destination` | `str` | obligatorio | Canal o topic lógico, p. ej. `"wallet.events"`. |
| `event_id` | `str` | UUID automático | ID único de esta instancia de evento. |
| `timestamp` | `datetime` | `datetime.now(UTC)` | Hora de creación en UTC. |
| `headers` | `dict[str, str]` | `{}` | Metadatos arbitrarios: IDs de correlación, contexto de traza, etc. |

Tres campos merecen atención especial. `event_id` es un UUID estable generado por el bus en el momento de la publicación —tu **clave de idempotencia** para semántica de exactamente una vez, disponible en cada oyente sin trabajo adicional. `timestamp` registra cuándo se observó el hecho, no cuándo lo procesa el oyente, así que se mantiene exacto incluso si un oyente corre con retraso. `headers` transporta preocupaciones transversales como IDs de traza distribuida —metadatos que no tienen nada que ver con la carga útil de negocio pero que importan enormemente para la observabilidad. Como el sobre está congelado, los manejadores pueden pasarlo con seguridad a través de fronteras asíncronas sin copias defensivas.

`event_type` contiene el **nombre de clase** del evento de dominio —`"WalletOpened"`, `"FundsDeposited"` o `"FundsWithdrawn"`— no una ruta separada por puntos. Los oyentes se suscriben por esos mismos nombres de clase, de modo que el contrato de suscripción lo define el modelo de dominio, no convenciones de cadena inventadas fuera de él.

**Qué acaba de pasar.** No te eche para atrás la tabla de seis campos: en el código del día a día solo tocas dos de ellos. Cuando publicas proporcionas `event_type`, `payload` y `destination`; el bus rellena `event_id`, `timestamp` y `headers` por ti. Cuando reaccionas, lees `envelope.event_type` para saber *qué* ocurrió y `envelope.payload` para conocer los detalles. Los otros tres campos están ahí cuando los necesitas (idempotencia, ordenación, trazas) y discretamente fuera del camino cuando no.

### Los eventos de dominio en el agregado Wallet

El agregado `Wallet` lanza eventos de dominio tipados, como dataclasses congeladas. El nombre de clase de cada evento se convierte en su `event_type` de enrutamiento en el bus:

::: listing lumen/models/entities/v1/wallet_entity.py | Listado 8.1 — Eventos de dominio lanzados por el agregado Wallet
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent


@dataclass(frozen=True)
class WalletOpened(DomainEvent):
    wallet_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    def deposit(self, amount: Money) -> None:
        """Credit *amount*; raises FundsDeposited."""
        self._assert_currency(amount)
        self.balance = self.balance.add(amount)
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )
    # … open() and withdraw() follow the same pattern
:::

`DomainEvent` es una dataclass congelada base. Su propiedad `event_type` devuelve `type(self).__name__` —el nombre de clase— que es exactamente lo que `EventPublisher` usa como clave de enrutamiento. `raise_event` acumula el evento en el buffer del agregado; el manejador de comandos vacía ese buffer llamando a `wallet.clear_events()` tras una persistencia con éxito.

### El puente de publicación

En lugar de repetir el bucle de vaciado en cada manejador de comandos, Lumen lo extrae en una única corrutina `publish_domain_events`. El puente serializa cada evento vaciado con `dataclasses.asdict`, y luego llama a `publisher.publish` con el nombre de clase como `event_type` y `"wallet.events"` como canal lógico:

::: listing lumen/core/services/wallets/event_publishing.py | Listado 8.2 — publish_domain_events conecta los eventos vaciados al bus EDA
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from lumen.core.services.listeners.wallet_audit_listener import (
    WALLET_EVENTS_DESTINATION,
)
from pyfly.domain import DomainEvent
from pyfly.eda import EventPublisher


def _to_payload(event: DomainEvent) -> dict[str, Any]:
    """Flatten a frozen-dataclass domain event into a dict."""
    payload: dict[str, Any] = dataclasses.asdict(event)
    payload.setdefault("event_type", event.event_type)
    return payload


async def publish_domain_events(
    publisher: EventPublisher, events: Iterable[DomainEvent]
) -> None:
    """Publish each drained domain event on the wallet events channel.

    The envelope's ``event_type`` is the domain event class name
    (``WalletOpened`` / ``FundsDeposited`` / ``FundsWithdrawn``).
    """
    for event in events:
        await publisher.publish(
            destination=WALLET_EVENTS_DESTINATION,
            event_type=event.event_type,
            payload=_to_payload(event),
        )
:::

`WALLET_EVENTS_DESTINATION` es la constante `"wallet.events"` definida en `wallet_audit_listener.py` y compartida por publicador y oyente para que el nombre del canal no pueda divergir. `event.event_type` es la propiedad del nombre de clase en `DomainEvent`: `"WalletOpened"`, `"FundsDeposited"` o `"FundsWithdrawn"`.

### Cablear el publicador en los manejadores de comandos

En el Capítulo 7, los manejadores de comandos cargaban agregados, dirigían el comportamiento de dominio y guardaban —dejando los eventos acumulados tirados por el suelo. Ahora cierras esa carencia. Inyecta un `EventPublisher` junto al `WalletRepository` y un `async_sessionmaker`, decora `do_handle` con `@transactional()`, y tras `repo.upsert(...)` vacía el buffer del agregado y publica cada evento a través del puente.

El decorador `@transactional()` (de `pyfly.data.relational.sqlalchemy`) abre una `AsyncSession` dedicada a partir del `async_sessionmaker` inyectado, la vincula al repositorio durante la llamada, hace commit en caso de éxito y rollback en caso de fallo. Eso significa que la secuencia cargar → mutar → guardar es una unidad de trabajo confirmada, y no se publica ningún evento a menos que la fila aterrice realmente en la base de datos.

Aquí está el cambio, desglosado en las cuatro ediciones que harás a `DepositFundsHandler`.

**Paso 1 — Añade el publicador al constructor.** Junto al parámetro existente `repository`, acepta `events: EventPublisher` y guárdalo como `self._events`. Tipéalo como el *protocolo* `EventPublisher`, nunca como `InMemoryEventBus` —eso es lo que mantiene al manejador ignorante de qué bus está corriendo.

**Paso 2 — Acepta la fábrica de sesiones.** Añade `session_factory: async_sessionmaker[AsyncSession]` y guárdalo como `self._session_factory`. El decorador `@transactional()` busca exactamente este nombre de atributo para abrir su unidad de trabajo, así que el nombre importa.

**Paso 3 — Decora `do_handle` con `@transactional()`.** Esto envuelve toda la secuencia cargar-mutar-guardar en una sola transacción confirmada.

**Paso 4 — Vacía y publica tras guardar.** Como último paso dentro de `do_handle`, después de `self._repository.upsert(...)`, llama a `await publish_domain_events(self._events, wallet.clear_events())`. `wallet.clear_events()` devuelve los eventos acumulados *y* vacía el buffer, de modo que nunca se publican dos veces.

Juntando esas cuatro ediciones obtienes el `DepositFundsHandler` actualizado:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 8.3 — DepositFundsHandler: unidad de trabajo @transactional y luego publicar
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

Tres decisiones de diseño merecen mención. Primero, `events: EventPublisher` está tipado como el protocolo, no como `InMemoryEventBus` —el contenedor de inyección de dependencias inyecta la implementación que esté registrada, así que el manejador nunca sabe ni le importa qué bus está activo. Segundo, la llamada de publicación se sitúa *después* de `self._repository.upsert(...)` dentro de la unidad de trabajo `@transactional()`: si la persistencia falla, el decorador hace rollback antes de llegar a `publish_domain_events`, de modo que los oyentes nunca ven un hecho que nunca se persistió. Tercero, el manejador trabaja directamente con la entidad ORM mediante los mappers `to_aggregate` / `to_entity` —el agregado se rehidrata desde la fila, se muta y se mapea de vuelta a una fila antes del upsert. Si la publicación falla tras una persistencia con éxito, tienes un reto de entrega al-menos-una-vez —el Capítulo 10 lo aborda con patrones de outbox transaccional. Por ahora, el bus en memoria nunca falla.

!!! note "Nota: Ejecútalo"
    Con la aplicación en ejecución (`uv run pyfly run --server uvicorn`), abre un monedero y deposita en él desde una segunda terminal:

    ```bash
    # Open a wallet — returns its id
    curl -s -X POST http://localhost:8080/api/v1/wallets \
      -H 'content-type: application/json' \
      -d '{"owner_id": "u-1", "currency": "EUR"}'
    # {"wallet_id": "wlt-…"}

    # Deposit 1500 minor units (15.00 EUR) into that wallet
    curl -s -X POST http://localhost:8080/api/v1/wallets/wlt-…/deposit \
      -H 'content-type: application/json' \
      -d '{"amount": 1500}'
    # {"wallet_id": "wlt-…", "balance": 1500}
    ```

    La respuesta HTTP confirma el saldo, pero la evidencia más interesante está en el log de la aplicación: como el depósito publicó un evento `FundsDeposited` y el oyente de auditoría (que construirás en la siguiente sección) reacciona a él, verás una línea de log `wallet_audit_observed` para `event_type=FundsDeposited`. ¿Todavía no hay oyente? Entonces la publicación ocurre en silencio —que es precisamente el sentido: el manejador no sabe si alguien está escuchando.

**Qué acaba de pasar.** El manejador de comandos ahora hace una cosa más tras guardar: vacía los eventos que el agregado acumuló y se los entrega al bus. La ordenación crucial es *guardar primero, publicar segundo*, todo dentro de una transacción. Si la escritura en la base de datos hace rollback, los eventos nunca se publican, así que un oyente nunca puede observar un hecho que en realidad no se persistió. El manejador ganó cuatro líneas y cero conocimiento nuevo —sigue sin tener ni idea de qué, si es que algo, reaccionará.

El `OpenWalletHandler` sigue el mismo patrón:

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listado 8.4 — OpenWalletHandler: @transactional, upsert y luego publicar WalletOpened
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        # @transactional resolves the unit-of-work session from here.
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: OpenWallet) -> str:
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

`return wallet_id` viene *después* de la llamada de publicación —el manejador cumple su contrato solo una vez que cada hecho producido por la operación ha sido despachado. El id del monedero se genera localmente con `uuid4()` en lugar de delegarlo a un método del repositorio; `to_entity` mapea el agregado a una fila antes del upsert para que el `Repository` del framework reciba el modelo ORM que espera.

### El atajo @publish_result

En servicios más sencillos donde el valor de retorno de un método *es* la carga útil del evento —común en código que no ha adoptado el patrón de agregado completo— `@publish_result` elimina por completo la llamada manual de publicación:

::: listing lumen/eda/publish_result_example.py | Listado 8.5 — @publish_result auto-publica el valor de retorno del método
from pyfly.eda import publish_result
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()


@publish_result(bus, destination="wallet.events", event_type="FundsTransferred")
async def transfer_funds(source_id: str, target_id: str, amount: int) -> dict:
    # Business logic omitted — the returned dict IS the event payload.
    return {
        "source_id": source_id,
        "target_id": target_id,
        "amount": amount,
    }
:::

Cuando `transfer_funds` retorna, el decorador intercepta el resultado y llama a `bus.publish` con él como carga útil —sin necesidad de bucle repetitivo. `destination` y `event_type` quedan fijados en el momento de la decoración, manteniendo limpia la función de negocio. `@publish_result` también acepta un predicado `condition` opcional: el evento se publica solo cuando el resultado satisface la prueba, lo cual resulta útil para flujos de trabajo condicionales en los que no toda ejecución con éxito debe difundirse.

::: figure art/figures/08-eda.svg | Figura 8.1 — Un publicador, muchos oyentes independientes.

!!! spring "Equivalencia con Spring"
    `EventPublisher` es el equivalente en PyFly del `ApplicationEventPublisher` de Spring. Llamar a `publisher.publish(...)` equivale a `applicationEventPublisher.publishEvent(event)`. El decorador `@event_listener` (siguiente sección) refleja el `@EventListener` de Spring para reacciones síncronas, en la misma transacción. `@publish_result` logra lo que los desarrolladores de Spring a menudo cablean manualmente con consejos AOP `@AfterReturning`.

---

## Reaccionar con @event_listener

Publicar un evento es solo la mitad de la película. Un evento al que nadie reacciona no es más que una entrada de log. El valor del modelo orientado a eventos reside en las *reacciones* que habilita —comportamientos independientes que se activan en respuesta al mismo hecho publicado, cada uno ajeno a los demás.

El decorador **`@event_listener`** de PyFly es la forma más sencilla de registrar una reacción. Decora cualquier método asíncrono con los nombres de clase que le interesan, y el `ApplicationContext` cablea la suscripción durante el arranque —sin necesidad de una referencia al bus en el momento de la decoración.

```python
from pyfly.eda import event_listener, EventEnvelope

@event_listener(event_types=["FundsDeposited"])
async def on_funds_deposited(envelope: EventEnvelope) -> None:
    ...
```

`event_types` acepta nombres de clase exactos. Los oyentes dentro de una clase `@service` reciben un `EventEnvelope` como único argumento. Como el emparejamiento ocurre a nivel del bus —no dentro de tu función— un único método oyente puede suscribirse a varios tipos de evento en una sola declaración.

### WalletAuditListener

El oyente de producción de Lumen es `WalletAuditListener`. Se suscribe a los tres eventos de dominio del monedero y mantiene dos proyecciones en memoria: un **rastro de auditoría** ordenado y un **total neto de depósitos acumulado** por monedero.

Lo ensamblaremos en pequeñas piezas. Lee primero los cuatro pasos, y luego estudia el listado completo más abajo —es el mismo código, mostrado entero.

**Paso 1 — Declara una clase `@service` simple.** Un oyente no es más que un bean de servicio con algo de estado. Dale un `__init__` que inicialice las dos proyecciones: `self._entries: list[AuditEntry] = []` para el rastro de auditoría y `self._running_totals: dict[str, int] = {}` para los totales por monedero.

**Paso 2 — Escribe el método de reacción.** Añade un `async def on_wallet_event(self, envelope: EventEnvelope) -> None`. Recibe un `EventEnvelope` y nada más.

**Paso 3 — Estámpalo con `@event_listener`.** Decora el método con `@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])`. Un método puede suscribirse a los tres nombres de clase en una sola declaración. Este decorador no se suscribe de inmediato —*estampa* el método con metadatos que el `ApplicationContext` encuentra en el arranque y usa para auto-suscribirlo al bean `EventPublisher`.

**Paso 4 — Proyecta el evento.** Dentro del método, añade un `AuditEntry` por cada evento, y luego ramifica según `envelope.event_type` para ajustar el total acumulado: ponlo a cero en `WalletOpened`, súmalo en `FundsDeposited`, réstalo en `FundsWithdrawn`. Expón accesores de lectura (`entries`, `entries_for`, `running_total`) para que otro código pueda consultar las proyecciones.

::: listing lumen/core/services/listeners/wallet_audit_listener.py | Listado 8.6 — WalletAuditListener: rastro de auditoría + proyección de total acumulado
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener

logger = logging.getLogger(__name__)

WALLET_EVENTS_DESTINATION = "wallet.events"


@dataclass(frozen=True)
class AuditEntry:
    """One observed domain event, captured for the audit trail."""

    event_type: str
    wallet_id: str
    event_id: str
    occurred_at: datetime
    payload: dict[str, object]


@service
class WalletAuditListener:
    """In-memory audit log + running-total projection over wallet events."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._running_totals: dict[str, int] = {}

    @event_listener(
        event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"]
    )
    async def on_wallet_event(self, envelope: EventEnvelope) -> None:
        """Project every wallet domain event into the read models."""
        payload = dict(envelope.payload)
        wallet_id = str(payload.get("wallet_id", ""))

        self._entries.append(
            AuditEntry(
                event_type=envelope.event_type,
                wallet_id=wallet_id,
                event_id=str(payload.get("event_id", envelope.event_id)),
                occurred_at=envelope.timestamp,
                payload=payload,
            )
        )

        if envelope.event_type == "WalletOpened":
            self._running_totals.setdefault(wallet_id, 0)
        elif envelope.event_type == "FundsDeposited":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = (
                self._running_totals.get(wallet_id, 0) + amount
            )
        elif envelope.event_type == "FundsWithdrawn":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = (
                self._running_totals.get(wallet_id, 0) - amount
            )

        logger.info(
            "wallet_audit_observed",
            extra={"event_type": envelope.event_type, "wallet_id": wallet_id},
        )

    @property
    def entries(self) -> list[AuditEntry]:
        """A snapshot of the audit log, in observation order."""
        return list(self._entries)

    def entries_for(self, wallet_id: str) -> list[AuditEntry]:
        """The audit entries recorded for one wallet."""
        return [e for e in self._entries if e.wallet_id == wallet_id]

    def running_total(self, wallet_id: str) -> int:
        """Net funds (deposited minus withdrawn) for wallet_id, minor units."""
        return self._running_totals.get(wallet_id, 0)
:::

Esto es lo que hace el oyente, paso a paso.

`@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])` le dice al `ApplicationContext` que suscriba `on_wallet_event` a esos tres nombres de clase. Como la clase es un bean `@service`, PyFly la descubre en el arranque y cablea las suscripciones automáticamente —nunca llamas a `bus.subscribe` a mano.

`on_wallet_event` recibe un `EventEnvelope`. `envelope.event_type` es el nombre de clase del evento de dominio lanzado. `envelope.payload` es el dict producido por `dataclasses.asdict` en el puente de publicación, así que sus claves coinciden exactamente con los nombres de los campos de la dataclass —`wallet_id`, `amount`, `currency`, `balance`.

El método añade un `AuditEntry` por cada evento, y luego ramifica según `event_type` para actualizar el total acumulado. Fíjate en lo que falta: ninguna importación del agregado `Wallet`, ninguna llamada al repositorio, ningún conocimiento de cómo se procesó el depósito. La proyección reacciona puramente al hecho publicado.

**Qué acaba de pasar.** Has escrito una reacción autónoma. El decorador `@event_listener` es toda la historia del cableado —no hay ninguna llamada `bus.subscribe(...)` en ningún sitio de este archivo. En el arranque, el `ApplicationContext` escanea tus beans `@service`, encuentra el método estampado `on_wallet_event` y lo suscribe al bus para cada uno de los tres nombres de clase. El oyente y los manejadores de comandos nunca se referencian entre sí; están conectados únicamente a través de los eventos que acuerdan nombrar.

!!! tip "Consejo: metadatos del sobre en las proyecciones"
    `envelope.timestamp` te da la hora autoritativa del evento —cuándo se registró el hecho, no cuándo corrió el oyente. Guárdala en tu modelo de lectura y obtienes una columna `occurred_at` barata gratis, sin desviación de reloj entre escritor y lector.

### Probar el oyente de extremo a extremo

La suite de pruebas ejercita la ruta completa de publicar-y-recibir sin mocks. El conftest cablea un `InMemoryEventBus` compartido, refleja el paso de descubrimiento de `@event_listener` suscribiendo `on_wallet_event` a cada nombre de clase declarado, y registra manejadores de comandos reales que comparten la misma referencia al bus:

```python
# tests/conftest.py (abbreviated)
from pyfly.eda.adapters.memory import InMemoryEventBus

@pytest_asyncio.fixture
async def event_bus() -> InMemoryEventBus:
    yield InMemoryEventBus()

@pytest_asyncio.fixture
async def audit_listener(event_bus: InMemoryEventBus) -> WalletAuditListener:
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener
```

Con ese cableado en su sitio, la prueba envía comandos reales y hace aserciones sobre los modelos de lectura del oyente:

::: listing lumen/tests/test_event_listener.py | Listado 8.7 — Prueba de extremo a extremo: los comandos publican, el oyente proyecta
from __future__ import annotations

import pytest
from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus


@pytest.mark.asyncio
async def test_listener_observes_wallet_events(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=400))

    entries = audit_listener.entries_for(wallet_id)
    assert [e.event_type for e in entries] == [
        "WalletOpened",
        "FundsDeposited",
        "FundsWithdrawn",
    ]

    deposited = entries[1]
    assert deposited.payload["amount"] == 1500
    assert deposited.payload["currency"] == "EUR"
    assert deposited.payload["balance"] == 1500

    # running_total = deposited − withdrawn
    assert audit_listener.running_total(wallet_id) == 1100
:::

La prueba demuestra la cadena completa: `OpenWalletHandler` → `publish_domain_events` → `InMemoryEventBus` → `WalletAuditListener.on_wallet_event` → `audit_listener.entries_for(...)`. Sin mocks, sin fakes —la ruta de código de producción corre tal como está escrita.

!!! note "Nota: Ejecútalo"
    Ejecuta la prueba del oyente de eventos desde la raíz del proyecto Lumen:

    ```bash
    uv run --extra dev pytest tests/test_event_listener.py -q
    ```

    Deberías ver pasar la suite:

    ```text
    ...                                                                      [100%]
    3 passed in 0.XXs
    ```

    Las tres pruebas cubren el camino feliz (abrir, depositar, retirar y luego comprobar el rastro de auditoría y el total acumulado), el caso de proyección vacía antes de que se ejecute ningún comando, y el caso negativo en el que una retirada con descubierto lanza una excepción y por tanto *no* publica ningún evento —así que no debe aparecer en el registro de auditoría. Si una prueba falla con un error de atributo inexistente sobre `__pyfly_event_patterns__`, el decorador `@event_listener` no está aplicado a `on_wallet_event`; revisa el Paso 3 de la sección anterior.

Lo que hace convincente a este diseño es que añadir el oyente no requirió ningún cambio en los manejadores de comandos, en el agregado `Wallet` ni en ningún repositorio. `DepositFundsHandler` no tiene ni idea de que existe una proyección. Ambos lados son totalmente independientes —cada uno es una consecuencia del mismo hecho publicado, conectados únicamente por el bus.

---

## Cuando los oyentes fallan: estrategias de error

Un oyente que se comporta mal plantea una pregunta puntiaguda: ¿debería el fallo detener toda la cadena de entrega, o debería el bus seguir notificando a los oyentes restantes? La respuesta correcta depende del papel del oyente. PyFly te da control explícito en lugar de imponer una única política.

Por defecto, `InMemoryEventBus` invoca a los oyentes secuencialmente y propaga cualquier excepción —el comportamiento correcto para desarrollo, donde un oyente que falla debe aflorar ruidosamente. En producción normalmente necesitas un control más fino.

**`ErrorStrategy`** es un enum que gobierna cómo se comporta el bus cuando un oyente lanza una excepción:

```python
from pyfly.eda import ErrorStrategy
```

| Miembro | Valor | Comportamiento |
|---|---|---|
| `IGNORE` | `"IGNORE"` | Traga la excepción silenciosamente. El procesamiento continúa con el siguiente manejador. |
| `LOG_AND_CONTINUE` | `"LOG_AND_CONTINUE"` | Registra el error y luego continúa. El valor por defecto más seguro para oyentes no críticos. |
| `RETRY` | `"RETRY"` | Reintenta la entrega. El número de reintentos y el back-off se configuran por separado. |
| `DEAD_LETTER` | `"DEAD_LETTER"` | Mueve el evento fallido a un destino de cartas muertas para inspección posterior. |
| `FAIL_FAST` | `"FAIL_FAST"` | Propaga la excepción de inmediato. No se invoca ningún manejador más. |

!!! tip "Consejo: ajusta la estrategia a la criticidad del oyente"
    Un oyente de auditoría debería usar `LOG_AND_CONTINUE` —un registrador de auditoría roto no debe detener una transacción financiera. Una proyección que alimenta las respuestas de consulta podría justificar `RETRY` para asegurar que el modelo de lectura se mantenga consistente. Un notificador puede tolerar `IGNORE`, ya que un correo de bienvenida perdido no es un problema de integridad de datos.

!!! warning "Advertencia: efectos secundarios e idempotencia"
    Si un oyente realiza un efecto secundario —escribir una fila en la base de datos, enviar un correo— y el bus reintenta la entrega tras un fallo transitorio, el efecto puede ejecutarse más de una vez. Diseña los oyentes para que sean idempotentes: escribe una fila solo si el `event_id` no se ha registrado ya, envía un correo solo si la bandera de bienvenida no está ya activada. El `envelope.event_id` (un UUID estable generado por el bus) es tu clave de idempotencia.

---

## En memoria hoy, un broker mañana

**`InMemoryEventBus`** es la implementación lista para usar —el `EventPublisher` por defecto que proporciona el `ApplicationContext`. Corre enteramente en proceso: `publish` es una llamada asíncrona directa, no hay serialización, y los eventos no entregados se desvanecen si el proceso muere. Para desarrollo local, pruebas de integración y monolitos que no necesitan entrega entre procesos, eso es perfectamente aceptable.

Entender cómo funciona internamente el bus en memoria facilita razonar sobre el comportamiento en los límites —y apreciar exactamente qué cambia cuando intercambias por un broker.

```python
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()

bus.subscribe("FundsDeposited", my_handler)

await bus.publish(
    destination="wallet.events",
    event_type="FundsDeposited",
    payload={"wallet_id": "w-001", "amount": 5000, "currency": "EUR", "balance": 5000},
)
```

Una llamada a `publish` ejecuta cuatro pasos en secuencia:

1. Envuelve los argumentos en un `EventEnvelope` con un `event_id` generado y un `timestamp` en UTC.
2. Itera cada par registrado `(pattern, handler)`.
3. Para cada par donde `fnmatch.fnmatch(event_type, pattern)` es `True`, llama al manejador con el sobre.
4. Los manejadores corren secuencialmente en orden de suscripción.

Las suscripciones usan el `fnmatch` de Python, así que `"Funds*"` empareja tanto con `"FundsDeposited"` como con `"FundsWithdrawn"`, y `"*"` empareja con todo. La invocación secuencial del paso 4 hace que el orden de los oyentes sea determinista —útil en pruebas— pero también significa que un oyente lento retrasa a todos los posteriores. Los adaptadores respaldados por broker normalmente despachan en paralelo; ten presente esa diferencia al razonar sobre el rendimiento.

Como cada oyente en Lumen depende del *protocolo* `EventPublisher`, no de `InMemoryEventBus` directamente, la implementación se intercambia sin tocar un solo oyente. El Capítulo 10 introduce adaptadores de Kafka y RabbitMQ; cambiar a cualquiera de ellos es un cambio de configuración —`WalletAuditListener` sigue funcionando sin modificación.

!!! note "Nota: InMemoryEventBus y las pruebas"
    `InMemoryEventBus` es también la herramienta adecuada para las pruebas. Inyecta un `InMemoryEventBus` fresco como fixture, suscribe un manejador que capture, ejercita tu manejador de comandos y haz aserciones sobre los objetos `EventEnvelope` que el manejador recibió —incluyendo `event_type`, `payload`, `event_id` y `timestamp`. Sin mockear, sin fakes, solo el bus real con entradas controladas.

---

## Lo que construiste {.recap}

La Parte III está abierta.

Este capítulo cerró el ciclo que el Capítulo 7 inició. `Wallet` lanzó eventos de dominio en el Capítulo 6; los manejadores de comandos los publicaron aquí; `WalletAuditListener` reacciona a esos hechos sin saber nada de la ruta de comandos que los disparó.

La arquitectura es genuinamente orientada a eventos dentro de un solo proceso. Aquí tienes una referencia rápida de cada pieza:

| Pieza | Papel |
|---|---|
| `EventPublisher` | Puerto —un protocolo que cumple cualquier implementación de bus |
| `InMemoryEventBus` | Adaptador por defecto —en proceso, sin configuración; activado por `pyfly.eda.provider: memory` |
| `EventEnvelope` | Transporta la carga útil + `event_id`, `timestamp`, `destination`, `headers` |
| `@event_listener(event_types=[...])` | Decorador de suscripción —nombres de clase; el contexto lo cablea en el arranque |
| `publish_domain_events` | Puente —vacía `wallet.clear_events()`, serializa con `dataclasses.asdict`, llama a `publisher.publish` |
| `ErrorStrategy` | Controla la gestión de fallos: `IGNORE`, `LOG_AND_CONTINUE`, `RETRY`, `DEAD_LETTER`, `FAIL_FAST` |

Tres principios se trasladan al resto de la Parte III: **guarda antes de publicar** —los oyentes nunca deben ver hechos no confirmados; **diseña los oyentes para la idempotencia** —los reintentos deben ser seguros; **depende del puerto, no del adaptador** —el bus puede intercambiarse sin tocar el código de los oyentes.

El Capítulo 9 lleva la idea del evento más lejos. En lugar de mantener un modelo de lectura separado junto a un agregado mutable, almacenas los eventos mismos como el sistema de registro —aplicando event sourcing (suministro de eventos) al libro mayor para que cada saldo histórico sea calculable desde primeros principios.

---

## Pruébalo tú mismo {.exercises}

1. **Añade un oyente `FraudDetector`.** Crea un servicio `FraudDetector` que se suscriba a `"FundsDeposited"` usando `@event_listener(event_types=["FundsDeposited"])`. Si el `amount` en la carga útil supera `1_000_000` (diez mil euros en unidades menores), registra una advertencia que incluya el `envelope.event_id`, el `envelope.timestamp` y el `wallet_id` de la carga útil. Verifica que se dispara publicando un evento `FundsDeposited` directamente a un `InMemoryEventBus` en una prueba unitaria, y comprueba que la advertencia se activó.

2. **Extiende `WalletAuditListener` con filtrado por evento.** Añade un método `entries_by_type(self, event_type: str) -> list[AuditEntry]` que devuelva solo las entradas con un `event_type` coincidente. Escribe una prueba que abra un monedero, haga dos depósitos y una retirada, y compruebe que `entries_by_type("FundsDeposited")` devuelve exactamente dos entradas.

3. **Observa el comportamiento de la estrategia de error.** Crea un oyente cuyo manejador lance `RuntimeError("failure")` incondicionalmente. Regístralo junto a un manejador que capture añadiendo a una lista en un `InMemoryEventBus`. Configura `ErrorStrategy.LOG_AND_CONTINUE` y confirma que el manejador que captura sigue recibiendo el evento a pesar del fallo. Luego cambia a `ErrorStrategy.FAIL_FAST` y confirma que el manejador que captura *no* recibe el evento.
