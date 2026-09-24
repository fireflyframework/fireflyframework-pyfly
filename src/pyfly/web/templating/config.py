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
"""Typed configuration for browser applications."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.web.templates")
@dataclass
class TemplateProperties:
    enabled: bool = False
    directories: list[str] = field(default_factory=lambda: ["templates"])
    packages: list[str] = field(default_factory=list)
    strict_undefined: bool = True
    auto_reload: bool = False
    cache_size: int = 400

    def __post_init__(self) -> None:
        if self.cache_size < 0:
            raise ValueError("Template cache-size must be nonnegative")


@config_properties(prefix="pyfly.web.static")
@dataclass
class StaticProperties:
    enabled: bool = False
    path: str = "/static"
    directories: list[str] = field(default_factory=lambda: ["static"])
    packages: list[str] = field(default_factory=list)
    cache_control: str = "no-cache"


@config_properties(prefix="pyfly.web.errors")
@dataclass
class HtmlErrorProperties:
    html_enabled: bool = False
    templates: dict[str, str] = field(default_factory=dict)
    default_template: str = "errors/error.html"
    include_stacktrace: str = "on-debug"
    max_stack_frames: int = 30
    max_trace_length: int = 20000

    def __post_init__(self) -> None:
        if self.include_stacktrace not in {"never", "on-debug", "always"}:
            raise ValueError("Error include-stacktrace must be never, on-debug, or always")
        if not 1 <= self.max_stack_frames <= 200 or not 256 <= self.max_trace_length <= 100000:
            raise ValueError("Error trace limits must be 1..200 frames and 256..100000 characters")


@config_properties(prefix="pyfly.web.branding")
@dataclass
class BrandingProperties:
    name: str = "PyFly"
    tagline: str = "Build something that matters."
    logo_url: str = ""
    favicon_url: str = ""
    primary_color: str = "#4cbb2f"
    accent_color: str = "#c2e85f"
    documentation_url: str = "https://fireflyframework.github.io/fireflyframework-pyfly/docs/"
    support_url: str = "https://github.com/fireflyframework/fireflyframework-pyfly/issues"
    footer: str = "Built with PyFly · Firefly Software Foundation"
    assets_path: str = "/_pyfly/web"

    def __post_init__(self) -> None:
        for color in (self.primary_color, self.accent_color):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("Brand colors must be six-digit hex values")
        for url in (self.logo_url, self.favicon_url, self.documentation_url, self.support_url):
            if not url:
                continue
            parsed = urlsplit(url)
            if (
                any(char.isspace() or ord(char) < 32 for char in url)
                or "\\" in url
                or url.startswith("//")
                or not (url.startswith("/") or (parsed.scheme in {"https", "http"} and parsed.netloc))
            ):
                raise ValueError("Brand URLs must be application-relative paths or HTTP(S) URLs")
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", self.assets_path):
            raise ValueError("Brand assets-path must be a non-root URL path without a trailing slash")


@config_properties(prefix="pyfly.web.welcome")
@dataclass
class WelcomeProperties:
    enabled: bool = True
    template: str = "pyfly/welcome.html"
