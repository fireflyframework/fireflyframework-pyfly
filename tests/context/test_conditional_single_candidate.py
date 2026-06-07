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
"""@ConditionalOnSingleCandidate (v26.06.49) — exactly-one-candidate / unique-@primary."""

from __future__ import annotations

from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.conditions import conditional_on_single_candidate
from pyfly.core.config import Config


class Repo:
    pass


class SqlRepo(Repo):
    pass


class MemRepo(Repo):
    pass


def _ev(container: Container) -> ConditionEvaluator:
    return ConditionEvaluator(Config({}), container)


def _cond(bean_type: type) -> dict:
    return {"type": "on_single_candidate", "bean_type": bean_type}


def _register_interface_bean(container: Container, interface: type, impl: type) -> None:
    """Mirror how @bean returning *interface* with concrete *impl* registers: a concrete reg,
    an interface->impl binding, and an interface-alias reg SHARING the concrete's factory
    (exactly what ApplicationContext does at lines 482/500-502)."""
    container.register(impl)
    container.bind(interface, impl)

    def factory() -> None:  # the shared @bean factory closure
        return None

    container._registrations[impl].factory = factory
    if interface not in container._registrations:
        container.register(interface)  # the alias reg (impl_type == interface)
    container._registrations[interface].factory = factory  # alias shares the factory


def test_single_impl_of_interface_matches() -> None:
    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    assert _ev(c)._eval_on_single_candidate(_cond(Repo)) is True


def test_interface_alias_is_not_double_counted() -> None:
    # Core regression guard: the alias reg must collapse onto its concrete (one group).
    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    assert len(_ev(c)._candidate_bean_groups(Repo)) == 1


def test_concrete_base_that_is_also_a_binding_key_counts_both() -> None:
    # Regression for the review finding: a concrete base bean that is ALSO a _bindings key
    # (because a registered subclass is bound to it) must NOT be skipped — two distinct beans.
    class Svc:
        pass

    class PremiumSvc(Svc):
        pass

    c = Container()
    c.register(Svc)
    c.register(PremiumSvc)
    c.bind(Svc, PremiumSvc)  # Svc is now a _bindings key AND its own distinct bean
    assert _ev(c)._eval_on_single_candidate(_cond(Svc)) is False  # two real candidates


def test_two_impls_no_primary_is_ambiguous() -> None:
    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    _register_interface_bean(c, Repo, MemRepo)
    assert _ev(c)._eval_on_single_candidate(_cond(Repo)) is False


def test_two_impls_one_primary_matches() -> None:
    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    _register_interface_bean(c, Repo, MemRepo)
    c._registrations[SqlRepo].primary = True  # @bean(primary=True) path
    assert _ev(c)._eval_on_single_candidate(_cond(Repo)) is True


def test_two_impls_two_primaries_is_ambiguous() -> None:
    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    _register_interface_bean(c, Repo, MemRepo)
    c._registrations[SqlRepo].primary = True
    c._registrations[MemRepo].primary = True
    assert _ev(c)._eval_on_single_candidate(_cond(Repo)) is False


def test_zero_candidates_is_false() -> None:
    assert _ev(Container())._eval_on_single_candidate(_cond(Repo)) is False


def test_declaring_class_is_excluded() -> None:
    class SelfRepo(Repo):
        pass

    c = Container()
    c.register(SelfRepo)
    # The only Repo is the declaring class itself -> excluded -> no candidates.
    assert _ev(c)._eval_on_single_candidate(_cond(Repo), declaring_cls=SelfRepo) is False


def test_decorator_records_condition_and_is_pass2_routed() -> None:
    @conditional_on_single_candidate(Repo)
    class Guarded:
        pass

    assert Guarded.__pyfly_conditions__ == [{"type": "on_single_candidate", "bean_type": Repo}]  # type: ignore[attr-defined]

    c = Container()
    _register_interface_bean(c, Repo, SqlRepo)
    evaluator = _ev(c)
    # Bean-dependent: ignored in pass 1, enforced in pass 2.
    assert evaluator.should_include(Guarded, bean_pass=False) is True
    assert evaluator.should_include(Guarded, bean_pass=True) is True
