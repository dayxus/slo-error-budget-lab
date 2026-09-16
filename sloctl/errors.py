"""Exception hierarchy used across sloctl.

Every exception below is expected by design: the CLI catches ``SLOError`` and
turns it into a single-line message on stderr plus exit code 1, so the tool can
be wired into a pipeline without parsing tracebacks.
"""

from __future__ import annotations


class SLOError(Exception):
    """Base class for every error sloctl reports to the operator."""


class ConfigError(SLOError):
    """A SLO definition is missing, malformed or semantically invalid."""


class SamplesError(SLOError):
    """A sample fixture is missing, malformed or internally inconsistent."""


class EngineError(SLOError):
    """The error budget math cannot be applied to the given inputs."""
