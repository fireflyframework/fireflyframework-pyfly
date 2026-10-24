"""Installed namespace and package ownership contracts for native Agentic."""

import importlib.util


def test_agentic_is_available_in_the_pyfly_namespace():
    assert importlib.util.find_spec("pyfly.agentic") is not None
