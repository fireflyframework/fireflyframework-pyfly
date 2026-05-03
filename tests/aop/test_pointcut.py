# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for the pointcut expression matcher."""

from __future__ import annotations

from pyfly.aop.pointcut import matches_pointcut


class TestMatchesPointcut:
    """matches_pointcut covers exact, single-star, double-star, and partial globs."""

    def test_exact_match(self) -> None:
        assert matches_pointcut("service.OrderService.create", "service.OrderService.create")

    def test_star_matches_single_segment_method(self) -> None:
        assert matches_pointcut("service.OrderService.*", "service.OrderService.create")

    def test_star_matches_single_segment_class(self) -> None:
        assert matches_pointcut("service.*.create", "service.OrderService.create")

    def test_star_matches_single_segment_module(self) -> None:
        assert matches_pointcut("*.OrderService.create", "service.OrderService.create")

    def test_star_all_segments(self) -> None:
        assert matches_pointcut("*.*.*", "service.OrderService.create")

    def test_doublestar_any_depth(self) -> None:
        assert matches_pointcut("**.*Service.*", "a.b.c.OrderService.create")

    def test_doublestar_single_depth(self) -> None:
        assert matches_pointcut("**.*", "module.method")

    def test_doublestar_deep(self) -> None:
        assert matches_pointcut("**.do_work", "a.b.c.d.e.do_work")

    def test_no_match_completely_different(self) -> None:
        assert not matches_pointcut("service.OrderService.create", "other.Foo.bar")

    def test_star_does_not_cross_dots(self) -> None:
        assert not matches_pointcut("*.my_method", "a.b.MyClass.my_method")

    def test_prefix_wildcard_matches(self) -> None:
        assert matches_pointcut("mymod.MyClass.get_*", "mymod.MyClass.get_order")

    def test_prefix_wildcard_no_match(self) -> None:
        assert not matches_pointcut("mymod.MyClass.get_*", "mymod.MyClass.set_order")

    def test_service_star_star(self) -> None:
        assert matches_pointcut("service.*.*", "service.OrderService.create")

    def test_service_star_star_no_extra_depth(self) -> None:
        assert not matches_pointcut("service.*.*", "service.sub.OrderService.create")
