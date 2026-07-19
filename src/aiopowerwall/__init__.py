"""Async Tesla Powerwall 3 client (TEDAPI v1r over RSA-signed LAN).

This package provides a single high-level entry point,
:class:`PowerwallClient`, plus typed exceptions and TypedDict response models.

The RSA key pair used for v1r signing must be registered with the gateway
out-of-band (typically via the Tesla Fleet API). This library only consumes
an already-paired private key.
"""

from __future__ import annotations

from .client import (
    DEFAULT_GATEWAY_HOST,
    GRID_EXPORT_RULES,
    OPERATION_MODES,
    PowerwallClient,
    backup_time_remaining,
    battery_level,
    battery_level_raw,
    current_power,
    raw_to_scaled_reserve,
    raw_to_scaled_soc,
    scaled_to_raw_reserve,
    scaled_to_raw_soc,
)
from .energysite import (
    ISLAND_MODE_OFF_GRID,
    ISLAND_MODE_ON_GRID,
    PowerwallEnergySite,
)
from .exceptions import (
    PowerwallAuthenticationError,
    PowerwallConnectionError,
    PowerwallError,
    PowerwallFaultError,
    PowerwallProtocolError,
    PowerwallRateLimitError,
)
from .models import (
    AuthorizedClient,
    AuthorizedClientsPayload,
    BackupEvent,
    BackupEventsPayload,
    ComponentsPayload,
    ConfigPayload,
    ControllerPayload,
    FirmwareDetails,
    ManualBackupInfo,
    PowerLocation,
    StatusPayload,
)

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_GATEWAY_HOST",
    "GRID_EXPORT_RULES",
    "ISLAND_MODE_OFF_GRID",
    "ISLAND_MODE_ON_GRID",
    "OPERATION_MODES",
    "AuthorizedClient",
    "AuthorizedClientsPayload",
    "BackupEvent",
    "BackupEventsPayload",
    "ComponentsPayload",
    "ConfigPayload",
    "ControllerPayload",
    "FirmwareDetails",
    "ManualBackupInfo",
    "PowerLocation",
    "PowerwallAuthenticationError",
    "PowerwallClient",
    "PowerwallConnectionError",
    "PowerwallEnergySite",
    "PowerwallError",
    "PowerwallFaultError",
    "PowerwallProtocolError",
    "PowerwallRateLimitError",
    "StatusPayload",
    "__version__",
    "backup_time_remaining",
    "battery_level",
    "battery_level_raw",
    "current_power",
    "raw_to_scaled_reserve",
    "raw_to_scaled_soc",
    "scaled_to_raw_reserve",
    "scaled_to_raw_soc",
]
