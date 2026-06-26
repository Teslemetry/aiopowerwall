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
    PowerwallClient,
    backup_time_remaining,
    battery_level,
    battery_level_scaled,
    current_power,
    raw_to_scaled_reserve,
    raw_to_scaled_soc,
    scaled_to_raw_reserve,
    scaled_to_raw_soc,
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

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_GATEWAY_HOST",
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
    "PowerwallError",
    "PowerwallFaultError",
    "PowerwallProtocolError",
    "PowerwallRateLimitError",
    "StatusPayload",
    "__version__",
    "backup_time_remaining",
    "battery_level",
    "battery_level_scaled",
    "current_power",
    "raw_to_scaled_reserve",
    "raw_to_scaled_soc",
    "scaled_to_raw_reserve",
    "scaled_to_raw_soc",
]
