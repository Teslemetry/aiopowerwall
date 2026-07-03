"""Tesla Fleet API ``EnergySite``-compatible adapter over a local Powerwall.

:class:`PowerwallEnergySite` wraps a :class:`~aiopowerwall.client.PowerwallClient`
and presents the same *duck-typed* surface as the cloud ``EnergySite`` /
``TeslemetryEnergySite`` classes from ``tesla_fleet_api`` — the same method
names, signatures, and ``dict[str, Any]`` return shapes — **without importing
that package**. Compatibility is by convention so a primary/secondary energy
router (which lives in the separate ``tesla-fleet-api`` project) can treat this
LOCAL adapter and a cloud ``EnergySite`` as interchangeable, preferring the
local LAN path (TEDAPI v1r) and falling through to the cloud when the local
path is unavailable or a command is not yet implemented locally.

Design notes:

* **Implemented commands** map to the corresponding
  :class:`PowerwallClient` call and normalise the result into the cloud energy
  command envelope, ``{"response": {"code": 201, "message": "", "result":
  True}}``. Data reads (:meth:`get_backup_events`, :meth:`live_status`) wrap
  their payload under ``response`` the same way the cloud HTTP responses do.
* **Placeholder commands** (things the local v1r path cannot do yet) raise
  :class:`NotImplementedError` with a ``TODO``. They are scaffolded now so the
  full command surface exists and can be "wired up once implemented"; once the
  router supports per-command failover, a ``NotImplementedError`` cleanly falls
  back to the cloud ``EnergySite`` until the local path lands.
* :meth:`site_info` is deliberately **absent** — the router should always fall
  through to the cloud for it (we do not try to replicate it locally).
* :meth:`connect_if_needed` is a router health signal (not part of the cloud
  ``EnergySite`` surface): the router's default health check feature-detects it
  to decide whether the local primary is reachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .client import PowerwallClient, battery_level

# Island-mode wire values (mirror ``tesla_fleet_api.const.EnergyIslandMode`` by
# convention — we do not import it). Mode 6 opens the grid contactor
# (off-grid); mode 1 closes it (on-grid / reconnect).
ISLAND_MODE_ON_GRID: int = 1
ISLAND_MODE_OFF_GRID: int = 6

# Default duration for a manual "max backup" event when the caller does not
# specify one — matches :meth:`PowerwallClient.schedule_max_backup`.
_DEFAULT_BACKUP_DURATION_SECONDS: int = 7200

# Best-effort translation of the local gateway grid-status string into the
# cloud ``live_status`` ``(grid_status, island_status)`` pair. The cloud uses
# ``"Active"``/``"Inactive"`` for ``grid_status`` and
# ``"on_grid"``/``"off_grid"``/``"island_status_unknown"`` for ``island_status``.
_GRID_STATUS_MAP: dict[str, tuple[str, str]] = {
    "SystemGridConnected": ("Active", "on_grid"),
    "SystemIslandedActive": ("Inactive", "off_grid"),
    "SystemIslandedReady": ("Inactive", "off_grid"),
    "SystemTransitionToGrid": ("Active", "on_grid"),
    "SystemTransitionToIsland": ("Inactive", "off_grid"),
    "SystemMicroGridFaulted": ("Inactive", "off_grid"),
    "SystemWaitForUser": ("Inactive", "island_status_unknown"),
}
_GRID_STATUS_UNKNOWN: tuple[str, str] = ("Unknown", "island_status_unknown")


def _ok_response() -> dict[str, Any]:
    """Return a fresh cloud-style successful energy command envelope.

    Mirrors the shape the Tesla Fleet API returns for energy command POST
    endpoints (``/backup``, ``/operation``, …): a top-level ``response`` object
    carrying ``code``/``message``/``result``.
    """
    return {"response": {"code": 201, "message": "", "result": True}}


def _instant_power(meters: Mapping[str, Any], location: str) -> float | None:
    """Return ``meters[location].instant_power`` as a float, or None."""
    entry = meters.get(location)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("instant_power")
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _system_status_field(status: Mapping[str, Any], field: str) -> float | None:
    """Return ``status["control"]["systemStatus"][field]`` as a float, or None."""
    control = status.get("control")
    if not isinstance(control, Mapping):
        return None
    system_status = control.get("systemStatus")
    if not isinstance(system_status, Mapping):
        return None
    value = system_status.get(field)
    if not isinstance(value, (int, float)):
        return None
    return float(value)


class PowerwallEnergySite:
    """``EnergySite``-shaped adapter backed by a local :class:`PowerwallClient`.

    Instances are constructed from an existing, configured
    :class:`PowerwallClient`; the adapter never opens its own transport. Method
    names/signatures match the cloud ``EnergySite`` so a duck-typed router can
    use this as the local primary.
    """

    def __init__(self, client: PowerwallClient) -> None:
        self._client = client

    @property
    def powerwall(self) -> PowerwallClient:
        """The wrapped local client (named to avoid colliding with the router's
        ``client.is_connected`` health probe — see :meth:`connect_if_needed`)."""
        return self._client

    # ── Router health signal (not part of the cloud EnergySite surface) ──────

    async def connect_if_needed(self) -> None:
        """Ensure the local gateway is authenticated/reachable.

        Delegates to :meth:`PowerwallClient.connect` (idempotent). The router's
        default health check feature-detects this method: a successful return
        marks the local primary healthy, and any exception routes the call to
        the cloud fallback.
        """
        await self._client.connect()

    # ── Implemented commands (local mapping exists) ─────────────────────────

    async def operation(self, default_real_mode: str) -> dict[str, Any]:
        """Set the site operation mode (``self_consumption``/``autonomous``/
        ``backup``) — maps to :meth:`PowerwallClient.set_operation_mode`."""
        await self._client.set_operation_mode(str(default_real_mode))
        return _ok_response()

    async def backup(self, backup_reserve_percent: int) -> dict[str, Any]:
        """Set the backup reserve to a user-facing percentage (0-100) — maps to
        :meth:`PowerwallClient.set_backup_reserve` (which applies Tesla's
        user-facing→raw scaling)."""
        await self._client.set_backup_reserve(backup_reserve_percent)
        return _ok_response()

    async def set_island_mode(
        self, mode: int, force: bool | None = None
    ) -> dict[str, Any]:
        """Open/close the grid contactor — maps to
        :meth:`PowerwallClient.set_island_mode`.

        ``mode`` is :data:`ISLAND_MODE_OFF_GRID` (6) to island or
        :data:`ISLAND_MODE_ON_GRID` (1) to reconnect. ``force`` defaults to
        ``True`` for off-grid, ``False`` otherwise (matching the cloud).
        """
        mode_int = int(mode)
        off_grid = mode_int == ISLAND_MODE_OFF_GRID
        if force is None:
            force = off_grid
        await self._client.set_island_mode(
            off_grid=off_grid, force=force, mode_override=mode_int
        )
        return _ok_response()

    async def go_off_grid(self) -> dict[str, Any]:
        """Disconnect from the grid — maps to
        :meth:`PowerwallClient.go_off_grid`."""
        await self._client.go_off_grid()
        return _ok_response()

    async def reconnect_grid(self) -> dict[str, Any]:
        """Reconnect to the grid — maps to
        :meth:`PowerwallClient.reconnect_grid`."""
        await self._client.reconnect_grid()
        return _ok_response()

    async def schedule_backup_event(
        self,
        start_time: str | None = None,
        duration_seconds: int | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Schedule a manual (max) backup event — maps to
        :meth:`PowerwallClient.schedule_max_backup`.

        The local path schedules a manual backup starting immediately with a
        fixed (maximum) priority, so ``start_time`` and ``priority`` are
        accepted for signature compatibility but **not honoured** — the event
        always starts now at max priority. ``duration_seconds`` defaults to
        :data:`_DEFAULT_BACKUP_DURATION_SECONDS` when omitted.
        """
        await self._client.schedule_max_backup(
            duration_seconds
            if duration_seconds is not None
            else _DEFAULT_BACKUP_DURATION_SECONDS
        )
        return _ok_response()

    async def cancel_backup_event(self) -> dict[str, Any]:
        """Cancel the active manual backup event — maps to
        :meth:`PowerwallClient.cancel_max_backup`."""
        await self._client.cancel_max_backup()
        return _ok_response()

    async def get_backup_events(self) -> dict[str, Any]:
        """Return the active manual backup and scheduled backup events.

        Maps to :meth:`PowerwallClient.get_backup_events` and wraps the local
        payload under ``response`` (the cloud gRPC response shape is not
        reproducible verbatim locally, so we surface the local fields directly).
        """
        payload = await self._client.get_backup_events()
        return {
            "response": {
                "manual_backup": payload.get("manual_backup"),
                "backup_events": payload.get("backup_events", []),
            }
        }

    async def live_status(self) -> dict[str, Any]:
        """Return a best-effort cloud-shaped ``live_status`` payload.

        Built from data the local gateway already exposes — ``/api/meters/
        aggregates`` (instantaneous power per location), the gateway status
        query (user-facing state of charge plus nominal pack energy), and the
        grid-status string. The result mirrors the cloud
        ``live_status().response`` object.

        ``percentage_charged``, ``energy_left`` and ``total_pack_energy`` all
        come from the one :meth:`PowerwallClient.get_status` read: the
        user-facing SoC via :func:`~aiopowerwall.client.battery_level`, and
        the Wh figures directly from ``control.systemStatus`` — the same
        ``nominalEnergyRemainingWh`` / ``nominalFullPackEnergyWh`` fields that
        back :func:`~aiopowerwall.client.battery_level_raw`.

        Gaps: some cloud keys have no local v1r equivalent and are returned as
        ``None`` rather than guessed — ``backup_capable``,
        ``grid_services_active``, ``grid_services_power``, ``storm_mode_active``,
        ``timestamp`` and ``wall_connectors``.
        """
        meters = await self._client.get_meters_aggregates()
        status = await self._client.get_status()
        grid_status_raw = await self._client.get_grid_status()

        grid_status, island_status = _GRID_STATUS_MAP.get(
            grid_status_raw, _GRID_STATUS_UNKNOWN
        )

        return {
            "response": {
                "solar_power": _instant_power(meters, "solar"),
                "energy_left": _system_status_field(
                    status, "nominalEnergyRemainingWh"
                ),
                "total_pack_energy": _system_status_field(
                    status, "nominalFullPackEnergyWh"
                ),
                "percentage_charged": battery_level(status),
                "backup_capable": None,
                "battery_power": _instant_power(meters, "battery"),
                "load_power": _instant_power(meters, "load"),
                "grid_power": _instant_power(meters, "site"),
                "grid_services_power": None,
                "generator_power": _instant_power(meters, "generator"),
                "grid_status": grid_status,
                "grid_services_active": None,
                "island_status": island_status,
                "storm_mode_active": None,
                "timestamp": None,
                "wall_connectors": None,
            }
        }

    # ── Placeholder commands (no faithful local mapping yet) ────────────────
    #
    # These raise NotImplementedError on purpose: the full command surface is
    # scaffolded now so it can be wired up once a local implementation exists,
    # and a router with per-command failover treats NotImplementedError as a
    # signal to fall through to the cloud EnergySite.

    async def storm_mode(self, enabled: bool) -> dict[str, Any]:
        """TODO: not available over local v1r — storm-watch participation is a
        cloud/Tesla-account setting. Falls back to the cloud EnergySite."""
        raise NotImplementedError(
            "storm_mode is not available over the local Powerwall v1r path"
        )

    async def off_grid_vehicle_charging_reserve(
        self, off_grid_vehicle_charging_reserve_percent: int
    ) -> dict[str, Any]:
        """TODO: no local v1r equivalent yet for the off-grid vehicle charging
        reserve. Falls back to the cloud EnergySite."""
        raise NotImplementedError(
            "off_grid_vehicle_charging_reserve is not implemented locally yet"
        )

    async def grid_import_export(
        self,
        disallow_charge_from_grid_with_solar_installed: bool | None = None,
        customer_preferred_export_rule: str | None = None,
    ) -> dict[str, Any]:
        """TODO: map onto ``config.json`` writes (the export-rule /
        charge-from-grid keys) once the exact local fields are confirmed. Its
        semantics differ enough from ``curtail`` that mapping to that would be
        wrong, so this stays a placeholder and falls back to the cloud."""
        raise NotImplementedError(
            "grid_import_export is not implemented locally yet"
        )

    async def time_of_use_settings(
        self, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """TODO: map the tariff/time-of-use content onto local ``config.json``
        writes once the field mapping is confirmed. Falls back to the cloud."""
        raise NotImplementedError(
            "time_of_use_settings is not implemented locally yet"
        )

    # Historical data reads: the local gateway does not retain the time-series
    # history these return, so they fall back to the cloud.

    async def backup_history(
        self,
        period: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """TODO: local v1r does not retain backup (off-grid) event history."""
        raise NotImplementedError(
            "backup_history is not available over the local Powerwall v1r path"
        )

    async def charge_history(
        self,
        start_date: str,
        end_date: str,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """TODO: local v1r does not retain wall-connector charge history."""
        raise NotImplementedError(
            "charge_history is not available over the local Powerwall v1r path"
        )

    async def energy_history(
        self,
        period: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """TODO: local v1r does not retain aggregated energy history."""
        raise NotImplementedError(
            "energy_history is not available over the local Powerwall v1r path"
        )

    # Energy-device gRPC commands (common / authorization / TEG). These talk to
    # the gateway and are prime candidates to wire up over v1r later, but are
    # not implemented in aiopowerwall yet.

    async def get_system_info(self) -> dict[str, Any]:
        """TODO: wire up over v1r (firmware/serial/DIN are available via
        :meth:`PowerwallClient.get_firmware_details`, but the cloud gRPC
        response shape is not reproduced yet)."""
        raise NotImplementedError("get_system_info is not implemented locally yet")

    async def raw_networking_status(self) -> dict[str, Any]:
        """TODO: wire up the networking-status gRPC command over v1r."""
        raise NotImplementedError(
            "raw_networking_status is not implemented locally yet"
        )

    async def get_networking_status(self) -> dict[str, Any]:
        """TODO: wire up the networking-status gRPC command over v1r."""
        raise NotImplementedError(
            "get_networking_status is not implemented locally yet"
        )

    async def wifi_scan(self) -> dict[str, Any]:
        """TODO: wire up the WiFi-scan gRPC command over v1r."""
        raise NotImplementedError("wifi_scan is not implemented locally yet")

    async def get_device_cert(self) -> dict[str, Any]:
        """TODO: wire up the device-cert gRPC command over v1r."""
        raise NotImplementedError("get_device_cert is not implemented locally yet")

    async def get_cellular_info(self) -> dict[str, Any]:
        """TODO: wire up the cellular-info gRPC command over v1r."""
        raise NotImplementedError(
            "get_cellular_info is not implemented locally yet"
        )

    async def check_for_update(self) -> dict[str, Any]:
        """TODO: wire up the check-for-update gRPC command over v1r."""
        raise NotImplementedError(
            "check_for_update is not implemented locally yet"
        )

    async def check_for_update_urgency(self) -> dict[str, Any]:
        """TODO: wire up the update-urgency gRPC command over v1r."""
        raise NotImplementedError(
            "check_for_update_urgency is not implemented locally yet"
        )

    async def check_internet(self) -> dict[str, Any]:
        """TODO: wire up the check-internet gRPC command over v1r."""
        raise NotImplementedError("check_internet is not implemented locally yet")

    async def set_local_site_config(
        self, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """TODO: map onto local ``config.json`` writes once the request fields
        are confirmed."""
        raise NotImplementedError(
            "set_local_site_config is not implemented locally yet"
        )

    async def get_teg_config(self) -> dict[str, Any]:
        """TODO: the cloud TEG ``get_config`` gRPC response differs from the
        local ``config.json`` returned by :meth:`PowerwallClient.get_config`;
        not reproduced yet."""
        raise NotImplementedError("get_teg_config is not implemented locally yet")

    async def list_authorized_clients(self) -> dict[str, Any]:
        """TODO: wire up the authorized-clients gRPC command over v1r."""
        raise NotImplementedError(
            "list_authorized_clients is not implemented locally yet"
        )

    async def add_authorized_client(
        self,
        public_key: bytes | str | None = None,
        description: str | None = None,
        key_type: int | None = None,
        authorized_client_type: int | None = None,
    ) -> dict[str, Any]:
        """TODO: wire up key registration (``add_authorized_client_request``)
        over v1r."""
        raise NotImplementedError(
            "add_authorized_client is not implemented locally yet"
        )

    async def remove_authorized_client(
        self, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """TODO: wire up key removal over v1r."""
        raise NotImplementedError(
            "remove_authorized_client is not implemented locally yet"
        )

    async def get_signed_commands_public_key(self) -> dict[str, Any]:
        """TODO: wire up the signed-commands public-key gRPC command over v1r."""
        raise NotImplementedError(
            "get_signed_commands_public_key is not implemented locally yet"
        )


__all__ = [
    "ISLAND_MODE_OFF_GRID",
    "ISLAND_MODE_ON_GRID",
    "PowerwallEnergySite",
]
