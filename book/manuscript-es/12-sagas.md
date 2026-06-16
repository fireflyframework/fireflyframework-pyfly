<span class="eyebrow">Capítulo 12</span>

# Transacciones distribuidas: sagas, flujos de trabajo y TCC {.chtitle}

::: figure art/openers/ch12.svg | &nbsp;

El Capítulo 10 envió los eventos del monedero de Lumen a través de las
fronteras de proceso mediante Kafka. El Capítulo 11 dividió la aplicación
en servicios que cooperan entre sí y mostró cómo invocarlos por HTTP.
Ambos pasos desbloquearon escala y autonomía de los equipos, pero también
expusieron una nueva clase de peligro: varios agregados —o varios
servicios— pueden necesitar cambiar su estado como parte de una sola
operación de negocio, sin ninguna transacción ACID distribuida que te
proteja.

Imagina una transferencia entre monederos de Lumen. Cargas el monedero
origen y luego abonas el destino. Si se carga el origen y el abono falla
—moneda incorrecta, monedero inexistente— el dueño del origen pierde dinero
sin que se haya depositado nada en el otro lado. No puedes envolver dos
llamadas independientes al repositorio en un único `BEGIN … COMMIT` cuando
cada agregado posee su propia frontera de consistencia, y el commit de dos
fases entre agregados independientes es operativamente frágil.

La respuesta es la **consistencia eventual con compensación explícita**.
Cada paso confirma su propio almacén de forma independiente, y diseñas una
ruta de recuperación —una **transacción de compensación**— para cada paso
que pudiera tener éxito antes de que otro posterior falle. Cuando toda la
secuencia tiene éxito obtienes tu resultado de negocio; cuando cualquier
paso falla, el motor recorre hacia atrás los pasos completados, invocando
cada compensación para restaurar un estado consistente. Este capítulo te
muestra cómo construirlo con el módulo `pyfly.transactional` de PyFly.

Modelarás la transferencia de dinero como una **saga orquestada**: una
clase central que declara cada paso y su compensación, organizada en un DAG
(grafo acíclico dirigido) para que el motor pueda ejecutar en paralelo los
pasos independientes. Después explorarás la compensación en profundidad, el
patrón **Workflow** (flujo de trabajo) para flujos de larga duración o con
intervención humana, y **TCC (Try-Confirm-Cancel)** como alternativa basada
en reservas. Una sección final muestra cómo la persistencia conectable
permite al motor sobrevivir a una caída de proceso y reanudar
automáticamente las ejecuciones obsoletas.

---

## El problema de las escrituras distribuidas

Antes de escribir nada de código, concretemos los modos de fallo.

### Dos agregados, sin red de seguridad

La transferencia entre monederos de Lumen opera sobre dos agregados
`Wallet` almacenados en el mismo esquema de PostgreSQL pero tratados como
objetos de dominio independientes: cada uno se carga, se muta y se guarda en
su propio viaje de ida y vuelta. Los dos pasos son:

1. **Cargar el origen** — retira `amount` del `Wallet` origen (impone `balance >= 0`).
2. **Abonar el destino** — deposita `amount` en el `Wallet` destino (impone coincidencia de moneda).

En un monolito, ambas escrituras podrían compartir una sola transacción de
base de datos. En el servicio de dominio de Lumen cada paso es una llamada
independiente al repositorio. Una discrepancia de moneda en el destino, o un
ID de monedero inexistente, hace que el paso 2 falle después de que el paso
1 ya haya confirmado, dejando el monedero origen cargado y el destino sin
cambios. El usuario pierde dinero.

Reintentar toda la operación no es seguro: podrías cargar el origen dos
veces. Saltarse silenciosamente el paso fallido deja los saldos
inconsistentes. Necesitas un patrón con principios que confirme cada paso de
forma independiente y deshaga de forma consistente cada paso completado
cuando hay un fallo.

### Consistencia eventual y compensación

Una **saga** descompone la operación en una secuencia de transacciones
locales, cada una confirmando su propio almacén de forma independiente.
Cuando un paso falla, el motor ejecuta **transacciones de compensación** en
orden inverso para cada paso completado. Las compensaciones no son rollbacks
de base de datos; son *deshacer semánticos*: nuevas operaciones hacia
delante que revierten el efecto. "Reabonar el monedero origen" es una nueva
operación de depósito que restaura el saldo original, no un rollback.

!!! note "Las sagas son eventualmente consistentes"
    Una saga no te ofrece serializabilidad ni aislamiento. Entre el momento en que se carga el monedero origen y el momento en que se abona el monedero destino, otra petición podría leer el monedero origen y ver un saldo inferior al que finalmente tendrá. Este es el compromiso que aceptas cuando eliges operar sobre agregados independientes sin un bloqueo distribuido. Las sagas te dan *consistencia al final* —o todos los pasos hacia delante confirmaron o todos quedaron compensados— no *consistencia en cada punto*.

---

## Una saga orquestada

El módulo `pyfly.transactional` de PyFly proporciona los decoradores
`@saga` y `@saga_step`. Declaras una clase por saga, anotas cada método
como un paso con su compensación, y declaras el orden de dependencias. El
motor descubre la clase a través del contenedor de inyección de
dependencias, construye un DAG validado en el arranque y dirige la ejecución
de forma asíncrona.

!!! note "Término nuevo: orquestación"
    *Orquestación* significa que un componente central —aquí, el `SagaEngine` de PyFly— decide el orden en que se ejecutan los pasos y qué hacer cuando uno falla. La alternativa, la *coreografía*, hace que cada servicio reaccione a eventos sin un director central. Este capítulo usa orquestación porque hace que la ruta de recuperación sea explícita y fácil de probar: el motor posee las reglas, tu clase de saga solo declara los pasos.

Construiremos la saga de transferencia en cuatro movimientos: encender el
motor, declarar la clase de saga, observar el DAG que el motor construye a
partir de ella y, por último, invocar el motor desde un servicio. Vamos uno
a uno.

### Activar el motor

El motor transaccional se activa mediante el decorador de arranque
(*starter*) `@enable_domain_stack` sobre tu clase de aplicación, y ese único
decorador es todo lo que necesitas. No hace falta YAML adicional. En Lumen:

**Añade el decorador de arranque.** Abre tu clase de aplicación y apila
`@enable_domain_stack` por encima de `@pyfly_application`. Un decorador de
*arranque* (*starter*) es la forma que tiene PyFly de encender un área de
funcionalidad completa (aquí, el motor transaccional y su cableado de
inyección de dependencias) sin que tengas que registrar cada componente a
mano; el equivalente en Spring es una anotación `@EnableXxx`.

::: listing lumen/app.py | Listado 12.1 — Activar el motor transaccional mediante el domain stack
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.transfers",
        # ... other packages
    ],
)
class LumenApplication:
    pass
:::

**Apagarlo, o encenderlo bajo un starter más reducido.**
`@enable_domain_stack` ya establece `pyfly.transactional.enabled: true` por
ti, así que el motor está activo en cuanto el decorador está sobre la clase.
Esa misma propiedad es la palanca a la que recurres en dos situaciones:

- **Para apagar el motor** bajo el domain stack, ponla en `false` en
  `application.yaml`: la autoconfiguración se condiciona a que el valor sea
  exactamente `"true"`, así que cualquier otra cosa lo deshabilita.
- **Para encenderlo bajo un starter más reducido** como `@enable_core_stack`
  (que *no* incluye el motor transaccional), añade tú mismo la propiedad:

```yaml
pyfly:
  transactional:
    enabled: true
```

!!! note "Pruébalo: confirma que el motor quedó cableado"
    Arranca la aplicación en su puerto por defecto (`pyfly.server.port` es `8080` en la v26.6.110) y observa el log de arranque:

    ```bash
    uv run pyfly run
    ```

    Entre las líneas de arranque deberías ver registrarse los componentes transaccionales, por ejemplo:

    ```
    INFO  pyfly.starters.domain  domain stack enabled: transactional engine active
    INFO  pyfly.transactional    registered saga 'money-transfer' (2 steps)
    INFO  pyfly.server           Uvicorn running on http://0.0.0.0:8080
    ```

    Si pones explícitamente `transactional.enabled: false` (o habilitas solo el core stack sin añadir la propiedad), la línea de la saga nunca aparece y `SagaEngine.execute(...)` lanzará más adelante `ValueError: Saga 'money-transfer' is not registered`. Ver la línea `registered saga` es tu prueba de que el cableado funcionó.

**Cómo funciona:** `@enable_domain_stack` fusiona
`DOMAIN_STACK_PROPERTIES` en la configuración activa, y ese diccionario ya
contiene `pyfly.transactional.enabled: "true"`, así que el decorador
*registra y* activa el motor en un solo movimiento. La autoconfiguración,
`TransactionalEngineAutoConfiguration`, está protegida por
`@conditional_on_property("pyfly.transactional.enabled", having_value="true")`;
como el starter puso el valor en `"true"`, la condición coincide y la
autoconfiguración cablea cada componente del motor —`SagaEngine`,
`TccEngine`, `WorkflowEngine`, `SagaRegistry`,
`InMemoryPersistenceAdapter` y `LoggerEventsAdapter`— en el contenedor de
inyección de dependencias. El `OrchestrationBeanPostProcessor` escanea
entonces cada bean producido en el arranque: cualquier bean que lleve
metadatos `__pyfly_saga__` se registra automáticamente en `SagaRegistry`.
Nunca llamas a `registry.register_from_bean()` en código de producción.

!!! note "Qué acaba de pasar"
    Un pequeño cambio —un único decorador— te dio un motor de sagas completamente cableado. `@enable_domain_stack` declaró los componentes y los encendió (establece `pyfly.transactional.enabled: true` por ti), y un post-procesador de beans de arranque encontró tus clases de saga y las registró por ti. A partir de aquí solo escribes clases de saga y llamas a `SagaEngine.execute(...)`; la fontanería está hecha.

### Declarar la saga de transferencia

La transferencia entre monederos de Lumen es una saga de dos pasos: cargar
el monedero origen y luego abonar el destino. Si el abono falla —moneda
incorrecta o monedero inexistente— el motor compensa reabonando el origen,
devolviendo ambos saldos a sus valores originales.

!!! note "Término nuevo: compensación"
    Una *compensación* (o *transacción de compensación*) es el deshacer de un paso. No es un rollback de base de datos: para cuando compensas, la escritura original ya ha confirmado en su propio almacén. En cambio es una *nueva operación hacia delante* que revierte semánticamente el efecto. El deshacer de "cargar el origen" no es `ROLLBACK`; es "depositar la misma cantidad de vuelta en el origen". Cada paso que cambia el estado necesita una compensación que le corresponda.

Constrúyela en tres movimientos. **Paso 1**: declara la clase y apila los
decoradores. **Paso 2**: escribe los pasos hacia delante y su compensación.
**Paso 3**: cablea los parámetros con marcadores de inyección. El archivo
completo está abajo; luego recorremos cada movimiento.

::: listing lumen/core/services/transfers/money_transfer_saga.py | Listado 12.2 — MoneyTransferSaga: cargo → abono, con compensación
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.transfers.transfer_request import TransferRequest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.domain import AggregateNotFound
from pyfly.transactional.saga.annotations import (
    FromStep,
    Input,
    saga,
    saga_step,
)
from pyfly.transactional.saga.core.context import SagaContext

MONEY_TRANSFER_SAGA = "money-transfer"


@dataclass(frozen=True)
class DebitResult:
    wallet_id: str
    amount: int
    currency: Currency
    balance: int


@saga(name=MONEY_TRANSFER_SAGA)
@service
class MoneyTransferSaga:
    """Debit source wallet, credit destination; compensate on failure."""

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    # -- Step 1: debit the source ----------------------------------------

    @saga_step(id="debit-source", compensate="recredit_source")
    async def debit_source(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> DebitResult:
        entity = await self._repository.find_by_id(
            request.source_wallet_id
        )
        if entity is None:
            raise AggregateNotFound("Wallet", request.source_wallet_id)
        wallet = to_aggregate(entity)
        wallet.withdraw(
            Money(amount=request.amount, currency=request.currency)
        )
        await self._repository.upsert(to_entity(wallet))
        wallet.clear_events()
        return DebitResult(
            wallet_id=request.source_wallet_id,
            amount=request.amount,
            currency=request.currency,
            balance=wallet.balance.amount,
        )

    async def recredit_source(
        self,
        debit: Annotated[DebitResult, FromStep("debit-source")],
    ) -> int:
        """Compensation: put the money back. Receives the forward step's
        result via FromStep — NOT the saga input."""
        entity = await self._repository.find_by_id(debit.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", debit.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=debit.amount, currency=debit.currency)
        )
        await self._repository.upsert(to_entity(wallet))
        wallet.clear_events()
        return wallet.balance.amount

    # -- Step 2: credit the destination ----------------------------------

    @saga_step(id="credit-destination", depends_on=["debit-source"])
    async def credit_destination(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> int:
        entity = await self._repository.find_by_id(
            request.destination_wallet_id
        )
        if entity is None:
            raise AggregateNotFound(
                "Wallet", request.destination_wallet_id
            )
        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=request.amount, currency=request.currency)
        )
        await self._repository.upsert(to_entity(wallet))
        wallet.clear_events()
        return wallet.balance.amount
:::

**Cómo funciona, paso a paso:**

**Paso 1: la pila de decoradores.**
`@saga(name=MONEY_TRANSFER_SAGA)` estampa `__pyfly_saga__` en la clase con
el nombre de la saga. El decorador solo adjunta metadatos: no envuelve la
clase ni crea un proxy. **El requisito crítico** es que `@saga` debe
apilarse *por encima de* `@service`. La anotación `@service` hace que el
contenedor de inyección de dependencias instancie y escanee el bean en el
arranque; el gancho `OrchestrationBeanPostProcessor.after_init()` ve
entonces `__pyfly_saga__` en el bean y llama a
`SagaRegistry.register_from_bean()`. Sin `@service`, la clase nunca se
escanea y la saga no puede ejecutarse por nombre.

!!! warning "El orden de los decoradores no es opcional"
    Lee la pila de arriba abajo: `@saga` está *por encima* de `@service`. Intercámbialos —`@service` por encima de `@saga`— y el bean sigue registrándose con la inyección de dependencias, pero los metadatos de la saga se aplican al objeto ya envuelto, el post-procesador nunca los encuentra y `execute("money-transfer")` falla con `ValueError: Saga 'money-transfer' is not registered`. Si te topas con ese error, comprueba primero el orden de los decoradores.

**Paso 2: los métodos de paso.**
`@saga_step` adjunta los metadatos `__pyfly_saga_step__` directamente al
método asíncrono —sin envoltura, sin proxy— así que
`inspect.iscoroutinefunction` sigue devolviendo `True` y el motor hace
correctamente `await` de la llamada. El parámetro
`compensate="recredit_source"` nombra el *método de la misma clase* que se
invoca al deshacer este paso. Omitir `depends_on` (o pasar `[]`) significa
que el paso puede ejecutarse en cuanto el motor arranca.

**Interacción con el repositorio: el ciclo cargar-mutar-guardar.** Cada
paso sigue el mismo patrón de tres fases, usando el repositorio del
framework `WalletRepository(Repository[WalletEntity, str])`:

1. `find_by_id(id)` — carga la fila cruda `WalletEntity` desde la base de datos.
2. `to_aggregate(entity)` — rehidrata la rica raíz de agregado `Wallet` a
   partir de esa fila; el agregado impone todas las invariantes
   (`balance >= 0`, coincidencia de moneda).
3. Mutar — llama a `wallet.withdraw(...)` o `wallet.deposit(...)` sobre el
   agregado, dejando que lance `BusinessRuleViolation` si se rompe una
   invariante antes de que ocurra ninguna escritura.
4. `upsert(to_entity(wallet))` — aplana el agregado mutado de vuelta a un
   `WalletEntity` y llama a `session.merge` + `flush`, de modo que la
   escritura es visible para los pasos posteriores en la misma
   `AsyncSession` sin confirmar.

Como los pasos de la saga comparten una `AsyncSession`, `upsert` hace flush
para que cada paso vea la escritura del anterior; la frontera de aplicación
que los rodea posee el commit final.

**Paso 3: cablea los parámetros.**
La inyección de parámetros usa `typing.Annotated` con **instancias de
marcador**, no clases desnudas:

- `Annotated[TransferRequest, Input()]` — `Input()` es una instancia (fíjate
  en los paréntesis); `Input` desnudo sin `()` no se resuelve.
- `Annotated[DebitResult, FromStep("debit-source")]` — lee el resultado que
  el paso `"debit-source"` almacenó en `SagaContext` al completarse.
- `ctx: SagaContext` — inyectado por tipo; no hace falta marcador
  `Annotated`.

El resolutor inspecciona las anotaciones de tipo en tiempo de ejecución
mediante `typing.get_type_hints(func, include_extras=True)`.

**Los métodos de compensación no reciben la entrada de la saga.**
`recredit_source` toma `Annotated[DebitResult, FromStep("debit-source")]`
—el valor que devolvió el paso hacia delante— no la `TransferRequest`.
Recarga la entidad mediante `find_by_id`, rehidrata el agregado, deposita de
vuelta la cantidad original y hace upsert: el mismo ciclo
cargar-mutar-guardar que los pasos hacia delante. Las compensaciones siempre
leen de `ctx.step_results` mediante `FromStep`, nunca de la entrada
original.

!!! note "Qué acaba de pasar"
    Escribiste una sola clase que contiene toda la historia de la transferencia: un paso hacia delante para cargar, su compensación para reabonar y un segundo paso hacia delante para abonar. Los decoradores le dijeron al motor *qué es cada método* (un paso, una compensación) y *cómo se conectan* (`compensate=`, `depends_on=`). No escribiste ningún bucle de orquestación ni lógica de rollback con try/except: eso es trabajo del motor. Tu código solo describe la operación de negocio y su deshacer.

### El DAG de pasos

!!! note "Término nuevo: DAG"
    Un *DAG* —grafo acíclico dirigido— es un conjunto de pasos conectados por flechas de "debe-ejecutarse-antes", sin ciclos (ningún paso puede, directa o indirectamente, depender de sí mismo). El motor lee tus declaraciones `depends_on`, construye este grafo y lo ordena en *capas*: todo lo que está en la capa 0 no tiene dependencias sin satisfacer y se ejecuta primero; la capa 1 se ejecuta cuando termina la capa 0; y así sucesivamente. Los pasos de la misma capa son independientes, así que el motor los ejecuta a la vez. Un ciclo haría imposible la división en capas, por lo que el motor lo rechaza en el arranque y no en tiempo de ejecución.

Los dos pasos forman una cadena lineal:

::: figure art/figures/12-saga.svg | Figura 12.1 — DAG de MoneyTransferSaga: los pasos se ejecutan en orden de capa topológica; los pasos independientes de una capa se ejecutan con asyncio.gather.

```
Layer 0:  debit-source
              │
Layer 1:  credit-destination
```

Como `credit-destination` depende de `debit-source`, se ejecutan
secuencialmente. Una saga más compleja —una comprobación de fraude y una
comprobación de KYC que son independientes entre sí pero ambas alimentan un
paso de captura— colocaría las dos comprobaciones en la misma capa y las
ejecutaría de forma concurrente con `asyncio.gather`.

### Ejecutar la saga

La clase de saga solo *describe* la operación. Para *ejecutarla* necesitas
un servicio ligero que inyecte el motor y lo invoque por nombre.

**Paso 1: inyecta `SagaEngine`.** Declara un `@service` cuyo constructor
tome un parámetro `SagaEngine`; el contenedor de inyección de dependencias
te entrega el motor autoconfigurado. **Paso 2: llama a `execute`** con el
nombre de la saga y la carga de entrada. **Paso 3: pliega el `SagaResult`**
en un diccionario pequeño y amigable con JSON para el invocador.

::: listing lumen/core/services/transfers/transfer_service.py | Listado 12.3 — Ejecutar la saga de transferencia de dinero
from __future__ import annotations

from typing import Any

from lumen.core.services.transfers.money_transfer_saga import MONEY_TRANSFER_SAGA
from lumen.core.services.transfers.transfer_request import TransferRequest
from pyfly.container import service
from pyfly.transactional.saga.core.result import SagaResult
from pyfly.transactional.saga.engine.saga_engine import SagaEngine


@service
class TransferService:
    """Run the money-transfer saga and report the outcome."""

    def __init__(self, saga_engine: SagaEngine) -> None:
        self._saga_engine = saga_engine

    async def transfer(self, request: TransferRequest) -> dict[str, Any]:
        result: SagaResult = await self._saga_engine.execute(
            saga_name=MONEY_TRANSFER_SAGA,
            input_data=request,
        )

        if result.success:
            debit = result.result_of("debit-source")
            return {
                "status": "completed",
                "correlation_id": result.correlation_id,
                "source_balance": debit.balance,
                "destination_balance": result.result_of("credit-destination"),
            }

        return {
            "status": "failed",
            "correlation_id": result.correlation_id,
            "failed_steps": list(result.failed_steps().keys()),
            "compensated_steps": list(result.compensated_steps().keys()),
            "error": str(result.error),
        }
:::

**Cómo funciona:** `saga_engine.execute()` resuelve `MoneyTransferSaga`
desde el registro por nombre, crea un `SagaContext` con un `correlation_id`
UUID autogenerado y empieza a ejecutar capas. En caso de éxito,
`SagaResult.success` es `True` y `result_of("debit-source")` devuelve el
`DebitResult` que produjo el paso hacia delante. En caso de fallo,
`result.failed_steps()` devuelve un diccionario de ID de paso a
`StepOutcome` para cada paso que agotó sus reintentos;
`result.compensated_steps()` devuelve los pasos que se deshicieron
correctamente.

`SagaResult` es una dataclass inmutable y congelada. Sus miembros clave:

- `result.success` — `True` cuando cada paso hacia delante se completó.
- `result.result_of(step_id)` — el valor que devolvió ese paso, o `None`.
- `result.failed_steps()` — diccionario de ID de paso → `StepOutcome` para los pasos fallidos.
- `result.compensated_steps()` — diccionario de ID de paso → `StepOutcome` para los pasos compensados.
- `result.correlation_id` — UUID para correlacionar logs y trazas entre servicios.
- `result.error` — la excepción que detuvo la saga, o `None` en caso de éxito.

!!! note "Pruébalo: el camino feliz y el camino compensado"
    Expón `TransferService.transfer` detrás de una ruta HTTP (el Capítulo 11 cubrió los controladores) y ejercita ambos desenlaces contra la aplicación en ejecución en `pyfly.server.port` (`8080`).

    Una transferencia válida entre dos monederos existentes de la misma moneda devuelve el resumen completado:

    ```bash
    curl -s -X POST http://localhost:8080/transfers \
      -d '{"source_wallet_id":"w-1","destination_wallet_id":"w-2","amount":500,"currency":"EUR"}'
    ```

    ```json
    {
      "status": "completed",
      "correlation_id": "8f3c…",
      "source_balance": 9500,
      "destination_balance": 10500
    }
    ```

    Ahora apunta la transferencia a un monedero destino que no existe. `credit-destination` lanza `AggregateNotFound`, el motor compensa `debit-source` y la respuesta informa exactamente de eso:

    ```bash
    curl -s -X POST http://localhost:8080/transfers \
      -d '{"source_wallet_id":"w-1","destination_wallet_id":"does-not-exist","amount":500,"currency":"EUR"}'
    ```

    ```json
    {
      "status": "failed",
      "correlation_id": "1a2b…",
      "failed_steps": ["credit-destination"],
      "compensated_steps": ["debit-source"],
      "error": "Wallet 'does-not-exist' not found"
    }
    ```

    La observación clave: vuelve a leer `w-1` después y su saldo está de vuelta en `9500`; `debit-source` fue deshecho por `recredit_source`. Una transferencia fallida deja ambos monederos exactamente como empezaron.

!!! spring "Equivalencia con Spring"
    `@saga` / `@saga_step` reflejan `@Saga` / `@SagaStep` en la librería Java `fireflyframework-transactional-engine`. La regla de la pila de decoradores (`@saga` sobre `@service`) refleja la regla de Java de que `@Saga` debe estar sobre una clase anotada con `@Service` para que el `WorkflowBeanPostProcessor` pueda descubrirla. Los marcadores de inyección de parámetros (`Input()`, `FromStep("id")`) se corresponden directamente con `@Input` y `@FromStep` en la versión Java. El modelo asíncrono difiere: Java usa Project Reactor (`Mono<T>`) mientras que PyFly usa `async/await` nativo con `asyncio.gather` para las capas paralelas.

---

## La compensación en profundidad

El camino feliz es directo: cada paso tiene éxito y la saga confirma. El
verdadero reto de diseño es el camino infeliz. Entender qué ocurre en caso
de fallo —y por qué la compensación debe diseñarse con cuidado— es lo que
separa una saga fiable de una frágil.

### Qué se ejecuta en caso de fallo

Cuando un paso falla tras todos los reintentos, el motor entra en *modo de
compensación*. Inspecciona `SagaContext` en busca de cada paso cuyo estado
sea `DONE` y luego llama a sus métodos de compensación en orden inverso al
de finalización bajo la política por defecto `STRICT_SEQUENTIAL`. En
`MoneyTransferSaga`, un monedero destino inexistente hace que
`credit-destination` lance `AggregateNotFound`. El motor compensa entonces
el paso que ya se había completado:

```
Forward path:  debit-source ✓  →  credit-destination ✗
Compensation:  recredit_source (for debit-source)
```

El efecto neto: el monedero origen se restaura a su saldo original y el
monedero destino nunca fue tocado, como si la transferencia nunca hubiera
ocurrido.

Los métodos de compensación reciben sus argumentos a través del mismo
sistema de inyección que los pasos hacia delante.
`Annotated[DebitResult, FromStep("debit-source")]` lee el `DebitResult` que
`debit_source` almacenó en el contexto al completarse, de modo que siempre
compensas con los datos realmente confirmados, nunca con una aproximación.

!!! note "Pruébalo: demuestra la compensación en una prueba"
    No necesitas un servidor en ejecución para verificar el camino infeliz: una prueba unitaria rápida contra el motor basta, y es el tipo de prueba que escribirás para cada saga. Dirige `TransferService.transfer` con un monedero destino que no existe y luego comprueba que la compensación se ejecutó:

    ```python
    async def test_failed_transfer_compensates_the_debit(transfer_service):
        result = await transfer_service.transfer(
            TransferRequest(
                source_wallet_id="w-1",
                destination_wallet_id="does-not-exist",
                amount=500,
                currency=Currency.EUR,
            )
        )
        assert result["status"] == "failed"
        assert result["failed_steps"] == ["credit-destination"]
        assert result["compensated_steps"] == ["debit-source"]
    ```

    Ejecuta solo esta prueba (el grupo `--extra dev` instala pytest; el Capítulo 16 cubre los fixtures en profundidad):

    ```bash
    uv run --extra dev pytest -q -k compensates
    ```

    Salida esperada:

    ```
    1 passed in 0.05s
    ```

    Una prueba en verde aquí es tu garantía de que una transferencia rota nunca deja dinero desaparecido.

### Políticas de compensación

Cinco políticas gobiernan cómo ejecuta el motor las compensaciones.
Establece el valor por defecto global en YAML o anúlalo por ejecución:

```yaml
pyfly:
  transactional:
    saga:
      compensation_policy: STRICT_SEQUENTIAL
```

| Política | Comportamiento | Úsala cuando |
|--------|-----------|----------|
| `STRICT_SEQUENTIAL` | Orden inverso, una a una. Se detiene al primer error de compensación. | El orden importa; un rollback parcial es inaceptable. |
| `GROUPED_PARALLEL` | Invierte las capas topológicas; compensa cada capa en paralelo. | Quieres velocidad sin violar la estructura de dependencias. |
| `RETRY_WITH_BACKOFF` | Orden inverso con backoff exponencial. Continúa si los reintentos tienen éxito. | Es probable que haya fallos de red transitorios durante la compensación. |
| `CIRCUIT_BREAKER` | Rastrea fallos consecutivos; se abre tras 3 y omite el resto. | Evitar fallos en cascada; la recuperación manual gestiona los pasos omitidos. |
| `BEST_EFFORT_PARALLEL` | Todas las compensaciones a la vez; los errores se registran, nunca se lanzan. | La velocidad es crítica; una reconciliación aparte gestiona los fallos parciales. |

!!! warning "La compensación debe ser idempotente"
    El motor puede llamar a un método de compensación más de una vez. Si el motor cae entre la llamada a `void_payment` y la persistencia del resultado de la compensación, llamará a `void_payment` de nuevo al reiniciarse. Tus métodos de compensación deben ser seguros de llamar varias veces con los mismos argumentos. Para anulaciones de pagos, esto significa que el `PaymentsService` debe tratar una doble anulación como un no-op (devolver éxito si ya estaba anulado, no lanzar). Diseña la compensación *antes* de diseñar el paso hacia delante: la idempotencia no es una ocurrencia tardía.

### Configuración de compensación por paso

Puedes anular el número de reintentos y el tiempo de espera de un paso de
compensación sin cambiar el comportamiento del paso hacia delante:

::: listing lumen/transfer/transfer_saga_hardened.py | Listado 12.4 — Reintento y tiempo de espera de compensación por paso
from pyfly.container import service
from pyfly.transactional.saga.annotations import saga, saga_step


@saga(name="money-transfer-hardened")
@service
class HardenedTransferSaga:

    @saga_step(
        id="debit-wallet",
        compensate="refund_wallet",
        depends_on=[],
        retry=3,
        backoff_ms=200,
        timeout_ms=5_000,
        compensation_retry=5,
        compensation_backoff_ms=1_000,
        compensation_timeout_ms=8_000,
        compensation_critical=True,
    )
    async def debit_wallet(self, *args: object) -> None: ...

    async def refund_wallet(self, *args: object) -> None: ...
:::

**Cómo funciona:** `compensation_retry=5` da a la compensación cinco
intentos propios, independientes de los tres reintentos del paso hacia
delante. `compensation_critical=True` significa que si la compensación agota
todos sus reintentos y aun así falla, el motor lanza esa excepción,
sacando a la superficie el *fallo de compensación* como un error observable
en lugar de tragárselo silenciosamente.

### Pasos de compensación externos

Cuando la lógica de compensación es lo bastante compleja como para merecer
su propia clase, o cuando vive en un módulo diferente, sácala por completo:

::: listing lumen/transfer/compensation_steps.py | Listado 12.5 — Clase de paso de compensación externo
from typing import Annotated

from pyfly.container import service
from pyfly.transactional.saga.annotations import (
    FromStep,
    compensation_step,
)

from lumen.core.services.transfers.money_transfer_saga import DebitResult
from lumen.models.repositories.wallet_repository import WalletRepository


@compensation_step(saga="money-transfer", for_step_id="debit-source")
@service
class SourceRecreditCompensation:

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        debit: Annotated[DebitResult, FromStep("debit-source")],
    ) -> None:
        """External alternative to the inline recredit_source method."""
        ...
:::

El `SagaRegistry` descubre las clases `@compensation_step` en el arranque
junto a las clases `@saga` y las cablea en sus definiciones de paso
automáticamente. El parámetro `for_step_id` debe coincidir exactamente con
la cadena `id` del paso.

---

## Flujos de trabajo y señales

`@saga` es la herramienta adecuada cuando todos los pasos se conocen de
antemano y la operación se completa en minutos. Algunos procesos de negocio
son inherentemente más largos: la aprobación de un préstamo a la espera de
un responsable de cumplimiento, una incorporación de varios pasos bloqueada
por un clic en un correo, un pago que necesita un periodo de enfriamiento
antes de liquidarse. Estos encajan en el patrón **Workflow** (flujo de
trabajo).

### En qué se diferencian los flujos de trabajo de las sagas

| | Saga | Workflow |
|---|---|---|
| Duración | De segundos a minutos | De minutos a días |
| Espera | Solo reintentos | Señales, temporizadores, flujos hijos |
| Intervención humana | No | Sí (`@wait_for_signal`) |
| Persistencia de estado | Punto de control por saga | Tras cada capa |
| Despliegue en DAG | Sí (capas paralelas) | Sí + primitivas de compuerta |

### Declarar un flujo de trabajo

::: listing lumen/transfer/approval_workflow.py | Listado 12.6 — LargeTransferWorkflow: aprobación dirigida por señales para transferencias de alto valor
from __future__ import annotations

from pyfly.container import service
from pyfly.transactional.core.model import TriggerMode
from pyfly.transactional.workflow.annotations import (
    compensation_step,
    on_workflow_complete,
    on_workflow_error,
    wait_for_signal,
    workflow,
    workflow_query,
    workflow_step,
)


@workflow(
    id="large-transfer-approval",
    trigger_mode=TriggerMode.SYNC,
    timeout_ms=86_400_000,    # 24 hours
    max_retries=1,
)
@service
class LargeTransferWorkflow:
    """High-value transfers require a compliance officer to approve."""

    @workflow_step(id="enrich-request", depends_on=[])
    async def enrich_request(self, payload: dict) -> dict:
        return {**payload, "risk_score": 0.12}

    @workflow_step(
        id="compliance-review",
        depends_on=["enrich-request"],
        compensatable=True,
        compensation_method="release_review",
        timeout_ms=82_800_000,
    )
    @wait_for_signal("approved", timeout_ms=82_800_000)
    async def compliance_review(self) -> None:
        """Suspends until a compliance officer delivers the signal."""

    @compensation_step(for_step="compliance-review")
    async def release_review(self) -> None:
        """Called if the workflow is cancelled during review."""

    @workflow_step(
        id="settle-transfer",
        depends_on=["compliance-review"],
    )
    async def settle_transfer(self, payload: dict) -> dict:
        return {"settled": True}

    @workflow_query(name="status")
    async def get_status(self, ctx: object) -> str:
        return str(getattr(ctx, "status", "UNKNOWN"))

    @on_workflow_complete
    async def on_done(self, ctx: object) -> None:
        pass   # emit audit event

    @on_workflow_error
    async def on_error(self, ctx: object, err: Exception) -> None:
        pass   # alert on-call
:::

**Cómo funciona:** La pila de decoradores sigue la misma regla que las
sagas: `@workflow` por encima de `@service`. `@workflow(id=...)` toma
argumentos solo por palabra clave: `id` es obligatorio; todos los demás son
opcionales. `@wait_for_signal("approved", timeout_ms=82_800_000)` se apila
por encima de `@workflow_step` y le dice al motor que suspenda en ese paso
hasta que se entregue una señal llamada `"approved"`. El motor persiste el
`ExecutionContext` en el `ExecutionPersistenceProvider` configurado; si el
proceso se reinicia, rehidrata el contexto y reanuda desde la última capa
completada.

`@compensation_step(for_step="compliance-review")` usa el argumento por
palabra clave `for_step` (no posicional) y registra `release_review` como el
manejador (handler) de compensación para el paso `compliance-review`.

`@workflow_query(name="status")` marca un método como un manejador de
consulta del lado de lectura: invocable mientras el flujo de trabajo está
suspendido sin hacer avanzar la ejecución.

### Manejar el motor de flujos de trabajo

::: listing lumen/transfer/approval_controller.py | Listado 12.7 — Iniciar un flujo de trabajo y entregar una señal
from __future__ import annotations

from pyfly.container import service
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.result import WorkflowResult


@service
class TransferApprovalService:

    def __init__(self, workflow_engine: WorkflowEngine) -> None:
        self._wf = workflow_engine

    async def request_large_transfer(self, payload: dict) -> str:
        result: WorkflowResult = await self._wf.start(
            "large-transfer-approval",
            input=payload,
        )
        # Returns immediately; workflow is now suspended at compliance-review.
        return result.correlation_id

    async def approve(self, correlation_id: str, reviewer_id: str) -> None:
        await self._wf.deliver_signal(
            correlation_id,
            "approved",
            payload={"by": reviewer_id},
        )

    async def check_status(self, correlation_id: str) -> str:
        return await self._wf.query(correlation_id, "status")
:::

**Cómo funciona:** `workflow_engine.start(workflow_id, input=payload)`
ejecuta la primera capa (`enrich-request`) de forma síncrona, luego suspende
en `compliance-review` por culpa de `@wait_for_signal`. Devuelve un
`WorkflowResult` inmediatamente con un `correlation_id`: el invocador guarda
este ID y consulta más tarde. Cuando se llama a `deliver_signal()`, el flujo
de trabajo se reanuda y `settle-transfer` se ejecuta hasta completarse.

`WorkflowResult` lleva: `workflow_id`, `correlation_id`, `status` (un enum
`ExecutionStatus`), `duration_ms`, `step_results` (diccionario) y
`variables`. El booleano `result.successful` es `True` cuando `status` es
`COMPLETED` o `CONFIRMED`.

### El constructor programático

Cuando necesitas construir un flujo de trabajo de forma dinámica —a partir
de una configuración en base de datos o de un motor de reglas— usa
`WorkflowBuilder`:

::: listing lumen/transfer/dynamic_workflow.py | Listado 12.8 — Construir un flujo de trabajo de forma programática
from pyfly.transactional.workflow.builder import WorkflowBuilder
from pyfly.transactional.workflow.definition import WorkflowDefinition


async def enrich_fn(payload: dict) -> dict:
    return {**payload, "enriched": True}


async def settle_fn(payload: dict) -> dict:
    return {"settled": True}


definition: WorkflowDefinition = (
    WorkflowBuilder("simple-transfer")
    .step("enrich", enrich_fn, depends_on=[])
    .wait_signal(
        "await-approval",
        "approved",
        depends_on=["enrich"],
        timeout_ms=3_600_000,
    )
    .step(
        "settle",
        settle_fn,
        depends_on=["await-approval"],
    )
    .build()
)
:::

`WorkflowBuilder.step(step_id, handler, *, depends_on, timeout_ms, max_retries, ...)` acepta un invocable y argumentos por palabra clave para dependencias, tiempos de espera y reintentos. `wait_signal(step_id, signal, *, depends_on, timeout_ms)` inserta un paso de compuerta de señal sin un manejador real: crea una corrutina interna no-op que el motor sustituye por la lógica de espera de señal. `build()` devuelve un `WorkflowDefinition` que registras directamente con `WorkflowEngine`.

---

## TCC: Try-Confirm-Cancel

El patrón saga ejecuta los pasos hacia delante y compensa hacia atrás.
**TCC (Try-Confirm-Cancel)** adopta un enfoque diferente: todos los
participantes primero *reservan tentativamente* sus recursos sin confirmar
(Try), y luego todos *confirman* esas reservas (Confirm) o todos las
*liberan* (Cancel). Esto te da una semántica fuerte de todo-o-nada entre
participantes sin un bloqueo distribuido.

TCC encaja en escenarios donde cada participante puede mantener una reserva
de forma barata; por ejemplo, preautorizar una retención en una tarjeta de
pago en lugar de cobrarla inmediatamente.

### Las tres fases

1. **Try** — cada participante reserva recursos. Las reservas son visibles
   internamente pero no son definitivas. Si algún Try falla, los
   participantes que tuvieron éxito cancelan sus reservas.
2. **Confirm** — si todas las fases Try tienen éxito, el coordinador instruye
   a cada participante a confirmar su reserva.
3. **Cancel** — si alguna fase Try falla, el coordinador instruye a cada
   participante que completó su Try a liberar su reserva.

### Declarar una transacción TCC

::: listing lumen/transfer/transfer_tcc.py | Listado 12.9 — WalletTransferTcc: Try-Confirm-Cancel para la reserva de pago
from __future__ import annotations

from typing import Annotated

from pyfly.container import service
from pyfly.transactional.tcc.annotations import (
    FromTry,
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.core.context import TccContext

from lumen.wallet.service import WalletService
from lumen.payments.service import PaymentsService


@tcc(
    name="wallet-transfer",
    timeout_ms=30_000,
    retry_enabled=True,
    max_retries=3,
    backoff_ms=500,
)
@service
class WalletTransferTcc:
    """Reserve funds and payment in lockstep; confirm or cancel together."""

    @tcc_participant(id="wallet-hold", order=1, timeout_ms=5_000)
    class WalletParticipant:

        def __init__(self, wallet_svc: WalletService) -> None:
            self._wallet = wallet_svc

        @try_method(timeout_ms=4_000, retry=2, backoff_ms=200)
        async def try_hold(
            self,
            request: object,
            ctx: TccContext,
        ) -> str:
            """Tentatively hold funds — does not debit yet."""
            return await self._wallet.hold_funds(
                wallet_id=getattr(request, "sender_id", ""),
                amount=getattr(request, "amount_cents", 0),
            )   # returns a hold_id

        @confirm_method(timeout_ms=5_000, retry=3)
        async def confirm_hold(
            self,
            hold_id: Annotated[str, FromTry()],
            ctx: TccContext,
        ) -> None:
            await self._wallet.commit_hold(hold_id)

        @cancel_method(timeout_ms=3_000, retry=2)
        async def cancel_hold(
            self,
            hold_id: Annotated[str, FromTry()],
        ) -> None:
            await self._wallet.release_hold(hold_id)

    @tcc_participant(id="payment-auth", order=2, timeout_ms=8_000)
    class PaymentParticipant:

        def __init__(self, payments_svc: PaymentsService) -> None:
            self._payments = payments_svc

        @try_method(timeout_ms=6_000, retry=2, backoff_ms=300)
        async def try_auth(
            self,
            request: object,
            ctx: TccContext,
        ) -> str:
            return await self._payments.pre_authorise(
                amount=getattr(request, "amount_cents", 0),
            )   # returns auth_id

        @confirm_method(timeout_ms=8_000, retry=3)
        async def confirm_auth(
            self,
            auth_id: Annotated[str, FromTry()],
            ctx: TccContext,
        ) -> None:
            await self._payments.capture_auth(auth_id)

        @cancel_method(timeout_ms=4_000, retry=2)
        async def cancel_auth(
            self,
            auth_id: Annotated[str, FromTry()],
        ) -> None:
            await self._payments.void_auth(auth_id)
:::

**Cómo funciona:** `@tcc_participant(order=1)` le dice al motor TCC que
ejecute la fase Try de `WalletParticipant` antes que la de
`PaymentParticipant`: un `order` menor significa más temprano. **`FromTry()`**
es el equivalente de `FromStep` en TCC: inyecta el valor devuelto por el
`@try_method` del propio participante en su `@confirm_method` y su
`@cancel_method`.

El motor ejecuta las fases Try de todos los participantes en secuencia de
`order`. Si todos los Try tienen éxito, ejecuta todos los métodos Confirm.
Si algún Try falla, ejecuta Cancel para cada participante que completó su
Try, de nuevo en el orden declarado. Un participante `optional=True` que
falla su Try no dispara un Cancel global; su fallo se registra y se omite.

### Ejecutar una transacción TCC

::: listing lumen/transfer/tcc_service.py | Listado 12.10 — Ejecutar una transacción TCC
from __future__ import annotations

from typing import Any

from pyfly.container import service
from pyfly.transactional.tcc.core.result import TccResult
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine

from lumen.core.services.transfers.transfer_request import TransferRequest


@service
class TccTransferService:

    def __init__(self, tcc_engine: TccEngine) -> None:
        self._engine = tcc_engine

    async def transfer(self, req: TransferRequest) -> dict[str, Any]:
        result: TccResult = await self._engine.execute(
            tcc_name="wallet-transfer",
            input_data=req,
        )

        if result.success:
            hold_id = result.result_of("wallet-hold")
            return {
                "status": "confirmed",
                "hold_id": hold_id,
                "correlation_id": result.correlation_id,
            }

        failed = result.failed_participants()
        return {
            "status": "cancelled",
            "failed": list(failed.keys()),
            "error": str(result.error),
        }
:::

### TCC frente a saga: elegir el patrón adecuado

Usa esta tabla para elegir entre los dos enfoques:

| Pregunta | Saga | TCC |
|----------|------|-----|
| ¿Los pasos se ejecutan independientemente? | Sí — cada uno confirma localmente | No — todas las fases Try deben tener éxito primero |
| ¿Necesita lógica de compensación? | Sí, por paso | No — Cancel gestiona el rollback |
| ¿Se necesita reserva de recursos? | No | Sí — los participantes mantienen recursos durante Try |
| Mejor para | Operaciones secuenciales largas | Bloqueos cortos de todo-o-nada |

---

## Persistencia: sobrevivir a una caída

El motor almacena el estado de la saga y de TCC a través del protocolo
`TransactionalPersistencePort`. El adaptador por defecto mantiene el estado
en memoria —rápido para desarrollo, pero perdido al reiniciar el proceso—.
Los despliegues de producción cambian por un adaptador duradero.

### Cómo fluye el estado

Cada vez que un paso se completa —con éxito o no— el motor llama a:

1. `persistence_port.update_step_status(correlation_id, step_id, status)` — registra el desenlace del paso.
2. `persistence_port.mark_completed(correlation_id, successful)` — registra el resultado final de la saga.

En el arranque, `SagaRecoveryService` consulta
`persistence_port.get_stale(before)` para encontrar ejecuciones que
empezaron pero nunca se completaron. Para cada saga obsoleta que sigue en
estado `IN_FLIGHT`, marca la saga como `FAILED` y emite eventos de ciclo de
vida para que los sistemas de observabilidad puedan alertar al equipo de
guardia.

### Configuración

```yaml
pyfly:
  transactional:
    saga:
      persistence_enabled: true
      recovery_enabled: true
      recovery_interval_seconds: 60
      stale_threshold_seconds: 600
      cleanup_older_than_hours: 24
```

Con `recovery_enabled: true`, el framework ejecuta
`SagaRecoveryService.recover_stale()` en una tarea en segundo plano cada
`recovery_interval_seconds` segundos. Las sagas actualizadas por última vez
hace más de `stale_threshold_seconds` segundos se consideran atascadas, se
marcan como fallidas y se sacan a la superficie para investigación manual o
reintento automático.

### Implementar un adaptador de persistencia personalizado

Para persistir en una base de datos real, implementa
`TransactionalPersistencePort` y registra tu implementación como un `@bean`
o `@component`. La autoconfiguración detecta tu bean en el arranque y lo usa
con preferencia sobre `InMemoryPersistenceAdapter`:

::: listing lumen/infra/persistence/saga_postgres_adapter.py | Listado 12.11 — Esqueleto de un adaptador de persistencia para PostgreSQL
from __future__ import annotations

from datetime import datetime
from typing import Any

from pyfly.container import component
from pyfly.transactional.shared.ports.outbound import (
    TransactionalPersistencePort,
)


@component
class SagaPostgresAdapter(TransactionalPersistencePort):

    async def persist_state(self, state: dict[str, Any]) -> None:
        # INSERT INTO saga_executions ...
        ...

    async def get_state(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        # SELECT * FROM saga_executions WHERE ...
        ...

    async def update_step_status(
        self,
        correlation_id: str,
        step_id: str,
        status: str,
    ) -> None: ...

    async def mark_completed(
        self, correlation_id: str, successful: bool
    ) -> None: ...

    async def get_in_flight(self) -> list[dict[str, Any]]:
        return []

    async def get_stale(
        self, before: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def cleanup(self, older_than: datetime) -> int:
        return 0

    async def is_healthy(self) -> bool:
        return True
:::

!!! tip "Usa SagaRecoveryService en pruebas de integración"
    En pruebas que simulan una caída, crea un `SagaRecoveryService` con un `InMemoryPersistenceAdapter`, ejecuta una saga hasta un punto intermedio, márcala manualmente como obsoleta y luego llama a `await recovery.recover_stale(stale_threshold_seconds=0)`. Comprueba que `SagaResult.success` es `False` y que los pasos correctos están marcados como fallidos. Esto te da confianza en tu lógica de recuperación sin levantar una base de datos real.

---

## El constructor programático de sagas

Cuando necesitas construir una saga a partir de configuración dinámica
—cargando definiciones de paso desde una base de datos de reglas o un
archivo de configuración— `SagaBuilder` te da la API fluida completa sin
ningún decorador:

::: listing lumen/transfer/dynamic_saga.py | Listado 12.12 — Construir una saga de forma programática con SagaBuilder
from __future__ import annotations

from pyfly.transactional.saga.registry.saga_builder import SagaBuilder
from pyfly.transactional.saga.core.result import SagaResult


async def debit_fn(req: object, ctx: object) -> str:
    return "debit-ref-001"


async def capture_fn(req: object, ctx: object) -> str:
    return "txn-001"


async def refund_fn(result: str) -> None:
    pass   # undo debit


saga_def = (
    SagaBuilder("dynamic-transfer")
    .step("debit")
        .handler(debit_fn)
        .compensate(refund_fn)
        .retry(3)
        .backoff_ms(200)
        .timeout_ms(5_000)
        .jitter(enabled=True, factor=0.3)
        .add()
    .step("capture")
        .handler(capture_fn)
        .depends_on("debit")
        .retry(2)
        .backoff_ms(500)
        .add()
    .layer_concurrency(5)
    .build()
)
:::

**Cómo funciona:** Cada llamada `.step(step_id)` devuelve un `StepBuilder`.
Encadena métodos de configuración —`.handler()`, `.compensate()`,
`.depends_on()`, `.retry()`, `.backoff_ms()`, `.timeout_ms()`,
`.jitter()`— y luego llama a `.add()` para finalizar el paso y devolver el
`SagaBuilder` padre. `.build()` ejecuta la misma validación del DAG que la
vía de los decoradores: los manejadores que faltan, las referencias
`depends_on` inexistentes y los ciclos lanzan todos `SagaValidationError`
inmediatamente en el momento del registro.

---

## Lo que construiste {.recap}

Empezaste con un problema concreto: una transferencia entre monederos a
través de dos agregados independientes no puede usar una sola transacción de
base de datos. Declaraste `MoneyTransferSaga` apilando `@saga` sobre
`@service`, lo que hace que el `OrchestrationBeanPostProcessor` la registre
en el `SagaEngine` autoconfigurado en el arranque. Cada paso usa instancias
de marcador `Annotated[T, Input()]` y `Annotated[T, FromStep("step-id")]`
—no clases desnudas— para la inyección de parámetros; `ctx: SagaContext` se
inyecta por tipo. El método de compensación `recredit_source` no recibe la
entrada de la saga; obtiene el `DebitResult` del paso hacia delante mediante
`FromStep("debit-source")`. Cuando `credit-destination` lanza
`AggregateNotFound`, el motor ejecuta automáticamente `recredit_source`,
dejando ambos saldos sin cambios.

Exploraste la compensación en profundidad: cinco políticas que van desde la
secuencial estricta hasta la paralela de mejor esfuerzo, los reintentos y
tiempos de espera de compensación por paso, y el requisito innegociable de
que todas las compensaciones sean idempotentes. Viste cómo
`@workflow(id=...) @service` y `@wait_for_signal` suspenden un flujo de
trabajo de larga duración hasta que un humano entrega una señal, y cómo
`WorkflowResult.successful` informa del estado final. Recorriste TCC como
una alternativa basada en reservas que bloquea recursos entre todos los
participantes antes de confirmar ninguno. Por último, cableaste un
`TransactionalPersistencePort` personalizado y configuraste
`SagaRecoveryService` para detectar y sacar a la superficie ejecuciones
obsoletas tras una caída.

Conceptos clave que llevarte:

- **`@saga` sobre `@service`** — la pila de decoradores que hace que una clase sea a la vez un bean de inyección de dependencias y una saga registrada; `@saga` por sí solo no basta.
- **Instancias de marcador** — `Input()`, `FromStep("step-id")` deben ser instancias (con paréntesis), no clases desnudas.
- **Compensación mediante `FromStep`** — los métodos de compensación reciben el resultado del paso hacia delante, nunca la entrada de la saga.
- **`SagaEngine.execute(saga_name, input_data)`** — la única llamada que devuelve `SagaResult` con `.success`, `.result_of()`, `.failed_steps()`, `.compensated_steps()`.
- **`@workflow(id=...) @service` / `@wait_for_signal`** — alternativa dirigida por señales y de larga duración; comprueba `WorkflowResult.successful` para el estado final.
- **`@tcc` sobre `@service` / `@tcc_participant`** — coordinación basada en reservas; `FromTry()` (instancia) inyecta el resultado del try en los métodos confirm y cancel.
- **`TransactionalPersistencePort`** — implementa y registra este protocolo para darle al motor estado duradero y recuperación ante caídas.

---

## Pruébalo tú mismo {.exercises}

**Ejercicio 1 — Validación de saldo en paralelo.** Añade un paso `validate-source` a `MoneyTransferSaga` que compruebe que el monedero origen tiene fondos suficientes, sin realizar el cargo. El paso debería ejecutarse *en paralelo* con nada (sin `depends_on`), y `debit-source` debería depender de él. Extiende `credit-destination` para que dependa de `debit-source` como antes. Verifica la topología mediante `SagaRegistry.get("money-transfer")` en una prueba y comprueba que `definition.steps["debit-source"].depends_on == ["validate-source"]`.

**Ejercicio 2 — Manejador de errores de compensación.** Cambia la política de compensación global de `MoneyTransferSaga` a `RETRY_WITH_BACKOFF` en `application.yaml`. Luego haz deliberadamente que `recredit_source` lance `RuntimeError` en la primera llamada y tenga éxito en la segunda. Escribe una prueba de pytest usando `AsyncMock` sobre `WalletRepository` que verifique que la saga acaba compensando con éxito y que `result.compensated_steps()` contiene `"debit-source"`.

**Ejercicio 3 — Persistencia personalizada.** Implementa `TransactionalPersistencePort` respaldado por un `dict` de Python plano que registre cada llamada. Regístralo como un `@service` y escribe una prueba que ejecute `TransferService`, luego llame a `get_state(correlation_id)` en tu adaptador y compruebe que el `status` registrado es `"COMPLETED"`. Extiende la prueba para simular una saga obsoleta estableciendo manualmente `status = "IN_FLIGHT"` y un `started_at` pasado, y luego comprueba que `SagaRecoveryService.recover_stale(stale_threshold_seconds=0)` devuelve `1`.
