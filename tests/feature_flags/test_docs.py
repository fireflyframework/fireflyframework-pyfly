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
"""Feature-flag documentation, navigation and the shared contract stay connected."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "modules" / "feature-flags.md"
CONTRACT = ROOT / "docs" / "modules" / "feature-flags-contract.md"
CONTRACT_SHA256 = "a06bcac44b7c6b003be02da07e53a1a7c848c477c49f752a5f9e6f1b5b00e70b"


def test_guide_is_reachable_and_covers_the_public_workflow() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    for heading in (
        "## Quick start",
        "## Defining flags",
        "## Targeting recipes",
        "## Gating code",
        "## Evaluation context",
        "## Sources and precedence",
        "## The store",
        "## Managing flags at runtime",
        "## Serving flags to other services",
        "## Observability",
        "## Testing",
        "## Configuration reference",
        "## Troubleshooting",
    ):
        assert heading in guide, heading
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8").split("\nnav:\n", 1)[1]
    assert "modules/feature-flags.md" in nav
    assert "modules/feature-flags-contract.md" in nav


def test_contract_page_matches_the_shared_contract_snapshot() -> None:
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == CONTRACT_SHA256


def test_every_index_links_the_guide() -> None:
    for path in ("README.md", "docs/README.md", "docs/modules/README.md", "docs/index.md", "docs/spring-comparison.md"):
        assert "feature-flags.md" in (ROOT / path).read_text(encoding="utf-8"), path


def test_cli_reference_and_both_books_cover_the_shipped_commands() -> None:
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "### pyfly flags" in cli
    assert "[pyfly flags](#pyfly-flags)" in cli
    for command in ("list", "show", "enable", "disable", "default-variant", "put", "delete", "evaluate"):
        assert f"pyfly flags {command}" in cli
    for edition in ("manuscript", "manuscript-es"):
        appendix = (ROOT / "book" / edition / "93-appendix-d-cli.md").read_text(encoding="utf-8")
        assert "pyfly flags evaluate" in appendix
        assert "pyfly flags put" in appendix
