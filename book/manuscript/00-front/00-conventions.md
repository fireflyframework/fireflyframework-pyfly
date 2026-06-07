## Conventions

This page explains the typographic and structural conventions used throughout the book.

### Code Listings

Every multi-line code example has a **file-name tab** in the top-left corner showing the file it belongs to, and a **"Listing N.N"** caption below the block identifying it by chapter and sequence number. For example:

::: listing wallet/domain/wallet.py | Listing 5.1 — Wallet aggregate root
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

Inline code references within prose use `monospace` font, as in "the `@component` decorator registers the class with PyFly's container."

### Callouts

Four callout styles appear in the margins and body:

!!! note "Note"
    Notes provide supplementary context or clarify a subtlety in the main text — worth reading, but not blocking.

!!! tip "Tip"
    Tips share a shortcut, idiom, or best practice that will save you time in real projects.

!!! warning "Warning"
    Warnings flag a common mistake or a sharp edge that can cause hard-to-debug problems if ignored.

!!! spring "Spring parity"
    Spring parity callouts map a PyFly concept directly to its Spring Boot equivalent — ideal for developers migrating from the JVM ecosystem.

### Figures

Diagrams are numbered **Figure N.N** and captioned below the image. They are embedded as inline SVG, so they render crisply at any zoom level in both the screen and print editions. You will meet the first one on the opening page of Chapter 1.
