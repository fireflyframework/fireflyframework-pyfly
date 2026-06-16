<span class="eyebrow">Capítulo 7</span>

# CQRS: comandos y consultas {.chtitle}

::: figure art/openers/ch07.svg | &nbsp;

El monedero (wallet) de Lumen es ya un ciudadano de primera clase del dominio. El agregado `Wallet` impone sus propias invariantes, emite eventos de dominio y persiste a través de una frontera de repositorio limpia. El controlador, sin embargo, sigue llamando a `WalletApplicationService` directamente: un método por operación, con lecturas y escrituras compartiendo la misma ruta de código. Ese diseño está bien a pequeña escala, pero muestra fricciones a medida que el sistema crece. El equipo quiere cachear los saldos de los monederos, mantener un único rastro de auditoría para cada escritura, añadir reglas de autorización a operaciones concretas y probar cada pieza de lógica en aislamiento completo de las demás.

**CQRS** —Command Query Responsibility Segregation, segregación de responsabilidad entre comandos y consultas— aborda todo esto trazando una línea clara entre las dos cosas que un servicio puede hacer: *cambiar el estado* y *leer el estado*. Las escrituras se convierten en **comandos**: mensajes fuertemente tipados, con nombre e inmutables que fluyen a través de un `CommandBus`. Las lecturas se convierten en **consultas**: mensajes igualmente tipados que fluyen a través de un `QueryBus`. Cada bus ejecuta una tubería fija —validación, autorización, ejecución y, después (para los comandos), publicación de eventos de dominio—. Tu manejador (handler) implementa exactamente una intención; el bus se encarga de todo lo demás.

Al final de este capítulo, el controlador de Lumen despacha comandos y consultas en lugar de llamar al servicio directamente. `OpenWallet`, `DepositFunds` y `WithdrawFunds` recorren la ruta de comandos; `GetWallet`, `GetBalance`, `ListWallets` y `ListRichWallets` recorren la ruta de consultas. El agregado `Wallet` que construiste en el Capítulo 6 permanece intacto: CQRS no reemplaza el modelo de dominio; es el mecanismo de entrega de instrucciones hacia él.

!!! note "Nueva jerga, en términos sencillos"
    Un **bus** aquí no es hardware: es un único objeto al que le entregas un mensaje, y él averigua qué manejador debe ejecutarse. Un **manejador** es una clase pequeña que hace el trabajo para exactamente un tipo de mensaje. Un **DTO** (objeto de transferencia de datos) es una forma plana —id, propietario, saldo— que pones en el cable como JSON; está deliberadamente separado de tu objeto de dominio rico. Una **proyección** es una porción de solo lectura de tus datos, moldeada para una vista concreta. Conocerás cada uno de estos a medida que construimos, una pieza cada vez, así que no te preocupes si ahora mismo te resultan abstractos.

Este capítulo está construido en torno a PyFly **v26.6.110**, y cada listado está tomado literalmente del ejemplo de Lumen en `samples/lumen/src/lumen`. Iremos despacio: construiremos primero los comandos, luego sus manejadores, después las consultas y, por último, conectaremos todo en el controlador, ejecutando la aplicación y las pruebas en cada hito para que puedas ver cómo cobran vida las piezas antes de añadir la siguiente.

---

## Por qué separar las lecturas de las escrituras

Imagina Lumen al final del Capítulo 6. `WalletController` llama a `WalletApplicationService.credit(wallet_id, amount)`. Esa llamada muta el estado, pero nada en la firma del método lo hace evidente. El equipo quiere añadir una caché de saldos. ¿Dónde va? ¿Dentro de `credit`? ¿En un decorador alrededor del servicio? La pregunta revela el problema: a un único método de servicio se le pide servir a dos amos —la ruta de escritura, que siempre debe tocar la base de datos, y la ruta de lectura, que debería evitarla siempre que sea posible—. Atornillar la caché a un método de escritura es incómodo en el mejor de los casos y peligroso en el peor.

Las escrituras y las lecturas tienen formas fundamentalmente distintas. Una escritura lleva intención y datos: «deposita 1 500 unidades menores en el monedero wlt-001». Una lectura lleva una pregunta: «¿cuál es el saldo actual del monedero wlt-001?». La primera debe alcanzar la base de datos cada vez. La segunda es repetible: preguntar dos veces debería devolver la misma respuesta sin duplicar la carga de la base de datos. Canalizar ambas a través del mismo método mezcla preocupaciones que escalan de forma distinta, se prueban de forma distinta y necesitan un comportamiento transversal distinto.

El beneficio más profundo es la **claridad de intención**. Cuando un compañero lee `wallet_service.credit(wallet_id, amount)`, tiene que inspeccionar la implementación para saber si es seguro llamarlo dos veces, si publica eventos y si es idempotente. Cuando lee `DepositFunds(wallet_id=..., amount=...)`, la intención es inequívoca; y si la intención resulta estar equivocada, cambias el nombre del comando, no la firma del servicio.

Tres beneficios concretos importan para Lumen:

**Escalado independiente.** Las lecturas suelen superar a las escrituras en un orden de magnitud o más. Una vez que las dos rutas están separadas, el bus puede cachear los resultados de las consultas sin tocar la ruta de escritura. Puedes enrutar las consultas a una réplica de lectura y los comandos a la base de datos primaria con un cambio de configuración, no de código.

**Manejadores enfocados.** Cada manejador implementa exactamente una operación. `DepositFundsHandler` carga un monedero, impulsa su comportamiento de dominio, lo persiste y drena eventos; nada más. `GetBalanceHandler` carga un monedero y devuelve una proyección ligera; nada más. Como los manejadores son clases Python planas con dependencias inyectadas, puedes probar cada uno por unidad en aislamiento completo de la capa HTTP.

**Preocupaciones transversales centralizadas.** La validación, la autorización y las trazas distribuidas se implementan una sola vez en la tubería del bus y se aplican uniformemente a cada manejador, sin código repetitivo en el propio manejador. Añadir autorización por operación más adelante es cuestión de sobrescribir `authorize()` en el comando; el bus garantiza que se ejecute antes de que se llegue siquiera a `do_handle`.

---

## Comandos y manejadores de comandos

Antes de escribir una sola línea de código de manejador, da nombre a las intenciones de tu sistema. En el dominio de monederos de Lumen pueden ocurrir tres cosas: se puede abrir un monedero, se pueden depositar fondos y se pueden retirar fondos. Cada una es un **comando**: un mensaje con nombre e inmutable que expresa una intención. El bus lo entrega; el manejador actúa sobre él; el agregado de dominio impone las reglas. Los comandos no son llamadas a métodos disfrazadas de objetos: son contratos explícitos que viven en tu base de código como ciudadanos de primera clase.

Un comando es un dataclass congelado que hereda de `Command[R]`, donde `R` es el tipo que devuelve el manejador. El parámetro genérico es documentación y una pista para el verificador de tipos; el bus no lo impone en tiempo de ejecución.

!!! note "¿Qué es `Command[R]`?"
    El `[R]` de `Command[R]` es un *parámetro de tipo genérico*: un marcador de posición para «lo que sea que devuelva este comando». `OpenWallet(Command[str])` dice «al enviarme, recibes de vuelta un `str`» (el nuevo id del monedero). `DepositFunds(Command[int])` dice «al enviarme, recibes de vuelta un `int`» (el nuevo saldo). Tu editor y tu verificador de tipos usan esto para detectar errores; en tiempo de ejecución el bus simplemente devuelve lo que devolvió el manejador.

Los comandos de Lumen viven en tres archivos separados bajo `lumen/core/services/wallets/`, uno por intención. Los construiremos uno a uno.

**Paso 1 — Escribe el comando `OpenWallet`.** Crea `open_wallet_command.py`. Lleva los dos datos necesarios para abrir un monedero —quién es su propietario y qué moneda contiene— y un gancho `validate()` que rechaza un propietario en blanco antes incluso de que el bus busque un manejador.

::: listing lumen/core/services/wallets/open_wallet_command.py | Listado 7.1 — OpenWallet: un comando congelado con validación incorporada
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet. Returns the generated wallet id."""

    owner_id: str
    currency: Currency

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.owner_id.strip():
            return ValidationResult.failure(
                "owner_id", "Owner id is required"
            )
        return ValidationResult.success()
:::

**Paso 2 — Escribe los comandos `DepositFunds` y `WithdrawFunds`.** Cada uno lleva un `wallet_id` al que apuntar y un `amount` en unidades menores, y valida que el id esté presente y que el importe sea positivo. Son deliberadamente gemelos casi idénticos: la misma forma, dirección opuesta.

::: listing lumen/core/services/wallets/deposit_funds_command.py | Listado 7.2 — DepositFunds: importe en unidades menores, sin campo de moneda
from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class DepositFunds(Command[int]):
    """Deposit ``amount`` minor units. Returns the new balance."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Deposit amount must be > 0"
            )
        return ValidationResult.success()
:::

::: listing lumen/core/services/wallets/withdraw_funds_command.py | Listado 7.3 — WithdrawFunds: la misma forma que DepositFunds
from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class WithdrawFunds(Command[int]):
    """Withdraw ``amount`` minor units. Returns the new balance."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Withdrawal amount must be > 0"
            )
        return ValidationResult.success()
:::

Cuatro decisiones de diseño están horneadas en cada comando:

- **`frozen=True`** hace que el dataclass sea inmutable en el mismo momento en que se construye. Los campos no pueden mutarse accidentalmente en una capa de la tubería antes de llegar a otra, y los mensajes inmutables son hasheables por defecto, lo cual es útil al almacenarlos o compararlos en las pruebas.

- **`validate()`** es un gancho asíncrono que el bus llama antes de despachar el manejador. `OpenWallet.validate` comprueba que `owner_id` no esté en blanco; `DepositFunds.validate` y `WithdrawFunds.validate` comprueban que el importe sea positivo. Estas precondiciones corresponden al comando: no requieren ninguna consulta a la base de datos y no pertenecen al agregado de dominio. El agregado impone invariantes que necesitan estado cargado (descubierto, coincidencia de moneda); los comandos imponen invariantes que se pueden conocer únicamente a partir de los campos. Mantener separadas estas dos capas significa que el agregado nunca se llama con datos estructuralmente incorrectos.

- **Sin campo `currency`** en `DepositFunds` ni en `WithdrawFunds`. La propia moneda del monedero es la única moneda válida para un depósito o una retirada, y el repositorio la resuelve una vez que el agregado está cargado. Llevar una moneda en el comando invitaría a discrepancias; el agregado impone la invariante a partir de su propio estado.

- **Nomenclatura en modo imperativo**: `DepositFunds`, no `WalletDeposit` ni `DepositFundsCommand`. Esto hace que el registro de comandos se lea como un rastro de auditoría de negocio —una secuencia de cosas que *ocurrieron*— en lugar de una lista de operaciones técnicas.

!!! note "Lo que acaba de ocurrir"
    Ahora tienes tres archivos pequeños, cada uno describiendo *una cosa que el sistema puede hacer*, sin más lógica que un par de comprobaciones de campo. Todavía no hay manejadores: estos comandos no hacen nada por sí solos. Esa es la idea: un comando es un sobre, no el trabajador que lo abre. A continuación escribirás los trabajadores (los manejadores) que realmente llevan a cabo cada intención.

### Implementar un manejador de comandos

Un manejador de comandos hereda de `CommandHandler[C, R]` e implementa exactamente un método: `do_handle`. Tú escribes el *qué*; el bus lo envuelve con el *cómo*.

**Ambos decoradores en cada manejador son obligatorios.** `@command_handler` registra la clase en el `HandlerRegistry` introspeccionando el primer argumento de tipo genérico; no se necesita registro manual. `@service` conecta el manejador al contenedor de inyección de dependencias de PyFly para que los argumentos del constructor se resuelvan e inyecten automáticamente en el arranque. El orden importa: `@command_handler` arriba, `@service` justo debajo. Sin `@service`, el contenedor de inyección de dependencias nunca instancia la clase y el bus no puede encontrar el manejador; sin `@command_handler`, el registro nunca mapea el tipo de comando a la clase. Omitir cualquiera de los dos decoradores es un fallo silencioso: el bus lanza «no handler found» en el momento del despacho.

**`@transactional()` convierte `do_handle` en una unidad de trabajo confirmada.** Los manejadores de comandos inyectan `session_factory: async_sessionmaker[AsyncSession]` y la almacenan como `self._session_factory`. Cuando `@transactional()` ejecuta `do_handle`, abre una sesión nueva desde esa factoría, la intercambia en el repositorio durante la llamada, confirma en caso de éxito y revierte ante cualquier excepción. Sin `@transactional()`, la sesión compartida del framework solo hace flush: la escritura sobrevive dentro de la petición pero nunca se confirma en la base de datos.

!!! note "Flush frente a commit, en términos sencillos"
    Un **flush** empuja tus cambios pendientes a la conexión de la base de datos para que las consultas posteriores en la *misma* sesión puedan verlos, pero siguen estando dentro de una transacción abierta que se puede revertir. Un **commit** los hace permanentes. Sin `@transactional()`, tu depósito haría flush (visible a mitad de la petición) pero nunca commit (desaparecería después de la petición). El decorador es lo que hace que el cambio persista.

**Paso 3 — Escribe `OpenWalletHandler`.** Ahora construye el trabajador para el primer comando. Crea `open_wallet_handler.py`, apila `@command_handler` sobre `@service`, inyecta el repositorio, el publicador de eventos y la factoría de sesiones, e implementa el único método `do_handle`.

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listado 7.4 — OpenWalletHandler: unidad de trabajo @transactional() + upsert
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
    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
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

Recorre `do_handle` paso a paso. `f"wlt-{uuid4()}"` genera un identificador estable con prefijo. `Wallet.open(...)` llama a la factoría, que impone la precondición de propietario no vacío y bufferiza un evento `WalletOpened`. `to_entity(wallet)` mapea el agregado a una fila plana `WalletEntity`. `repository.upsert(...)` llama a `session.merge` —una sola llamada que inserta si no existe ninguna fila o actualiza si existe— y luego hace flush. Usar `upsert` en lugar de `save` evita un `IntegrityError` en la clave primaria: el agregado es dueño de su id, así que tanto INSERT como UPDATE usan la misma cadena estable como clave. `wallet.clear_events()` drena el búfer y `publish_domain_events` reenvía cada evento al bus de EDA. El decorador `@transactional()` confirma la sesión al salir. El manejador devuelve el ID del monedero, que fluye de vuelta al controlador como el valor de retorno de `send`.

Observa el requisito del constructor: `super().__init__()` es obligatorio en `CommandHandler`. Si lo omites, la contabilidad interna de la clase base —contexto de correlación, ganchos de ciclo de vida— nunca se inicializa. El repositorio, `EventPublisher` y `session_factory` los inyecta el contenedor de inyección de dependencias a partir de las anotaciones de tipo; no se necesita ninguna configuración de factoría.

**Paso 4 — Escribe los manejadores de depósito y retirada.** Estos dos añaden un movimiento que `OpenWalletHandler` no necesitaba: *cargan* un monedero existente antes de actuar sobre él. La forma es la misma en ambos, diferenciándose solo en si llaman a `wallet.deposit(...)` o a `wallet.withdraw(...)`.

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 7.5 — DepositFundsHandler: find_by_id → to_aggregate → act → upsert
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
    async def do_handle(self, command: DepositFunds) -> int:  # type: ignore[override]
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

::: listing lumen/core/services/wallets/withdraw_funds_handler.py | Listado 7.6 — WithdrawFundsHandler: patrón idéntico, el descubierto lo rechaza el agregado
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from an existing wallet; returns the new balance."""

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
    async def do_handle(self, command: WithdrawFunds) -> int:  # type: ignore[override]
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

`DepositFundsHandler` y `WithdrawFundsHandler` siguen el patrón clásico: **find → to_aggregate → act → to_entity → upsert → drain**. `repository.find_by_id` devuelve la fila plana `WalletEntity`; `to_aggregate(entity)` rehidrata el objeto de dominio rico para que las invariantes del agregado estén en alcance. `Money` se construye a partir del `amount` del comando y de la moneda del *monedero* —nunca una moneda del propio comando— porque el monedero es dueño de esa invariante. Si `wallet.withdraw` rechaza (el saldo quedaría negativo), lanza `BusinessRuleViolation`, que se propaga como HTTP 422 sin una sola línea de código de manejo de errores en el manejador.

Fíjate en lo que está ausente: ningún bloque try/except, ninguna llamada de registro, ninguna configuración de trazas. Todo eso pertenece a la tubería del bus. El manejador es una expresión pura de la intención de negocio.

!!! note "Lo que acaba de ocurrir"
    El lado de escritura está ya completo: tres comandos y tres manejadores. Enviar `OpenWallet` crea y persiste un monedero nuevo; enviar `DepositFunds` o `WithdrawFunds` carga uno, impulsa su comportamiento de dominio y lo guarda. La pila `@command_handler` + `@service` significa que PyFly los descubre y conecta en el arranque: nunca los llamas directamente y nunca los registras a mano.

**Ejecútalo: confirma que el lado de escritura funciona de principio a fin.** El ejemplo de Lumen ya incluye una prueba que ejercita la ruta completa de comandos. Desde el directorio `samples/lumen`, ejecuta solo esa prueba:

::: listing terminal | Listado 7.4a — Ejercitar la ruta de comandos
uv run --extra dev pytest tests/test_cqrs_flow.py::test_full_wallet_lifecycle -q
:::

Deberías ver una única prueba que pasa:

```
1 passed in 0.42s
```

Esa única prueba abre un monedero, deposita 1 500 unidades menores, retira 500 y afirma que el saldo queda en 1 000, demostrando que `OpenWalletHandler`, `DepositFundsHandler` y `WithdrawFundsHandler` confirman todos a través del bus. Si ves `0 items collected`, no estás en el directorio `samples/lumen`; haz `cd` allí primero. Si ves `no handler found`, comprueba dos veces que ambos decoradores estén presentes en cada manejador: esa es la causa más común con diferencia.

### El mapeador entidad↔agregado

Los manejadores de comandos no interactúan con el repositorio a través del agregado de dominio. Interactúan a través de una fila plana `WalletEntity` —la forma de persistencia que entiende el `Repository[WalletEntity, str]` del framework— y usan `wallet_mapper` para traducir entre los dos mundos:

```python
# Aggregate → row (before upsert)
to_entity(wallet)      # Wallet → WalletEntity

# Row → aggregate (after find_by_id)
to_aggregate(entity)   # WalletEntity → Wallet
```

Esta separación mantiene el agregado libre de anotaciones de SQLAlchemy y el repositorio libre de lógica de dominio. El mapeador es un único módulo; cambiar el esquema de almacenamiento toca un archivo, no cada manejador.

### Enviar un comando

El `CommandBus` es el único punto de entrada para todas las escrituras. La autoconfiguración de PyFly registra un `DefaultCommandBus` como singleton en el contenedor de inyección de dependencias; decláralo como argumento del constructor y el framework lo inyecta. Enviar un comando es una única llamada con await:

```python
from pyfly.cqrs import DefaultCommandBus
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.interfaces.enums.v1.currency import Currency

wallet_id: str = await command_bus.send(
    OpenWallet(owner_id="u-1", currency=Currency.EUR)
)
balance: int = await command_bus.send(
    DepositFunds(wallet_id=wallet_id, amount=1500)
)
```

`send` es una corrutina: siempre hazle `await`. El valor de retorno es lo que devolvió `do_handle`: un ID de monedero `str` para `OpenWallet`, y el nuevo saldo como un `int` (unidades menores) para `DepositFunds` y `WithdrawFunds`. Si algo en la tubería falla —validación, autorización o el propio manejador—, la excepción se envuelve en `CommandProcessingException` y se propaga fuera de `send`, donde el manejador de errores global la mapea al código de estado HTTP apropiado.

::: figure art/figures/07-cqrs.svg | Figura 7.1 — Los comandos fluyen al modelo de escritura; las consultas, al modelo de lectura.

!!! spring "Equivalencia con Spring"
    `CommandBus.send(command)` es el equivalente en Python de `CommandGateway.send(command)` o `CommandGateway.sendAndWait(command)` del framework Axon. Cada clase de manejador de comandos corresponde a un método anotado con `@CommandHandler` en Axon, o a un `@MessageHandler` en el modelo ApplicationEventPublisher de Spring Modulith. El decorador `@command_handler` es la contrapartida de PyFly de `@CommandHandler`: registra el manejador en el registro introspeccionando el parámetro de tipo genérico, exactamente igual que Axon resuelve los métodos manejadores por el tipo del parámetro. La apilación de `@service` refleja el hecho de que en Spring cada bean `@CommandHandler` es también un `@Component` de Spring: el registro y la inyección son inseparables. El decorador `@transactional()` se corresponde directamente con `@Transactional` de Spring: ambos abren una sesión de unidad de trabajo, confirman en caso de éxito y revierten ante cualquier excepción, de modo que `upsert` (respaldado por `session.merge`) es el análogo en Python de `repository.save()` dentro de un método `@Transactional`.

---

## Consultas y manejadores de consultas

Los comandos viajan en una dirección: hacia el modelo de escritura. Las consultas son el viaje de vuelta: le piden al sistema una proyección del estado actual y esperan una respuesta, no un efecto secundario.

Una **consulta** es un dataclass congelado que hereda de `Query[R]`, donde `R` es el tipo del resultado. Como los comandos, las consultas son mensajes inmutables, pero no llevan intención de cambiar el estado. `query_bus.query(GetBalance(...))` carga datos frescos del repositorio y devuelve un DTO tipado. Las consultas no necesitan `@transactional()`: las lecturas no mutan el estado, así que no hay nada que confirmar ni revertir.

Las consultas devuelven **DTO de lectura** en lugar de agregados de dominio. La separación es deliberada. Si `GetWalletHandler` devolviera un agregado `Wallet`, la capa de API quedaría acoplada a cada campo del agregado: un cambio en el modelo de dominio podría romper silenciosamente el contrato de la API. Un modelo Pydantic `WalletDto` dedicado proyecta exactamente los campos que necesita la respuesta HTTP. ¿Añades un campo a `Wallet`? La proyección cambia solo si lo incluyes explícitamente en el DTO. ¿Eliminas un campo de `Wallet`? La proyección compila hasta que la limpies.

El lado de lectura refleja el lado de escritura paso a paso —mensaje de consulta y luego manejador de consulta—, pero con dos simplificaciones: sin `@transactional()` (nada que confirmar) y sin publicación de eventos (nada cambió).

**Paso 5 — Escribe las consultas de búsqueda única.** Crea `get_wallet_query.py` y `get_balance_query.py`. Ambas llevan solo un `wallet_id`; lo que difiere es lo que prometen devolver.

::: listing lumen/core/services/wallets/get_wallet_query.py | Listado 7.7 — GetWallet: una consulta de búsqueda única que devuelve un WalletDto completo
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetWallet(Query[WalletDto | None]):
    """Look up a wallet by its identifier."""

    wallet_id: str
:::

::: listing lumen/core/services/wallets/get_balance_query.py | Listado 7.8 — GetBalance: una consulta más ligera que devuelve solo la proyección del saldo
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetBalance(Query[BalanceDto | None]):
    """Look up just the balance of a wallet by its identifier."""

    wallet_id: str
:::

Ambas consultas llevan solo `wallet_id`. `GetWallet` devuelve un `WalletDto` —la representación completa, incluyendo `id`, `owner_id`, `currency`, `balance_minor`, `balance` y `created_at`—. `GetBalance` devuelve un `BalanceDto` —una proyección más ligera que omite `owner_id` y `created_at`—. Un sondeo de saldo no necesita el propietario; dejar fuera esos campos ahorra ancho de banda y evita exponer accidentalmente la titularidad de la cuenta en una respuesta que quien llama podría registrar. Mantener las dos consultas separadas significa que puedes ajustar cada una de forma independiente —caché, autorización o un almacén de lectura dedicado— sin tocar la otra.

**Paso 6 — Escribe los manejadores de las consultas de búsqueda única.** Los manejadores de consultas viven bajo el mismo paquete `wallets/` que los comandos. **Se aplica la misma apilación `@query_handler` + `@service`**: `@query_handler` registra la clase en el registro de manejadores; `@service` la conecta al contenedor de inyección de dependencias. Ambos decoradores son obligatorios por las mismas razones que en los manejadores de comandos. Fíjate en cuánto más pequeños son que los manejadores de comandos: sin factoría de sesiones, sin publicador de eventos, solo el repositorio y una proyección de una línea.

::: listing lumen/core/services/wallets/get_wallet_handler.py | Listado 7.9 — GetWalletHandler: find_by_id → entity_to_dto → return
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetWalletHandler(QueryHandler[GetWallet, WalletDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetWallet) -> WalletDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_dto(entity) if entity is not None else None
:::

::: listing lumen/core/services/wallets/get_balance_handler.py | Listado 7.10 — GetBalanceHandler: vista @projection mediante Mapper.project
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetBalance) -> BalanceDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

Ambos manejadores delegan la proyección en `wallet_mapper` —el único módulo dueño de la forma del DTO—. `entity_to_dto` rellena los seis campos de `WalletDto` directamente desde la fila. `entity_to_balance_dto` toma un camino distinto: llama a `Mapper.project(entity, BalanceView)` contra una interfaz marcada con `@projection` que declara exactamente los cuatro campos que necesita el endpoint de saldo, con una transformación registrada que calcula `balance` (unidades mayores) a partir de `balance_minor`. El mapeador copia solo esos campos declarados —un equivalente del lado de lectura de las proyecciones por interfaz de Spring Data—. Ninguno de los dos manejadores toca el modelo Pydantic directamente; renombrar un campo toca un archivo.

### Consultas paginadas y por especificación

El lado de lectura no se detiene en las búsquedas de un único recurso. Los sistemas de producción necesitan listas con metadatos de paginación y la capacidad de filtrar por predicados en tiempo de ejecución. El framework gestiona ambas cosas a través de la clase base `Repository`.

!!! note "Pageable y Specification, en términos sencillos"
    Un **`Pageable`** agrupa tres cosas que necesita un endpoint de listado: qué página quieres, qué tamaño tiene cada página y cómo ordenar. Una **`Specification`** es un filtro reutilizable y componible —piénsalo como una cláusula `WHERE` que puedes construir como objeto y combinar con `&` (and), `|` (or) y `~` (not) antes de que llegue siquiera al SQL—. Ambas provienen de la capa de datos del framework; tú no escribes el SQL.

**Paso 7 — Escribe las consultas de listado y sus manejadores.** `ListWallets` envuelve un `Pageable` (número de página, tamaño, ordenación) y le pide al repositorio una porción contada, ordenada y limitada. `ListRichWallets` añade un umbral `min_minor` y lo ejecuta a través de una `Specification` componible. Construye primero los dos mensajes de consulta y luego sus manejadores.

::: listing lumen/core/services/wallets/list_wallets_query.py | Listado 7.11 — ListWallets: una consulta que lleva un Pageable
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.data import Page, Pageable
from pyfly.cqrs import Query


@dataclass(frozen=True)
class ListWallets(Query[Page[WalletDto]]):
    """List wallets, one page at a time."""

    pageable: Pageable
:::

::: listing lumen/core/services/wallets/list_rich_wallets_query.py | Listado 7.12 — ListRichWallets: añade un umbral de saldo para el filtrado por Specification
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.data import Page, Pageable
from pyfly.cqrs import Query


@dataclass(frozen=True)
class ListRichWallets(Query[Page[WalletDto]]):
    """List wallets whose balance is at least ``min_minor``, paged."""

    min_minor: int
    pageable: Pageable
:::

::: listing lumen/core/services/wallets/list_wallets_handler.py | Listado 7.13 — ListWalletsHandler: find_all(pageable) + Page.map
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListWalletsHandler(QueryHandler[ListWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_all(query.pageable)
        return page.map(entity_to_dto)
:::

::: listing lumen/core/services/wallets/list_rich_wallets_handler.py | Listado 7.14 — ListRichWalletsHandler: Specification + find_all_by_spec_paged
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListRichWalletsHandler(QueryHandler[ListRichWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListRichWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_rich(query.min_minor, query.pageable)
        return page.map(entity_to_dto)
:::

`find_all(pageable)` se hereda de la clase base `Repository` del framework. Cuenta el total de filas, aplica la ordenación del `Pageable` y rebana con `LIMIT`/`OFFSET`, devolviendo un `Page[WalletEntity]` que lleva `items`, `total`, `page`, `size`, `total_pages`, `has_next` y `has_previous`. `Page.map(entity_to_dto)` transforma los elementos sin tocar los metadatos. El controlador envuelve el resultado en un `PageDto` para el cable.

`find_rich` está definido en el propio `WalletRepository` y delega en el heredado `find_all_by_spec_paged`. Construye una `Specification` —un predicado `WHERE` componible— y la pasa junto con el `Pageable`. El framework añade la cláusula `WHERE`, la ordenación y el `LIMIT`/`OFFSET`, y luego ejecuta una consulta de conteo para el total. El manejador llama a `repo.find_rich(query.min_minor, query.pageable)` y mapea la página exactamente como antes.

Ejecutar una consulta pasa por `QueryBus.query`:

```python
from pyfly.cqrs import DefaultQueryBus
from lumen.core.services.wallets.get_balance_query import GetBalance

balance_dto = await query_bus.query(GetBalance(wallet_id="wlt-001"))
```

El valor de retorno es lo que devolvió `do_handle` —un `BalanceDto` o `None`—. `None` significa que el monedero no se encontró. El controlador es responsable de traducir eso a HTTP 404, manteniendo las preocupaciones HTTP fuera del manejador.

!!! note "Las consultas devuelven None, no excepciones"
    Los manejadores de consultas devuelven `None` cuando el recurso no se encuentra, en lugar de lanzar `AggregateNotFound`. Esto es una convención deliberada: una consulta que no encuentra nada no es un error, es una respuesta. El controlador convierte un resultado `None` en una respuesta 404, manteniendo la preocupación HTTP fuera del manejador.

!!! note "Lo que acaba de ocurrir"
    Ambos lados de CQRS existen ya. Tres comandos y tres manejadores cambian el estado; cuatro consultas y cuatro manejadores lo leen. Ninguno de ellos sabe nada de HTTP, y ninguno se registra a mano: los decoradores lo hacen en el arranque. Lo único que falta es la frontera HTTP que convierte una petición web en un mensaje y el resultado de un mensaje en una respuesta web. Esa es el controlador, que construyes a continuación.

**Ejecútalo: confirma que cada manejador está registrado.** Antes de tocar el controlador, demuestra que el bus descubrió todos tus manejadores. Arranca la aplicación y luego pregúntale al indicador de salud de CQRS cuántos manejadores encontró. Desde el directorio `samples/lumen`:

::: listing terminal | Listado 7.14a — Arrancar Lumen
uv run pyfly run --server uvicorn
:::

En una segunda terminal, consulta el endpoint de salud del actuator. En v26.6.110 el actuator vive en su propio puerto de gestión, **9090** por defecto, no en el 8080 de la aplicación:

::: listing terminal | Listado 7.14b — Contar los manejadores registrados
curl -s localhost:9090/actuator/health | python -m json.tool
:::

Busca el bloque `cqrs_health_indicator`. Con todos los manejadores de comandos y consultas de este capítulo en su sitio, reporta tres manejadores de comandos y cuatro de consultas:

```json
"cqrs_health_indicator": {
  "status": "UP",
  "details": {"command_handlers": 3, "query_handlers": 4}
}
```

Si un recuento es menor de lo que esperas, a un manejador le falta uno de sus dos decoradores y el registro nunca lo mapeó. Detén el servidor con `Ctrl-C` cuando termines.

!!! note "El actuator vive ahora en su propio puerto"
    En PyFly v26.6.110 la API de negocio y el actuator se ejecutan en puertos **separados**, al estilo de Spring. Tus endpoints de monedero escuchan en `pyfly.server.port` (por defecto **8080**), mientras que los endpoints del actuator y el panel de administración escuchan en `pyfly.management.server.port` (por defecto **9090**), que está abierto y sin autenticar por defecto. Lumen mantiene los valores por defecto, así que la salud está en `localhost:9090/actuator/health` y la API de monederos está en `localhost:8080/api/v1/wallets`. La exposición HTTP por defecto del actuator es `health,info`; expón más mediante `pyfly.management.endpoints.web.exposure.include`. Bloquea el puerto de gestión en producción con `pyfly.management.security.enabled: true`, o desactívalo por completo con `pyfly.management.server.port: -1`.

---

## Conectar el bus al controlador

El controlador es la frontera HTTP del sistema. Su único trabajo es traducir una petición HTTP en un mensaje de dominio y mapear el resultado de vuelta a una respuesta HTTP. Todo lo que hay en medio pertenece al bus y a los manejadores, y esa frontera se vuelve mucho más limpia una vez que el controlador despacha comandos y consultas en lugar de llamar a métodos de servicio directamente.

Antes de CQRS, `WalletController` mantenía una referencia a `WalletApplicationService` y llamaba a sus métodos directamente. Cada vez que la interfaz del servicio cambiaba —un nuevo parámetro, un método renombrado, un tipo de retorno distinto—, el controlador tenía que cambiar también. Con CQRS, el controlador sabe una sola cosa: qué mensaje enviar.

El controlador inyecta `DefaultCommandBus` y `DefaultQueryBus` por tipo —las **clases de bus concretas**, no protocolos abstractos—. Esta es la importación correcta:

```python
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
```

¿Por qué clases concretas? La autoconfiguración de CQRS de PyFly registra exactamente una instancia de cada bus en el contenedor de inyección de dependencias. Inyectar por el tipo concreto es inequívoco: sin despacho por protocolo, y el verificador de tipos ve toda la superficie de `send` / `query`. Usar un alias de protocolo requeriría un enlace explícito en el contenedor; el tipo concreto funciona de fábrica.

### Orden de las rutas: por qué los manejadores de un solo recurso se llaman `wallet_*`

El framework registra las rutas de un controlador en **orden alfabético por nombre de método**; el enrutador de Starlette aplica entonces la coincidencia «gana el primero registrado». Esto significa que un segmento literal como `/rich` debe registrarse *antes* que la variable de ruta `/{wallet_id}`; de lo contrario, cada petición `GET /api/v1/wallets/rich` coincidiría con la ruta variable y buscaría un monedero cuyo id es la cadena `"rich"`.

Los manejadores de colección se llaman `list_wallets` y `list_rich_wallets`; los manejadores de un solo recurso se llaman `wallet_detail` y `wallet_balance`. Alfabéticamente, `l` va antes que `w`, así que las rutas de colección (`GET /`, `GET /rich`) se registran siempre por delante de las rutas parametrizadas (`GET /{wallet_id}`, `GET /{wallet_id}/balance`). Si renombras `wallet_detail` por algo que ordene antes que `list_*`, la ruta `/rich` se romperá silenciosamente.

**Paso 8 — Conecta los buses al controlador.** Sustituye la antigua dependencia de `WalletApplicationService` por los dos buses, y luego convierte cada endpoint en una sola línea: construye un comando o consulta a partir de la petición, despáchalo y devuelve el resultado. Aquí está el controlador completo.

::: listing lumen/web/controllers/wallet_controller.py | Listado 7.15 — WalletController: DefaultCommandBus + DefaultQueryBus + endpoints de listado paginado
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.page_dto import PageDto
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.data import Pageable, Sort
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body, PathVar, QueryParam, Valid,
    get_mapping, post_mapping, request_mapping,
)

#: Newest-first ordering shared by the list endpoints.
_NEWEST_FIRST = Sort.by("created_at").descending()


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, list, inspect."""

    def __init__(
        self, commands: DefaultCommandBus, queries: DefaultQueryBus
    ) -> None:
        self._commands = commands
        self._queries = queries

    # --- commands --------------------------------------------------------

    @post_mapping("", status_code=201)
    async def open_wallet(
        self, request: Valid[Body[OpenWalletRequest]]
    ) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(owner_id=request.owner_id, currency=request.currency)
        )
        return {"wallet_id": wallet_id}

    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            DepositFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    @post_mapping("/{wallet_id}/withdraw")
    async def withdraw(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            WithdrawFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # --- paged / specification queries (registered before /{wallet_id}) --

    @get_mapping("")
    async def list_wallets(
        self, page: QueryParam[int] = 1, size: QueryParam[int] = 20
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListWallets(pageable=Pageable.of(page, size, _NEWEST_FIRST))
        )
        return PageDto.from_page(result)

    @get_mapping("/rich")
    async def list_rich_wallets(
        self,
        min_minor: QueryParam[int] = 0,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListRichWallets(
                min_minor=min_minor,
                pageable=Pageable.of(page, size, _NEWEST_FIRST),
            )
        )
        return PageDto.from_page(result)

    # --- single-wallet queries (named wallet_* so they sort after list_*) -

    @get_mapping("/{wallet_id}")
    async def wallet_detail(self, wallet_id: PathVar[str]) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(
            GetBalance(wallet_id=wallet_id)
        )
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
:::

Compara el constructor con su forma anterior a CQRS. Antes, el controlador tomaba `WalletApplicationService` —una clase concreta cuyas firmas de método filtraban lógica de negocio hacia la capa HTTP—. Ahora toma `DefaultCommandBus` y `DefaultQueryBus` —dos canales opacos—. El controlador sabe *qué* enviar; no sabe nada sobre *cómo* se procesa el mensaje.

Mira `open_wallet`. Antes, llamaba a `self._service.open_wallet(owner_id=..., currency=...)` —un contrato posicional que se rompe cada vez que el servicio gana un nuevo parámetro—. Ahora construye `OpenWallet(owner_id=request.owner_id, currency=request.currency)` —un objeto con nombre e inmutable cuyos campos son su propia API—. ¿Añades un campo al comando? El controlador permanece igual hasta que decidas rellenarlo.

Los DTO de petición (`OpenWalletRequest`, `DepositRequest`) son modelos Pydantic en `lumen/interfaces/dtos/v1/`. `OpenWalletRequest` valida la longitud de `owner_id` y restringe `currency` al enum `Currency`. `DepositRequest` lo comparten tanto el endpoint de depósito como el de retirada: ambos mueven un `amount` positivo en la propia moneda del monedero. Las restricciones a nivel de campo en esos DTO las impone `Valid[Body[...]]` antes incluso de que se llame al manejador.

Los endpoints de listado paginado (`list_wallets`, `list_rich_wallets`) construyen un `Pageable` a partir de los parámetros de la cadena de consulta, despachan la consulta a través del bus y envuelven el `Page[WalletDto]` resultante en un `PageDto` para el cable. `PageDto` es un modelo Pydantic que refleja todos los campos de metadatos de `Page` —`total`, `total_pages`, `has_next`, `has_previous`—, de modo que los clientes obtienen sobres de paginación consistentes sin un serializador personalizado.

Los métodos `wallet_detail` y `wallet_balance` muestran la única preocupación HTTP que queda en el controlador: traducir un resultado de consulta `None` a un 404 mediante `ResourceNotFoundException`. Ese mapeo corresponde aquí porque 404 es un código de estado HTTP y el manejador deliberadamente no tiene conocimiento de HTTP. Los tipos de retorno se declaran como `WalletDto` y `BalanceDto` —modelos Pydantic que el framework serializa a JSON automáticamente—.

!!! tip "Deja que el bus lance"
    No necesitas capturar `CommandProcessingException` ni `QueryProcessingException` en el controlador a menos que quieras personalizar la forma del error. El manejador de excepciones global mapea `AggregateNotFound` a 404 y `BusinessRuleViolation` a 422 —igual que antes—. Las excepciones del bus propagan esas originales de forma transparente.

**Ejecútalo: recorre la ruta HTTP completa.** La rebanada vertical está completa: petición HTTP → comando/consulta → bus → manejador → dominio → repositorio → respuesta. Demuéstralo desde fuera. Arranca la aplicación (`uv run pyfly run --server uvicorn`) y luego, en una segunda terminal, abre un monedero:

::: listing terminal | Listado 7.15a — Abrir un monedero por HTTP
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"u-1","currency":"EUR"}'
:::

El endpoint `open_wallet` despacha `OpenWallet` y devuelve el id generado:

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c"}
```

Copia ese id, deposita en él y luego vuelve a leer el saldo:

::: listing terminal | Listado 7.15b — Depositar y luego leer el saldo
curl -s -X POST localhost:8080/api/v1/wallets/wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c/deposit \
  -H 'content-type: application/json' -d '{"amount":1500}'

curl -s localhost:8080/api/v1/wallets/wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c/balance
:::

El depósito devuelve el nuevo saldo en unidades menores; la consulta de saldo devuelve el `BalanceDto`:

```json
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c", "balance_minor": 1500}
{"wallet_id": "wlt-c5bbb2a7-dd49-4321-932e-e4c6bfa5cc2c", "balance_minor": 1500, "balance": 15.0}
```

Por último, confirma que la decisión de orden de rutas de antes da sus frutos: lista los monederos y el subconjunto «ricos»:

::: listing terminal | Listado 7.15c — Ambas rutas de listado resuelven correctamente
curl -s 'localhost:8080/api/v1/wallets?page=1&size=20'
curl -s 'localhost:8080/api/v1/wallets/rich?min_minor=1000'
:::

Ambas devuelven un sobre `PageDto` (`items`, `total`, `total_pages`, `has_next`, `has_previous`). La llamada `/rich` resuelve a `list_rich_wallets`, *no* a `wallet_detail` buscando un monedero cuyo id sea la cadena literal `"rich"`, precisamente porque `list_*` ordena antes que `wallet_*`. Si `/rich` alguna vez devuelve un 404, ese orden se ha roto; revisa la regla de nombres de método de arriba.

!!! warning "Usa un id real"
    El id de monedero de arriba es ilustrativo: el tuyo será distinto en cada llamada a `open_wallet`. Pega el id que devuelva tu propio `POST /api/v1/wallets` en las URL de depósito y saldo, o recibirás un 404.

---

## La tubería del manejador

Una sola llamada a `send` o `query` desencadena más que solo el manejador. Entender la tubería te dice dónde poner cada preocupación transversal y, lo que es igual de importante, dónde *no* ponerla.

!!! note "¿Qué es una «tubería»?"
    Una **tubería** es simplemente una secuencia fija de pasos que el bus ejecuta alrededor de tu manejador, como una cadena de montaje. Tu mensaje entra por un extremo, pasa por la validación y la autorización, se maneja y (para los comandos) se publican sus eventos, antes de que el resultado salga por el otro extremo. Tú escribes solo el paso del medio (`do_handle`); el bus es dueño del resto, de forma idéntica para cada mensaje.

La tubería se define una sola vez, en el bus, y se aplica uniformemente a cada manejador. Nunca escribes lógica de tubería dentro de un manejador. El orden es estricto:

| Paso | Dónde se define | Aplica a | Resultado del fallo |
|---|---|---|---|
| Validación de precondiciones de negocio | gancho `validate()` en el mensaje | Comandos + Consultas | `CqrsValidationException` (HTTP 422) |
| Autorización | gancho `authorize()` en el mensaje | Comandos + Consultas | `AuthorizationException` (HTTP 403) |
| Ejecución del manejador | `do_handle()` | Comandos + Consultas | Excepciones de dominio (4xx/5xx) |
| Publicación de eventos de dominio | tubería del bus (post-manejador) | Solo comandos | — |
| Limpieza del ID de correlación | tubería del bus (bloque finally) | Comandos + Consultas | — |

### Validación

Sin un paso de validación estructurado, cada manejador empezaría con sus propias cláusulas de guarda: comprobar que este campo no esté en blanco, comprobar que aquel importe sea positivo. Esa lógica se duplicaría entre manejadores y se probaría solo a través de rutas de integración. Centralizar la validación en el propio mensaje resuelve ambos problemas.

El bus invoca `validate()` antes de buscar el manejador. Si la validación falla, el bus lanza `CqrsValidationException` sin llegar nunca al manejador.

El gancho de validación es también el lugar adecuado para precondiciones entre campos que se pueden conocer únicamente a partir de los campos —demasiado simples para el agregado de dominio, demasiado específicas de la aplicación para el modelo de petición—:

```python
@dataclass(frozen=True)
class DepositFunds(Command[int]):
    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:
        if not self.wallet_id.strip():
            return ValidationResult.failure(
                "wallet_id", "Wallet id is required"
            )
        if self.amount <= 0:
            return ValidationResult.failure(
                "amount", "Deposit amount must be > 0"
            )
        return ValidationResult.success()
```

### Autorización

Una vez que un mensaje es estructuralmente válido, el bus pregunta: ¿está *permitido* a quien llama realizar esta operación? La autorización responde antes de que ocurra cualquier acceso a la base de datos —más eficiente y más seguro, ya que nunca cargas datos sensibles solo para descartarlos porque quien llama carecía de permiso—.

Tanto los comandos como las consultas exponen un gancho `authorize()`. Devuelve `AuthorizationResult.success()` para permitir la ejecución, o `AuthorizationResult.failure(resource, message)` para denegarla. El bus lanza `AuthorizationException` al denegar, mapeando a HTTP 403 mediante el manejador de errores global.

Una regla práctica limpia: usa `authorize()` en el comando para comprobaciones a **nivel de operación** —quién tiene permiso para llamar a este comando en absoluto— y deja las decisiones a **nivel de recurso** (¿puede quien llama acceder a *este monedero concreto*?) al manejador, que tiene el agregado cargado en alcance:

::: listing lumen/cqrs/commands_auth.py | Listado 7.16 — Gancho de autorización en un comando
from __future__ import annotations
from dataclasses import dataclass

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs import Command


@dataclass(frozen=True)
class CloseWallet(Command[None]):
    """Close a wallet.  Only internal service accounts may do this."""
    wallet_id: str
    requested_by: str

    async def authorize(self) -> AuthorizationResult:
        internal_accounts = {"ops-service", "compliance-bot"}
        if self.requested_by not in internal_accounts:
            return AuthorizationResult.failure(
                "wallet",
                "Only internal service accounts may close wallets",
            )
        return AuthorizationResult.success()
:::

`CloseWallet.authorize` comprueba un conjunto conocido de cuentas de servicio internas. Si `requested_by` no está en el conjunto, la autorización falla antes de que se llame al manejador. El conjunto normalmente provendría de un valor de configuración o de un claim de token inyectado en la frontera del controlador —aquí está codificado a fuego por legibilidad—. El punto clave es que la comprobación vive dentro del comando, no dispersa por el código del manejador.

### Trazas distribuidas

Cuando una única petición HTTP desencadena múltiples comandos —y cada comando puede llamar a servicios aguas abajo—, necesitas una forma de coser juntos todos los registros y spans. Eso es lo que proporciona `CorrelationContext`.

Ambos buses establecen un ID de correlación al inicio de cada ejecución de la tubería. Si el mensaje ya lleva un ID (establecido mediante `command.set_correlation_id(id)`), se usa ese ID; de lo contrario, se genera un nuevo UUID. El ID anterior siempre se restaura en un bloque `finally`, de modo que los despachos de comandos anidados dentro de la misma petición no pisotean la traza externa.

`CorrelationContext` se propaga a través de las cadenas de `await` mediante las `contextvars` de Python —no hay necesidad de pasar el ID manualmente por cada argumento de función—. Para la propagación entre servicios, serializa el contexto en las cabeceras salientes y restáuralo en el lado receptor:

```python
from pyfly.cqrs.tracing.correlation import CorrelationContext

# On the sending side
headers = CorrelationContext.create_context_headers()
# {"X-Correlation-ID": "...", "X-Trace-ID": "...", "X-Span-ID": "..."}

# On the receiving side
CorrelationContext.extract_context_from_headers(headers)
```

Las tres cabeceras —`X-Correlation-ID`, `X-Trace-ID` y `X-Span-ID`— siguen la nomenclatura de W3C Trace Context, así que son compatibles con infraestructura instrumentada con OpenTelemetry de fábrica.

!!! tip "Dónde poner la lógica transversal"
    La tubería del bus es el hogar adecuado para las preocupaciones que se aplican a *todas* las operaciones: validación, autorización, trazas y métricas. El manejador es el hogar adecuado para las preocupaciones específicas de *una* operación: cargar el agregado, impulsar el comportamiento, guardar, drenar eventos. Si te encuentras añadiendo un try/except a cada manejador, o copiando la misma comprobación de precondición en varios manejadores, eso pertenece a la tubería —ya sea como un gancho `validate()` en el comando o como un servicio a nivel de bus—. La tubería escala uniformemente; el código repetitivo del manejador, no.

---

## Lo que construiste {.recap}

La Parte II está completa. Lumen tiene ahora una rebanada vertical completa desde HTTP hasta el dominio y de vuelta —una construida sobre decisiones arquitectónicas que escalarán sin reescribir—.

En el Capítulo 5 le diste persistencia al sistema: un `WalletRepository` que subclasifica `Repository[WalletEntity, str]` —el repositorio genérico estilo Spring Data del framework, que proporciona `find_by_id`, `find_all(pageable)`, `find_all_by_spec_paged` y más de fábrica, con la `AsyncSession` inyectada por la autoconfiguración relacional—. En el Capítulo 6 promoviste el monedero a un agregado DDD propiamente dicho: `Money` como objeto de valor inmutable, `Wallet(AggregateRoot[str])` como frontera de consistencia que impone las invariantes de descubierto, coincidencia de moneda e importe positivo, con los eventos de dominio `WalletOpened`, `FundsDeposited` y `FundsWithdrawn` bufferizados en el agregado y drenados al bus de eventos tras un guardado exitoso.

En este capítulo separaste el modelo de escritura del modelo de lectura. `OpenWallet`, `DepositFunds` y `WithdrawFunds` son mensajes de comando congelados y validados que fluyen a través de `DefaultCommandBus` —una tubería que ejecuta validación, autorización, ejecución del manejador, publicación de eventos de dominio y trazas distribuidas automáticamente para cada comando—. Cada manejador de comandos lleva `@transactional()` en `do_handle`: el decorador abre una unidad de trabajo confirmada desde `self._session_factory`, intercambia la sesión en el repositorio, confirma en caso de éxito y revierte en caso de fallo. La persistencia pasa por `repository.upsert` —respaldado por `session.merge`—, de modo que INSERT y UPDATE comparten una única ruta de código con clave en el propio id del agregado.

`GetWallet` y `GetBalance` son mensajes de consulta que fluyen a través de `DefaultQueryBus` —la misma tubería sin el paso de publicación de eventos, y sin `@transactional()` porque las lecturas no confirman—. `GetBalanceHandler` proyecta a través de una interfaz `BalanceView` marcada con `@projection` y `Mapper.project`, copiando solo los campos declarados y aplicando una transformación de unidades mayores registrada. `ListWallets` y `ListRichWallets` completan el lado de consultas: `find_all(pageable)` devuelve un `Page[WalletEntity]` contado, ordenado y limitado por offset; `find_all_by_spec_paged` ejecuta un predicado `Specification` componible sobre la misma maquinaria de paginación. Ambos usan `Page.map(entity_to_dto)` para proyectar los elementos sin tocar los metadatos.

Cada manejador lleva la pila `@command_handler` + `@service` (o `@query_handler` + `@service`): el primer decorador registra la clase introspeccionando su argumento de tipo genérico; el segundo la conecta al contenedor de inyección de dependencias para que las dependencias del constructor se inyecten automáticamente.

`WalletController` ya no sabe nada de la capa de servicio. Inyecta `DefaultCommandBus` y `DefaultQueryBus`, construye un comando o consulta a partir de la petición HTTP, lo despacha y o bien devuelve el resultado o bien lanza una excepción de dominio. Los métodos manejadores de un solo recurso se llaman `wallet_detail` y `wallet_balance` —una elección deliberada para que ordenen alfabéticamente *después* de los métodos de colección `list_wallets` y `list_rich_wallets`, garantizando que el segmento literal `/rich` se registre antes que la ruta variable `/{wallet_id}`—.

Añadir un nuevo comando significa ahora tres cosas: definir un dataclass congelado, implementar un `do_handle` decorado con `@command_handler` + `@service` y anotado con `@transactional()`, y añadir un endpoint que llame a `self._commands.send`. La tubería se aplica automáticamente.

**Ejecútalo: todo el capítulo, en un solo comando.** Desde el directorio `samples/lumen`, ejecuta las pruebas del flujo CQRS una última vez para confirmar que cada pieza que construiste este capítulo sigue encajando:

::: listing terminal | Listado 7.17 — Verificar la rebanada CQRS completa
uv run --extra dev pytest tests/test_cqrs_flow.py -q
:::

Pasan los cinco escenarios: el ciclo de vida de camino feliz, la consulta de no encontrado, el descubierto rechazado, el depósito no positivo rechazado y el depósito a un monedero desconocido rechazado:

```
5 passed in 0.61s
```

Vale la pena detenerse en esos últimos cuatro casos: cada uno ejercita la *tubería*, no el manejador. El descubierto lo rechaza el agregado y aflora como `CommandProcessingException`; el depósito no positivo nunca llega a un manejador en absoluto porque `validate()` lo rechaza primero. No escribiste ni una sola línea de código de manejo de errores para conseguir nada de eso.

---

## Pruébalo tú mismo {.exercises}

1. **Traza el ciclo de vida completo en la batería de pruebas.** Abre `samples/lumen/tests/test_cqrs_flow.py` y ejecútalo contra una base de datos real usando Testcontainers (Capítulo 11). La prueba `test_full_wallet_lifecycle` abre un monedero, deposita 1 500 unidades menores, retira 500 y luego consulta tanto `GetWallet` como `GetBalance`. Recórrela con un depurador: confirma que `wallet.clear_events()` drena los eventos `FundsDeposited` y `FundsWithdrawn` después de cada llamada a `upsert`, y que `GetWallet` devuelve un `WalletDto` con `balance_minor == 1000` y `balance == 10.0`.

2. **Observa `upsert` frente a `save`.** En una prueba, llama a `DepositFunds` dos veces sobre el mismo monedero sin `@transactional()` y observa el `IntegrityError`. Luego restaura `@transactional()` y verifica que ambos depósitos se confirman. Abre `WalletRepository.upsert` y traza cómo `session.merge` resuelve el conflicto de clave primaria que un `INSERT` simple lanzaría.

3. **Añade una consulta `ListByOwner`.** Define `ListByOwner(Query[list[WalletDto]])` con un campo `owner_id: str`. Implementa `ListByOwnerHandler` —decorado con `@query_handler` + `@service`— que llame a `WalletRepository.find_by_owner_id(query.owner_id)` (el stub de consulta derivada ya existe) y mapee la lista de resultados con `entity_to_dto`. Añade un endpoint `GET /api/v1/wallets/by-owner/{owner_id}` a `WalletController`. Asegúrate de que el nombre del nuevo método de endpoint ordene antes que `wallet_detail` para que Starlette haga coincidir primero el segmento literal `/by-owner/…`.

4. **Añade autorización a `WithdrawFunds`.** Extiende `WithdrawFunds` con un campo `initiated_by: str`. Sobrescribe `authorize()` para que devuelva `AuthorizationResult.failure("withdraw", "Initiator is required")` cuando `initiated_by` esté en blanco, y `AuthorizationResult.success()` en caso contrario. Actualiza `WithdrawFundsHandler.do_handle` para registrar `command.initiated_by` en la carga útil del evento `FundsWithdrawn`. Escribe una prueba que llame a `await WithdrawFunds(wallet_id="wlt-1", amount=100, initiated_by="").authorize()` y afirme que el resultado deniega la autorización.
