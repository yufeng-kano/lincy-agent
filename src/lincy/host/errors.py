"""Host-level errors; cli.main() is the only place that catches them."""

from __future__ import annotations


class HostError(Exception):
    """Operator-facing failure; status_code maps onto control API responses."""

    status_code: int = 500


class ConfigInvalid(HostError):
    pass


class WorkspaceNotReady(HostError):
    pass


class EnvironmentCheckFailed(HostError):
    pass


class BuildFailed(HostError):
    pass


class UpgradeInProgress(HostError):
    status_code = 409
