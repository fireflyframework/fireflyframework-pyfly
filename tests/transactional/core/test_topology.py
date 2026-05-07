# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the DAG TopologyBuilder."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.topology import TopologyBuilder, TopologyError


class TestTopologyBuilder:
    def test_linear_dag(self) -> None:
        layers = TopologyBuilder.build_layers({"a": [], "b": ["a"], "c": ["b"]})
        assert layers == [["a"], ["b"], ["c"]]

    def test_diamond_dag(self) -> None:
        layers = TopologyBuilder.build_layers(
            {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
        )
        assert layers[0] == ["a"]
        assert sorted(layers[1]) == ["b", "c"]
        assert layers[2] == ["d"]

    def test_independent_steps_first_layer(self) -> None:
        layers = TopologyBuilder.build_layers({"a": [], "b": [], "c": []})
        assert sorted(layers[0]) == ["a", "b", "c"]

    def test_missing_dependency_raises(self) -> None:
        with pytest.raises(TopologyError, match="unknown step"):
            TopologyBuilder.build_layers({"a": ["nope"]})

    def test_cycle_raises(self) -> None:
        with pytest.raises(TopologyError, match="cycle"):
            TopologyBuilder.build_layers({"a": ["b"], "b": ["a"]})
