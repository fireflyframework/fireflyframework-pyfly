from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import BaseEntity


class Product(BaseEntity):
    __tablename__ = "webapp_products"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class ProductWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ProductForm(ProductWrite):
    edit_version: str = ""
