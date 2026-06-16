<span class="eyebrow">Capítulo 6</span>

# Diseño guiado por el dominio {.chtitle}

::: figure art/openers/ch06.svg | &nbsp;

La funcionalidad del monedero de Lumen funciona. Los depósitos llegan, los saldos se actualizan y la base de datos lo persiste todo entre reinicios. Pero fíjate bien en la capa de servicio y notarás algo incómodo: la comprobación de descubierto vive en el método del servicio, no en el propio monedero. La validación de divisa es una comparación de propiedades repartida entre un puñado de sentencias `if`. Nada impide que un futuro desarrollador —o tu yo futuro a las 11 de la noche— se salte esas defensas llamando directamente a `repo.save(entity)`.

El **diseño guiado por el dominio** (Domain-Driven Design) resuelve esto haciendo que el modelo sea responsable de sus propias reglas. Los datos dejan de ser una bolsa pasiva de valores que cualquier invocador puede mutar; se convierten en un objeto con criterio propio: uno que hace cumplir sus invariantes, anuncia lo que ha ocurrido y coopera con la capa de persistencia mientras permanece libre de cualquier importación de base de datos.

Este capítulo asciende el monedero a un agregado DDD en condiciones: un objeto de valor `Money`, una raíz de agregado `Wallet` que protege la regla de descubierto y la regla de coincidencia de divisa, y un conjunto de `DomainEvent`s que se emiten cada vez que el monedero cambia de estado. Un mapeador ligero convierte entre el rico modelo de dominio y el registro plano de persistencia; ninguno de los dos lados necesita conocer la forma del otro.

---

## Entidades y objetos de valor

Antes de poder construir un modelo que haga cumplir sus propias reglas, necesitas un vocabulario para los dos tipos fundamentalmente distintos de objetos que aparecen en todo dominio.

Piensa en qué hace que dos monederos sean distintos. Aunque dos monederos contengan exactamente cien euros, siguen siendo monederos separados que pertenecen a propietarios separados: te importa *cuál* tienes. Ahora piensa en el importe en sí. Cien euros son cien euros; el objeto Python exacto que contiene el valor es irrelevante. Si un depósito añade cincuenta euros al saldo de un monedero, no quieres actualizar el importe existente en su sitio: quieres derivar un importe completamente nuevo que registre el resultado. Mutar en el sitio invita a errores de aliasing en los que dos partes del código comparten sin saberlo una referencia al mismo objeto y ven los cambios del otro.

!!! note "La jerga, en lenguaje llano"
    Algunas palabras se repiten a lo largo de este capítulo. Una **invariante** es una regla que siempre debe cumplirse; para un monedero, "el saldo nunca es negativo". Un **agregado** es un pequeño grupo de objetos que cambian juntos y deben mantenerse coherentes como conjunto. Un **error de aliasing** ocurre cuando dos fragmentos de código sostienen accidentalmente el *mismo* objeto y uno lo muta, sorprendiendo al otro. Ten presentes estos tres conceptos; el resto del capítulo trata en gran medida de prevenir el último y garantizar el primero dentro del segundo.

El DDD da nombre a estos dos roles: **entidades** y **objetos de valor**, y el módulo `pyfly.domain` de PyFly los convierte en conceptos de primera clase:

| Concepto | Base de PyFly | Igualdad | Mutación |
|---|---|---|---|
| **`Entity[TID]`** | `Entity` | Identidad: iguales solo cuando coincide el `id` | Permitida mediante métodos propios |
| **`ValueObject`** | `ValueObject` | Estructural: se comparan todos los campos | Prohibida; `replace(**changes)` crea una nueva instancia |

Las entidades transitorias (las que tienen `id=None`) se comparan iguales solo por la identidad de objeto de Python, así que puedes meter entidades en conjuntos y diccionarios sin preocuparte por colisiones de hash de objetos sin guardar.

El dinero es el objeto de valor de manual. Un importe de cien euros no es un objeto específico que sigues en el tiempo; es un valor. Dos instancias separadas de `Money(100, "EUR")` son iguales. Un depósito no muta el importe existente: produce uno nuevo, dejando el original intacto y el modelo libre de efectos secundarios ocultos.

Aquí está el objeto de valor `Money` para Lumen:

::: listing lumen/models/entities/v1/money.py | Listado 6.1 — Money: un objeto de valor inmutable con aritmética consciente de la divisa
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.domain import BusinessRuleViolation, ValueObject


@dataclass(frozen=True)
class Money(ValueObject):
    """An exact monetary amount in a single currency.

    ``amount`` is in minor units (e.g. cents): ``Money(1050,
    Currency.EUR)`` is €10.50. Arithmetic returns new ``Money``
    instances and refuses to mix currencies.
    """

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise BusinessRuleViolation(
                "money-amount-integer",
                "amount must be an integer number of minor units",
            )

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """The additive identity for *currency* (a zero balance)."""
        return cls(amount=0, currency=currency)

    def add(self, other: Money) -> Money:
        """Return ``self + other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: Money) -> Money:
        """Return ``self - other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    @property
    def major_units(self) -> float:
        """The amount rendered as a major-unit decimal (cents / 100)."""
        return round(self.amount / 100, 2)

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise BusinessRuleViolation(
                "money-currency-mismatch",
                f"cannot combine {self.currency.value} "
                f"with {other.currency.value}",
            )

    def __str__(self) -> str:
        return f"{self.major_units:.2f} {self.currency.value}"
:::

**Cómo funciona.** El importe se almacena en **unidades menores** —céntimos enteros, peniques o cualquiera que sea la denominación más pequeña de la divisa— para eliminar por completo el redondeo en coma flotante. Los cálculos financieros que usan `float` son una fuente crónica de errores de un céntimo de más o de menos que solo afloran en producción, normalmente durante la conciliación. Almacenar 10,50 € como `amount=1050` mantiene toda la aritmética exacta. `__post_init__` rechaza de inmediato los importes que no sean enteros con `BusinessRuleViolation("money-amount-integer")`, así que un `float` perdido como `10.5` nunca entra en silencio en el modelo.

El campo `currency` usa el enum `Currency` (`Currency.EUR`, `Currency.USD`, `Currency.GBP`) en lugar de una cadena pelada, descartando erratas en el momento de la construcción. Las comparaciones de divisa dentro de `_assert_same_currency` usan la comprobación de identidad de Python (`is`) —lanzando `BusinessRuleViolation("money-currency-mismatch")` si difieren— para que el error aflore exactamente donde se cometió el fallo.

Tanto `add` como `subtract` devuelven una *nueva* instancia de `Money` en lugar de modificar `self`, consecuencia directa de `frozen=True`. Esta garantía de inmutabilidad significa que el agregado que contiene un valor `Money` nunca puede quedar parcialmente actualizado: o bien el reemplazo completo tiene éxito, o bien el valor antiguo permanece en su sitio. Las propiedades `is_positive` e `is_negative` exponen el signo sin filtrar el entero crudo. `major_units` convierte a un decimal para mostrarlo, y `__str__` da formato como `"10.50 EUR"` mediante `currency.value`.

**Constrúyelo paso a paso.** Si estás creando `Money` desde cero, este es el orden en que escribirlo, y por qué importa cada línea.

Paso 1 — Declara los dos campos y congela la clase. Añade `amount: int` y `currency: Currency` a una clase decorada con `@dataclass(frozen=True)` que herede de `ValueObject`. `frozen=True` es lo que hace que el objeto sea inmutable y te da la igualdad estructural de regalo: dos instancias de `Money` con el mismo importe y divisa ahora son `==`.

Paso 2 — Protege el constructor. Añade `__post_init__` para rechazar cualquier cosa que no sea un entero simple. La comprobación `isinstance(self.amount, bool)` es deliberada: en Python `True` es un `int`, y no quieres que `Money(True, ...)` se cuele.

Paso 3 — Añade la factoría `zero`. Un monedero se abre con saldo cero, así que `Money.zero(currency)` se lee mejor en el punto de llamada que `Money(0, currency)`.

Paso 4 — Añade `add` y `subtract`, cada uno pasando primero por `_assert_same_currency`. Devolver un `Money` completamente nuevo (sin mutar nunca `self`) es lo que previene el error de aliasing de la apertura de la sección.

Paso 5 — Añade los ayudantes de visualización: `is_positive`, `is_negative`, `major_units` y `__str__`.

**Ejecútalo.** `Money` no depende del runtime del framework ni de una base de datos, así que puedes ejercitar todas las reglas desde un prompt de Python. Arranca uno con `uv run python` desde el directorio `samples/lumen` y escribe:

```python
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> ten_fifty = Money(1050, Currency.EUR)
>>> str(ten_fifty)
'10.50 EUR'
>>> ten_fifty.add(Money(450, Currency.EUR))
Money(amount=1500, currency=<Currency.EUR: 'EUR'>)
>>> ten_fifty.add(Money(100, Currency.USD))
Traceback (most recent call last):
  ...
pyfly.domain.exceptions.BusinessRuleViolation: cannot combine EUR with USD
>>> Money(10.5, Currency.EUR)
Traceback (most recent call last):
  ...
pyfly.domain.exceptions.BusinessRuleViolation: amount must be an integer number of minor units
```

Las dos trazas son la clave: el modelo rechaza una suma entre divisas y un importe en coma flotante *en el momento en que cometes el fallo*, no tres capas más abajo durante la conciliación.

Lumen incluye estos comportamientos exactos como pruebas. Ejecútalas con:

```
uv run --extra dev pytest tests/test_money.py -q
```

y deberías ver pasar las seis:

```
......                                                             [100%]
6 passed in 0.0Xs
```

**Qué acaba de pasar.** Has construido un objeto de valor imposible de usar mal: no se puede mutar, no puede mezclar divisas y no puede contener un `float`. Cualquier otra pieza del modelo del monedero se apoyará en estas garantías, por lo cual `Money` va primero.

!!! note "Unidades menores frente a decimal"
    Almacenar el dinero como céntimos enteros es una convención; otra es el `decimal.Decimal` de Python con una escala fija. Ambas son válidas. Lo que importa es elegir una y ceñirse a ella dentro del contexto delimitado (bounded context). Para Lumen, las unidades menores enteras mantienen el modelo libre de configuración de precisión en tiempo de importación, y `__post_init__` impone la restricción con `BusinessRuleViolation("money-amount-integer")` para que un `float` nunca entre en silencio en el modelo.

!!! spring "Equivalencia con Spring"
    `ValueObject` refleja el conjunto `@ValueObject` / `@Embeddable` del ecosistema JPA de Spring y la interfaz marcadora `ValueObject` de Spring Modulith. El dataclass con `frozen=True` se corresponde con el tipo `record` de Java introducido en Java 16: inmutable, igualdad basada en valor, sintaxis concisa. La anotación `@ValueObject` de jMolecules expresa la misma intención.

---

## La raíz de agregado

`Money` resuelve el problema de representación: ahora los importes son inmutables y conscientes de la divisa. Pero Lumen todavía necesita algo que *posea* el saldo del monedero y decida cuándo se permite un depósito o una retirada. Ese es el papel de la **raíz de agregado**.

Una entidad se convierte en raíz de agregado cuando posee un grupo de objetos relacionados y actúa como el único punto de entrada para todos los cambios dentro de ese grupo. La raíz de agregado es la **frontera de coherencia**: ningún código externo entra y muta directamente un objeto interno. Todos los cambios fluyen a través de los métodos de la raíz, que hacen cumplir las reglas. Este es el diseño que evita el atajo de las 11 de la noche descrito en la introducción del capítulo: una vez que todo cambio debe fluir a través de la raíz, no hay puerta trasera.

`AggregateRoot[TID]` amplía `Entity[TID]` con una sola adición: un búfer interno de **eventos de dominio pendientes**. Cada método que cambia el estado llama a `self.raise_event(event)` para registrar lo ocurrido. Cuando el repositorio guarda el agregado, el servicio de aplicación vacía ese búfer con `clear_events()` y publica los eventos en el bus de eventos. Verás el ciclo completo de publicación en la sección Eventos de dominio; por ahora, concéntrate en el propio agregado.

Aquí está la raíz de agregado `Wallet`:

::: listing lumen/models/entities/v1/wallet_entity.py | Listado 6.2 — Wallet: la raíz de agregado que posee el saldo e impone sus reglas
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent


# ── Domain events ─────────────────────────────────────────────────────────────

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


# ── Aggregate root ────────────────────────────────────────────────────────────

class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    __slots__ = ("owner_id", "balance", "created_at")

    def __init__(
        self,
        id: str,
        owner_id: str,
        balance: Money,
        created_at: datetime | None = None,
    ) -> None:
        super().__init__(id)
        self.owner_id = owner_id
        self.balance = balance
        self.created_at = created_at or datetime.now(UTC)

    @property
    def currency(self) -> Currency:
        return self.balance.currency

    # ── Factory method ─────────────────────────────────────────────────────

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: Currency) -> Wallet:
        """Open a new, empty wallet; raises WalletOpened."""
        if not owner_id.strip():
            raise BusinessRuleViolation(
                "wallet-owner-required", "owner_id is required"
            )
        wallet = cls(
            id=wallet_id,
            owner_id=owner_id,
            balance=Money.zero(currency),
        )
        wallet.raise_event(
            WalletOpened(
                wallet_id=wallet_id,
                owner_id=owner_id,
                currency=currency.value,
            )
        )
        return wallet

    # ── Behaviour ──────────────────────────────────────────────────────────

    def deposit(self, amount: Money) -> None:
        """Credit *amount* to the balance; raises FundsDeposited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "wallet-deposit-positive",
                "deposit amount must be > 0",
            )
        self.balance = self.balance.add(amount)
        assert self.id is not None
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    def withdraw(self, amount: Money) -> None:
        """Debit *amount*; refuses to overdraw. Raises FundsWithdrawn."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "wallet-withdrawal-positive",
                "withdrawal amount must be > 0",
            )
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "wallet-insufficient-funds",
                f"cannot withdraw {amount}; balance is {self.balance}",
            )
        self.balance = remaining
        assert self.id is not None
        self.raise_event(
            FundsWithdrawn(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.balance.currency:
            raise BusinessRuleViolation(
                "wallet-currency-mismatch",
                f"wallet holds {self.balance.currency.value}, "
                f"got {amount.currency.value}",
            )
:::

**Cómo funciona.** La frontera del agregado se impone en tres niveles. Primero, `__slots__` bloquea el conjunto de atributos, y `balance` y `owner_id` son deliberadamente públicos-pero-propios: solo los métodos del propio agregado (`deposit`, `withdraw`) los mutan, mientras que la propiedad `currency` es una comodidad de solo lectura que delega en `balance.currency`. Segundo, el classmethod factoría `open` es la única forma legítima de crear un monedero nuevo: el invocador suministra el `wallet_id` (para que la capa de aplicación controle la generación de IDs), `open` valida que `owner_id` no esté en blanco, inicializa el saldo con `Money.zero(currency)` y encola de inmediato `WalletOpened`. Usar una factoría en lugar de llamar a `__init__` directamente asegura que el evento de apertura *nunca* se olvide, ni siquiera en un fixture de prueba. Tercero, los eventos de dominio —`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`— son dataclasses congelados. `FundsDeposited` y `FundsWithdrawn` llevan un campo `balance` (el saldo posterior a la operación en unidades menores), de modo que un suscriptor nunca necesita volver a consultar al agregado para conocer el estado actual.

**Constrúyelo paso a paso.** La clase `Wallet` tiene más piezas móviles que `Money`, así que este es el orden para ensamblarla.

Paso 1 — Define primero las tres clases de evento (`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`), cada una un `@dataclass(frozen=True)` que herede de `DomainEvent`. Tienen que existir antes de que el agregado pueda referenciarlas. Cubrimos los eventos a fondo en la sección dentro de dos; por ahora son simplemente los registros que el monedero emitirá.

Paso 2 — Declara el agregado. Hereda de `AggregateRoot[str]` (el `[str]` dice que el id es una cadena), define `__slots__` para bloquear los nombres de atributo y escribe `__init__` para almacenar `owner_id`, `balance` y `created_at`. Llama a `super().__init__(id)` primero para que la clase base configure el id y el búfer interno de eventos.

Paso 3 — Añade la propiedad de solo lectura `currency` que delega en `self.balance.currency`. Es una comodidad que evita que los invocadores tengan que bajar dos niveles hasta `wallet.balance.currency`.

Paso 4 — Escribe el classmethod factoría `open`. Valida `owner_id`, construye el monedero con un saldo `Money.zero(currency)` y llama a `self.raise_event(WalletOpened(...))`. Hacer `open` un classmethod —en lugar de esperar que los invocadores usen `__init__` más un evento manual— garantiza que el evento de apertura nunca se olvide.

Paso 5 — Escribe `deposit` y `withdraw`. Cada uno valida primero (coincidencia de divisa, importe positivo y, para `withdraw`, fondos suficientes), *después* muta `self.balance`, *después* lanza su evento. El orden importa: nunca mutes antes de que hayan pasado todas las comprobaciones, o puedes dejar el monedero en un estado a medio cambiar.

Paso 6 — Añade el ayudante privado `_assert_currency` que comparten ambas transiciones.

!!! note "raise_event no lanza una excepción"
    A pesar del nombre, `raise_event` no lanza nada. *Añade* un evento a un búfer interno del agregado (`AggregateRoot._pending_events`). Nada se publica en ese momento. Un paso posterior —el servicio de aplicación, tras un guardado exitoso— vacía el búfer con `clear_events()` y entrega los eventos al bus de eventos. Piensa en `raise_event` como "anota que esto ocurrió", no como "aborta".

**Ejecútalo.** Igual que `Money`, el agregado `Wallet` es Python puro: no hace falta base de datos. Desde `uv run python`:

```python
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> w = Wallet.open("wlt-1", "owner-1", Currency.EUR)
>>> w.deposit(Money(1000, Currency.EUR))
>>> w.withdraw(Money(400, Currency.EUR))
>>> str(w.balance)
'6.00 EUR'
>>> [e.event_type for e in w.pending_events()]
['WalletOpened', 'FundsDeposited', 'FundsWithdrawn']
>>> w.withdraw(Money(9999, Currency.EUR))
Traceback (most recent call last):
  ...
pyfly.domain.exceptions.BusinessRuleViolation: cannot withdraw 99.99 EUR; balance is 6.00 EUR
```

Fíjate en los tres eventos que esperan en el búfer tras las operaciones exitosas, y fíjate en que el intento de descubierto se lanzó *antes* de tocar el saldo: `pending_events()` sigue mostrando tres, no cuatro. La batería de pruebas de Lumen afirma exactamente esto:

```
uv run --extra dev pytest tests/test_wallet_aggregate.py -q
```

```
......                                                             [100%]
6 passed in 0.0Xs
```

**Qué acaba de pasar.** El monedero ahora posee sus reglas. No hay forma de dejarlo en descubierto, no hay forma de alimentarlo con la divisa equivocada y no hay forma de cambiar su estado sin dejar atrás un evento que registre lo ocurrido. La capa de servicio ya no tiene que recordar nada de eso.

El diagrama de abajo muestra el panorama completo: estado, invariantes y los eventos que el monedero emite.

::: figure art/figures/06-aggregate.svg | Figura 6.1 — El agregado Wallet: estado, invariantes y los eventos que emite.

!!! spring "Equivalencia con Spring"
    `AggregateRoot[str]` se corresponde con `org.jmolecules.ddd.types.AggregateRoot<A, ID>` de jMolecules y con `AbstractAggregateRoot<A>` de Spring Data, que ofrece el mismo mecanismo `registerEvent()` / `@DomainEvents` / `@AfterDomainEventPublication`. El patrón es idéntico en espíritu: el agregado acumula eventos en un búfer; el repositorio los vacía tras un guardado exitoso; un `DomainEventPublisher` los despacha. El `raise_event` + `clear_events` de PyFly es el equivalente Python de `registerEvent` + `@AfterDomainEventPublication`.

---

## Proteger las invariantes

La raíz de agregado solo es valiosa si las reglas que impone son genuinamente inalcanzables por cualquier otra vía. Eso es lo que significa **invariante** en DDD: una condición que el modelo debe mantener sin importar cómo se le llame, quién lo llame o cuántos servicios existan en la aplicación. Una invariante no es una sugerencia: es una restricción que no puede violarse porque el modelo no expone ningún mecanismo para hacerlo.

El `Wallet` de Lumen tiene tres invariantes:

1. El saldo nunca debe bajar de cero (sin descubierto).
2. Los fondos solo pueden depositarse o retirarse en la divisa nativa del monedero.
3. Los importes de depósito y retirada deben ser estrictamente positivos.

Las tres se imponen dentro de los métodos del agregado. La excepción del framework para esto es **`BusinessRuleViolation`** de `pyfly.domain`. Toma dos argumentos obligatorios: un identificador `rule` estable y legible por máquina, y un `message` legible por humanos. Los identificadores de Lumen —`"wallet-insufficient-funds"`, `"wallet-currency-mismatch"`, `"wallet-deposit-positive"`, `"wallet-withdrawal-positive"`— son etiquetas en kebab-case que viajan en el cuerpo de respuesta RFC 7807 y en los campos de log estructurado.

`BusinessRuleViolation` amplía `pyfly.kernel.BusinessException`, así que el mapeador de detalles de problema (problem-details) RFC 7807 del Capítulo 4 la traduce automáticamente a una respuesta HTTP 422, sin necesidad de un manejador adicional.

!!! note "Qué significa aquí RFC 7807"
    RFC 7807 es el estándar web para "detalles de problema": una forma JSON pequeña y predecible (`type`, `title`, `status`, `detail`, más tus propios campos) que devuelve una API cuando algo falla. El mapeador de PyFly convierte automáticamente cualquier `BusinessException` en uno de estos, de modo que el identificador `rule` que pones en `BusinessRuleViolation` acaba en el cuerpo de respuesta donde un cliente puede leerlo sin analizar prosa en inglés.

**Comprueba que la invariante se sostiene.** La promesa de una invariante es que una operación *fallida* deja el modelo exactamente como estaba. Puedes demostrarlo desde `uv run python`:

```python
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> from pyfly.domain import BusinessRuleViolation
>>> w = Wallet.open("wlt-1", "owner-1", Currency.EUR)
>>> w.deposit(Money(500, Currency.EUR))
>>> w.clear_events()            # drain the open + deposit events
[WalletOpened(...), FundsDeposited(...)]
>>> try:
...     w.withdraw(Money(501, Currency.EUR))
... except BusinessRuleViolation as exc:
...     print(exc.rule)
...
wallet-insufficient-funds
>>> str(w.balance)              # unchanged — the rule fired before any mutation
'5.00 EUR'
>>> w.pending_events()          # and no event was queued for the failed attempt
[]
```

Esa es toda la garantía en tres líneas: el identificador de la regla es estable y legible por máquina, el saldo no se movió y ningún evento se filtró por una operación que nunca ocurrió.

!!! warning "Mantén las invariantes en el modelo, no en el servicio"
    Devolver la comprobación de descubierto a `WalletService` crea dos problemas. Primero, cualquier código que llame a `repo.save(entity)` directamente se salta la comprobación por completo. Segundo, acabas duplicando la regla en cada vía que modifica un monedero: el servicio, un trabajo en segundo plano, un comando de administración. Cuando la regla cambia —digamos que el equipo de producto introduce un colchón de descubierto configurable— hay exactamente un sitio que actualizar: el método del agregado. Esa es toda la cuestión.

La diferencia entre una defensa a nivel de servicio y una invariante de agregado es la exigibilidad. Una defensa de servicio es una convención; una invariante de agregado es una restricción física impuesta por el encapsulamiento. Para concretarlo, así es como se ve el enfoque a nivel de servicio y por qué es frágil:

::: listing lumen/wallet_service_before.py | Listado 6.3 — Antes: reglas de negocio dispersas por el servicio (frágil)
# DO NOT DO THIS — rules that belong in the model
from pyfly.container import service


@service
class WalletServiceBefore:

    async def withdraw(self, wallet_id: str, amount: float) -> None:
        # Rule lives here — but anyone calling repo.save directly skips it
        wallet = {"id": wallet_id, "balance": 50.0, "currency": "EUR"}
        if wallet["balance"] < amount:
            raise ValueError("Insufficient funds")
        wallet["balance"] -= amount
        # ... save
:::

Y así es como se ve el servicio después de que el modelo asume la propiedad:

::: listing lumen/wallet_service_after.py | Listado 6.4 — Después: el servicio delega en el agregado
from pyfly.container import service
from pyfly.domain import AggregateNotFound

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money


@service
class WalletService:

    async def withdraw(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: Currency,
    ) -> None:
        # The service orchestrates; the aggregate decides.
        wallet = await self._repo.find(wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", wallet_id)
        wallet.withdraw(Money(amount=amount_cents, currency=currency))
        await self._repo.save(wallet)
        # Events are drained and published by the repository/service boundary
:::

**Cómo funciona.** La versión "después" se lee como una instrucción: `wallet.withdraw(...)` significa "pídele al monedero que retire". El servicio no sabe —ni le importa— qué implica eso. Confía en que el agregado o bien tendrá éxito, o bien lanzará una `BusinessRuleViolation`. Ese patrón de orquestador ligero tiene un beneficio práctico para el flujo de trabajo del equipo: un desarrollador nuevo puede implementar un endpoint `transfer` sin leer `WalletService` en absoluto. Las restricciones viven en `Wallet`: un único sitio donde mirar.

El identificador `rule` de `BusinessRuleViolation` también importa. Cadenas como `"wallet-insufficient-funds"` y `"wallet-currency-mismatch"` viajan en el cuerpo de respuesta RFC 7807, donde el código del cliente puede emparejarlas sin analizar mensajes de texto libre. También aparecen en los campos de log estructurado, lo que hace que escribir alertas de producción sea sencillo.

!!! note "AggregateNotFound"
    `AggregateNotFound` es la segunda excepción de dominio en `pyfly.domain`. Lánzala cuando el agregado solicitado no exista: se corresponde con una respuesta de detalles de problema 404 mediante el mismo manejador RFC 7807. El constructor toma el nombre del tipo de agregado y el ID: `AggregateNotFound("Wallet", wallet_id)`.

---

## Eventos de dominio

Tu agregado ya hace cumplir sus invariantes y controla todos los cambios de estado. Pero Lumen acabará necesitando reaccionar a esos cambios: actualizar un registro de auditoría, enviar una notificación push, disparar la detección de fraude, publicar un asiento en el libro mayor. La solución tentadora es poner esos efectos secundarios directamente dentro de `deposit` y `withdraw`. Eso acopla el modelo de dominio a la infraestructura: de repente tu monedero necesita conocer los topics de Kafka y las plantillas de correo, y cada prueba unitaria arrastra una conexión a un broker.

Los **eventos de dominio** cortan ese acoplamiento. Un evento de dominio registra algo que *ocurrió* dentro del agregado: en pasado, un hecho inmutable. El agregado no sabe qué se hará con el hecho; solo lo registra. Los consumidores aguas abajo —escuchadores de eventos, proyectores, servicios de notificación— se suscriben y reaccionan en su propio contexto, sin que el agregado dependa jamás de ellos.

`DomainEvent` de `pyfly.domain` es una base de dataclass congelado que autorrellena tres campos cuando se crea una instancia:

- `event_id` — un UUID v4 que identifica de forma única esta ocurrencia.
- `occurred_at` — una marca de tiempo UTC en el momento de la construcción.
- `event_type` — una propiedad que por defecto es el nombre de la clase de la subclase (`"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`).

Ya viste los tres eventos del monedero definidos en el Listado 6.2. Aquí están aislados, con una mirada explícita a lo que obtienes de la base:

::: listing lumen/models/entities/v1/wallet_entity.py | Listado 6.5 — Eventos de dominio y los campos que DomainEvent proporciona automáticamente
from dataclasses import dataclass
from pyfly.domain import DomainEvent


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
    balance: int = 0   # balance after the deposit, in minor units


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0   # balance after the withdrawal, in minor units


# Each event carries event_id (UUID), occurred_at (UTC datetime),
# and event_type (class name) — all set by DomainEvent.__post_init__.

def demonstrate_event_fields() -> None:
    evt = FundsDeposited(
        wallet_id="w-1",
        amount=5000,
        currency="EUR",
        balance=15000,
    )
    print(evt.event_id)       # e.g. "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    print(evt.occurred_at)    # e.g. datetime(2026, 6, 7, 9, 30, 0, tzinfo=UTC)
    print(evt.event_type)     # "FundsDeposited"
:::

**Cómo funciona.** Cada clase de evento declara solo los campos exclusivos de esa ocurrencia. Todo lo demás viene de `DomainEvent`: un `event_id` UUID para el procesamiento idempotente, `occurred_at` para el rastro de auditoría y `event_type` —el nombre de la clase— para el enrutamiento en el consumidor sin inspeccionar la clase Python. Todos los campos tienen como valor por defecto cero o cadena vacía para que la maquinaria del dataclass con `frozen=True` pueda ofrecer construcción mediante argumentos por palabra clave sin requerir argumentos posicionales.

Fíjate en que `FundsDeposited` lleva tanto `amount` (la transacción) como `balance` (el saldo posterior a la operación, en unidades menores). Un suscriptor que actualice un saldo de modelo de lectura no necesita ninguna llamada de vuelta al agregado ni a la base de datos: todo está en el evento. Ese diseño autocontenido mantiene simples a los consumidores y elimina viajes de ida y vuelta adicionales.

!!! note "Modelo de lectura, en lenguaje llano"
    Un **modelo de lectura** (read-model) es una copia separada de los datos, optimizada para consultas y moldeada para mostrarlos: un total de panel, un índice de búsqueda, una caché. Como `FundsDeposited` ya lleva el `balance` posterior a la operación, un servicio que mantenga tal copia puede aplicar el evento a ciegas sin volver a cargar nunca el monedero. Construiremos modelos de lectura como es debido en un capítulo posterior; aquí, basta con anotar por qué poner `balance` en el evento merece la pena.

**Ejecútalo.** Puedes ver los tres campos autorrellenados en cualquier evento desde `uv run python`:

```python
>>> from lumen.models.entities.v1.wallet_entity import FundsDeposited
>>> evt = FundsDeposited(wallet_id="w-1", amount=5000, currency="EUR", balance=15000)
>>> evt.event_type
'FundsDeposited'
>>> evt.event_id           # a fresh UUID, set by DomainEvent.__post_init__
'3fa85f64-5717-4562-b3fc-2c963f66afa6'
>>> evt.occurred_at        # a UTC timestamp, also set automatically
datetime.datetime(2026, 6, 16, 9, 30, 0, tzinfo=datetime.timezone.utc)
```

Declaraste cuatro campos; obtuviste siete, porque `DomainEvent` aporta `event_id`, `occurred_at` y la propiedad `event_type`. Ese es todo el atractivo de heredar de él: cada evento se autoidentifica con cero código adicional.

El ciclo de vida del evento abarca dos fases. Dentro del agregado: cuando `wallet.deposit(amount)` tiene éxito, llama a `self.raise_event(FundsDeposited(...))`, añadiendo el evento a un búfer privado de `AggregateRoot`. Todavía no se publica nada. En la frontera del servicio: después de que el repositorio guarde el agregado y la transacción confirme, el servicio de aplicación vacía el búfer y publica. Esta secuencia de *guardar primero, publicar después* garantiza que nunca se despache un evento por un cambio que no llegó a persistir. El Listado 6.6 muestra esa frontera al completo:

::: listing lumen/wallet_application_service.py | Listado 6.6 — Vaciar los eventos de dominio tras un guardado exitoso
import uuid

from pyfly.container import service
from pyfly.eda import EventPublisher

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet


@service
class WalletApplicationService:

    def __init__(
        self,
        repo: object,              # typed as WalletRepository in practice
        events: EventPublisher,
    ) -> None:
        self._repo = repo
        self._events = events

    async def open_wallet(self, owner_id: str, currency: Currency) -> str:
        wallet_id = str(uuid.uuid4())
        wallet = Wallet.open(
            wallet_id=wallet_id, owner_id=owner_id, currency=currency
        )
        await self._repo.save(wallet)
        for event in wallet.clear_events():
            await self._events.publish(event)
        return wallet_id

    async def deposit(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: Currency,
    ) -> None:
        wallet = await self._repo.find(wallet_id)
        wallet.deposit(Money(amount=amount_cents, currency=currency))
        await self._repo.save(wallet)
        for event in wallet.clear_events():
            await self._events.publish(event)
:::

**Cómo funciona.** `open_wallet` llama a `Wallet.open`, que encola internamente un evento `WalletOpened`; tras el `save`, `wallet.clear_events()` devuelve ese único evento y `publish` lo despacha. `deposit` sigue el mismo patrón de tres pasos: cargar, mutar, guardar; luego vaciar. El bucle `for event in wallet.clear_events()` es deliberadamente explícito en lugar de ocultarse dentro del repositorio, porque el servicio de aplicación es el sitio adecuado para decidir *cuándo* ocurre la publicación: después de la frontera de la transacción, no antes.

**El ciclo de publicación, paso a paso.** Cada método de comando del servicio de aplicación sigue el mismo ritmo de cuatro tiempos. Apréndelo una vez y cada caso de uso futuro (transferencia, reembolso, congelación) se escribirá solo:

Paso 1 — Cargar (o crear). Para un monedero nuevo, llama a la factoría `Wallet.open`; para uno existente, `await self._repo.find(wallet_id)`.

Paso 2 — Mutar mediante un método de comportamiento. Llama a `wallet.deposit(...)` o `wallet.withdraw(...)`. Aquí es donde se ejecutan las invariantes y se encolan los eventos en el búfer del agregado.

Paso 3 — Guardar. `await self._repo.save(wallet)`. Nada se ha publicado todavía, a propósito: si el guardado falla, ningún evento escapa.

Paso 4 — Vaciar y publicar. `for event in wallet.clear_events(): await self._events.publish(event)`. Esto se ejecuta solo después de que el guardado tenga éxito, de modo que nunca se despacha un evento por un cambio que no persistió.

**Qué acaba de pasar.** El agregado decide *qué* ocurrió y lo registra; el servicio de aplicación decide *cuándo* se entera el mundo. Mantener esas dos responsabilidades separadas es la razón por la que el monedero permanece libre de cualquier código de broker, cola o transacción, y por la que el orden de publicación es "guardar primero, publicar después".

!!! tip "Orden de los eventos"
    `raise_event` añade al búfer en el orden de llamada. `clear_events` lo vacía y lo limpia, devolviendo los eventos en el mismo orden. Si un único método del agregado lanza varios eventos (una operación por lotes, por ejemplo), llegan al bus de eventos en el orden en que se lanzaron: los más antiguos primero.

---

## Dominio frente a persistencia

Con el modelo de dominio y sus eventos en su sitio, queda una tensión: ¿cómo llega el agregado `Wallet` a la base de datos?

El atajo tentador es anotar `Wallet` directamente con campos `Mapped[]` de SQLAlchemy y un `__tablename__`. Eso fusiona dos preocupaciones que cambian a ritmos muy distintos: las reglas de negocio evolucionan con el producto; las definiciones de columna evolucionan con el esquema. Mezclarlas significa que un cambio de esquema te obliga a tocar el agregado, y un cambio de regla arriesga romper accidentalmente un mapeo de columna. También arrastra SQLAlchemy a cada prueba unitaria.

La alternativa son dos modelos que coexisten sin conocerse el uno al otro, y un mapeador ligero que convierte entre ellos:

| Modelo | Contiene | Conoce |
|---|---|---|
| `Wallet` | Reglas de negocio, eventos de dominio, invariantes | Nada fuera de `pyfly.domain` |
| `WalletEntity` | Cinco columnas: `id`, `owner_id`, `currency`, `balance_minor`, `created_at` | Solo SQLAlchemy + `pyfly.data` |

`Wallet` es Python puro: sin anotaciones `Mapped[]`, sin `__tablename__`. Puedes instanciarlo en una prueba unitaria con dos líneas y ejercitar todas las invariantes sin una conexión a base de datos. `WalletEntity` es persistencia pura: hereda de `Base` de `pyfly.data.relational.sqlalchemy`, lleva columnas tipadas de SQLAlchemy 2.0 y no sabe nada de reglas de dominio ni de eventos. El `Repository[WalletEntity, str]` del framework (Capítulo 5) almacena y recupera filas; un mapeador ligero convierte entre la fila y el agregado en cada cruce.

El Listado 6.7 muestra `WalletEntity` —la fila del ORM— seguido de las dos funciones del mapeador que cruzan la frontera:

::: listing lumen/models/entities/v1/wallet_orm.py | Listado 6.7 — WalletEntity: la fila de persistencia de SQLAlchemy
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

::: listing lumen/core/mappers/wallet_mapper.py | Listado 6.8 — wallet_mapper: funciones puras que cruzan la frontera dominio/persistencia
from __future__ import annotations

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity


def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a Wallet aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )


def to_aggregate(entity: WalletEntity) -> Wallet:
    """Rehydrate a Wallet aggregate from a persistence row."""
    currency = Currency(entity.currency)
    return Wallet(
        id=entity.id,
        owner_id=entity.owner_id,
        balance=Money(amount=entity.balance_minor, currency=currency),
        created_at=entity.created_at,
    )
:::

**Cómo funciona.** `WalletEntity` hereda de `Base` en lugar de llevar lógica de dominio alguna, de modo que importarla registra la tabla `wallets` en `Base.metadata`; el `EngineLifecycle` del framework crea la tabla al arrancar cuando se establece `ddl-auto=create`. La clave primaria es el propio id de cadena del agregado (`wlt-…`), no un sustituto, de modo que la fila y el `Wallet` comparten una sola identidad y no hace falta traducción.

`to_entity` escribe `wallet.balance.amount` (un entero) directamente en `balance_minor`: sin conversión a `float`. `to_aggregate` reconstruye un enum `Currency` a partir de la cadena ISO-4217 almacenada mediante `Currency(entity.currency)`, y luego construye un `Money` a partir del valor entero crudo en unidades menores. El campo `created_at` se preserva en el viaje de ida y vuelta, de modo que los agregados rehidratados conservan su marca de tiempo original. No hay ningún cruce de frontera en coma flotante por ninguna parte.

**Construye el mapeador paso a paso.** Un mapeador no es más que dos funciones puras. No hay magia del framework que cablear; las escribes y las llamas en los momentos adecuados.

Paso 1 — Escribe `to_entity(wallet)`. Lee cada pieza del agregado y cópiala en la fila plana: `wallet.id`, `wallet.owner_id`, `wallet.currency.value` (la cadena ISO, no el enum), `wallet.balance.amount` (las unidades menores enteras crudas) y `wallet.created_at`. El `assert wallet.id is not None` hace explícita la precondición: solo persistes monederos que ya tienen un id.

Paso 2 — Escribe `to_aggregate(entity)`, lo inverso. Reconstruye el enum `Currency` a partir de la cadena almacenada con `Currency(entity.currency)`, envuelve el entero de nuevo en un `Money` y construye un `Wallet`. Pasar `created_at=entity.created_at` preserva la marca de tiempo original a través del viaje de ida y vuelta.

Paso 3 — Llámalas en la frontera, no dentro del agregado. El manejador de comandos llama a `to_entity` justo antes de `repo.save(...)` y a `to_aggregate` justo después de `repo.find(...)`. La clase `Wallet` nunca importa la fila, y la fila nunca importa el `Wallet`.

**Ejecútalo.** El viaje de ida y vuelta es Python puro: no se requiere una base de datos en vivo para demostrar que preserva todos los campos. Desde `uv run python`:

```python
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> from lumen.core.mappers import wallet_mapper
>>> w = Wallet.open("wlt-1", "owner-1", Currency.EUR)
>>> w.deposit(Money(2500, Currency.EUR))
>>> row = wallet_mapper.to_entity(w)        # aggregate -> flat row
>>> (row.id, row.currency, row.balance_minor)
('wlt-1', 'EUR', 2500)
>>> back = wallet_mapper.to_aggregate(row)  # flat row -> aggregate
>>> str(back.balance), back.owner_id
('25.00 EUR', 'owner-1')
```

El entero `2500` cruza en ambas direcciones intacto: ningún `float` aparece jamás en la frontera de persistencia, que es toda la razón por la que `Money` almacena unidades menores.

**Qué acaba de pasar.** Mantuviste dos modelos que nunca se importan el uno al otro y los uniste con dos funciones diminutas. Un cambio de esquema ahora toca solo `WalletEntity`; un cambio de regla toca solo `Wallet`. Ninguno puede romper al otro por accidente.

El mapeador es deliberadamente estrecho. No hace cumplir reglas: eso lo hacen `Wallet.__init__` y los métodos de comportamiento. No publica eventos: eso lo hace el servicio de aplicación. Solo traduce la forma.

**El repositorio.** El servicio de aplicación nunca interactúa con `WalletEntity` directamente. En su lugar, un manejador de comandos llama a `wallet_mapper.to_entity(wallet)` antes de persistir y a `wallet_mapper.to_aggregate(entity)` después de cargar, mientras que el `WalletRepository(Repository[WalletEntity, str])` del framework gestiona todo el SQL. El Capítulo 5 cubre `Repository` al completo; el punto clave aquí es que el propio `Wallet` nunca importa SQLAlchemy: el agregado permanece libre de preocupaciones de persistencia a ambos lados de la frontera del mapeador.

!!! spring "Equivalencia con Spring"
    Esta estructura de dos modelos más mapeador es el equivalente Python del patrón
    defendido en *Implementing Domain-Driven Design* de Vaughn Vernon para Spring:
    una `WalletJpaEntity` anotada con `@Entity` (la fila de persistencia), un
    objeto de dominio `Wallet` (el agregado) y un `WalletAssembler` o un mapeador
    generado por MapStruct que traduce entre ellos. El
    `JpaRepository<WalletJpaEntity, String>` de Spring Data JPA se corresponde con el
    `Repository[WalletEntity, str]` de PyFly. La estructura es idéntica; el código
    repetitivo es menor.

---

## Especificaciones para las reglas de negocio

El agregado protege bien las operaciones que cambian el estado: no puedes dejar un monedero en descubierto ni depositar la divisa equivocada. Pero no todas las reglas tratan sobre la mutación. Algunas reglas son comprobaciones de elegibilidad: "antes de mostrar a este usuario el botón de retirada, ¿está el monedero en un estado operable?" o "de diez mil monederos, ¿cuáles cumplen los requisitos para el bono de fidelidad?". Estos son predicados de solo lectura, y codificarlos como métodos del agregado abarrotaría `Wallet` con lógica de consulta no relacionada con las transiciones de estado.

El **patrón Especificación** lo resuelve limpiamente. Una especificación es un predicado con nombre y reutilizable: un único método `is_satisfied_by(obj) -> bool` envuelto en un objeto que se compone con otros usando operadores booleanos. Como cada regla es su propia clase, puedes nombrar las reglas con claridad, reutilizarlas entre servicios y combinarlas en tiempo de ejecución según el contexto, algo que una cadena de `if` no puede hacer.

`Specification[T]` de `pyfly.domain` es un predicado en memoria componible. Hereda de ella, implementa `is_satisfied_by` y combina instancias con `&` (y), `|` (o) y `~` (no). Una especificación también es directamente invocable, así que puedes pasarla al `filter` integrado de Python sin código adaptador alguno.

!!! note "Dos tipos de especificación"
    `pyfly.domain.Specification` es el predicado en memoria que se usa dentro de los servicios de dominio. `pyfly.data.relational.sqlalchemy.Specification` (Capítulo 5) es el predicado de consulta consciente de la base de datos que empuja la regla hacia abajo, hasta el SQL. Los dos coexisten. Las especificaciones de dominio son para la lógica de negocio; las especificaciones de datos son para las consultas.

Aquí hay una especificación que expresa la regla "elegible para retirada":

::: listing lumen/domain/specs.py | Listado 6.9 — EligibleForWithdrawal: una Especificación de dominio componible
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.wallet_entity import Wallet
from pyfly.domain import Specification


class HasPositiveBalance(Specification[Wallet]):
    """The wallet has at least one cent remaining."""

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return wallet.balance.is_positive


class IsInCurrency(Specification[Wallet]):
    """The wallet holds a specific currency."""

    def __init__(self, currency: Currency) -> None:
        self._currency = currency

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return wallet.balance.currency is self._currency


# Compose: a wallet is eligible for withdrawal if it has a positive
# balance in the requested currency.
def eligible_for_withdrawal(currency: Currency) -> Specification[Wallet]:
    return HasPositiveBalance() & IsInCurrency(currency)


# Use as a predicate:
def filter_eligible(
    wallets: list[Wallet],
    currency: Currency,
) -> list[Wallet]:
    spec = eligible_for_withdrawal(currency)
    return list(filter(spec, wallets))
:::

**Cómo funciona.** `HasPositiveBalance` delega en `wallet.balance.is_positive` (una propiedad, sin paréntesis). `IsInCurrency` usa comparación de identidad (`is`) porque `Currency` es un `StrEnum` con miembros singleton. La factoría `eligible_for_withdrawal` las combina con `&`, produciendo un compuesto cuyo `is_satisfied_by` devuelve `True` solo cuando pasan ambas comprobaciones. Como `Specification` implementa `__call__`, pasas el compuesto directamente a `filter()`: no hace falta un envoltorio lambda.

**Construye una especificación paso a paso.**

Paso 1 — Hereda de `Specification[Wallet]` e implementa el único método requerido, `is_satisfied_by(self, wallet) -> bool`. Ese es todo el contrato: devuelve `True` o `False`, nunca lances.

Paso 2 — Si la regla necesita un parámetro (como una divisa con la que coincidir), tómalo en `__init__` y almacénalo, como hace `IsInCurrency`. Una regla sin parámetros como `HasPositiveBalance` se salta esto.

Paso 3 — Compón con los operadores booleanos. `HasPositiveBalance() & IsInCurrency(currency)` construye una nueva especificación cuyo `is_satisfied_by` es verdadero solo cuando lo son ambas mitades. `|` es o, `~` es no.

Paso 4 — Úsala como predicado. Como `Specification` implementa `__call__`, el compuesto *es* un invocable, así que puedes entregarlo directamente al `filter()` integrado de Python sin lambda de por medio.

**Ejecútalo.** Las especificaciones son objetos corrientes. Coloca las clases del Listado 6.9 en un módulo (digamos `src/lumen/domain/specs.py`) y pruébalas desde `uv run python`:

```python
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> from lumen.domain.specs import eligible_for_withdrawal
>>> empty = Wallet.open("wlt-1", "owner-1", Currency.EUR)
>>> funded = Wallet.open("wlt-2", "owner-2", Currency.EUR)
>>> funded.deposit(Money(1000, Currency.EUR))
>>> usd = Wallet.open("wlt-3", "owner-3", Currency.USD)
>>> usd.deposit(Money(1000, Currency.USD))
>>> spec = eligible_for_withdrawal(Currency.EUR)
>>> spec(funded), spec(empty), spec(usd)
(True, False, False)
>>> [w.id for w in filter(spec, [empty, funded, usd])]
['wlt-2']
```

Solo el monedero EUR con fondos satisface ambas mitades del compuesto, así que `filter` conserva únicamente ese.

**Qué acaba de pasar.** Expresaste una regla de negocio de solo lectura como un objeto con nombre y reutilizable en lugar de un `if` en línea. Se compone con otras reglas en tiempo de ejecución, pasa directamente a `filter` y es trivial de probar unitariamente de forma aislada; y, crucialmente, vive *fuera* del agregado, que es donde corresponden las comprobaciones de elegibilidad.

La disciplina de diseño clave: una especificación es un *predicado*, no una *defensa*. Devuelve `True` o `False` y nunca lanza. Las invariantes del agregado (descubierto, divisa que no coincide) corresponden a dentro de `deposit` y `withdraw` porque deben *impedir* un cambio de estado. Las especificaciones corresponden a los servicios y a los manejadores de consulta porque *seleccionan* o *clasifican*: nunca mutan.

Las especificaciones son especialmente útiles allí donde las reglas se combinan dinámicamente: una búsqueda de administración que añade filtros según el rol del operador, o un trabajo por lotes que parte una lista en monederos elegibles y no elegibles. Cada clase tiene exactamente un método y ningún efecto secundario, lo que hace triviales las pruebas unitarias aisladas.

!!! tip "Specification.of para lambdas rápidas"
    Para predicados puntuales que no necesitan una clase, usa el método factoría: `spec = Specification.of(lambda w: w.balance.amount >= 1000, name="minimum-balance")`. Se compone con `&`, `|` y `~` de la misma forma que una subclase completa.

---

## Lo que construiste {.recap}

El monedero de Lumen es ahora un modelo de dominio de primera clase.

`Money` es un `ValueObject` congelado que almacena importes como unidades menores enteras, impone la homogeneidad de divisa mediante `_assert_same_currency` y se reemplaza en lugar de mutarse. `__post_init__` rechaza los `float` de inmediato. `is_positive` e `is_negative` son propiedades; `major_units` y `__str__` se encargan de la visualización.

`Wallet(AggregateRoot[str])` es la frontera de coherencia. Su factoría `open` y los métodos de comportamiento `deposit`/`withdraw` hacen cumplir las tres invariantes —sin descubierto, sin operaciones entre divisas, sin importes no positivos— lanzando `BusinessRuleViolation` con un identificador de regla estable. Cada cambio de estado encola un evento de dominio (`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`); el saldo posterior a la operación se registra en cada evento, de modo que los suscriptores no necesitan ninguna llamada de vuelta. Tras un guardado exitoso, el servicio de aplicación vacía los eventos con `clear_events()` y los entrega a `EventPublisher`.

La capa de persistencia solo ve `WalletEntity` (cinco columnas, sin lógica de dominio). `to_aggregate` en `wallet_mapper` rehidrata la fila en un `Wallet`, y `to_entity` la aplana de vuelta; el `WalletRepository(Repository[WalletEntity, str])` del framework gestiona todo el SQL sin que el agregado importe jamás SQLAlchemy. `Specification[Wallet]` te da un predicado componible e invocable para comprobaciones de elegibilidad que viven fuera de la frontera del agregado.

El controlador queda intacto. El servicio encogió. Las reglas las hace cumplir el objeto que las posee.

---

## Pruébalo tú mismo {.exercises}

1. **Añade un método `transfer_to` y un objeto de valor `DailyLimit`.** Añade un `DailyLimit(ValueObject)` con `max_amount: int` y `currency: Currency`, decorado con `@dataclass(frozen=True)`. Luego añade `Wallet.transfer_to(target: Wallet, amount: Money) -> None`. El método debería llamar a `self.withdraw(amount)` y `target.deposit(amount)` en secuencia, lanzando `BusinessRuleViolation("transfer-currency-mismatch", ...)` si los dos monederos contienen divisas distintas. Como `Wallet` usa `__slots__`, añade `"_frozen"` a la tupla si también haces el ejercicio 2. Verifica que ambos monederos acumulan cada uno un evento `FundsWithdrawn` / `FundsDeposited`, respectivamente, y que una transferencia entre monederos que no coinciden se lanza antes de modificar ninguno de los dos saldos.

2. **Añade un evento `WalletFrozen` y un comportamiento `freeze()`.** Define `WalletFrozen(DomainEvent)` con `wallet_id: str = ""` y `reason: str = ""`. Añade `"frozen"` a `__slots__` y un atributo `frozen: bool` (por defecto `False`) a `Wallet.__init__`. Añade un método `freeze(reason: str) -> None` que ponga `self.frozen = True` y llame a `self.raise_event(WalletFrozen(...))`. Protege `deposit` y `withdraw` con `if self.frozen: raise BusinessRuleViolation("wallet-frozen", ...)` al principio de cada método. Luego escribe un escuchador de eventos usando `@event_listener` de `pyfly.eda` que registre una advertencia estructurada cada vez que se publique un evento `WalletFrozen`.

3. **Expresa una regla de negocio como una Especificación.** Escribe un `MinimumBalance(Specification[Wallet])` que compruebe si el saldo de un monedero está en o por encima de un importe umbral (en unidades menores) pasado a su `__init__`. Combínalo con `IsInCurrency` del Listado 6.9 usando `&` para producir una función factoría `premium_eligible(currency: Currency, threshold: int)`. Llama a `list(filter(premium_eligible(Currency.EUR, 50000), wallets))` sobre una lista de monederos de prueba y afirma que solo aparecen en el resultado los monederos con al menos 500,00 EUR (50 000 céntimos).
