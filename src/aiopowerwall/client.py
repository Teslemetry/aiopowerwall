"""High-level async Powerwall 3 client built on the TEDAPI v1r transport.

Typical use::

    from aiopowerwall import PowerwallClient

    async with PowerwallClient(
        host="192.168.91.1",
        gateway_password="...",
        rsa_private_key_pem=pem_bytes,
    ) as client:
        await client.connect()
        soc = await client.battery_level()
        status = await client.get_status()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Final, TypeVar, cast

import aiohttp
from google.protobuf.timestamp_pb2 import Timestamp

from . import queries
from .exceptions import (
    PowerwallError,
    PowerwallProtocolError,
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
from .proto import combined_pb2, tedapi_pb2
from .transport import V1rTransport

_LOGGER = logging.getLogger(__name__)

DEFAULT_GATEWAY_HOST: Final = "192.168.91.1"

_T = TypeVar("_T")


class _TTLCache:
    """Tiny per-key TTL cache with an asyncio lock to coalesce concurrent loads."""

    __slots__ = ("_data", "_locks", "_ttl")

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        ts, value = entry
        if (time.monotonic() - ts) >= self._ttl:
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


def _lookup(data: Any, *keys: str) -> Any:
    """Walk a nested mapping; return None if any key is missing."""
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


class PowerwallClient:
    """Async client for a Powerwall 3 gateway over `/tedapi/v1r`.

    The client is reentrant — concurrent calls to the same getter are
    coalesced through the TTL cache, and writes invalidate the relevant
    cache entries.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_GATEWAY_HOST,
        gateway_password: str,
        rsa_private_key_pem: bytes,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 5.0,
        cache_status_ttl: float = 5.0,
        cache_config_ttl: float = 30.0,
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
        self._fast_cache = _TTLCache(cache_status_ttl)
        self._slow_cache = _TTLCache(cache_config_ttl)

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

    async def get_battery_soe(self) -> float:
        """Return battery state-of-charge percentage (0-100)."""
        await self.connect()
        data = await self._transport.api_get("/api/system_status/soe")
        if not isinstance(data, Mapping) or "percentage" not in data:
            raise PowerwallProtocolError(
                f"Unexpected SoE payload: {data!r}"
            )
        return float(data["percentage"])

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

    async def get_config(self, *, force: bool = False) -> ConfigPayload:
        """Return the gateway configuration (`config.json`)."""
        return await self._cached(
            self._slow_cache, "config", force, self._fetch_config
        )

    async def get_status(self, *, force: bool = False) -> StatusPayload:
        """Return the gateway status (DeviceControllerQuery, narrow form)."""
        return await self._cached(
            self._fast_cache, "status", force, self._fetch_status
        )

    async def get_device_controller(
        self, *, force: bool = False
    ) -> ControllerPayload:
        """Return the extended DeviceControllerQuery payload (controller view)."""
        return await self._cached(
            self._fast_cache, "controller", force, self._fetch_controller
        )

    async def get_components(self, *, force: bool = False) -> ComponentsPayload:
        """Return Powerwall 3 device component data (PCH, BMS, HVP, …)."""
        return await self._cached(
            self._slow_cache, "components", force, self._fetch_components
        )

    async def get_firmware_version(
        self, *, details: bool = False, force: bool = False
    ) -> str | FirmwareDetails:
        """Return the gateway firmware version string, or a details dict."""
        cache_key = "firmware_details" if details else "firmware"
        cache = self._slow_cache

        async def _fetch() -> str | FirmwareDetails:
            return await self._fetch_firmware(details=details)

        return await self._cached(cache, cache_key, force, _fetch)

    # ── Convenience helpers (read-only) ─────────────────────────────────────

    async def battery_level(self, *, force: bool = False) -> float | None:
        """Battery state-of-charge as a percentage (0-100), or None if unknown.

        Computed from `nominalEnergyRemainingWh / nominalFullPackEnergyWh`
        in the cached status payload — for a directly-reported SoC, see
        :meth:`get_battery_soe`.
        """
        status = await self.get_status(force=force)
        remaining = _lookup(
            status, "control", "systemStatus", "nominalEnergyRemainingWh"
        )
        full = _lookup(status, "control", "systemStatus", "nominalFullPackEnergyWh")
        if not remaining or not full:
            return None
        return float(remaining) / float(full) * 100

    async def current_power(
        self,
        location: PowerLocation | str | None = None,
        *,
        force: bool = False,
    ) -> float | dict[str, float | None] | None:
        """Return real power for a meter location, or all locations if None."""
        status = await self.get_status(force=force)
        meters = _lookup(status, "control", "meterAggregates")
        if not isinstance(meters, list):
            return None
        power_map: dict[str, float | None] = {
            str(m.get("location", "")).upper(): m.get("realPowerW")
            for m in meters
            if isinstance(m, Mapping) and m.get("location") is not None
        }
        if location is None:
            return power_map
        return power_map.get(str(location).upper())

    async def backup_time_remaining(self, *, force: bool = False) -> float | None:
        """Estimated backup runtime in hours at current load, or None if unknown."""
        status = await self.get_status(force=force)
        remaining = _lookup(
            status, "control", "systemStatus", "nominalEnergyRemainingWh"
        )
        load = await self.current_power("LOAD", force=force)
        if not remaining or not isinstance(load, (int, float)) or load <= 0:
            return None
        return float(remaining) / float(load)

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

        # Successful write — invalidate any cached views of config.
        self._slow_cache.invalidate("config")

    async def schedule_max_backup(self, duration_seconds: int = 7200) -> None:
        """Schedule a manual "max backup" event (reserve set to 100%)."""
        if duration_seconds < 60:
            raise ValueError("duration_seconds must be at least 60")
        din = await self.connect()

        # The gateway requires any prior event to be cancelled before a new
        # one is scheduled — even an expired one.
        await self._send_teg_request(
            din,
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

        await self._send_teg_request(
            din,
            request_field="schedule_manual_backup_event_request",
            response_field="schedule_manual_backup_event_response",
            populate=populate,
        )

    async def cancel_max_backup(self) -> None:
        """Cancel the active manual backup event (no-op if none active)."""
        din = await self.connect()
        await self._send_teg_request(
            din,
            request_field="cancel_manual_backup_event_request",
            response_field="cancel_manual_backup_event_response",
            populate=lambda req: req.SetInParent(),
            allow_missing_response=True,
        )

    async def get_backup_events(self) -> BackupEventsPayload:
        """Return the active manual backup and any scheduled backup events."""
        din = await self.connect()
        response_envelope = await self._send_teg_request(
            din,
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

    # ── Internals: caching ──────────────────────────────────────────────────

    async def _cached(
        self,
        cache: _TTLCache,
        key: str,
        force: bool,
        loader: Callable[[], Awaitable[_T]],
    ) -> _T:
        if not force:
            cached = cache.get(key)
            if cached is not None:
                return cast(_T, cached)
        async with cache.lock(key):
            if not force:
                cached = cache.get(key)
                if cached is not None:
                    return cast(_T, cached)
            value = await loader()
            cache.set(key, value)
            return value

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

    async def _fetch_firmware(
        self, *, details: bool
    ) -> str | FirmwareDetails:
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

        version: str = response.firmware.system.version.text
        if not details:
            return version
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

    async def _send_teg_request(
        self,
        din: str,
        *,
        request_field: str,
        response_field: str,
        populate: Any,
        allow_missing_response: bool = False,
    ) -> combined_pb2.MessageEnvelope:
        """Issue a TEGMessages command and return the response envelope.

        The verbose builder approach (vs. inline construction) keeps the
        write helpers readable — TEG commands are small and uniform.
        """
        teg = combined_pb2.TEGMessages()
        populate(getattr(teg, request_field))

        envelope = combined_pb2.MessageEnvelope()
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = (
            combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
        )
        envelope.recipient.din = din
        envelope.teg.CopyFrom(teg)

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
                f"Malformed TEG response: {err}"
            ) from err
        if not response.HasField("teg") or not response.teg.HasField(response_field):
            if allow_missing_response:
                return response
            _LOGGER.warning(
                "TEG response missing %s (request=%s inner len=%d hex=%s parsed=%s)",
                response_field,
                request_field,
                len(inner),
                inner.hex(),
                str(response).replace("\n", " | "),
            )
            raise PowerwallProtocolError(
                f"TEG response missing {response_field}"
            )
        return response

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


__all__ = [
    "DEFAULT_GATEWAY_HOST",
    "PowerwallClient",
]
