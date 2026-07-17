"""High-level async Powerwall 3 client built on the TEDAPI v1r transport.

Typical use::

    from aiopowerwall import PowerwallClient, battery_level

    async with PowerwallClient(
        host="192.168.91.1",
        gateway_password="...",
        rsa_private_key_pem=pem_bytes,
    ) as client:
        await client.connect()
        status = await client.get_status()
        soc = battery_level(status)

Every method on :class:`PowerwallClient` issues a fresh gateway request —
the library does not cache responses or coalesce concurrent calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, cast

import aiohttp
from google.protobuf.timestamp_pb2 import Timestamp

from . import queries
from .exceptions import (
    PowerwallError,
    PowerwallProtocolError,
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
    StatusPayload,
)
from .proto import combined_pb2, tedapi_pb2
from .transport import V1rTransport

_LOGGER = logging.getLogger(__name__)

DEFAULT_GATEWAY_HOST: Final = "192.168.91.1"

# Accepted values for ``default_real_mode`` (the gateway operation mode). A
# plain top-level string in ``config.json`` with no scaling — verified on PW3
# that a local v1r write of each value sticks and reads back verbatim.
OPERATION_MODES: Final = ("self_consumption", "autonomous", "backup")

# setIslandModeRequest mode values used by both PW2 and PW3.
_ISLAND_MODE_OFF_GRID: Final = 6
_ISLAND_MODE_ON_GRID: Final = 1

# Tesla scales the user-facing backup reserve (what the app and Fleet API
# show, 0-100) into the gateway's raw ``config.json`` value as
# ``raw = scaled * _RESERVE_SCALE + _RESERVE_OFFSET``. The bottom 5% is an
# inaccessible buffer, so app-0% maps to raw-5%. Verified on PW3 firmware
# across scaled→raw datapoints 5→9.75, 10→14.5, 20→24, 35→38.25, 60→62.
_RESERVE_SCALE: Final = 0.95
_RESERVE_OFFSET: Final = 5.0

# State-of-charge is reported locally on the raw physical pack scale
# (``get_battery_soe_raw`` / ``battery_level_raw``), but the Tesla app and
# Fleet API (``live_status.percentage_charged``) show a user-facing value with
# the same inaccessible bottom-5% buffer removed as backup reserve — i.e. the
# identical transform ``raw = scaled * 0.95 + 5``. Kept as separate constants
# (rather than reusing the reserve ones) because the two were verified
# independently and are conceptually distinct settings. Verified on PW3: local
# raw 52.7778 == Fleet ``percentage_charged`` 50.2924.
_SOC_SCALE: Final = 0.95
_SOC_OFFSET: Final = 5.0

# Wire-format constants for the hand-rolled setIslandMode / triggerIslanding
# envelopes — see :meth:`PowerwallClient._build_island_envelope`.
_WT_VARINT: Final = 0
_WT_LEN: Final = 2

# Field numbers within TEGMessages for islanding commands (verbatim from
# the Tesla protobuf schema, not present in our checked-in combined.proto).
_TEG_FIELD_SET_ISLAND_MODE_REQUEST: Final = 3
_TEG_FIELD_TRIGGER_ISLANDING_REQUEST: Final = 5

def _lookup(data: Any, *keys: str) -> Any:
    """Walk a nested mapping; return None if any key is missing."""
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _enum_suffix(enum_type: Any, value: int, prefix: str) -> str:
    """Return a protobuf enum value's name with its common ``prefix`` stripped."""
    name: str = enum_type.Name(value)
    return name.removeprefix(prefix)


class PowerwallClient:
    """Async client for a Powerwall 3 gateway over `/tedapi/v1r`.

    Every method issues a fresh gateway request — there is no response
    cache and no request coalescing. Callers that want freshness control
    or de-duplication should layer it on top.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_GATEWAY_HOST,
        gateway_password: str,
        rsa_private_key_pem: bytes,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._owns_session = session is None
        self._session = session or aiohttp.ClientSession()
        self._transport = V1rTransport(
            host=host,
            password=gateway_password,
            rsa_private_key_pem=rsa_private_key_pem,
            session=self._session,
            timeout=timeout,
        )
        self._din: str | None = None
        self._connect_lock = asyncio.Lock()

        # Pre-curtailment snapshot, captured by ``curtail`` and consumed
        # by ``restore_from_curtailment``.
        self._saved_real_mode: str | None = None
        self._saved_reserve_percent: int | None = None
        self._curtailment_active = False

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def __aenter__(self) -> PowerwallClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying aiohttp session if this client owns it."""
        if self._owns_session and not self._session.closed:
            await self._session.close()

    @property
    def host(self) -> str:
        return self._transport.host

    @property
    def din(self) -> str | None:
        """Gateway DIN, or None until :meth:`connect` succeeds."""
        return self._din

    async def connect(self) -> str:
        """Authenticate and fetch the gateway DIN. Idempotent."""
        din = self._din
        if din is not None:
            return din
        async with self._connect_lock:
            din = self._din
            if din is not None:
                return din
            await self._transport.login()
            din = await self._transport.fetch_din()
            self._din = din
            return din

    # ── Standard JSON endpoints (bearer-auth, no v1r signing) ───────────────

    async def get_meters_aggregates(self) -> dict[str, Any]:
        """Return `/api/meters/aggregates` (instantaneous power per location)."""
        await self.connect()
        data = await self._transport.api_get("/api/meters/aggregates")
        if not isinstance(data, dict):
            raise PowerwallProtocolError(
                f"meters/aggregates payload is not an object: {type(data).__name__}"
            )
        return cast(dict[str, Any], data)

    async def get_battery_soe_raw(self) -> float:
        """Return the **raw** physical state-of-charge percentage (0-100).

        This is ``/api/system_status/soe`` verbatim — the gateway's raw value
        in which the bottom 5% is an inaccessible buffer, so it reads higher
        than the Tesla app and Fleet API. Use :meth:`get_battery_soe` for the
        user-facing value.
        """
        await self.connect()
        data = await self._transport.api_get("/api/system_status/soe")
        if not isinstance(data, Mapping) or "percentage" not in data:
            raise PowerwallProtocolError(
                f"Unexpected SoE payload: {data!r}"
            )
        return float(data["percentage"])

    async def get_battery_soe(self) -> float:
        """Return the **user-facing** state-of-charge percentage (0-100).

        Matches what the Tesla app and Fleet API
        (`live_status.percentage_charged`) display.
        :meth:`get_battery_soe_raw` returns the gateway's raw physical value
        (bottom 5% is an inaccessible buffer); this applies
        :func:`raw_to_scaled_soc` to it — raw 5% maps to 0%.
        """
        return raw_to_scaled_soc(await self.get_battery_soe_raw())

    async def get_grid_status(self) -> str:
        """Return the gateway grid status string (e.g. ``SystemGridConnected``)."""
        await self.connect()
        data = await self._transport.api_get("/api/system_status/grid_status")
        if not isinstance(data, Mapping) or "grid_status" not in data:
            raise PowerwallProtocolError(
                f"Unexpected grid_status payload: {data!r}"
            )
        return str(data["grid_status"])

    # ── TEDAPI v1r reads ────────────────────────────────────────────────────

    async def get_din(self) -> str:
        """Return the cached DIN; calls :meth:`connect` if not yet known."""
        return await self.connect()

    async def get_config(self) -> ConfigPayload:
        """Return the gateway configuration (`config.json`)."""
        return await self._fetch_config()

    async def get_status(self) -> StatusPayload:
        """Return the gateway status (DeviceControllerQuery, narrow form)."""
        return await self._fetch_status()

    async def get_device_controller(self) -> ControllerPayload:
        """Return the extended DeviceControllerQuery payload (controller view)."""
        return await self._fetch_controller()

    async def get_components(self) -> ComponentsPayload:
        """Return Powerwall 3 device component data (PCH, BMS, HVP, …)."""
        return await self._fetch_components()

    async def get_firmware_details(self) -> FirmwareDetails:
        """Return the gateway firmware details (version, hashes, hardware)."""
        return await self._fetch_firmware()

    # ── TEDAPI v1r writes / commands ────────────────────────────────────────

    async def write_config(self, updates: Mapping[str, Any]) -> None:
        """Patch ``config.json`` with dotted-path updates (read-modify-write).

        Example::

            await client.write_config({"site_info.backup_reserve_percent": 5})

        Uses the gateway's optimistic-lock hash; raises
        :class:`PowerwallProtocolError` if the gateway rejects the update.
        """
        din = await self.connect()

        # Read current config + hash directly (bypassing the cache, since we
        # need the hash and the cache stores the parsed dict only).
        current_blob, current_hash = await self._read_filestore(
            din, "config.json"
        )
        try:
            config = json.loads(current_blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise PowerwallProtocolError(
                f"config.json blob is not valid JSON: {err}"
            ) from err

        for dotted_path, value in updates.items():
            self._apply_dotted_update(config, dotted_path, value)

        write_msg = combined_pb2.Message()
        envelope = write_msg.message
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = (
            combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
        )
        envelope.recipient.din = din
        update_req = envelope.filestore.updateFileRequest
        update_req.domain = combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        update_req.file.name = "config.json"
        update_req.file.blob = json.dumps(config).encode("utf-8")
        update_req.hash = current_hash

        inner = await self._transport.post_v1r(
            envelope.SerializeToString(), din
        )
        response = combined_pb2.MessageEnvelope()
        try:
            response.ParseFromString(inner)
        except Exception as err:
            raise PowerwallProtocolError(
                f"Malformed updateFile response: {err}"
            ) from err
        if not response.HasField("filestore"):
            raise PowerwallProtocolError(
                "updateFile response missing filestore payload"
            )

    async def set_backup_reserve(self, percent: float) -> None:
        """Set the backup reserve to a **user-facing** percentage (0-100).

        ``percent`` matches what the Tesla app and Fleet API display. Tesla
        scales it into the gateway's raw ``config.json`` value as
        ``raw = percent * 0.95 + 5`` (the bottom 5% is an inaccessible
        buffer); this method computes that raw value and writes it.

        Use :meth:`set_backup_reserve_raw` if you already have a raw value
        and want it written verbatim. Raises :class:`ValueError` if
        ``percent`` is outside 0-100.
        """
        raw = scaled_to_raw_reserve(percent)
        await self.write_config({"site_info.backup_reserve_percent": raw})

    async def set_backup_reserve_raw(self, percent: float) -> None:
        """Set the **raw** backup reserve value (gateway ``config.json`` scale).

        Writes ``site_info.backup_reserve_percent`` verbatim. The raw value
        is *not* what the Tesla app / Fleet API show — app-0% is raw-5%. Use
        :meth:`set_backup_reserve` for the user-facing scale. Raises
        :class:`ValueError` if ``percent`` is outside 0-100.
        """
        if not 0 <= percent <= 100:
            raise ValueError("percent must be between 0 and 100")
        await self.write_config({"site_info.backup_reserve_percent": percent})

    async def set_operation_mode(self, mode: str) -> None:
        """Set the gateway operation mode (``default_real_mode``).

        ``mode`` must be one of :data:`OPERATION_MODES` —
        ``self_consumption``, ``autonomous`` or ``backup``. The value is a
        plain top-level string in ``config.json`` (no scaling); this method
        writes it verbatim. Raises :class:`ValueError` for any other value.

        Mirrors what the Tesla app and Fleet API ``operation()`` set; the
        Fleet read-back is ``site_info.default_real_mode``.
        """
        if mode not in OPERATION_MODES:
            raise ValueError(
                f"mode must be one of {OPERATION_MODES!r}, got {mode!r}"
            )
        await self.write_config({"default_real_mode": mode})

    async def schedule_max_backup(self, duration_seconds: int = 7200) -> None:
        """Schedule a manual "max backup" event (reserve set to 100%)."""
        if duration_seconds < 60:
            raise ValueError("duration_seconds must be at least 60")
        din = await self.connect()

        # The gateway requires any prior event to be cancelled before a new
        # one is scheduled — even an expired one.
        await self._send_command_request(
            din,
            category="teg",
            message_cls=combined_pb2.TEGMessages,
            request_field="cancel_manual_backup_event_request",
            response_field="cancel_manual_backup_event_response",
            populate=lambda req: req.SetInParent(),
            allow_missing_response=True,
        )

        def populate(req: Any) -> None:
            req.scheduling_info.start_time.CopyFrom(
                Timestamp(seconds=int(time.time()))
            )
            req.scheduling_info.duration_seconds = duration_seconds
            # MAX_UINT64 — pre-empts any other scheduled event.
            req.scheduling_info.priority = (1 << 64) - 1

        await self._send_command_request(
            din,
            category="teg",
            message_cls=combined_pb2.TEGMessages,
            request_field="schedule_manual_backup_event_request",
            response_field="schedule_manual_backup_event_response",
            populate=populate,
        )

    async def cancel_max_backup(self) -> None:
        """Cancel the active manual backup event (no-op if none active)."""
        din = await self.connect()
        await self._send_command_request(
            din,
            category="teg",
            message_cls=combined_pb2.TEGMessages,
            request_field="cancel_manual_backup_event_request",
            response_field="cancel_manual_backup_event_response",
            populate=lambda req: req.SetInParent(),
            allow_missing_response=True,
        )

    async def set_island_mode(
        self,
        *,
        off_grid: bool,
        force: bool = True,
        mode_override: int | None = None,
    ) -> None:
        """Send a ``setIslandModeRequest`` to the gateway via local v1r.

        ``mode=6`` requests off-grid (contactor open); ``mode=1`` requests
        grid reconnect. ``force=True`` is required on off-grid — without it
        the gateway acknowledges the message but does not operate the
        contactor.

        .. warning:: The PowerSync project reports that local v1r returns a
            success response for this command but **does not actuate the
            contactor** — only the Fleet-API cloud relay path actually
            islands the gateway. This implementation issues the local
            command verbatim; verify the contactor state with
            :meth:`get_status` (``islanding.contactorClosed``) before
            relying on it. See :meth:`trigger_islanding` for the explicit
            black-start command.

        Use :meth:`go_off_grid` / :meth:`reconnect_grid` for the
        higher-level convenience wrappers.
        """
        din = await self.connect()
        mode = (
            mode_override
            if mode_override is not None
            else (_ISLAND_MODE_OFF_GRID if off_grid else _ISLAND_MODE_ON_GRID)
        )

        teg_payload = self._build_set_island_mode_teg(mode=mode, force=force)
        envelope_bytes = self._build_command_envelope(din, teg_payload)

        _LOGGER.info(
            "set_island_mode: mode=%d force=%s din=%s", mode, force, din
        )
        inner = await self._transport.post_v1r(envelope_bytes, din)
        # The response envelope echoes a TEGAPISetIslandModeResponse with
        # ``result`` (int32) — but our checked-in TEGMessages doesn't model
        # those fields, so a successful post (no fault) is the strongest
        # local confirmation available.
        _LOGGER.debug(
            "set_island_mode: gateway returned %d bytes (no protobuf parse)",
            len(inner),
        )

    async def go_off_grid(
        self,
        *,
        force: bool = True,
        mode_override: int | None = None,
    ) -> None:
        """Disconnect from the grid (request contactor open) via local v1r.

        Thin wrapper around :meth:`set_island_mode` — see that method's
        notes about cloud-relay vs local-only command behaviour.
        """
        await self.set_island_mode(
            off_grid=True, force=force, mode_override=mode_override
        )

    async def reconnect_grid(self) -> None:
        """Reconnect to the grid (request contactor close) via local v1r."""
        await self.set_island_mode(off_grid=False, force=False)

    async def trigger_islanding(self) -> None:
        """Send ``triggerIslandingBlackStartRequest`` via local v1r.

        ``setIslandModeRequest`` only updates the desired mode preference;
        ``triggerIslandingBlackStartRequest`` is the explicit black-start
        command that drives the full islanding transition (grid-frequency
        ramp-down, contactor open, inverter restart in island mode). Try
        this if :meth:`go_off_grid` does not actually disconnect.
        """
        din = await self.connect()
        teg_payload = self._build_teg_field(
            _TEG_FIELD_TRIGGER_ISLANDING_REQUEST, b""
        )
        envelope_bytes = self._build_command_envelope(din, teg_payload)

        _LOGGER.info("trigger_islanding: din=%s", din)
        inner = await self._transport.post_v1r(envelope_bytes, din)
        _LOGGER.debug(
            "trigger_islanding: gateway returned %d bytes", len(inner)
        )

    async def curtail(self, *, reserve_percent: int = 100) -> None:
        """Stop grid export by switching to ``backup`` mode + high reserve.

        Saves the current ``default_real_mode`` and
        ``site_info.backup_reserve_percent`` so that
        :meth:`restore_from_curtailment` can put them back. Uses a config
        write — no contactor cycling, no solar dropout, but takes ~90s for
        the gateway to apply the change.
        """
        if not 0 <= reserve_percent <= 100:
            raise ValueError("reserve_percent must be between 0 and 100")

        config = await self.get_config()
        self._saved_real_mode = str(
            config.get("default_real_mode") or "self_consumption"
        )
        site_info = config.get("site_info")
        saved_reserve = (
            site_info.get("backup_reserve_percent")
            if isinstance(site_info, Mapping)
            else None
        )
        try:
            self._saved_reserve_percent = (
                int(saved_reserve) if saved_reserve is not None else 5
            )
        except (TypeError, ValueError):
            self._saved_reserve_percent = 5

        _LOGGER.info(
            "curtail: saving mode=%s reserve=%s%% → backup/%s%%",
            self._saved_real_mode,
            self._saved_reserve_percent,
            reserve_percent,
        )
        await self.write_config(
            {
                "default_real_mode": "backup",
                "site_info.backup_reserve_percent": reserve_percent,
            }
        )
        self._curtailment_active = True

    async def restore_from_curtailment(self) -> None:
        """Restore the operation mode + reserve captured by :meth:`curtail`.

        No-op safe to call when curtailment was never engaged — falls back
        to ``self_consumption`` + 5% if no pre-curtailment state is stored.
        """
        mode = self._saved_real_mode or "self_consumption"
        reserve = (
            self._saved_reserve_percent
            if self._saved_reserve_percent is not None
            else 5
        )
        _LOGGER.info("restore: writing mode=%s reserve=%s%%", mode, reserve)
        await self.write_config(
            {
                "default_real_mode": mode,
                "site_info.backup_reserve_percent": reserve,
            }
        )
        self._curtailment_active = False

    @property
    def curtailment_active(self) -> bool:
        """True between :meth:`curtail` and :meth:`restore_from_curtailment`."""
        return self._curtailment_active

    async def get_backup_events(self) -> BackupEventsPayload:
        """Return the active manual backup and any scheduled backup events."""
        din = await self.connect()
        response_envelope = await self._send_command_request(
            din,
            category="teg",
            message_cls=combined_pb2.TEGMessages,
            request_field="get_backup_events_request",
            response_field="get_backup_events_response",
            populate=lambda req: req.SetInParent(),
        )
        events_resp = response_envelope.teg.get_backup_events_response

        manual: ManualBackupInfo | None = None
        if events_resp.HasField("manual_backup_event"):
            scheduling = events_resp.manual_backup_event.scheduling_info
            end_time = scheduling.start_time.seconds + scheduling.duration_seconds
            manual = {
                "start_time": scheduling.start_time.seconds,
                "duration_seconds": scheduling.duration_seconds,
                "end_time": end_time,
                "active": int(time.time()) < end_time,
                "priority": scheduling.priority,
            }

        scheduled: list[BackupEvent] = [
            {
                "id": evt.id,
                "name": evt.name,
                "start_time": evt.scheduling_info.start_time.seconds,
                "duration_seconds": evt.scheduling_info.duration_seconds,
                "priority": evt.scheduling_info.priority,
            }
            for evt in events_resp.backup_events
        ]

        return {"manual_backup": manual, "backup_events": scheduled}

    async def list_authorized_clients(self) -> AuthorizedClientsPayload:
        """Return the authorized clients (paired keys) registered with the gateway."""
        din = await self.connect()
        response_envelope = await self._send_command_request(
            din,
            category="authorization",
            message_cls=combined_pb2.AuthorizationMessages,
            request_field="list_authorized_clients_request",
            response_field="list_authorized_clients_response",
            populate=lambda req: req.SetInParent(),
        )
        clients_resp = response_envelope.authorization.list_authorized_clients_response

        clients: list[AuthorizedClient] = [
            {
                "public_key": base64.b64encode(rec.public_key).decode("ascii"),
                "state": _enum_suffix(
                    combined_pb2.AuthorizedState, rec.state, "AUTHORIZED_STATE_"
                ),
                "type": _enum_suffix(
                    combined_pb2.AuthorizedClientType,
                    rec.type,
                    "AUTHORIZED_CLIENT_TYPE_",
                ),
                "description": rec.description,
                "key_type": _enum_suffix(
                    combined_pb2.AuthorizedKeyType,
                    rec.key_type,
                    "AUTHORIZED_KEY_TYPE_",
                ),
                "roles": [
                    _enum_suffix(
                        combined_pb2.AuthorizationRole, role, "AUTHORIZATION_ROLE_"
                    )
                    for role in rec.roles
                ],
                "verification": _enum_suffix(
                    combined_pb2.AuthorizedVerificationType,
                    rec.verification,
                    "AUTHORIZED_VERIFICATION_TYPE_",
                ),
                "added_time": (
                    rec.added_time.seconds if rec.HasField("added_time") else None
                ),
                "identifier": (
                    rec.identifier if rec.HasField("identifier") else None
                ),
                "authorized_by_public_key": (
                    base64.b64encode(rec.authorized_by_public_key).decode("ascii")
                    if rec.HasField("authorized_by_public_key")
                    else None
                ),
            }
            for rec in clients_resp.clients
        ]
        return {
            "clients": clients,
            "enable_line_switch_off": clients_resp.enable_line_switch_off,
        }

    # ── Internals: query builders ───────────────────────────────────────────

    async def _query_graphql(
        self, query_text: str, code: bytes, variables: str
    ) -> str:
        """Run a GraphQL DeviceController/Components query and return raw JSON."""
        din = await self.connect()
        msg = tedapi_pb2.Message()
        envelope = msg.message
        envelope.deliveryChannel = 1
        envelope.sender.local = 1
        envelope.recipient.din = din
        envelope.payload.send.num = 2
        envelope.payload.send.payload.value = 1
        envelope.payload.send.payload.text = query_text
        envelope.payload.send.code = code
        envelope.payload.send.b.value = variables
        msg.tail.value = 1

        inner = await self._transport.post_v1r(
            envelope.SerializeToString(), din
        )
        return self._parse_graphql_response(inner)

    @staticmethod
    def _parse_graphql_response(inner_bytes: bytes) -> str:
        """Extract the JSON text payload from a v1r GraphQL response.

        v1r returns a bare ``MessageEnvelope`` (no outer ``Message`` wrapper).
        """
        envelope = tedapi_pb2.MessageEnvelope()
        try:
            envelope.ParseFromString(inner_bytes)
        except Exception as err:
            raise PowerwallProtocolError(
                f"Malformed v1r query response: {err}"
            ) from err
        if not envelope.HasField("payload"):
            _LOGGER.warning(
                "v1r query response missing payload (inner len=%d parsed=%s)",
                len(inner_bytes),
                str(envelope).replace("\n", " | "),
            )
            raise PowerwallProtocolError("v1r query response missing payload")
        text: str = envelope.payload.recv.text
        if not text:
            raise PowerwallProtocolError("v1r query response payload is empty")
        return text

    async def _fetch_status(self) -> StatusPayload:
        text = await self._query_graphql(
            queries.STATUS_QUERY, queries.STATUS_CODE, "{}"
        )
        return self._json_payload(text, what="status")

    async def _fetch_controller(self) -> ControllerPayload:
        text = await self._query_graphql(
            queries.CONTROLLER_QUERY,
            queries.CONTROLLER_CODE,
            queries.CONTROLLER_VARIABLES,
        )
        return self._json_payload(text, what="device_controller")

    async def _fetch_components(self) -> ComponentsPayload:
        text = await self._query_graphql(
            queries.COMPONENTS_QUERY,
            queries.COMPONENTS_CODE,
            queries.COMPONENTS_VARIABLES,
        )
        return self._json_payload(text, what="components")

    async def _fetch_config(self) -> ConfigPayload:
        din = await self.connect()
        blob, _hash = await self._read_filestore(din, "config.json")
        try:
            data = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise PowerwallProtocolError(
                f"config.json blob is not valid JSON: {err}"
            ) from err
        if not isinstance(data, dict):
            raise PowerwallProtocolError("config.json is not an object")
        # Older firmwares occasionally omit `battery_blocks`; downstream
        # code in HA expects it to exist.
        data.setdefault("battery_blocks", [])
        return data

    async def _fetch_firmware(self) -> FirmwareDetails:
        din = await self.connect()
        msg = tedapi_pb2.Message()
        envelope = msg.message
        envelope.deliveryChannel = 1
        envelope.sender.local = 1
        envelope.recipient.din = din
        envelope.firmware.request = ""
        msg.tail.value = 1

        inner = await self._transport.post_v1r(
            envelope.SerializeToString(), din
        )

        # v1r returns a MessageEnvelope (no Message wrapper).
        response = tedapi_pb2.MessageEnvelope()
        try:
            response.ParseFromString(inner)
        except Exception as err:
            raise PowerwallProtocolError(
                f"Malformed firmware response: {err}"
            ) from err

        return {
            "system": {
                "gateway": {
                    "partNumber": response.firmware.system.gateway.partNumber,
                    "serialNumber": response.firmware.system.gateway.serialNumber,
                },
                "din": response.firmware.system.din,
                "version": {
                    "text": response.firmware.system.version.text,
                    "githash": response.firmware.system.version.githash,
                },
                "five": response.firmware.system.five.d,
                "six": response.firmware.system.six,
                "wireless": {"device": []},
            }
        }

    @staticmethod
    def _json_payload(text: str, *, what: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise PowerwallProtocolError(
                f"{what} payload is not valid JSON: {err}"
            ) from err
        if not isinstance(data, dict):
            raise PowerwallProtocolError(
                f"{what} payload is not an object: {type(data).__name__}"
            )
        return data

    # ── Internals: FileStore + TEG ──────────────────────────────────────────

    async def _read_filestore(self, din: str, name: str) -> tuple[bytes, bytes]:
        """Issue a FileStore read request and return ``(blob, hash)``."""
        msg = combined_pb2.Message()
        envelope = msg.message
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = (
            combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
        )
        envelope.recipient.din = din
        envelope.filestore.readFileRequest.domain = (
            combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        )
        envelope.filestore.readFileRequest.name = name

        inner = await self._transport.post_v1r(
            envelope.SerializeToString(), din
        )
        response = combined_pb2.MessageEnvelope()
        try:
            response.ParseFromString(inner)
        except Exception as err:
            raise PowerwallProtocolError(
                f"Malformed FileStore response: {err}"
            ) from err
        if not response.HasField("filestore"):
            _LOGGER.warning(
                "FileStore response missing filestore payload "
                "(name=%s inner len=%d hex=%s parsed=%s)",
                name,
                len(inner),
                inner.hex(),
                str(response).replace("\n", " | "),
            )
            raise PowerwallProtocolError(
                "FileStore response missing filestore payload"
            )
        read_resp = response.filestore.readFileResponse
        return read_resp.file.blob, read_resp.hash

    async def _send_command_request(
        self,
        din: str,
        *,
        category: str,
        message_cls: Any,
        request_field: str,
        response_field: str,
        populate: Any,
        allow_missing_response: bool = False,
    ) -> combined_pb2.MessageEnvelope:
        """Issue a ``MessageEnvelope.<category>`` command and return the response.

        ``category`` is the ``MessageEnvelope`` oneof field to populate/read
        (``"teg"``, ``"authorization"``, …) and ``message_cls`` is the
        corresponding ``*Messages`` class. The verbose builder approach (vs.
        inline construction) keeps the write helpers readable — these
        commands are small and uniform.
        """
        command = message_cls()
        populate(getattr(command, request_field))

        envelope = combined_pb2.MessageEnvelope()
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = (
            combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
        )
        envelope.recipient.din = din
        getattr(envelope, category).CopyFrom(command)

        try:
            inner = await self._transport.post_v1r(
                envelope.SerializeToString(), din
            )
        except PowerwallError:
            if allow_missing_response:
                # cancel-on-empty: tolerate "nothing to cancel" responses.
                return combined_pb2.MessageEnvelope()
            raise

        response = combined_pb2.MessageEnvelope()
        try:
            response.ParseFromString(inner)
        except Exception as err:
            raise PowerwallProtocolError(
                f"Malformed {category} response: {err}"
            ) from err
        if not response.HasField(category) or not getattr(
            response, category
        ).HasField(response_field):
            if allow_missing_response:
                return response
            _LOGGER.warning(
                "%s response missing %s (request=%s inner len=%d hex=%s parsed=%s)",
                category,
                response_field,
                request_field,
                len(inner),
                inner.hex(),
                str(response).replace("\n", " | "),
            )
            raise PowerwallProtocolError(
                f"{category} response missing {response_field}"
            )
        return response

    # ── Internals: hand-rolled wire encoders for islanding commands ─────────
    #
    # ``setIslandModeRequest`` and ``triggerIslandingBlackStartRequest`` live
    # at ``TEGMessages`` fields 3 and 5 in Tesla's schema — neither is
    # declared in our checked-in ``tedapi_combined.proto`` (which only models
    # the backup-event oneof at fields 45-50). Rather than regenerate the pb2
    # module, we emit raw protobuf wire bytes for the islanding payload and
    # let combined_pb2 handle the outer signing wrapper.

    @staticmethod
    def _varint(value: int) -> bytes:
        out = bytearray()
        while value >= 0x80:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value & 0x7F)
        return bytes(out)

    @classmethod
    def _wire_field(cls, field_num: int, wire: int, body: bytes) -> bytes:
        return cls._varint((field_num << 3) | wire) + body

    @classmethod
    def _field_varint(cls, field_num: int, value: int) -> bytes:
        return cls._wire_field(field_num, _WT_VARINT, cls._varint(value))

    @classmethod
    def _field_bytes(cls, field_num: int, value: bytes) -> bytes:
        return cls._wire_field(
            field_num, _WT_LEN, cls._varint(len(value)) + value
        )

    @classmethod
    def _field_string(cls, field_num: int, value: str) -> bytes:
        return cls._field_bytes(field_num, value.encode("utf-8"))

    @classmethod
    def _build_teg_field(cls, field_num: int, inner: bytes) -> bytes:
        """Encode TEGMessages with a single length-delimited submessage."""
        return cls._field_bytes(field_num, inner)

    @classmethod
    def _build_set_island_mode_teg(cls, *, mode: int, force: bool) -> bytes:
        """Encode TEGMessages.setIslandModeRequest{mode, force} bytes."""
        req = cls._field_varint(1, mode) + cls._field_varint(2, 1 if force else 0)
        return cls._build_teg_field(_TEG_FIELD_SET_ISLAND_MODE_REQUEST, req)

    @classmethod
    def _build_command_envelope(cls, din: str, teg_payload: bytes) -> bytes:
        """Encode a MessageEnvelope carrying ``teg_payload`` as the oneof.

        Mirrors the layout we'd otherwise build with
        ``combined_pb2.MessageEnvelope`` — ``deliveryChannel=HERMES_COMMAND``,
        ``sender.authorizedClient=CUSTOMER_MOBILE_APP``,
        ``recipient.din=<din>`` — except the ``teg`` payload is supplied as
        raw bytes since our pb2 doesn't model the islanding fields.
        """
        # Participant.sender — oneof id, field 4 = authorizedClient (varint).
        sender = cls._field_varint(4, 1)
        # Participant.recipient — oneof id, field 1 = din (string).
        recipient = cls._field_string(1, din)
        return (
            cls._field_varint(1, 2)  # deliveryChannel = HERMES_COMMAND
            + cls._field_bytes(2, sender)
            + cls._field_bytes(3, recipient)
            + cls._field_bytes(5, teg_payload)  # MessageEnvelope.teg
        )

    @staticmethod
    def _apply_dotted_update(
        config: dict[str, Any], dotted_path: str, value: Any
    ) -> None:
        keys = dotted_path.split(".")
        target = config
        for key in keys[:-1]:
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
                target[key] = existing
            target = existing
        target[keys[-1]] = value


def scaled_to_raw_reserve(scaled_percent: float) -> float:
    """Convert a user-facing reserve % (Tesla app / Fleet API) to the raw value.

    ``raw = scaled * 0.95 + 5``. The user-facing scale is 0-100; the bottom
    5% is an inaccessible buffer, so 0% user-facing is raw 5%. Raises
    :class:`ValueError` if ``scaled_percent`` is outside 0-100.
    """
    if not 0 <= scaled_percent <= 100:
        raise ValueError("scaled_percent must be between 0 and 100")
    return round(scaled_percent * _RESERVE_SCALE + _RESERVE_OFFSET, 4)


def raw_to_scaled_reserve(raw_percent: float) -> float:
    """Convert a gateway raw reserve value to the user-facing reserve %.

    Inverse of :func:`scaled_to_raw_reserve`: ``scaled = (raw - 5) / 0.95``.
    A raw value of 5 maps to user-facing 0%.
    """
    return round((raw_percent - _RESERVE_OFFSET) / _RESERVE_SCALE, 4)


def scaled_to_raw_soc(scaled_percent: float) -> float:
    """Convert a user-facing SoC % (Tesla app / Fleet API) to the raw value.

    ``raw = scaled * 0.95 + 5``. The user-facing scale is 0-100; the bottom
    5% is an inaccessible buffer, so user-facing 0% is raw 5%. Raises
    :class:`ValueError` if ``scaled_percent`` is outside 0-100.
    """
    if not 0 <= scaled_percent <= 100:
        raise ValueError("scaled_percent must be between 0 and 100")
    return round(scaled_percent * _SOC_SCALE + _SOC_OFFSET, 4)


def raw_to_scaled_soc(raw_percent: float) -> float:
    """Convert a gateway raw SoC value to the user-facing SoC %.

    Inverse of :func:`scaled_to_raw_soc`: ``scaled = (raw - 5) / 0.95``. A raw
    value of 5 maps to user-facing 0%. This is the transform that turns the
    local ``get_battery_soe_raw`` / ``battery_level_raw`` reading into the
    figure the Tesla app and Fleet API (`live_status.percentage_charged`) show.
    """
    return round((raw_percent - _SOC_OFFSET) / _SOC_SCALE, 4)


def battery_level_raw(status: StatusPayload) -> float | None:
    """Raw physical battery SoC % (0-100) from a status payload, or None.

    Computed from ``nominalEnergyRemainingWh / nominalFullPackEnergyWh``
    in a status payload returned by :meth:`PowerwallClient.get_status`. This
    is the **raw** physical value (bottom 5% is an inaccessible buffer); use
    :func:`battery_level` for the user-facing value the Tesla app and Fleet
    API show.
    """
    remaining = _lookup(
        status, "control", "systemStatus", "nominalEnergyRemainingWh"
    )
    full = _lookup(status, "control", "systemStatus", "nominalFullPackEnergyWh")
    if not remaining or not full:
        return None
    return float(remaining) / float(full) * 100


def battery_level(status: StatusPayload) -> float | None:
    """User-facing battery SoC % (0-100) from a status payload, or None.

    Matches what the Tesla app and Fleet API
    (`live_status.percentage_charged`) show. :func:`battery_level_raw`
    returns the underlying raw physical value (bottom 5% is an inaccessible
    buffer); this applies :func:`raw_to_scaled_soc` to it. Returns None when
    :func:`battery_level_raw` cannot determine the level.
    """
    raw = battery_level_raw(status)
    if raw is None:
        return None
    return raw_to_scaled_soc(raw)


def current_power(status: StatusPayload) -> dict[str, float | None]:
    """Return the real-power map (location → watts) from a status payload.

    Locations are upper-cased meter names (``LOAD``, ``SITE``, ``SOLAR``,
    ``BATTERY``). Missing or malformed meter aggregates yield an empty dict.
    """
    meters = _lookup(status, "control", "meterAggregates")
    if not isinstance(meters, list):
        return {}
    return {
        str(m.get("location", "")).upper(): m.get("realPowerW")
        for m in meters
        if isinstance(m, Mapping) and m.get("location") is not None
    }


def backup_time_remaining(status: StatusPayload) -> float | None:
    """Estimated backup runtime in hours at current load, or None if unknown."""
    remaining = _lookup(
        status, "control", "systemStatus", "nominalEnergyRemainingWh"
    )
    load = current_power(status).get("LOAD")
    if not remaining or not isinstance(load, (int, float)) or load <= 0:
        return None
    return float(remaining) / float(load)


__all__ = [
    "DEFAULT_GATEWAY_HOST",
    "OPERATION_MODES",
    "PowerwallClient",
    "backup_time_remaining",
    "battery_level",
    "battery_level_raw",
    "current_power",
    "raw_to_scaled_reserve",
    "raw_to_scaled_soc",
    "scaled_to_raw_reserve",
    "scaled_to_raw_soc",
]
