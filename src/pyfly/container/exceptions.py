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
"""Container exceptions — fatal errors during bean creation and startup."""

from __future__ import annotations

from pyfly.kernel.exceptions import InfrastructureException


class BeanCreationException(InfrastructureException):
    """Fatal error during bean creation — application cannot start.

    Analogous to Spring Boot's BeanCreationException.
    """

    def __init__(self, subsystem: str, provider: str, reason: str) -> None:
        self.subsystem = subsystem
        self.provider = provider
        self.reason = reason
        message = f"Failed to configure {subsystem} with provider '{provider}': {reason}"
        super().__init__(message=message, code=f"BEAN_CREATION_{subsystem.upper()}")


class NoSuchBeanError(BeanCreationException):
    """No bean found for the requested type or name."""

    def __init__(
        self,
        *,
        bean_type: type | None = None,
        bean_name: str | None = None,
        required_by: str | None = None,
        parameter: str | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        self.bean_type = bean_type
        self.bean_name = bean_name
        self.required_by = required_by
        self.parameter = parameter
        self.suggestions = suggestions or []

        type_desc = getattr(bean_type, "__name__", repr(bean_type)) if bean_type is not None else None
        if type_desc:
            headline = f"No bean of type '{type_desc}' is registered"
        elif bean_name:
            headline = f"No bean named '{bean_name}' is registered"
        else:
            headline = "No matching bean is registered"

        lines = [f"NoSuchBeanError: {headline}"]

        if required_by or parameter:
            lines.append("")
            if required_by:
                lines.append(f"  Required by: {required_by}")
            if parameter:
                lines.append(f"    Parameter: {parameter}")

        lines.append("")
        lines.append("  Suggestions:")
        lines.append("    - Add @component, @service, or @bean to a class that produces this type")
        lines.append("    - Check that the module is listed in scan_packages")
        lines.append("    - Check @conditional_on_* conditions and pyfly.yaml")

        if self.suggestions:
            lines.append("")
            lines.append(f"  Similar registered types: {', '.join(self.suggestions)}")

        message = "\n".join(lines)

        BeanCreationException.__init__(
            self,
            subsystem="resolution",
            provider=required_by or "container",
            reason=headline,
        )
        self.args = (message,)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class NoUniqueBeanError(BeanCreationException):
    """Multiple beans match the requested type but none is marked ``@primary``."""

    def __init__(
        self,
        *,
        bean_type: type,
        candidates: list[type],
        required_by: str | None = None,
        parameter: str | None = None,
    ) -> None:
        self.bean_type = bean_type
        self.candidates = candidates
        self.required_by = required_by
        self.parameter = parameter

        type_name = getattr(bean_type, "__name__", repr(bean_type))
        candidate_names = [getattr(c, "__name__", repr(c)) for c in candidates]
        headline = f"Multiple beans of type '{type_name}' found but none is marked @primary"

        lines = [f"NoUniqueBeanError: {headline}"]
        lines.append("")
        lines.append(f"  Candidates: {candidate_names}")

        if required_by or parameter:
            lines.append("")
            if required_by:
                lines.append(f"  Required by: {required_by}")
            if parameter:
                lines.append(f"    Parameter: {parameter}")

        lines.append("")
        lines.append("  Fix: Mark one implementation with @primary, or use Qualifier('name') to disambiguate")

        message = "\n".join(lines)

        BeanCreationException.__init__(
            self,
            subsystem="resolution",
            provider=required_by or "container",
            reason=headline,
        )
        self.args = (message,)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class BeanCurrentlyInCreationError(BeanCreationException):
    """Circular dependency detected during bean resolution.

    The ``chain`` attribute contains the dependency path as a deterministic
    ordered list (insertion order of resolution attempts).
    """

    def __init__(self, *, chain: list[type], current: type) -> None:
        self.chain = chain
        self.current = current

        chain_names = [t.__name__ for t in chain]
        chain_names.append(current.__name__)
        chain_str = " -> ".join(chain_names)
        headline = f"Circular dependency: {chain_str}"

        lines = [f"BeanCurrentlyInCreationError: {headline}"]
        lines.append("")
        lines.append("  Suggestion: Break the cycle with @post_construct or a factory pattern")

        message = "\n".join(lines)

        BeanCreationException.__init__(
            self,
            subsystem="resolution",
            provider=current.__name__,
            reason=headline,
        )
        self.args = (message,)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""
