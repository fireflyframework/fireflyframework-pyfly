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
"""The conformance files are the contract, byte for byte (spec 4.10): MANIFEST.sha256 is recomputed here."""

from __future__ import annotations

import hashlib

from tests.feature_flags.support import CONFORMANCE


def _contract_files() -> list[str]:
    """Every file of the folder but the manifest (and OS litter), as LC_ALL=C-sorted relative paths."""
    return sorted(
        path.relative_to(CONFORMANCE).as_posix()
        for path in CONFORMANCE.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256" and not path.name.startswith(".")
    )


def test_the_manifest_matches_every_conformance_file() -> None:
    manifest = CONFORMANCE / "MANIFEST.sha256"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (
        "069c33f3c8c1fcbda0881491d9895046c6a7bfe74deb1244b29a7534d8a5e321"
    )
    expected = manifest.read_text(encoding="utf-8")
    actual = "".join(
        f"{hashlib.sha256((CONFORMANCE / name).read_bytes()).hexdigest()}  {name}\n" for name in _contract_files()
    )
    assert actual == expected


def test_the_folder_holds_the_testbed_and_the_vectors() -> None:
    names = _contract_files()
    assert {"firefly-vectors.json", "testbed/LICENSE", "testbed/SOURCE.md"} <= set(names)
    assert "testbed/evaluator/flags/testkit-flags.json" in names
    assert len([name for name in names if name.startswith("testbed/evaluator/gherkin/")]) == 11
    assert "v3.10.2" in (CONFORMANCE / "testbed" / "SOURCE.md").read_text(encoding="utf-8")
