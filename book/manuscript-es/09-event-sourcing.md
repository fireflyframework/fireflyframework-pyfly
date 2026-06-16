<span class="eyebrow">Capítulo 9</span>

# Event sourcing del libro mayor {.chtitle}

::: figure art/openers/ch09.svg | &nbsp;

El Capítulo 8 dejó sin resolver una grieta estructural. El `WalletAuditListener` mantiene un modelo de lectura rápido reaccionando a eventos de dominio, pero el estado canónico del monedero sigue siendo una fila de la tabla `wallets`, que guarda una única columna `balance`. Cada vez que el saldo cambia, el valor anterior desaparece para siempre. Si un auditor de cumplimiento pregunta "¿cuál era el saldo del monedero `w-001` a las 14:32 del 3 de marzo?", la respuesta honesta es: no puedes saberlo. La base de datos solo recuerda el presente.

El **event sourcing** (almacenamiento de eventos como fuente de verdad) le da la vuelta a este diseño. En lugar de almacenar el estado *actual* y descartar cada cambio, almacenas la *secuencia de cambios* y derivas el estado actual reproduciéndolos. La tabla `wallets` desaparece. En su lugar hay un **flujo de eventos** (event stream): un registro de solo anexado de cada evento `LedgerOpened`, `Credited` y `Debited` que el libro mayor haya producido jamás. El saldo en cualquier instante es una función pura de los eventos hasta ese momento. Puedes rebobinar hasta las 14:32 de cualquier fecha porque tienes un registro completo de todo lo ocurrido entre entonces y ahora.

Un libro mayor financiero es el dominio ideal para el event sourcing. Los contables han comprendido durante siglos que la autoridad de un libro mayor proviene de sus asientos, no de un total acumulado al pie de la columna. El total acumulado es un *hecho derivado*; los asientos son la *fuente de verdad*. El módulo `pyfly.eventsourcing` de PyFly lleva esa intuición contable al código: los agregados emiten eventos de dominio, un `EventStore` los registra de forma inmutable, un repositorio reproduce el flujo para reconstruir el estado y un `ProjectionRunner` construye modelos de lectura encima.

Este capítulo construye el agregado `LedgerAccount`, un objeto de dominio orientado a eventos creado a propósito que convive junto al `Wallet` con estado almacenado del Capítulo 6. Verás cada componente del módulo `pyfly.eventsourcing` y cómo el almacén de eventos, los snapshots, las proyecciones y el outbox transaccional colaboran para darle a Lumen un libro mayor que es a la vez auditable y eficiente.

---

## Del estado a los eventos

La forma más clara de captar el cambio es comparar qué aspecto tiene la base de datos en cada modelo.

En el **modelo de almacenamiento de estado**, la base de datos guarda solo el estado actual del monedero:

| wallet_id | owner_id | balance_cents | currency | updated_at |
|---|---|---|---|---|
| w-001 | u-42 | 8500 | EUR | 2026-03-03 17:11 |

Cada `deposit` y `withdraw` sobrescribe `balance_cents`. La historia se pierde. Sabes que el monedero contiene 85,00 EUR ahora mismo; no puedes saber cómo llegó hasta ahí.

En el **modelo de almacenamiento de eventos**, la base de datos guarda el flujo de eventos:

| stream_id | seq | event_type | payload | occurred_at |
|---|---|---|---|---|
| led-001 | 1 | LedgerOpened | `{"currency":"EUR","owner_id":"u-42"}` | 2026-03-01 09:00 |
| led-001 | 2 | Credited | `{"amount":10000,"balance":10000}` | 2026-03-01 09:01 |
| led-001 | 3 | Debited | `{"amount":1500,"balance":8500}` | 2026-03-03 17:11 |

El saldo actual sigue siendo 85,00 EUR, pero ahora puedes leer cada decisión que condujo hasta él. Un auditor, un regulador o un investigador de fraude pueden reproducir el flujo desde cualquier desplazamiento y ver exactamente qué ocurrió y cuándo.

::: figure art/figures/09-eventsourcing.svg | Figura 9.1 — Almacenamiento de estado frente a almacenamiento de eventos: un modelo conserva una instantánea del presente; el otro conserva cada hecho que condujo hasta él.

La contrapartida es real. El almacenamiento de eventos encarece las lecturas por defecto —debes reproducir el flujo para calcular el saldo actual— y exige disciplina en torno a la evolución del esquema (los eventos son inmutables; no puedes renombrar un campo a posteriori). Ambas preocupaciones tienen solución en PyFly: los **snapshots** aceleran la reproducción de flujos largos, y los **upcasters** traducen las formas de eventos antiguas a las nuevas durante la carga. Verás ambos antes del final de este capítulo.

!!! note "Los eventos como sistema de registro"
    El event sourcing no es lo mismo que la arquitectura orientada a eventos. El Capítulo 8 usó EDA: el agregado almacenaba su estado de forma normal y publicaba eventos de dominio como efecto secundario. El event sourcing va más allá: los eventos *son* el estado. No hay una columna `balance` separada que mantener sincronizada; el saldo lo calcula el repositorio cada vez que carga el agregado.

---

## Una base separada para los agregados con event sourcing

Antes de escribir una línea de código del libro mayor, importa una distinción de nombres. El Capítulo 6 construyó `Wallet` sobre `pyfly.domain.AggregateRoot`, la clase base con estado almacenado. `LedgerAccount` usa una clase base **diferente**: `pyfly.eventsourcing.AggregateRoot`. Las dos viven en paquetes separados y, deliberadamente, no están relacionadas.

| Aspecto | `Wallet` del Capítulo 6 | `LedgerAccount` del Capítulo 9 |
|---|---|---|
| Clase base | `pyfly.domain.AggregateRoot` | `pyfly.eventsourcing.AggregateRoot` |
| Evento de dominio | `pyfly.domain.DomainEvent` | `pyfly.eventsourcing.DomainEvent` |
| El estado vive en | una fila de base de datos | el flujo de eventos |
| Repositorio | `WalletRepository` (R2DBC) | `LedgerAccountRepository` (`EventSourcedRepository`) |

`pyfly.eventsourcing.AggregateRoot` aporta la maquinaria de event sourcing a través de cuatro miembros:

- **`when(EventType, handler)`** — registra un manejador (handler) para una clase de evento dada. El manejador recibe `(aggregate, event)` como dos argumentos y realiza la mutación: una lambda, una función libre o una sola línea que delega en un método privado.
- **`apply(event)`** — encamina un evento recién creado a través de su manejador registrado y lo encola para el almacén de eventos. Ambas cosas ocurren atómicamente: el estado en memoria se actualiza de inmediato, sin ningún viaje de ida y vuelta al almacén.
- **`replay(event_type, event)`** — vuelve a ejecutar un evento persistido a través del mismo manejador *sin* volver a encolarlo. El repositorio llama a esto durante la carga para reconstruir el estado a partir del flujo almacenado.
- **`version`** — un contador entero que se incrementa después de cada evento despachado; el almacén lo usa como token de concurrencia optimista.

El orden de despacho es: primero el manejador registrado con `when()`; luego un método llamado `on_{event_type}` si existe en el agregado; si no se encuentra ninguno, se lanza `EventHandlerException`. Un manejador ausente corrompería silenciosamente el estado reconstruido, así que el agregado falla de forma ruidosa en lugar de seguir adelante.

!!! warning "Manejador de dos argumentos: la trampa más común"
    Todo manejador registrado con `when()` se invoca como `handler(aggregate, event)`: dos argumentos. Un método ligado como `self._on_opened` tiene firma `(self, event)`, lo que lo convierte en un invocable de un solo argumento desde el exterior. Pasar un método ligado directamente provoca un `TypeError` en tiempo de ejecución. El patrón usado a lo largo de este capítulo es una lambda de una sola línea —`lambda agg, evt: agg._on_opened(evt)`— que es correctamente de dos argumentos y mantiene la lógica real en un método privado donde puede probarse unitariamente y comprobarse de tipos de forma independiente.

---

## El agregado con event sourcing

En el Capítulo 6, `Wallet` mantenía `_balance: Money` como estado de Python directo: `deposit` le sumaba, `withdraw` le restaba. En la versión con event sourcing, el agregado nunca muta sus propios campos directamente. Cada cambio de estado está mediado por un evento de dominio: el método de comportamiento *aplica* el evento, el manejador del evento *actualiza los campos* y el `EventStore` persiste el evento. Al cargar, el repositorio reproduce todos los eventos almacenados a través de los mismos manejadores, reconstruyendo el estado en memoria evento a evento.

Esta doble indirección —aplicar, luego manejar— es la mecánica central del event sourcing. Impone una disciplina estricta: cada transición de estado se registra exactamente una vez como un evento, y el estado actual del agregado siempre es demostrable a partir de su historia.

!!! note "Jerga: agregado, manejador, fold"
    Un **agregado** es una única frontera de consistencia: un objeto que posee un conjunto de estado relacionado y las reglas que lo mantienen válido. `LedgerAccount` es un agregado: su saldo y su regla de descubierto viven juntos. Un **manejador** (a veces llamado *apply-handler*) es la pequeña función que toma un evento y actualiza los campos del agregado. Un **fold** es el término de la programación funcional para recorrer una lista y acumular un resultado elemento a elemento; reproducir un flujo de eventos hasta obtener un saldo es exactamente un fold sobre la lista de eventos. Verás "fold" usado como sinónimo de "el manejador se ejecuta sobre cada evento".

**Constructor sin argumentos.** `LedgerAccount.__init__` no toma argumentos. Esto es obligatorio porque `EventSourcedRepository` llama a la fábrica como `LedgerAccount()` y luego asigna `.id` antes de reproducir el flujo. Nunca construyas un libro mayor nuevo pasando argumentos a `__init__`; en su lugar, llama al classmethod `open`.

Construiremos el agregado en cuatro movimientos y luego ejecutaremos las pruebas unitarias para demostrar que cada invariante se cumple. Lee primero el listado completo y después recorre los pasos que aparecen debajo.

Aquí están los eventos de dominio y el agregado:

::: listing lumen/models/entities/v1/ledger_account.py | Listado 9.1 — LedgerAccount: un agregado con event sourcing que deriva su saldo de la reproducción
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import BusinessRuleViolation
from pyfly.eventsourcing import AggregateRoot, DomainEvent


# --- Domain events — the durable facts of the ledger -----------------

@dataclass
class LedgerOpened(DomainEvent):
    """The ledger was opened for an owner in a single currency."""
    account_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass
class Credited(DomainEvent):
    """Money moved *into* the ledger (a deposit / inbound transfer leg)."""
    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass
class Debited(DomainEvent):
    """Money moved *out of* the ledger (withdrawal / outbound transfer leg)."""
    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


# --- Event-sourced aggregate root ------------------------------------

class LedgerAccount(AggregateRoot):
    """An event-sourced money-movement ledger.

    Zero-arg constructible so it can serve as the repository's factory —
    EventSourcedRepository calls LedgerAccount() then assigns .id before
    replaying the stream. Use the open() classmethod to create new ledgers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.owner_id: str = ""
        self.currency: Currency = Currency.EUR
        self.balance: Money = Money.zero(Currency.EUR)
        # Register apply-handlers. _dispatch calls handler(aggregate, event).
        # Use a lambda so the callable is two-arg; delegate to a private
        # method to keep the real logic type-checked and unit-testable.
        self.when(LedgerOpened, lambda agg, evt: agg._on_opened(evt))
        self.when(Credited,     lambda agg, evt: agg._on_credited(evt))
        self.when(Debited,      lambda agg, evt: agg._on_debited(evt))

    # --- factory ---------------------------------------------------------

    @classmethod
    def open(
        cls, account_id: str, owner_id: str, currency: Currency
    ) -> "LedgerAccount":
        """Open a new empty ledger; appends LedgerOpened."""
        if not owner_id.strip():
            raise BusinessRuleViolation(
                "ledger-owner-required", "owner_id is required"
            )
        account = cls()
        account.id = account_id
        account.apply(
            LedgerOpened(
                account_id=account_id,
                owner_id=owner_id,
                currency=currency.value,
            )
        )
        return account

    # --- commands (validate invariants, then apply) -----------------------

    def credit(self, amount: Money) -> None:
        """Record money entering the ledger; appends Credited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "ledger-credit-positive", "credit amount must be > 0"
            )
        new_balance = self.balance.add(amount)
        self.apply(
            Credited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=new_balance.amount,
            )
        )

    def debit(self, amount: Money) -> None:
        """Record money leaving; refuses to overdraw. Appends Debited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "ledger-debit-positive", "debit amount must be > 0"
            )
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "ledger-insufficient-funds",
                f"cannot debit {amount}; balance is {self.balance}",
            )
        self.apply(
            Debited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=remaining.amount,
            )
        )

    # --- apply-handlers (pure folds, shared by apply + replay) -----------

    def _on_opened(self, event: object) -> None:
        self.owner_id = event.owner_id          # type: ignore[attr-defined]
        self.currency = Currency(event.currency) # type: ignore[attr-defined]
        self.balance = Money.zero(self.currency)

    def _on_credited(self, event: object) -> None:
        self.balance = Money(
            event.balance, Currency(event.currency)  # type: ignore[attr-defined]
        )

    def _on_debited(self, event: object) -> None:
        self.balance = Money(
            event.balance, Currency(event.currency)  # type: ignore[attr-defined]
        )

    # --- helpers ---------------------------------------------------------

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.currency:
            raise BusinessRuleViolation(
                "ledger-currency-mismatch",
                f"ledger holds {self.currency.value}, "
                f"got {amount.currency.value}",
            )
:::

**Cómo funciona.** `__init__` registra tres manejadores `when()` —uno por clase de evento— cada uno como una lambda de dos argumentos que delega en un método privado. Dentro de los manejadores no ocurre ninguna aritmética; los métodos de comportamiento (`credit`, `debit`) poseen toda la validación y calculan el nuevo estado antes de construir el evento. El manejador simplemente *aplica* el resultado ya calculado.

`account.apply(Credited(...))` hace dos cosas atómicamente: anexa el evento al búfer de eventos pendientes (para que el repositorio pueda persistirlo) y lo despacha de inmediato al manejador de `Credited` (para que `balance` se actualice en memoria). El estado en memoria del agregado es, por tanto, siempre coherente con sus eventos pendientes, incluso antes de guardar.

El método de fábrica `open` llama a `apply(LedgerOpened(...))` en lugar de fijar los campos directamente. Eso es intencionado: si cargaras este agregado desde su flujo de eventos, `LedgerOpened` pasaría por exactamente el mismo manejador `_on_opened` y produciría resultados idénticos. La ruta de escritura y la ruta de reproducción son el mismo código; esa simetría es la garantía de corrección del event sourcing.

El contador `version` empieza en cero y se incrementa después de cada evento despachado. Tras `open`, `account.version == 1`; tras un crédito, `account.version == 2`. Volverás a ver este número cuando el `EventStore` imponga la concurrencia optimista.

**Construyéndolo paso a paso.** El listado resulta denso la primera vez. Aquí está el mismo código como cuatro movimientos deliberados: el orden en el que realmente lo escribirías.

**Paso 1 — Define los hechos duraderos.** Escribe las tres dataclasses de evento (`LedgerOpened`, `Credited`, `Debited`) que extienden `pyfly.eventsourcing.DomainEvent`. Dale a cada campo un valor por defecto (`account_id: str = ""`, `amount: int = 0`). Los valores por defecto no son cosméticos: el repositorio reconstruye estas dataclasses a partir de un payload almacenado, y una dataclass con campos obligatorios no podría reconstruirse cuando un evento antiguo es anterior a un campo más nuevo.

**Paso 2 — Prepara el agregado vacío.** Escribe `__init__` sin parámetros. Inicializa los campos a valores por defecto neutros (`owner_id = ""`, `balance = Money.zero(Currency.EUR)`) y registra un manejador `when()` por clase de evento, cada uno como una lambda de dos argumentos que delega en un método privado. En este punto, el agregado es un lienzo en blanco que sabe cómo reaccionar a los eventos pero no ha visto ninguno.

**Paso 3 — Añade la fábrica y los comandos.** Escribe el classmethod `open` y los métodos `credit` / `debit`. Cada uno valida primero sus invariantes (coincidencia de divisa, importe positivo, sin descubierto), calcula el saldo resultante y solo entonces llama a `self.apply(SomeEvent(...))`. La validación vive en el comando; el manejador nunca valida.

**Paso 4 — Escribe los folds puros.** Escribe `_on_opened`, `_on_credited`, `_on_debited`. Cada uno lee campos del evento y los asigna al agregado: sin aritmética, sin validación, sin excepciones. Como estos mismos tres métodos se ejecutan tanto en la ruta de escritura como en la de reproducción, mantenerlos tontos es lo que garantiza que un libro mayor recargado sea igual al que está en vivo.

!!! tip "Por qué la validación pertenece al comando, no al manejador"
    El manejador se vuelve a ejecutar cada vez que el agregado se carga desde su historia. Si la validación viviera en el manejador, estarías volviendo a comprobar la regla de descubierto contra datos que ya la pasaron hace años; y peor aún, una regla que *haya cambiado* desde entonces podría rechazar un evento histórico que era perfectamente válido cuando ocurrió. Valida una vez, en la escritura que produce el evento; confía en el evento para siempre después.

`pending_events()` devuelve los eventos encolados desde el último guardado. Las pruebas unitarias accionan el agregado de forma aislada —sin necesidad de repositorio—, lo que hace que la verificación de invariantes sea sencilla:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.2 — Pruebas unitarias: el agregado en aislamiento, comandos e invariantes
def test_open_emits_ledger_opened_and_starts_empty() -> None:
    account = LedgerAccount.open(
        "led-1", owner_id="owner-1", currency=Currency.EUR
    )
    assert account.id == "led-1"
    assert account.owner_id == "owner-1"
    assert account.currency is Currency.EUR
    assert account.balance == Money.zero(Currency.EUR)
    # apply() queued the event for the store and bumped the version.
    [event] = account.pending_events()
    assert isinstance(event, LedgerOpened)
    assert event.account_id == "led-1"
    assert event.currency == "EUR"
    assert account.version == 1


def test_credit_then_debit_track_the_balance() -> None:
    account = LedgerAccount.open("led-2", owner_id="o", currency=Currency.EUR)
    account.credit(Money(1000, Currency.EUR))
    account.debit(Money(400, Currency.EUR))
    assert account.balance == Money(600, Currency.EUR)
    kinds = [type(e).__name__ for e in account.pending_events()]
    assert kinds == ["LedgerOpened", "Credited", "Debited"]
    assert account.version == 3


def test_debit_cannot_overdraw() -> None:
    account = LedgerAccount.open("led-3", owner_id="o", currency=Currency.EUR)
    account.credit(Money(500, Currency.EUR))
    with pytest.raises(BusinessRuleViolation) as exc:
        account.debit(Money(501, Currency.EUR))
    assert exc.value.rule == "ledger-insufficient-funds"
    # Invariant held: balance unchanged, no Debited event queued.
    assert account.balance == Money(500, Currency.EUR)
    assert [type(e).__name__ for e in account.pending_events()] == [
        "LedgerOpened",
        "Credited",
    ]
:::

**Ejecútalo.** Estas tres pruebas no necesitan ni base de datos ni almacén de eventos: el agregado se sostiene enteramente por sí mismo. Ejecuta solo las pruebas unitarias de este archivo desde la raíz del proyecto Lumen:

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k "open or credit or debit or currency"
```

Deberías ver pasar las pruebas en aislamiento:

```
....                                                                    [100%]
4 passed, 7 deselected in 0.05s
```

Los cuatro puntos son las cuatro pruebas que solo usan el agregado (las tres mostradas arriba más una comprobación de divisa no coincidente); las siete pruebas deseleccionadas son las respaldadas por el almacén, que ejecutaremos más adelante. Si ves un `TypeError` sobre el número de argumentos, casi con seguridad has caído en la trampa del método ligado de la advertencia anterior: pasar `self._on_opened` directamente a `when()` en lugar de envolverlo en una lambda de dos argumentos.

**Qué acaba de ocurrir.** Construiste un objeto de dominio que registra *qué cambió* en lugar de *qué es*. `open` emitió un hecho `LedgerOpened` y subió la versión a 1. `credit` y `debit` validaron cada uno su regla, calcularon el nuevo saldo y emitieron un hecho, de modo que un crédito seguido de un débito dejó tres hechos encolados y la versión en 3. Cuando una regla falló (la prueba de descubierto), el comando lanzó antes de llamar a `apply`, así que ningún evento defectuoso entró jamás en el búfer. El saldo en memoria del agregado y su lista de eventos pendientes se mantuvieron perfectamente acompasados, y nada tocó todavía una base de datos.

!!! tip "on_{event_type} como alternativa"
    En lugar de lambdas `when()`, puedes definir un método en el agregado con el nombre del `event_type` del evento, que es el **nombre de clase** del evento, así que `on_LedgerOpened(self, evt)` (coincidiendo con la dataclass `LedgerOpened`) se descubre automáticamente. Usa `when()` para una sola línea concisa y métodos con nombre para manejadores que necesiten varias sentencias o variables locales. El orden de despacho es: primero el manejador `when()`; luego el método `on_{event_type}`; luego `EventHandlerException` si no existe ninguno.

!!! spring "Equivalencia con Spring"
    `AggregateRoot` + `apply()` + `when()` es el equivalente en PyFly de `@Aggregate` + `AggregateLifecycle.apply(event)` + `@EventSourcingHandler` de Axon Framework. Axon usa el descubrimiento de manejadores guiado por anotaciones (`@EventSourcingHandler`); PyFly usa el registro con `when()` o la convención de métodos `on_*`. La mecánica de reproducción —cargar eventos del almacén, llamar a los mismos manejadores, reconstruir el estado— es idéntica en ambos frameworks.

---

## El EventStore

El agregado sabe cómo producir y reproducir eventos. El **`EventStore`** sabe cómo persistirlos y recuperarlos. Estas son preocupaciones deliberadamente separadas: el agregado es lógica de negocio pura sin E/S; el almacén de eventos es E/S pura sin lógica de negocio.

El protocolo `EventStore` expone dos operaciones centrales:

- **`append(aggregate_id, aggregate_type, events, *, expected_version)`** — persiste un lote de eventos para un flujo. Lanza `ConcurrencyError` si la versión real del flujo no coincide con `expected_version`.
- **`load(aggregate_id, *, after_sequence=0)`** — devuelve la secuencia ordenada de objetos `StoredEventEnvelope` desde el primer evento (o desde `after_sequence`) hasta el más reciente.

**`InMemoryEventStore`** es la implementación lista para usar. Al igual que `InMemoryEventBus` en el Capítulo 8, se ejecuta enteramente en el proceso, sin E/S: ideal para desarrollo y pruebas. Un despliegue de producción cambia a un adaptador respaldado por PostgreSQL o EventStoreDB.

`EventSourcedRepository` envuelve el `EventStore` y gestiona el ciclo completo de guardado/carga. El código de aplicación nunca llama al almacén directamente; llama a `repo.save(aggregate)` y `repo.load(aggregate_id)`, y el repositorio se encarga del resto.

Todas las importaciones provienen de dos ubicaciones:

```python
# Core event-sourcing types — all in the base package
from pyfly.eventsourcing import (
    AggregateRoot,
    DomainEvent,
    EventStore,
    InMemoryEventStore,
    SnapshotStore,
    InMemorySnapshotStore,
    StoredEventEnvelope,
)
# The generic repository lives in the .repository submodule
from pyfly.eventsourcing.repository import EventSourcedRepository
```

---

## El LedgerAccountRepository

Subclasificas `EventSourcedRepository` por dos motivos: para pasar la fábrica concreta y el almacén de snapshots a través de un único constructor bien nombrado y —opcionalmente— para sobrescribir `_envelope_to_event` de modo que los eventos reproducidos sean dataclasses tipadas reales en lugar de la bolsa de atributos genérica que produce la clase base.

!!! note "Jerga: sobre (envelope) y bolsa de atributos"
    Un `StoredEventEnvelope` es la *forma de cable* que el almacén persiste: envuelve el `payload` del evento (un dict plano) junto con los campos de contabilidad que el almacén necesita —`aggregate_id`, `aggregate_type`, `sequence`, `event_type`, `event_id` y `occurred_at`—. Piénsalo como el sobre con la dirección alrededor de la carta. Una **bolsa de atributos** es el objeto genérico de sustitución que crea el repositorio base al cargar si no sobrescribes nada: un objeto sin nombre con las claves del payload fijadas como atributos. Funciona —los manejadores solo leen atributos—, pero no es tu dataclass `Credited` real, así que no puede comprobarse de tipos ni probarse con `isinstance`. La sobrescritura de abajo cambia ese anonimato por la cosa real.

Construye el repositorio en tres pequeños pasos.

**Paso 1 — Asigna los nombres de clase de vuelta a las dataclasses.** El almacén registra el `event_type` de cada evento como la cadena del nombre de clase (`"Credited"`). Para reconstruir la dataclass real al cargar, necesitas una búsqueda de esa cadena a la clase. Eso es `_EVENT_TYPES`: una entrada por tipo de evento.

**Paso 2 — Subclasifica y reenvía el constructor.** `LedgerAccountRepository.__init__` toma el `store` (y un almacén de snapshots opcional `snapshots`) y reenvía todo a `super().__init__`, suministrando `factory=LedgerAccount` para que la clase base sepa cómo crear un agregado en blanco.

**Paso 3 — Sobrescribe `_envelope_to_event` para una reproducción tipada.** Busca el `event_type` en `_EVENT_TYPES`, filtra el payload almacenado quedándote con los campos que la dataclass realmente declara (usando `__dataclass_fields__`) y construye el evento real. Recurre a la hidratación genérica de la clase base para cualquier tipo de evento que no reconozcas.

::: listing lumen/models/repositories/ledger_repository.py | Listado 9.3 — LedgerAccountRepository: reproducción tipada mediante _envelope_to_event
from __future__ import annotations

from typing import ClassVar

from lumen.models.entities.v1.ledger_account import (
    Credited,
    Debited,
    LedgerAccount,
    LedgerOpened,
)
from pyfly.eventsourcing import (
    DomainEvent,
    EventStore,
    SnapshotStore,
    StoredEventEnvelope,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

# Map a stored event_type (the event class name) back to its dataclass.
_EVENT_TYPES: dict[str, type[DomainEvent]] = {
    LedgerOpened.__name__: LedgerOpened,
    Credited.__name__:     Credited,
    Debited.__name__:      Debited,
}


class LedgerAccountRepository(EventSourcedRepository[LedgerAccount]):
    """Loads/saves LedgerAccount aggregates via the event store."""

    SNAPSHOT_INTERVAL: ClassVar[int] = 100

    def __init__(
        self,
        store: EventStore,
        *,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        super().__init__(
            store,
            factory=LedgerAccount,
            snapshots=snapshots,
            snapshot_interval=self.SNAPSHOT_INTERVAL,
        )

    @staticmethod
    def _envelope_to_event(envelope: StoredEventEnvelope) -> object:
        """Rebuild the concrete event dataclass from a stored payload.

        Overrides the base-class generic hydration so that replayed events
        are the same dataclasses the aggregate applied on the write side.
        Unknown fields are ignored for forward-compatibility.
        """
        event_cls = _EVENT_TYPES.get(envelope.event_type)
        if event_cls is None:
            # Fall back to generic hydration for unrecognised event types.
            return EventSourcedRepository._envelope_to_event(envelope)
        field_names = {
            f.name for f in event_cls.__dataclass_fields__.values()
        }
        kwargs = {
            k: v for k, v in envelope.payload.items() if k in field_names
        }
        return event_cls(**kwargs)
:::

**Cómo funciona.** `super().__init__(store, factory=LedgerAccount, ...)` indica al repositorio base que llame a `LedgerAccount()` —sin argumentos— para crear un agregado en blanco, asigne `.id` y luego reproduzca el flujo. `factory` acepta cualquier invocable sin argumentos; pasar la propia clase (`factory=LedgerAccount`) es equivalente a `factory=lambda: LedgerAccount()`.

La sobrescritura de `_envelope_to_event` busca la cadena `event_type` almacenada en `_EVENT_TYPES`, usa `__dataclass_fields__` para filtrar el payload a campos conocidos y reconstruye la dataclass real. Los campos desconocidos se descartan silenciosamente —compatibilidad hacia delante en la práctica—: si una versión futura del evento añade un campo que el manejador antiguo no reconoce, el libro mayor sigue reproduciéndose en lugar de fallar. El repliegue a la clase base, al final, gestiona cualquier tipo de evento que el repositorio no conozca.

!!! note "Jerga: compatibilidad hacia delante (forward-compatibility)"
    Un lector es **compatible hacia delante** cuando puede ingerir datos escritos por una versión *más nueva* de sí mismo sin romperse: simplemente ignora las partes que no entiende. Descartar campos desconocidos del payload es exactamente eso: un escritor v2 puede añadir un `reference_code` a `Credited`, y un lector v1 aún en ejecución sigue plegando el evento correctamente en lugar de fallar ante el campo sorpresa.

**Ejecútalo.** Una prueba enfocada ejercita esta sobrescritura directamente: le entrega al repositorio un sobre construido a mano y comprueba que recibe de vuelta una dataclass `Credited` real:

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k typed_replay
```

```
.                                                                       [100%]
1 passed, 10 deselected in 0.05s
```

**Qué acaba de ocurrir.** Cableaste la frontera de persistencia sin escribir una sola línea de SQL ni de código de almacenamiento. El `EventSourcedRepository` base ya sabe cómo drenar los eventos pendientes en `save` y reproducirlos en `load`; tu subclase añadió solo dos cosas: una fábrica sin argumentos para poder construir un libro mayor en blanco, y un `_envelope_to_event` tipado para que los eventos que reproduce sean exactamente las mismas dataclasses que el agregado emitió en la ruta de escritura. Esa simetría es de lo que se trata todo: la escritura y la reproducción ejecutan código idéntico.

---

## Guardar, cargar y la prueba de la reproducción

Este es el momento hacia el que todo el capítulo ha estado avanzando: demostrar que un libro mayor recargado a partir de nada más que sus eventos almacenados es igual al libro mayor en vivo que los produjo. La prueba de abajo lo hace en dos mitades.

**Paso 1 — Escribe un libro mayor y persístelo.** Abre un libro mayor, acredítalo, debítalo y luego llama a `repo.save(account)`. El repositorio drena los tres eventos pendientes al almacén y limpia el búfer del agregado.

**Paso 2 — Recarga desde cero y compara.** Crea un repositorio *completamente nuevo* y llama a `load("acct-1")`. Nada en esta segunda mitad comparte memoria con el primer objeto. El saldo del libro mayor recuperado solo puede ser correcto si se recalculó reproduciendo el flujo almacenado, que es exactamente la prueba que queremos.

La siguiente prueba demuestra el ciclo completo de guardado y carga:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.4 — Prueba estelar: el saldo sobrevive a una recarga por reproducción
@pytest.mark.asyncio
async def test_balance_survives_reload_by_replay(
    event_store: InMemoryEventStore,
) -> None:
    # 1. Open + credit + debit, then persist the pending events.
    account = LedgerAccount.open(
        "acct-1", owner_id="owner-7", currency=Currency.EUR
    )
    account.credit(Money(2500, Currency.EUR))  # +25.00
    account.debit(Money(1000, Currency.EUR))   # -10.00 -> 15.00
    assert account.balance == Money(1500, Currency.EUR)

    repo = LedgerAccountRepository(event_store)
    await repo.save(account)
    # After committing, the aggregate has no pending events left.
    assert account.pending_events() == []

    # 2. Reconstruct from a *fresh* repository + aggregate. Nothing here
    #    carries the in-memory object's state — load() rebuilds the
    #    LedgerAccount purely by replaying the stored event stream.
    fresh_repo = LedgerAccountRepository(event_store)
    recovered = await fresh_repo.load("acct-1")

    assert recovered is not None
    assert recovered is not account
    assert recovered.owner_id == "owner-7"
    assert recovered.currency is Currency.EUR
    # The load-by-replay proof: balance recomputed from events, not stored.
    assert recovered.balance == Money(1500, Currency.EUR)
    # Three events were folded in: LedgerOpened, Credited, Debited.
    assert recovered.version == 3
    # A reconstructed aggregate has nothing pending — it was not "changed".
    assert recovered.pending_events() == []
:::

**Cómo funciona.** `repo.save(account)` llama a `store.append("acct-1", "LedgerAccount", pending_events, expected_version=0)`. Los tres eventos —`LedgerOpened`, `Credited`, `Debited`— se serializan en objetos `StoredEventEnvelope` y se escriben en el flujo en orden; el búfer de eventos pendientes del agregado se limpia a continuación.

`fresh_repo.load("acct-1")` llama a `store.load("acct-1")`, recibe los tres sobres, construye un `LedgerAccount()` en blanco mediante la fábrica, asigna `.id = "acct-1"` y pasa cada sobre por `_envelope_to_event` antes de reproducirlo. Los manejadores `_on_opened`, `_on_credited` y `_on_debited` se ejecutan en secuencia; tras los tres, `recovered.balance.amount` vale `1500`: el mismo valor que mantenía el agregado en vivo, calculado sin ningún estado compartido entre los dos objetos.

La prueba usa *dos instancias de repositorio independientes* que comparten el mismo almacén en memoria —`repo` para la escritura, `fresh_repo` para la lectura—. Esto demuestra que la reproducción, no la identidad de objetos dentro del proceso, es la fuente de verdad.

**Ejecútalo.** Ejecuta la prueba estelar más la prueba de inspección del flujo en crudo que la sigue:

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k "reload_by_replay or immutable_event_stream"
```

```
..                                                                      [100%]
2 passed, 9 deselected in 0.05s
```

**Qué acaba de ocurrir.** Demostraste que el event sourcing realmente funciona de principio a fin. El primer libro mayor vivió solo en memoria; tras `save`, sus hechos vivieron solo en el almacén. Un segundo objeto de libro mayor, no relacionado, reconstruyó entonces el mismo saldo exacto de 15,00 EUR plegando esos hechos de vuelta a través de los mismos manejadores, sin ninguna columna de saldo almacenada a la vista. La versión aterrizó en 3 (un evento por `LedgerOpened` / `Credited` / `Debited`), y el libro mayor reconstruido no tenía nada pendiente porque cargar no es lo mismo que cambiar.

El almacén de eventos también expone los sobres en crudo para inspección, útil para auditorías y pruebas:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.5 — El almacén contiene el flujo de eventos inmutable
@pytest.mark.asyncio
async def test_store_holds_the_immutable_event_stream(
    event_store: InMemoryEventStore,
) -> None:
    account = LedgerAccount.open(
        "acct-2", owner_id="o", currency=Currency.GBP
    )
    account.credit(Money(800, Currency.GBP))
    account.debit(Money(300, Currency.GBP))
    await LedgerAccountRepository(event_store).save(account)

    envelopes = await event_store.load("acct-2")
    assert [e.event_type for e in envelopes] == [
        "LedgerOpened", "Credited", "Debited"
    ]
    assert [e.sequence for e in envelopes] == [1, 2, 3]
    assert all(e.aggregate_type == "LedgerAccount" for e in envelopes)
    # The Debited payload carries the post-debit running balance.
    assert envelopes[2].payload["balance"] == 500
    assert await event_store.latest_version("acct-2") == 3
:::

!!! note "Argumento de fábrica"
    `EventSourcedRepository` acepta un invocable `factory` que produce una instancia de agregado en blanco. La fábrica debe devolver un agregado en su estado inicial de `__init__` —sin eventos aplicados— porque el repositorio aplicará la historia completa él mismo. Pasar una fábrica que construye un agregado ya mutado (por ejemplo, una que llama a `open()` internamente) corromperá la reproducción: `_on_opened` se ejecutaría dos veces, la segunda sobrescribiendo los campos que los eventos reales ya fijaron.

---

## Concurrencia optimista

Dos peticiones concurrentes —un crédito desde la app móvil y un débito automático de comisiones desde un trabajo en segundo plano— pueden cargar el mismo libro mayor en la misma versión, aplicar cada una su propio cambio y luego intentar guardar. Sin una protección de concurrencia, un guardado gana silenciosamente, los eventos del otro se pierden, los números de secuencia colisionan y el saldo resultante es erróneo.

La **concurrencia optimista** previene esto. Antes de anexar eventos nuevos, el `EventStore` compara la versión *actual* del flujo contra la versión *esperada* que el repositorio registró en el momento de la carga. Si coinciden, el anexado prosigue y la versión avanza. Si no coinciden —porque otro escritor ya anexó—, se lanza `ConcurrencyError` y la petición perdedora debe reintentar desde una carga fresca.

!!! note "Jerga: bloqueo optimista frente a pesimista"
    El bloqueo *pesimista* asume que el conflicto es probable, así que toma un bloqueo por adelantado: ningún otro escritor puede tocar la fila hasta que lo liberes. La concurrencia *optimista* asume que el conflicto es raro, así que no toma ningún bloqueo en absoluto: deja proceder a ambos escritores y solo comprueba, en el momento de guardar, si alguien más cambió el flujo primero. El perdedor reintenta. Para la mayoría de los libros mayores, dos escritores simultáneos sobre la *misma* cuenta son poco habituales, así que la estrategia optimista evita el coste del bloqueo en la ruta sin conflicto, abrumadoramente común.

El `expected_version` lo pasa el repositorio de forma implícita: registra la versión en la que cargó el agregado y se la suministra al almacén al guardar. Nunca gestionas números de versión en el código de aplicación.

La progresión de la versión es determinista: tras un guardado y una recarga, las escrituras posteriores avanzan la versión sin conflicto:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.6 — Continuar anexando tras una recarga: el bloqueo optimista avanza correctamente
@pytest.mark.asyncio
async def test_continues_appending_after_a_reload(
    event_store: InMemoryEventStore,
) -> None:
    repo = LedgerAccountRepository(event_store)
    account = LedgerAccount.open(
        "acct-3", owner_id="o", currency=Currency.EUR
    )
    account.credit(Money(1000, Currency.EUR))
    await repo.save(account)

    # Reload, mutate again, save again — version must advance, no conflict.
    reloaded = await repo.load("acct-3")
    assert reloaded is not None
    assert reloaded.version == 2
    reloaded.debit(Money(250, Currency.EUR))
    await repo.save(reloaded)

    final = await repo.load("acct-3")
    assert final is not None
    assert final.balance == Money(750, Currency.EUR)
    assert final.version == 3
    assert await event_store.latest_version("acct-3") == 3
:::

**Cómo funciona.** Tras el primer guardado, el flujo está en la versión 2. Al cargar, `reloaded.version == 2`. `repo.save(reloaded)` anexa con `expected_version=2`; el almacén avanza a 3 y tiene éxito. La carga `final` reproduce los tres eventos y confirma el saldo correcto.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k continues_appending
```

```
.                                                                       [100%]
1 passed, 10 deselected in 0.05s
```

**Qué acaba de ocurrir.** Un libro mayor recargado recordó su versión, de modo que el siguiente guardado se alineó limpiamente detrás de los eventos ya presentes en el flujo: sin conflicto, números de secuencia en orden, saldo correcto. Esta es la *ruta feliz* de la concurrencia optimista: un escritor cada vez. En el momento en que dos escritores compiten, el `expected_version` del segundo ya no coincidiría y el almacén lanzaría `ConcurrencyError` en su lugar, que es el caso que la advertencia de abajo te dice cómo manejar.

!!! warning "Maneja siempre ConcurrencyError"
    Cuando dos escritores compiten, el guardado perdedor lanza `ConcurrencyError`. Tu servicio de aplicación debe capturarlo y decidir qué hacer: reintentar el ciclo completo de cargar-mutar-guardar (apropiado para escrituras de baja contención), o exponer un 409 Conflict al llamante (apropiado cuando el llamante debería reenviar con datos frescos). Nunca tragues el error en silencio: un error de concurrencia tragado deja el flujo en un estado inconsistente.

---

## Snapshots

El event sourcing cambia simplicidad de escritura por coste de lectura. Cargar un libro mayor que ha registrado 10 000 movimientos de dinero significa reproducir 10 000 eventos cada vez que se necesita el agregado. Para la mayoría de los libros mayores el flujo se mantiene corto; para cuentas de alta frecuencia puede crecer hasta volverse prohibitivamente largo.

Los **snapshots** abordan esto. Un snapshot es un punto de control serializado del estado del agregado en una versión concreta. Al cargar, el repositorio busca primero el snapshot más reciente, deserializa el estado directamente hasta esa versión y luego reproduce solo los eventos que llegaron después de él. Un snapshot en la versión 9 000 reduce una reproducción de 10 000 eventos a 1 000 eventos.

`InMemorySnapshotStore` almacena los snapshots en memoria. Pásalo a `EventSourcedRepository` junto con el almacén de eventos mediante el argumento clave `snapshots`, exactamente como lo acepta `LedgerAccountRepository.__init__`:

```python
store = InMemoryEventStore()
snapshots = InMemorySnapshotStore()
repo = LedgerAccountRepository(store, snapshots=snapshots)
```

El repositorio decide cuándo hacer un snapshot automáticamente usando `snapshot_interval` (por defecto `100`, fijado por `LedgerAccountRepository.SNAPSHOT_INTERVAL`). Tras cada `save`, comprueba si la nueva versión del agregado **cruza** un múltiplo de `snapshot_interval`:

```python
# crosses_interval is True when this batch pushes the stream past a
# 100-event boundary — e.g., version 95 → 105 crosses the 100 mark.
crossed = (
    (aggregate.version // snapshot_interval)
    > (previous_version // snapshot_interval)
)
```

Esta lógica de cruce de intervalo (en lugar de divisibilidad exacta) maneja el caso común en el que un único lote de guardado se sitúa a horcajadas sobre el umbral. Una importación masiva que añade 10 eventos lleva la versión de 95 a 105 y dispara correctamente un snapshot, aunque ni 95 ni 105 sean exactamente divisibles por 100.

La costura del snapshot es inofensiva cuando el flujo es más corto que el intervalo; la siguiente prueba lo demuestra:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.7 — Almacén de snapshots cableado: la recarga sigue produciendo el estado correcto
@pytest.mark.asyncio
async def test_snapshot_store_round_trips_the_ledger() -> None:
    """With a snapshot store wired, reload still yields the right state.

    The ledger's stream is far shorter than the snapshot interval, so this
    proves the snapshot seam is harmless and the repository falls back
    to a full replay.
    """
    store = InMemoryEventStore()
    snapshots = InMemorySnapshotStore()
    repo = LedgerAccountRepository(store, snapshots=snapshots)

    account = LedgerAccount.open(
        "acct-4", owner_id="o", currency=Currency.EUR
    )
    account.credit(Money(5000, Currency.EUR))
    await repo.save(account)

    recovered = await repo.load("acct-4")
    assert recovered is not None
    assert recovered.balance == Money(5000, Currency.EUR)
:::

**Cómo funciona.** Tras el guardado, el repositorio comprueba: ¿es `version 2 // 100 > 0 // 100`? No; el umbral de snapshot no se ha cruzado, así que no se toma ningún snapshot. La siguiente `load` realiza una reproducción completa de los dos eventos y devuelve el saldo correcto. Una vez que un libro mayor cruza una frontera de 100 eventos, el repositorio serializa el estado del agregado en un sobre de snapshot. La siguiente carga encuentra el snapshot, deserializa directamente hasta esa versión y luego pide al almacén de eventos los eventos con un número de secuencia mayor que la versión del snapshot, reduciendo el coste de reproducción solo al delta.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k snapshot_store_round_trips
```

```
.                                                                       [100%]
1 passed, 10 deselected in 0.05s
```

**Qué acaba de ocurrir.** Cableaste un almacén de snapshots en el repositorio y el libro mayor aún se recargó correctamente, que es exactamente lo que debería pasar con un flujo corto. Como los dos eventos nunca cruzaron el intervalo de 100 eventos, no se escribió ningún snapshot y la carga recurrió a una simple reproducción completa. Los snapshots son optimización pura: cablear uno no cuesta nada para flujos cortos y rinde discretamente una vez que una cuenta se vuelve de alta frecuencia. Puedes demostrar la corrección de tu libro mayor con el almacén de snapshots presente o ausente: la respuesta es idéntica.

!!! tip "El intervalo de snapshot en producción"
    Un `snapshot_interval` de 100 es el valor por defecto y un punto de partida sensato. Para libros mayores de alta frecuencia podrías bajarlo; para cuentas que solo cambian unas pocas veces al día, un intervalo más alto reduce el coste de almacenamiento de snapshots. Los snapshots son una optimización, no un requisito de corrección: eliminarlos deja el sistema correcto pero más lento.

---

## Cableado de arranque y autoconfiguración

El módulo `pyfly.eventsourcing` se incluye en el paquete **base** `pyfly`, sin dependencia extra. Habilitarlo lleva dos pasos: fija `pyfly.eventsourcing.enabled: true` en `pyfly.yaml` y anota la aplicación con `@enable_domain_stack`. La autoconfiguración de PyFly registra entonces automáticamente los beans `event_store` y `snapshot_store`.

!!! note "Jerga: bean y autoconfiguración"
    Un **bean** es sencillamente un objeto que el framework crea una vez y entrega a cualquiera que lo pida: la misma idea de inyección de dependencias que viste en el Capítulo 2. La **autoconfiguración** es una clase de métodos de fábrica productores de beans que PyFly activa *solo cuando se cumple una condición*; aquí, la guarda `@conditional_on_property("pyfly.eventsourcing.enabled", having_value="true")`. Activa ese único indicador de configuración y los beans `event_store` y `snapshot_store` aparecen en el contenedor; déjalo apagado y la maquinaria de event sourcing permanece latente. Nunca llamas tú mismo a la fábrica.

La batería de pruebas lo confirma directamente:

::: listing tests/test_ledger_event_sourcing.py | Listado 9.8 — La autoconfiguración registra los beans del almacén de eventos
def test_auto_configuration_registers_event_store_beans() -> None:
    """enable_domain_stack activates this auto-config when
    pyfly.eventsourcing.enabled=true (set in pyfly.yaml), registering
    the in-memory event/snapshot stores the ledger repository depends on."""
    from pyfly.core.config import Config
    from pyfly.eventsourcing.auto_configuration import (
        EventSourcingAutoConfiguration,
    )

    auto = EventSourcingAutoConfiguration()
    cfg = Config()  # empty config -> providers default to "memory"
    assert isinstance(auto.event_store(cfg), InMemoryEventStore)
    assert isinstance(auto.snapshot_store(cfg), InMemorySnapshotStore)
:::

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q -k auto_configuration
```

```
.                                                                       [100%]
1 passed, 10 deselected in 0.05s
```

**Qué acaba de ocurrir.** Con el indicador `enabled` activado, la autoconfiguración produjo los dos almacenes en memoria de los que depende el repositorio del libro mayor, sin que tú instanciaras ninguno. Observa que los métodos de fábrica toman un argumento `Config`: así es como leen `pyfly.eventsourcing.store.provider` para decidir entre el valor por defecto `memory` y un adaptador respaldado por SQL. Pasar un `Config()` vacío los deja en `memory`, que es lo que la prueba comprueba.

En el código de aplicación, el repositorio se cablea mediante inyección de dependencias:

```python
# pyfly.yaml
pyfly:
  eventsourcing:
    enabled: true
```

```python
# In a service or handler — the beans are injected automatically
@component
class LedgerService:
    def __init__(
        self,
        event_store: EventStore,
        snapshot_store: SnapshotStore,
    ) -> None:
        self._repo = LedgerAccountRepository(
            event_store, snapshots=snapshot_store
        )
```

---

## Proyecciones

El almacén de eventos es el sistema de registro, pero la mayoría de las consultas de aplicación —"¿cuál es el saldo actual?", "muéstrame todos los libros mayores del propietario u-42", "¿qué cuentas están por encima de 1 000 EUR?"— no deben reproducir flujos de eventos en cada lectura. Deberían golpear un modelo de lectura precalculado: una tabla optimizada para consultas, mantenida sincronizada por un proceso en segundo plano que consume el flujo de eventos.

Ese proceso en segundo plano es una **proyección**. Una proyección se suscribe al flujo de eventos y actualiza un modelo de lectura cada vez que llega un evento relevante. PyFly proporciona `FunctionProjection` y `ProjectionRunner` en `pyfly.eventsourcing.projection`:

!!! note "Jerga: modelo de lectura, proyección, modelo de escritura"
    El **modelo de escritura** es la cara que has construido hasta ahora: el agregado y su flujo de eventos, optimizado para *registrar los cambios correctamente*. Un **modelo de lectura** es una copia separada de los datos, con forma de consulta, optimizada para *responder preguntas rápido* (una fila por libro mayor con su saldo actual, digamos). Una **proyección** es el proceso que mantiene un modelo de lectura sincronizado reproduciendo el flujo de eventos sobre él. Esta separación —un modelo para escrituras, otro para lecturas— es el patrón **CQRS** que viste en el Capítulo 7, ahora respaldado por un flujo de eventos en lugar de una tabla relacional.

- **`FunctionProjection(name, handler_fn)`** — envuelve una función asíncrona que recibe un `StoredEventEnvelope` y actualiza el modelo de lectura.
- **`ProjectionRunner(projection, store)`** — acciona la proyección iterando el `EventStore` en orden de número de secuencia y llamando al manejador para cada sobre.

Una proyección se arma en tres piezas. **Paso 1**: escribe el manejador, una función `async` que toma un sobre y actualiza el modelo de lectura (aquí un `dict` plano; en producción, una tabla de base de datos). **Paso 2**: envuélvelo, `FunctionProjection("balance_ledger", handler)` convierte la función pelada en una proyección con nombre. **Paso 3**: acciónalo, `ProjectionRunner(projection, store)` conecta la proyección al almacén de eventos y, al arrancar, le alimenta cada sobre almacenado en orden de secuencia.

Aquí hay una `BalanceLedgerProjection` que construye un modelo de lectura de saldos a partir del flujo de eventos:

::: listing lumen/eventsourcing/balance_projection.py | Listado 9.9 — BalanceLedgerProjection: un modelo de lectura construido a partir del flujo de eventos
from __future__ import annotations

import asyncio

from pyfly.eventsourcing import InMemoryEventStore
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner


# The in-process read model — in production, replace with a DB table.
_balance_store: dict[str, dict] = {}


async def _handle_envelope(envelope: object) -> None:
    """Update the balance read model for each ledger event."""
    event_type: str = getattr(envelope, "event_type", "")
    payload: dict = getattr(envelope, "payload", {})

    if event_type == "LedgerOpened":
        _balance_store[payload["account_id"]] = {
            "account_id": payload["account_id"],
            "owner_id": payload.get("owner_id", ""),
            "balance_cents": 0,
            "currency": payload.get("currency", ""),
        }
    elif event_type in ("Credited", "Debited"):
        account_id = payload["account_id"]
        if account_id in _balance_store:
            _balance_store[account_id]["balance_cents"] = (
                payload["balance"]
            )


def build_projection(store: InMemoryEventStore) -> ProjectionRunner:
    projection = FunctionProjection("balance_ledger", _handle_envelope)
    return ProjectionRunner(projection, store)


async def demo_projection(store: InMemoryEventStore) -> None:
    runner = build_projection(store)
    # start() launches a background polling task and returns immediately;
    # the read model is populated asynchronously as the loop drains the
    # store. Poll until the projection has caught up, then stop the runner.
    await runner.start()
    try:
        for _ in range(50):
            if "led-001" in _balance_store:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()

    balance = _balance_store.get("led-001", {})
    print(f"Balance read model: {balance}")
:::

**Cómo funciona.** `FunctionProjection("balance_ledger", _handle_envelope)` envuelve el manejador asíncrono. `ProjectionRunner(projection, store)` lo enlaza con el `InMemoryEventStore`. `await runner.start()` lanza una tarea de sondeo en segundo plano y retorna *de inmediato*; no se bloquea hasta que el almacén se drena. La tarea itera sobre `store.stream_all(...)`, llamando a `_handle_envelope` para cada sobre nuevo en orden y avanzando un cursor (`_last_event_id`) para no reprocesar nunca un evento. Como la población ocurre asíncronamente, la demo sondea `_balance_store` hasta que la proyección se ha puesto al día, y luego llama a `await runner.stop()` para detener el bucle antes de leer el resultado. Solo después de que la proyección haya procesado los sobres, `_balance_store` refleja el estado actual de cada libro mayor del almacén.

La proyección es deliberadamente sin estado: solo lee `envelope.event_type` y `envelope.payload`. No se carga ningún agregado; no se llama a ningún repositorio. El modelo de lectura es barato de reconstruir: detén el runner, limpia `_balance_store`, llama a `start()` de nuevo. Esta propiedad de reconstruir-desde-la-historia es exclusiva del event sourcing; los modelos de almacenamiento de estado ya han descartado la historia.

En producción, `_handle_envelope` escribiría en una base de datos real (PostgreSQL, Redis, Elasticsearch). El `ProjectionRunner` persistiría un cursor en una tabla de checkpoints para que los reinicios continúen desde el último evento procesado en lugar de reproducir todo desde el principio. El patrón de proyección es idéntico con independencia del almacenamiento subyacente.

!!! note "Proyecciones frente a listeners del Capítulo 8"
    La `BalanceProjection` del Capítulo 8 (Listado 8.4) era un suscriptor `@event_listener` sobre el `InMemoryEventBus`: reaccionaba a los eventos a medida que se publicaban. La `BalanceLedgerProjection` de este capítulo lee directamente del `EventStore`: puede reproducir la historia desde el principio, ponerse al día con el presente y continuar consumiendo eventos futuros. Ambas mantienen un modelo de lectura de saldos; la proyección sobre el almacén de eventos es reconstruible desde la historia; el listener del bus no lo es.

---

## El outbox transaccional

Considera la llamada `repo.save(account)` del Listado 9.4: tres eventos se anexan al almacén de eventos. Ahora supón que esos eventos también necesitan llegar a un broker externo: Kafka, RabbitMQ, otro microservicio. El enfoque ingenuo es llamar a `broker.publish(envelope)` inmediatamente después de `store.append(...)`. Pero ¿y si el proceso se cae entre el anexado y la publicación? Los eventos están en el almacén, pero el broker nunca los recibió. El servicio aguas abajo nunca se enteró del crédito.

El patrón del **outbox transaccional** resuelve esto. En lugar de publicar directamente, encolas el evento en un *outbox*: un intermediario duradero. El outbox persiste el evento junto a los eventos del agregado en la misma operación de almacén. Un trabajador en segundo plano separado (el *relay*) drena el outbox y reenvía cada evento al broker con semántica de al menos una vez. Si el relay se cae, reinicia y reintenta desde el último evento no confirmado.

El `TransactionalOutbox` de PyFly vive en `pyfly.eventsourcing`. Acepta una corrutina `publish` y un límite `max_attempts`, y expone dos métodos:

- **`enqueue(envelope)`** — añade un sobre de evento al outbox para su entrega.
- **`start()`** — arranca el bucle de relay en segundo plano que llama a `publish(envelope)` por cada elemento encolado, reintentando hasta `max_attempts` veces en caso de fallo.

::: listing lumen/eventsourcing/outbox_demo.py | Listado 9.10 — TransactionalOutbox: entrega fiable de al menos una vez a un broker
from __future__ import annotations

from pyfly.eventsourcing import (
    InMemoryEventStore,
    InMemorySnapshotStore,
    TransactionalOutbox,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.models.entities.v1.ledger_account import LedgerAccount
from lumen.models.entities.v1.money import Money
from lumen.interfaces.enums.v1.currency import Currency


# Simulated broker: collect published envelopes for inspection.
_published: list = []


async def _broker_publish(envelope: object) -> None:
    _published.append(envelope)


async def demo_outbox() -> None:
    store = InMemoryEventStore()
    repo = LedgerAccountRepository(store)
    outbox = TransactionalOutbox(publish=_broker_publish, max_attempts=5)
    await outbox.start()

    account = LedgerAccount.open("led-004", "u-11", Currency.EUR)
    account.credit(Money(5000, Currency.EUR))
    await repo.save(account)

    # Enqueue the stored envelopes into the outbox.
    for envelope in await store.load("led-004"):
        await outbox.enqueue(envelope)

    # The relay has delivered all envelopes to the broker.
    assert len(_published) == 2   # LedgerOpened + Credited
:::

**Cómo funciona.** El outbox mantiene los sobres en una cola duradera. `_broker_publish` es la función de entrega; sustitúyela por tu productor de Kafka o RabbitMQ. `max_attempts=5` significa que el relay reintenta una entrega fallida hasta cinco veces antes de mandar el sobre a la cola de mensajes muertos (dead-letter).

La garantía crítica: el outbox se drena de forma independiente de la petición que creó los eventos. Si el proceso se cae después de `repo.save(account)` pero antes de que el outbox termine de vaciarse, el siguiente reinicio retoma desde donde lo dejó y completa la entrega. El estado del agregado en el almacén de eventos ya es correcto; solo se interrumpió la entrega del lado del broker.

!!! warning "Al menos una vez, no exactamente una vez"
    El outbox garantiza que cada evento llega al broker *al menos una vez*. Si el relay entrega un evento y luego se cae antes de marcarlo como confirmado, el evento se entrega de nuevo en el reinicio. Tus consumidores del broker —y los servicios aguas abajo— deben ser idempotentes: usa el `envelope.event_id` como clave de deduplicación. El Capítulo 10 muestra cómo los adaptadores de consumidor de Kafka y RabbitMQ manejan la deduplicación automáticamente.

El outbox transaccional es el puente entre el event sourcing y la mensajería orientada a eventos. El Capítulo 10 retoma exactamente aquí, presentando los productores de Kafka y los exchanges de RabbitMQ y mostrando cómo configurar el relay para una entrega fiable a cada uno.

!!! spring "Equivalencia con Spring"
    El patrón del outbox transaccional es bien conocido en el ecosistema Spring con el mismo nombre. El `EventPublicationRegistry` de Spring Modulith y el `@TransactionalEventListener(phase = AFTER_COMMIT)` de Spring aproximan la misma garantía: el evento se registra de forma duradera antes de ser despachado. El almacén de eventos de Axon Server cumple un papel similar para las aplicaciones basadas en Axon: los eventos se escriben primero en el almacén, y los grupos de proyección / procesadores de eventos los consumen con garantías de al menos una vez desde el registro almacenado. El `TransactionalOutbox` de PyFly es el equivalente portable de ese patrón, sin requerir un servidor de eventos dedicado.

---

## Avanzado: upcasting y multitenencia

Dos preocupaciones aparecen en todo sistema con event sourcing de larga vida. Esta sección las presenta brevemente; un tratamiento completo queda fuera del alcance de este libro.

### Upcasting

Los eventos son inmutables. Una vez que un evento `Credited` se escribe en el flujo, no puedes volver atrás y añadir un campo `reference_code`. Pero los requisitos del producto cambian: dentro de tres meses el equipo de finanzas necesitará un código de referencia en cada crédito para la conciliación. Los eventos nuevos lo incluyen; los antiguos no.

Un **upcaster** es una función que transforma una forma de evento antigua en la forma actual durante la reproducción. El `EventStore` lo llama de forma transparente; el agregado nunca ve la forma antigua. Registras upcasters por tipo de evento y por versión:

```python
# Conceptual — upcaster API varies by adapter
def upcast_credited_v1(payload: dict) -> dict:
    payload.setdefault("reference_code", "LEGACY")
    return payload
```

El upcaster se ejecuta cuando el `EventStore` carga un evento cuya versión de esquema es inferior a la actual. Los datos antiguos se vuelven legibles sin una migración; los datos nuevos se escriben en el esquema actual.

### Multitenencia

Cuando varios inquilinos (tenants) comparten el mismo almacén de eventos, los eventos deben acotarse por inquilino para que un inquilino nunca pueda leer ni reproducir el flujo de otro. PyFly te da dos costuras para esto; ninguna vive en `EventSourcedRepository` (su constructor toma solo `store`, `factory`, `snapshots` y `snapshot_interval`: no hay parámetro `tenant_id`).

La primera costura está en el **propio sobre**. `StoredEventEnvelope` lleva un campo dedicado `tenant_id`, y `StoredEventEnvelope.of(...)` lo acepta como argumento clave:

```python
envelope = StoredEventEnvelope.of(
    aggregate_id="led-001",
    aggregate_type="LedgerAccount",
    sequence=0,
    event=Credited(...),
    tenant_id="tenant-A",
)
```

El adaptador SQL lo persiste como una columna `tenant_id` en `pyfly_event_store`, de modo que un almacén consciente de inquilinos puede filtrar cada consulta por ella. Esto mantiene limpio el `aggregate_id` a la vez que particiona el registro por inquilino.

La segunda costura es un **patrón que implementas tú mismo**: prefija cada `aggregate_id` con el identificador del inquilino —`"tenant-A::led-001"` en lugar de `"led-001"`— cuando llames a `repo.save` y `repo.load`. Un envoltorio fino consciente de inquilinos alrededor del repositorio puede aplicar y retirar el prefijo para que el código de aplicación nunca lo vea:

```python
class TenantLedgerRepository:
    def __init__(self, repo: LedgerAccountRepository, tenant_id: str) -> None:
        self._repo = repo
        self._prefix = f"{tenant_id}::"

    async def load(self, account_id: str) -> LedgerAccount | None:
        return await self._repo.load(self._prefix + account_id)
```

Las proyecciones deben acotar sus modelos de lectura de forma similar: normalmente incluyendo `tenant_id` como columna en la tabla del modelo de lectura y filtrando por ella en el momento de la consulta.

!!! note "Elegir event sourcing"
    El event sourcing añade complejidad operativa: upcasters, gestión de snapshots, procedimientos de reconstrucción de proyecciones, monitorización del relay del outbox. Elígelo deliberadamente para dominios donde la auditabilidad y las consultas de viaje en el tiempo son requisitos de primera clase: libros mayores financieros, historiales médicos, registros de cadena de suministro. Para dominios con mucho CRUD donde lo único que importa es el estado actual, el almacenamiento de estado es más simple y suficiente.

---

## Ejecuta el capítulo entero

Ejecutaste cada pieza de forma aislada a medida que la construías. Ahora ejecuta la batería completa del libro mayor para confirmar que todo sigue pasando en conjunto:

```bash
uv run --extra dev pytest tests/test_ledger_event_sourcing.py -q
```

```
...........                                                              [100%]
11 passed in 0.06s
```

Once puntos: las cuatro pruebas de aislamiento del agregado, la prueba estelar de recarga por reproducción, la inspección del flujo en crudo, la sobrescritura de reproducción tipada, la comprobación de concurrencia tras la recarga, el round-trip del snapshot, la búsqueda de libro mayor desconocido y la comprobación del cableado de autoconfiguración. Con todas en verde, el agregado `LedgerAccount` y su repositorio están completos y son correctos.

---

## Lo que construiste {.recap}

El `LedgerAccount` de Lumen es ahora un libro mayor totalmente orientado a eventos que coexiste con el `Wallet` con estado almacenado del Capítulo 6.

Las dos bases de agregado están separadas por diseño: `Wallet` extiende `pyfly.domain.AggregateRoot` y almacena su saldo en una fila de base de datos; `LedgerAccount` extiende `pyfly.eventsourcing.AggregateRoot` y deriva su saldo de un flujo de eventos inmutable. La distinción no es cosmética: la maquinaria de event sourcing (`apply`, `replay`, `when`) vive solo en la clase base de eventsourcing.

Los tres eventos de dominio —`LedgerOpened`, `Credited`, `Debited`— son `dataclass`es que extienden `pyfly.eventsourcing.DomainEvent`, cada uno con valores por defecto en sus campos para que el repositorio pueda reconstruirlos desde un payload almacenado. Los manejadores se registran con `when()` como lambdas de dos argumentos que delegan en métodos privados; la trampa del método ligado a evitar es que un método ligado ya es de un solo argumento desde el exterior, lo que provoca un `TypeError` en el momento del despacho.

`LedgerAccountRepository` es una subclase fina de `EventSourcedRepository[LedgerAccount]` que pasa la fábrica sin argumentos y sobrescribe `_envelope_to_event` para hidratar dataclasses concretas durante la reproducción. La prueba estelar lo confirmó: tras abrir, acreditar y debitar un libro mayor, un repositorio y un agregado completamente nuevos reconstruyeron el saldo correcto puramente reproduciendo el flujo almacenado, sin columna de saldo almacenada, sin estado compartido.

El contador `version` acciona la protección de concurrencia optimista: el almacén rechaza cualquier `append` cuyo `expected_version` no coincida con la versión real del flujo, forzando al escritor perdedor a reintentar desde una carga fresca. `InMemorySnapshotStore` resultó inofensivo cuando el flujo es más corto que el intervalo de snapshot y acelerará la reproducción una vez que un libro mayor cruce el umbral.

`BalanceLedgerProjection` usó `FunctionProjection` y `ProjectionRunner` para mantener un modelo de lectura de saldos rápido a partir del flujo de eventos en crudo, uno que puede reconstruirse desde la historia en cualquier momento, a diferencia del enfoque de listener del bus del Capítulo 8. `TransactionalOutbox` conectó luego el almacén de eventos con el mundo del broker, encolando eventos para una entrega de al menos una vez y reintentando en caso de fallo, de modo que ningún hecho se pierda silenciosamente entre el almacén y los consumidores aguas abajo. El Capítulo 10 retoma exactamente ahí, presentando los adaptadores de Kafka y RabbitMQ a los que apunta el relay del outbox.

---

## Pruébalo tú mismo {.exercises}

1. **Reproduce hasta un punto en el tiempo.** Aplica diez créditos de 100 céntimos cada uno a un `LedgerAccount`. Luego carga el agregado manualmente llamando a `await store.load("led-X")`, filtra los sobres a los que tengan `sequence <= 5` y reproduce solo esos a través de un `LedgerAccount()` nuevo. Comprueba que el saldo resultante es igual a 400 céntimos (cuatro créditos después de la apertura) en lugar de 1 000 céntimos (diez créditos). Esta es la "consulta de viaje en el tiempo" que los modelos de almacenamiento de estado no pueden ofrecer.

2. **Implementa un evento `Transferred` y un guardado de doble agregado.** Añade un `Transferred(DomainEvent)` con los campos `source_id: str`, `target_id: str`, `amount: int`, `currency: str`. Añade un método `transfer_to(target: LedgerAccount, amount: Money) -> None` a `LedgerAccount` que llame a `self.debit(amount)` y `target.credit(amount)` en secuencia, y luego aplique un evento `Transferred` sobre `self`. Cablea una corrutina `demo_transfer` que abra dos libros mayores, acredite 10 000 céntimos en el primero, transfiera 3 000 céntimos al segundo, guarde ambos agregados de forma independiente, recargue ambos desde el almacén y compruebe que los saldos son 7 000 y 3 000 céntimos respectivamente.

3. **Añade una `OwnerLedgerProjection`.** Escribe una segunda `FunctionProjection` llamada `owner_ledger` cuyo manejador mantenga un `dict[str, list[dict]]` que asigne cada `owner_id` a una lista cronológica de registros de transacción. Cada registro debe incluir `event_type`, `amount` (del payload para los eventos `Credited`/`Debited`) y el número de secuencia del sobre. Aliméntala con el mismo `InMemoryEventStore`, abre tres libros mayores para el mismo propietario, realiza una mezcla de créditos y débitos, arranca el runner de la proyección y comprueba que la lista de transacciones del propietario tiene el número correcto de entradas en el orden correcto.
