# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Dev commands

- `uv sync --extra dev` then `uv run pytest`, `uv run mypy`, `uv run ruff check .`.
- `ruff check` + `mypy` (strict, `files = ["src/aiopowerwall"]`) + `pytest` (asyncio auto
  mode) are the gates. `ruff format` is **not** the repo convention — existing source is
  not `ruff format`-clean, so don't reformat.

## EnergySite compat adapter (`src/aiopowerwall/energysite.py`)

`PowerwallEnergySite` wraps a `PowerwallClient` to present the Tesla Fleet API
`EnergySite` surface by **convention (duck typing)** so a primary/secondary energy
router can use the local LAN path as primary and a cloud `EnergySite` as fallback.
Deliberate conventions to preserve:

- **No dependency on `tesla_fleet_api`** — compatibility is by matching method names,
  signatures, and `dict[str, Any]` return shapes only. Do not add the import.
- **Command return shape**: implemented commands return the cloud energy command
  envelope `{"response": {"code": 201, "message": "", "result": True}}` (`_ok_response()`).
  Data reads (`get_backup_events`, `live_status`, `list_authorized_clients`) wrap their
  payload under `response`.
- **`site_info` is intentionally absent** (not a stub) so the router falls through to the
  cloud for it. Do not add it.
- **Placeholders vs omission**: everything with no faithful local mapping yet raises
  `NotImplementedError` with a `TODO` (so the full command surface is scaffolded and a
  per-command-failover router falls back to cloud). Only `site_info` is omitted entirely.
- `schedule_backup_event` maps to `schedule_max_backup`: `start_time`/`priority` are
  accepted for signature parity but **not honoured** (event starts now at max priority).
- `live_status` is best-effort from meters aggregates + gateway status + grid status.
  `percentage_charged`, `energy_left`, and `total_pack_energy` all come from a single
  `PowerwallClient.get_status()` read: SoC via `battery_level()`, and the Wh figures
  directly from `control.systemStatus.{nominalEnergyRemainingWh,nominalFullPackEnergyWh}`
  (the same fields `battery_level_raw()` uses). Prefer `get_status()` + `battery_level()`
  over a separate `get_battery_soe()` call so the Wh fields come along for free. Keys
  with no local v1r equivalent (`backup_capable`, `grid_services_*`, `storm_mode_active`,
  `timestamp`, `wall_connectors`) are returned as `None` rather than guessed.
  Grid-status→`(grid_status, island_status)` translation is a best-effort map
  (`_GRID_STATUS_MAP`).
- `connect_if_needed` is not part of the cloud `EnergySite`; it's the router's health
  signal (delegates to `PowerwallClient.connect`).
- The reference router (`tesla_fleet_api.tesla.router.Router`, sibling `python-tesla-fleet-api`
  project) dispatches by `hasattr(backend, name)` per call, not a fixed interface — a
  backend that omits a method entirely is just skipped (no error), while one that defines
  it and raises is retried on the next backend. That's why placeholders raise
  `NotImplementedError` rather than being left undefined: it keeps the full command
  surface scaffolded for later wiring while still falling through to the cloud today.
- `grid_import_export` maps to `PowerwallClient.set_grid_import_export`, which writes
  `site_info.customer_preferred_export_rule` (`GRID_EXPORT_RULES`: `battery_ok`/
  `pv_only`/`never`) and/or `site_info.disallow_charge_from_grid_with_solar_installed` in
  one `write_config` call. **Do not route `operation`/`backup`/`grid_import_export` to
  different backends in a router** — they share the same `config.json` document, and
  splitting them across local/cloud lets one write stomp or race the other. When a
  `config.json` field name isn't directly verified against hardware, cross-check it
  against `jasonacox/pypowerwall`'s v1r write path
  (`pypowerwall/tedapi/pypowerwall_tedapi.py`, `set_grid_export`/`set_grid_charging`) —
  same Tesla local gateway schema, actively maintained.

## v1r local login (`src/aiopowerwall/transport.py`)

`PowerwallClient(gateway_password=...)` takes the **full** gateway/WiFi password.
`V1rTransport` derives the actual `/api/login/Basic` "customer" password by
truncating to the last 5 characters (`_customer_password`) - this is a real,
undocumented Tesla gateway convention (also auto-derived by `jasonacox/pypowerwall`'s
v1r path), not a library quirk. Do not require callers to pass the pre-truncated
value; the full password is what's documented (README Quick start,
constructor docstring) and callers already have it.

## TEDAPI protobuf schema: `tesla-protocol` + local `tedapi.proto`

The TEG / FileStore / Authorization / signing schema (`transport_pb2`, `teg_api_pb2`,
`authorization_api_pb2`, `authorization_types_pb2`, `filestore_api_pb2`,
`signed_message_pb2`) comes from the published `tesla-protocol` PyPI package
(`tesla_protocol.energy_device`), pinned to an exact version in `pyproject.toml` —
this library carries no copy of that schema. `src/aiopowerwall/proto/tedapi.proto` /
`tedapi_pb2.py` is the one schema still checked in locally: it models the older
`tedapi` package (GraphQL query send/recv, firmware request/response) that
`tesla-protocol` does not carry an equivalent for.

- **Field names are snake_case in `tesla-protocol`**, not the camelCase some
  reverse-engineered Tesla protos use (e.g. `delivery_channel`, `authorized_client`,
  `read_file_request`, not `deliveryChannel`/`authorizedClient`/`readFileRequest`).
  Field *numbers* are what matter for wire compatibility; verify both name and number
  against the installed package (`python -c "from tesla_protocol.energy_device import
  X_pb2; print(X_pb2.Y.DESCRIPTOR.fields_by_name.keys())"`) before wiring up a new
  message rather than assuming a name from Tesla's other published schemas.
- **`teg_api_pb2.BackupEvent` field 3 is published as `sheduling_info`** (missing the
  first "c") in `tesla-protocol==1.3.1` — a real upstream typo, not a local mistake.
  `PowerwallClient.get_backup_events` deliberately reads `evt.sheduling_info` to match
  it. If a future `tesla-protocol` release fixes the spelling, bumping the pin will
  break that attribute access — check this spot first.
- `PowerwallClient._send_command_request(category=..., message_cls=..., ...)` is the
  shared helper for any `transport_pb2.MessageEnvelope` oneof category (not just
  `teg`) — reuse it for new categories instead of hand-rolling another
  `_send_*_request` copy.
- Regenerate `tedapi_pb2.py` with `protoc --python_out=. tedapi.proto` from
  `src/aiopowerwall/proto/` — but pin a `protoc`/`grpcio-tools` version whose emitted
  gencode version is `<=` the `protobuf` package version actually installed
  (`python -c "import google.protobuf; print(google.protobuf.__version__)"`). The
  latest `grpcio-tools` (via `uvx --from grpcio-tools python -m grpc_tools.protoc`)
  emits gencode that calls `ValidateProtobufRuntimeVersion` and hard-fails at import
  if that check is newer than the installed runtime — `grpcio-tools==1.68.0` (protoc
  ~28, gencode ~5.28) has stayed compatible with this repo's `protobuf>=4.25` floor.

## Release workflow (`.github/workflows/release.yml`)

`release.yml` is the top-level, tag-triggered (`v*.*.*`) publish workflow — not a
reusable `workflow_call` target. The PyPI trusted publisher for this project is
configured as workflow `release.yml` + environment `pypi`; PEP 740 attestation
signing checks that the *directly run* workflow's identity matches the publisher
config. Splitting this into a thin tag-triggered caller + a `workflow_call`
reusable `release.yml` breaks that match (the reusable workflow signs under a
different identity) and forces `attestations: false` to work around it. Keep
`release.yml` as the single top-level workflow; do not reintroduce a caller/callee
split for this repo's publish path.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
