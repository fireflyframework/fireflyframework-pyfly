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
"""The ``config`` layer: ``pyfly.feature-flags.flags`` and ``pyfly.feature-flags.evaluators`` (shorthand allowed).

The two maps are read with ``Config.get_section`` so flag keys stay verbatim: binding would relax ``new-checkout``
into ``new_checkout``. The layer is loaded once, at startup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.sources import SourceSnapshot

if TYPE_CHECKING:
    from pyfly.core.config import Config

__all__ = ["EVALUATORS_SECTION", "FLAGS_SECTION", "ConfigFlagSource"]

FLAGS_SECTION = "pyfly.feature-flags.flags"
EVALUATORS_SECTION = "pyfly.feature-flags.evaluators"


def _section(config: Config, prefix: str) -> Any:
    """The raw section at *prefix*: a mapping with its keys verbatim, or the value itself when it is not a mapping.

    ``Config.get_section`` answers ``{}`` for anything but a mapping, which would load zero flags without a word for
    ``flags: [new-checkout]``; the probe hands such a value on, for ``parse_document`` to reject with its reason.
    """
    probe = config.get(prefix)
    return config.get_section(prefix) if probe is None or isinstance(probe, Mapping) else probe


class ConfigFlagSource:
    """Inline definitions from the application configuration (the lowest layer)."""

    name = "config"
    fail_fast = True
    refresh_interval: float | None = None

    def __init__(self, flags: Any = None, evaluators: Any = None) -> None:
        """*flags* and *evaluators* are the two raw sections: a mapping (copied), ``None`` or an empty list (both
        empty, as the contract reads ``[]``), or any other value, which ``load()`` rejects (``flags must be an
        object``)."""
        self._flags = dict(flags) if isinstance(flags, Mapping) else flags
        self._evaluators = dict(evaluators) if isinstance(evaluators, Mapping) else evaluators

    @classmethod
    def from_config(cls, config: Config) -> ConfigFlagSource:
        """The layer of *config*'s ``pyfly.feature-flags.flags`` and ``.evaluators`` sections."""
        return cls(_section(config, FLAGS_SECTION), _section(config, EVALUATORS_SECTION))

    async def load(self) -> SourceSnapshot:
        document = parse_document({"flags": self._flags, "$evaluators": self._evaluators}, shorthand=True)
        return SourceSnapshot(document)

    async def close(self) -> None:
        return None
