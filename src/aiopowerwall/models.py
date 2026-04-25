"""TypedDict definitions for the JSON payloads returned by the TEDAPI.

These describe the shape of well-known fields the client uses or surfaces;
the gateway returns deeply nested objects with many more keys, so all of
these models are declared `total=False` and consumers should treat unknown
keys as opaque.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

PowerLocation = Literal[
    "BATTERY", "SITE", "LOAD", "SOLAR", "SOLAR_RGM", "GENERATOR", "CONDUCTOR"
]
"""Meter aggregate locations as reported by `control.meterAggregates[].location`."""


class FirmwareDetails(TypedDict, total=False):
    """Detailed firmware payload returned by `get_firmware_version(details=True)`."""

    system: dict[str, Any]


class BackupEvent(TypedDict, total=False):
    id: str
    name: str
    start_time: int
    duration_seconds: int
    priority: int


class ManualBackupInfo(TypedDict, total=False):
    start_time: int
    duration_seconds: int
    end_time: int
    active: bool
    priority: int


class BackupEventsPayload(TypedDict, total=False):
    """Result of :meth:`PowerwallClient.get_backup_events`."""

    manual_backup: ManualBackupInfo | None
    backup_events: list[BackupEvent]


# The Powerwall returns deeply nested config/status/components/controller
# payloads. These aliases give callers a stable named type without forcing
# us to enumerate every signal — for static analysis they behave like
# `dict[str, Any]` but are easier to grep for at call sites.

ConfigPayload = dict[str, Any]
StatusPayload = dict[str, Any]
ControllerPayload = dict[str, Any]
ComponentsPayload = dict[str, Any]

__all__ = [
    "BackupEvent",
    "BackupEventsPayload",
    "ComponentsPayload",
    "ConfigPayload",
    "ControllerPayload",
    "FirmwareDetails",
    "ManualBackupInfo",
    "PowerLocation",
    "StatusPayload",
]
