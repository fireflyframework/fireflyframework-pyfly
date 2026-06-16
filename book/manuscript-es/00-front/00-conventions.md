## Convenciones

Esta página explica las convenciones tipográficas y estructurales que se usan a lo largo del libro.

### Listados de código

Cada ejemplo de código de varias líneas tiene una **pestaña con el nombre del archivo** en la esquina superior izquierda que muestra el archivo al que pertenece, y un pie **"Listado N.N"** debajo del bloque que lo identifica por capítulo y número de secuencia. Por ejemplo:

::: listing wallet/domain/wallet.py | Listado 5.1 — Raíz del agregado Wallet
from pyfly.core import component
from dataclasses import dataclass, field
from decimal import Decimal

@component
@dataclass
class Wallet:
    id: str
    balance: Decimal = field(default=Decimal("0.00"))

    def deposit(self, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("Deposit amount must be positive")
        self.balance += amount
:::

Las referencias de código en línea dentro de la prosa usan fuente `monoespaciada`, como en "el decorador `@component` registra la clase en el contenedor de PyFly".

### Notas al margen

En los márgenes y en el cuerpo aparecen cuatro estilos de notas al margen:

!!! note "Nota"
    Las notas aportan contexto complementario o aclaran una sutileza del texto principal: merece la pena leerlas, pero no son bloqueantes.

!!! tip "Consejo"
    Los consejos comparten un atajo, un idiom o una buena práctica que te ahorrará tiempo en proyectos reales.

!!! warning "Advertencia"
    Las advertencias señalan un error habitual o un punto delicado que puede provocar problemas difíciles de depurar si se ignora.

!!! spring "Equivalencia con Spring"
    Las notas de equivalencia con Spring relacionan un concepto de PyFly directamente con su equivalente en Spring Boot: ideales para quienes migran desde el ecosistema de la JVM.

### Figuras

Los diagramas se numeran como **Figura N.N** y llevan un pie debajo de la imagen. Se incrustan como SVG en línea, de modo que se renderizan con nitidez a cualquier nivel de zoom tanto en la edición en pantalla como en la impresa. Te encontrarás con la primera en la página inicial del Capítulo 1.
