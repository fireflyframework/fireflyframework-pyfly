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
"""Regression tests for #8 — an explicit package name flows through generation."""

from __future__ import annotations

from pathlib import Path

from pyfly.cli.templates import _build_context, generate_project


class TestExplicitPackageName:
    def test_custom_package_name_used_for_paths(self, tmp_path: Path):
        generate_project(name="my-svc", project_dir=tmp_path / "p", archetype="core", features=[], package_name="svc")
        assert (tmp_path / "p" / "src" / "svc" / "__init__.py").exists()
        assert not (tmp_path / "p" / "src" / "my_svc").exists()

    def test_default_derives_from_name(self, tmp_path: Path):
        generate_project(name="my-svc", project_dir=tmp_path / "p", archetype="core", features=[])
        assert (tmp_path / "p" / "src" / "my_svc" / "__init__.py").exists()

    def test_build_context_honors_explicit_package(self):
        ctx = _build_context("my-svc", "core", [], package_name="svc")
        assert ctx["package_name"] == "svc"

    def test_build_context_default_derivation(self):
        ctx = _build_context("my-svc", "core", [])
        assert ctx["package_name"] == "my_svc"
