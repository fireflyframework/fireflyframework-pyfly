# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the starter meta-packages."""

from __future__ import annotations

from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.starters import (
    APPLICATION_STACK_PROPERTIES,
    CORE_STACK_PROPERTIES,
    DATA_STACK_PROPERTIES,
    DOMAIN_STACK_PROPERTIES,
    WEB_STACK_PROPERTIES,
    enable_application_stack,
    enable_core_stack,
    enable_data_stack,
    enable_domain_stack,
    enable_web_stack,
    register_application_stack,
    register_core_stack,
    register_data_stack,
    register_domain_stack,
    register_web_stack,
)

# ── Decorator markers ─────────────────────────────────────────────────


def test_core_stack_marks_class() -> None:
    @enable_core_stack
    class App: ...

    assert App.__pyfly_starter_core__ == CORE_STACK_PROPERTIES


def test_web_stack_marks_class() -> None:
    @enable_web_stack
    class App: ...

    assert App.__pyfly_starter_web__ == WEB_STACK_PROPERTIES
    assert App.__pyfly_starter_web__["pyfly.web.enabled"] == "true"
    assert App.__pyfly_starter_web__["pyfly.server.enabled"] == "true"


def test_application_stack_includes_core() -> None:
    @enable_application_stack
    class App: ...

    for k, v in CORE_STACK_PROPERTIES.items():
        assert App.__pyfly_starter_application__[k] == v
    assert App.__pyfly_starter_application__["pyfly.transactional.enabled"] == "true"
    assert App.__pyfly_starter_application__["pyfly.idp.enabled"] == "true"
    assert App.__pyfly_starter_application__["pyfly.plugins.enabled"] == "true"
    assert App.__pyfly_starter_application__["pyfly.i18n.enabled"] == "true"
    assert App.__pyfly_starter_application__["pyfly.scheduling.enabled"] == "true"


def test_data_stack_marks_relational_and_document() -> None:
    @enable_data_stack
    class App: ...

    assert App.__pyfly_starter_data__["pyfly.data.relational.enabled"] == "true"
    assert App.__pyfly_starter_data__["pyfly.data.document.enabled"] == "true"
    assert App.__pyfly_starter_data__["pyfly.client.enabled"] == "true"


def test_domain_stack_marks_eventsourcing() -> None:
    @enable_domain_stack
    class App: ...

    assert App.__pyfly_starter_domain__["pyfly.eventsourcing.enabled"] == "true"
    assert App.__pyfly_starter_domain__["pyfly.transactional.enabled"] == "true"
    assert App.__pyfly_starter_domain__["pyfly.rule-engine.enabled"] == "true"


def test_property_dicts_are_strings() -> None:
    for d in (
        CORE_STACK_PROPERTIES,
        WEB_STACK_PROPERTIES,
        APPLICATION_STACK_PROPERTIES,
        DATA_STACK_PROPERTIES,
        DOMAIN_STACK_PROPERTIES,
    ):
        for k, v in d.items():
            assert isinstance(k, str)
            assert isinstance(v, str)


# ── Re-exports ────────────────────────────────────────────────────────


def test_core_starter_re_exports_cqrs_and_di() -> None:
    from pyfly.starters.core import (
        Autowired,
        Command,
        CommandBus,
        CommandHandler,
        Query,
        QueryBus,
        QueryHandler,
        command_handler,
        component,
        configuration,
        pyfly_application,
        query_handler,
        rest_controller,
        service,
    )

    # Just touching them is enough — import-time would fail otherwise.
    for sym in (
        Autowired,
        Command,
        CommandBus,
        CommandHandler,
        Query,
        QueryBus,
        QueryHandler,
        command_handler,
        component,
        configuration,
        pyfly_application,
        query_handler,
        rest_controller,
        service,
    ):
        assert sym is not None


def test_web_starter_re_exports_web_decorators() -> None:
    from pyfly.starters.web import (
        Body,
        Cookie,
        File,
        Header,
        PathVar,
        QueryParam,
        UploadedFile,
        Valid,
        delete_mapping,
        get_mapping,
        patch_mapping,
        post_mapping,
        put_mapping,
        request_mapping,
        rest_controller,
        sse_mapping,
    )

    for sym in (
        Body,
        Cookie,
        File,
        Header,
        PathVar,
        QueryParam,
        UploadedFile,
        Valid,
        delete_mapping,
        get_mapping,
        patch_mapping,
        post_mapping,
        put_mapping,
        request_mapping,
        rest_controller,
        sse_mapping,
    ):
        assert sym is not None


def test_domain_starter_re_exports_ddd_primitives() -> None:
    from pyfly.starters.domain import (
        AggregateNotFound,
        AggregateRoot,
        BusinessRuleViolation,
        DomainEvent,
        DomainException,
        DomainRepository,
        Entity,
        Specification,
        ValueObject,
    )

    for sym in (
        AggregateNotFound,
        AggregateRoot,
        BusinessRuleViolation,
        DomainEvent,
        DomainException,
        DomainRepository,
        Entity,
        Specification,
        ValueObject,
    ):
        assert sym is not None


# ── Functional injection (the real test) ──────────────────────────────


def test_starter_decorator_actually_injects_properties_into_config() -> None:
    """The decorator must merge property defaults into the live config."""

    @enable_core_stack
    @pyfly_application(name="t-core", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)

    # Every property in CORE_STACK_PROPERTIES should now be readable
    # through Config.get() — proving the decorator did more than set
    # an attribute.
    assert app.config.get("pyfly.cqrs.enabled") == "true"
    assert app.config.get("pyfly.eda.provider") == "auto"
    assert app.config.get("pyfly.cache.enabled") == "true"
    assert app.config.get("pyfly.observability.enabled") == "true"
    assert app.config.get("pyfly.web.actuator.enabled") == "true"
    assert app.config.get("pyfly.aop.enabled") == "true"


def test_domain_decorator_injects_eventsourcing_and_transactional() -> None:
    @enable_domain_stack
    @pyfly_application(name="t-domain", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    assert app.config.get("pyfly.eventsourcing.enabled") == "true"
    assert app.config.get("pyfly.transactional.enabled") == "true"
    assert app.config.get("pyfly.rule-engine.enabled") == "true"
    # And the inherited core props:
    assert app.config.get("pyfly.cqrs.enabled") == "true"


def test_web_decorator_injects_web_and_server() -> None:
    @enable_web_stack
    @pyfly_application(name="t-web", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    assert app.config.get("pyfly.web.enabled") == "true"
    assert app.config.get("pyfly.server.enabled") == "true"
    assert app.config.get("pyfly.web.actuator.enabled") == "true"


def test_register_core_stack_imperative_api() -> None:
    @pyfly_application(name="t-imp-core", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    # Without the decorator nothing is set yet:
    assert app.config.get("pyfly.cqrs.enabled") is None
    register_core_stack(app)
    assert app.config.get("pyfly.cqrs.enabled") == "true"
    assert app.config.get("pyfly.eda.provider") == "auto"


def test_register_web_stack_imperative_api() -> None:
    @pyfly_application(name="t-imp-web", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    assert app.config.get("pyfly.web.enabled") is None
    register_web_stack(app)
    assert app.config.get("pyfly.web.enabled") == "true"
    assert app.config.get("pyfly.server.enabled") == "true"


def test_register_application_stack_imperative_api() -> None:
    @pyfly_application(name="t-imp-app", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    register_application_stack(app)
    assert app.config.get("pyfly.transactional.enabled") == "true"
    assert app.config.get("pyfly.security.enabled") == "true"
    assert app.config.get("pyfly.plugins.enabled") == "true"


def test_register_data_stack_imperative_api() -> None:
    @pyfly_application(name="t-imp-data", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    register_data_stack(app)
    assert app.config.get("pyfly.data.relational.enabled") == "true"
    assert app.config.get("pyfly.data.document.enabled") == "true"


def test_register_domain_stack_imperative_api() -> None:
    @pyfly_application(name="t-imp-domain", scan_packages=[])
    class App: ...

    app = PyFlyApplication(App)
    register_domain_stack(app)
    assert app.config.get("pyfly.eventsourcing.enabled") == "true"
    assert app.config.get("pyfly.transactional.enabled") == "true"


def test_user_config_overrides_starter_defaults() -> None:
    """User-provided pyfly.yaml values must beat starter defaults.

    Verifies the layering ``framework_defaults < starter_defaults <
    user_yaml`` invariant by exercising the underlying ``_deep_merge``
    ordering directly. The ``@enable_core_stack`` decorator passes its
    dict as ``starter_defaults`` to ``Config.from_sources``, which then
    merges user files on top — so any overlapping key in the user's
    ``pyfly.yaml`` ends up as the final value.
    """
    from pyfly.core.config import Config

    user_data = {"pyfly": {"cqrs": {"enabled": "false"}}}
    starter_data = {"pyfly": {"cqrs": {"enabled": "true"}, "eda": {"enabled": "true"}}}
    merged = Config._deep_merge(starter_data, user_data)
    assert merged["pyfly"]["cqrs"]["enabled"] == "false"
    # but other starter keys still survive:
    assert merged["pyfly"]["eda"]["enabled"] == "true"
