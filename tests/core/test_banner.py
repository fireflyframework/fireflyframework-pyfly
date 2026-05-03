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
"""Tests for startup banner rendering."""

from pyfly.core.banner import BannerMode, BannerPrinter


class TestBannerMode:
    def test_text_mode(self):
        assert BannerMode.TEXT.value == "TEXT"

    def test_minimal_mode(self):
        assert BannerMode.MINIMAL.value == "MINIMAL"

    def test_off_mode(self):
        assert BannerMode.OFF.value == "OFF"


class TestBannerPrinterText:
    def test_default_banner_contains_ascii_art(self):
        printer = BannerPrinter(mode=BannerMode.TEXT, version="0.1.0")
        output = printer.render()
        assert "____" in output
        assert "PyFly" in output

    def test_default_banner_contains_version(self):
        printer = BannerPrinter(mode=BannerMode.TEXT, version="1.2.3")
        output = printer.render()
        assert "v1.2.3" in output

    def test_default_banner_contains_framework_line(self):
        printer = BannerPrinter(mode=BannerMode.TEXT, version="0.1.0")
        output = printer.render()
        assert ":: PyFly Framework ::" in output

    def test_default_banner_contains_python_version(self):
        import sys

        printer = BannerPrinter(mode=BannerMode.TEXT, version="0.1.0")
        output = printer.render()
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert f"Python {py_ver}" in output

    def test_default_banner_contains_copyright(self):
        printer = BannerPrinter(mode=BannerMode.TEXT, version="0.1.0")
        output = printer.render()
        assert "Copyright 2026 Firefly Software Foundation." in output

    def test_default_banner_contains_license(self):
        printer = BannerPrinter(mode=BannerMode.TEXT, version="0.1.0")
        output = printer.render()
        assert "Apache License 2.0" in output


class TestBannerPrinterMinimal:
    def test_minimal_banner_one_line(self):
        printer = BannerPrinter(mode=BannerMode.MINIMAL, version="0.1.0")
        output = printer.render()
        assert output.strip() == ":: PyFly :: (v0.1.0)"

    def test_minimal_banner_version(self):
        printer = BannerPrinter(mode=BannerMode.MINIMAL, version="2.0.0")
        output = printer.render()
        assert "v2.0.0" in output


class TestBannerPrinterOff:
    def test_off_returns_empty(self):
        printer = BannerPrinter(mode=BannerMode.OFF, version="0.1.0")
        output = printer.render()
        assert output == ""


class TestBannerPrinterCustomFile:
    def test_custom_banner_file(self, tmp_path):
        banner_file = tmp_path / "custom-banner.txt"
        banner_file.write_text("Hello ${pyfly.version} - ${app.name}")
        printer = BannerPrinter(
            mode=BannerMode.TEXT,
            version="0.1.0",
            app_name="OrderService",
            custom_location=str(banner_file),
        )
        output = printer.render()
        assert "Hello 0.1.0 - OrderService" in output

    def test_custom_banner_python_version_placeholder(self, tmp_path):
        import sys

        banner_file = tmp_path / "banner.txt"
        banner_file.write_text("Running on Python ${python.version}")
        printer = BannerPrinter(
            mode=BannerMode.TEXT,
            version="0.1.0",
            custom_location=str(banner_file),
        )
        output = printer.render()
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert f"Running on Python {py_ver}" in output

    def test_custom_banner_profiles_placeholder(self, tmp_path):
        banner_file = tmp_path / "banner.txt"
        banner_file.write_text("Profiles: ${profiles.active}")
        printer = BannerPrinter(
            mode=BannerMode.TEXT,
            version="0.1.0",
            active_profiles=["dev", "local"],
            custom_location=str(banner_file),
        )
        output = printer.render()
        assert "Profiles: dev, local" in output

    def test_missing_custom_file_falls_back_to_default(self):
        printer = BannerPrinter(
            mode=BannerMode.TEXT,
            version="0.1.0",
            custom_location="/nonexistent/banner.txt",
        )
        output = printer.render()
        assert ":: PyFly Framework ::" in output


class TestBannerFromConfig:
    def test_from_config_text_mode(self):
        from pyfly.core.config import Config

        config = Config({"pyfly": {"banner": {"mode": "TEXT"}}})
        printer = BannerPrinter.from_config(config, version="0.1.0")
        assert printer._mode == BannerMode.TEXT

    def test_from_config_off_mode(self):
        from pyfly.core.config import Config

        config = Config({"pyfly": {"banner": {"mode": "OFF"}}})
        printer = BannerPrinter.from_config(config, version="0.1.0")
        assert printer._mode == BannerMode.OFF

    def test_from_config_defaults_to_text(self):
        from pyfly.core.config import Config

        config = Config({})
        printer = BannerPrinter.from_config(config, version="0.1.0")
        assert printer._mode == BannerMode.TEXT
