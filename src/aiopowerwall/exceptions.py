"""Typed exceptions raised by aiopowerwall."""

from __future__ import annotations


class PowerwallError(Exception):
    """Base class for all aiopowerwall errors."""


class PowerwallConnectionError(PowerwallError):
    """The Powerwall gateway could not be reached or the request timed out."""


class PowerwallAuthenticationError(PowerwallError):
    """Authentication with the Powerwall failed.

    Raised for HTTP 401/403 from `/api/login/Basic` or for v1r message faults
    that indicate an unregistered or inactive RSA key.
    """


class PowerwallRateLimitError(PowerwallError):
    """The Powerwall returned 429/503 — back off before retrying."""


class PowerwallProtocolError(PowerwallError):
    """The Powerwall returned an unexpected response (bad protobuf, JSON, etc.)."""


class PowerwallFaultError(PowerwallError):
    """A signed v1r request returned a non-NONE message_fault.

    The fault enum name is exposed via :pyattr:`fault`.
    """

    def __init__(self, fault: str, message: str | None = None) -> None:
        self.fault = fault
        super().__init__(message or f"Powerwall returned message fault: {fault}")
