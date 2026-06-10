"""Regression: Registration.display_name must be union-safe.

PEP 604 unions (``X | Y``) are ``types.UnionType`` and have no ``__name__``, so
deriving a default bean name via ``impl_type.__name__`` raised ``AttributeError``
during startup (``BeanCreationException``). ``display_name`` must never crash.
"""

from pyfly.container.registry import Registration


class _A:
    pass


class _B:
    pass


def test_display_name_uses_explicit_name() -> None:
    assert Registration(impl_type=_A, name="myBean").display_name == "myBean"


def test_display_name_falls_back_to_type_name() -> None:
    assert Registration(impl_type=_A).display_name == "_A"


def test_display_name_is_union_safe() -> None:
    # The case that crashed bean-name derivation at startup.
    reg = Registration(impl_type=(_A | _B))
    assert reg.display_name == "_A | _B"  # no AttributeError


def test_display_name_handles_optional_union() -> None:
    # ``_A | None`` (the PEP 604 form of Optional) is also a ``types.UnionType``
    # and must never raise; the exact rendering may vary by Python version, but
    # it is always a usable, non-empty name.
    name = Registration(impl_type=(_A | None)).display_name
    assert isinstance(name, str) and name
