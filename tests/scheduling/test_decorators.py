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
"""Tests for scheduling decorators."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pyfly.scheduling.decorators import async_method, scheduled


class TestScheduledDecorator:
    def test_cron_stores_metadata(self) -> None:
        """@scheduled(cron=...) stores cron metadata."""

        @scheduled(cron="0 0 * * *")
        def midnight_job() -> None:
            pass

        assert midnight_job.__pyfly_scheduled__ is True
        assert midnight_job.__pyfly_scheduled_cron__ == "0 0 * * *"
        assert midnight_job.__pyfly_scheduled_fixed_rate__ is None
        assert midnight_job.__pyfly_scheduled_fixed_delay__ is None
        assert midnight_job.__pyfly_scheduled_initial_delay__ is None

    def test_fixed_rate_stores_metadata(self) -> None:
        """@scheduled(fixed_rate=...) stores fixed_rate metadata."""
        rate = timedelta(seconds=30)

        @scheduled(fixed_rate=rate)
        def polling_job() -> None:
            pass

        assert polling_job.__pyfly_scheduled__ is True
        assert polling_job.__pyfly_scheduled_fixed_rate__ == rate
        assert polling_job.__pyfly_scheduled_cron__ is None
        assert polling_job.__pyfly_scheduled_fixed_delay__ is None

    def test_fixed_delay_stores_metadata(self) -> None:
        """@scheduled(fixed_delay=...) stores fixed_delay metadata."""
        delay = timedelta(minutes=5)

        @scheduled(fixed_delay=delay)
        def cleanup_job() -> None:
            pass

        assert cleanup_job.__pyfly_scheduled__ is True
        assert cleanup_job.__pyfly_scheduled_fixed_delay__ == delay
        assert cleanup_job.__pyfly_scheduled_cron__ is None
        assert cleanup_job.__pyfly_scheduled_fixed_rate__ is None

    def test_initial_delay_stored_alongside_cron(self) -> None:
        """@scheduled(initial_delay=...) stores initial_delay alongside cron."""
        delay = timedelta(seconds=10)

        @scheduled(cron="*/5 * * * *", initial_delay=delay)
        def delayed_cron_job() -> None:
            pass

        assert delayed_cron_job.__pyfly_scheduled__ is True
        assert delayed_cron_job.__pyfly_scheduled_cron__ == "*/5 * * * *"
        assert delayed_cron_job.__pyfly_scheduled_initial_delay__ == delay

    def test_no_trigger_raises_value_error(self) -> None:
        """@scheduled with no trigger raises ValueError."""
        with pytest.raises(
            ValueError,
            match="Exactly one of cron, fixed_rate, or fixed_delay must be specified",
        ):

            @scheduled()
            def bad_job() -> None:
                pass

    def test_multiple_triggers_raises_value_error(self) -> None:
        """@scheduled with multiple triggers (cron + fixed_rate) raises ValueError."""
        with pytest.raises(
            ValueError,
            match="Exactly one of cron, fixed_rate, or fixed_delay must be specified",
        ):

            @scheduled(cron="0 0 * * *", fixed_rate=timedelta(seconds=10))
            def conflicting_job() -> None:
                pass

    def test_preserves_function_identity(self) -> None:
        """@scheduled preserves function name and callable status."""

        @scheduled(fixed_rate=timedelta(seconds=1))
        def my_task() -> str:
            """My task docstring."""
            return "done"

        assert my_task.__name__ == "my_task"
        assert callable(my_task)
        assert my_task() == "done"


class TestAsyncMethodDecorator:
    def test_stores_async_metadata(self) -> None:
        """@async_method stores __pyfly_async__ = True."""

        @async_method
        def send_email() -> None:
            pass

        assert send_email.__pyfly_async__ is True

    def test_preserves_function_identity(self) -> None:
        """@async_method preserves function name and callable status."""

        @async_method
        def background_work() -> str:
            """Background work docstring."""
            return "result"

        assert background_work.__name__ == "background_work"
        assert callable(background_work)
        assert background_work() == "result"


class TestDecoratorStacking:
    def test_both_decorators_can_be_stacked(self) -> None:
        """Both @scheduled and @async_method can be applied; metadata from both is present."""

        @scheduled(fixed_rate=timedelta(seconds=60))
        @async_method
        def scheduled_async_task() -> None:
            pass

        assert scheduled_async_task.__pyfly_scheduled__ is True
        assert scheduled_async_task.__pyfly_scheduled_fixed_rate__ == timedelta(seconds=60)
        assert scheduled_async_task.__pyfly_async__ is True
