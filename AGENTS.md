# Project agent memory

Project-intrinsic knowledge for agents: build, test, release, and sharp edges that
should travel with the code.

## Dev commands

- `uv sync --extra dev`, then the gates: `uv run ruff check .`, `uv run mypy`
  (strict, `src/aiopowerwall` only), `uv run pytest` (asyncio auto mode).
- `ruff format` is **not** the repo convention — existing source is not format-clean,
  so don't reformat.

## EnergySite compat adapter (`src/aiopowerwall/energysite.py`)

`PowerwallEnergySite` wraps a `PowerwallClient` to present the Tesla Fleet API
`EnergySite` surface by duck typing, so a primary/secondary router can use the local
LAN path first and a cloud `EnergySite` as fallback. The module docstring documents the
conventions; the invariants to preserve:

- **Never import `tesla_fleet_api`** — compatibility is by matching method names,
  signatures, and `dict[str, Any]` return shapes only.
- Commands return the cloud envelope from `_ok_response()`; data reads wrap their
  payload under `response`.
- **`site_info` must stay absent** (not a stub) so the router falls through to cloud.
  Everything else with no faithful local mapping raises `NotImplementedError` with a
  `TODO`. The reference router (`tesla_fleet_api.tesla.router.Router`, sibling
  `python-tesla-fleet-api` project) dispatches by `hasattr(backend, name)` per call: an
  omitted method is skipped silently, a raising one is retried on the next backend.
  That is why placeholders raise rather than being left undefined.
- `schedule_backup_event` accepts `start_time`/`priority` for signature parity only;
  the event always starts now at max priority (`schedule_max_backup`).
- `live_status` is best-effort. Take SoC and the Wh figures from a single
  `get_status()` read (`battery_level()` + `control.systemStatus.*`) rather than a
  separate `get_battery_soe()` call. Keys with no local v1r equivalent are `None`,
  never guessed. Grid status mapping is `_GRID_STATUS_MAP`.
- `connect_if_needed` is not part of the cloud `EnergySite`; it is the router's health
  signal.
- **Do not route `operation`/`backup`/`grid_import_export` to different backends** —
  they all write the same gateway `config.json` document, and splitting them across
  local/cloud lets one write stomp or race the other. When a `config.json` field name
  is not verified against hardware, cross-check it against `jasonacox/pypowerwall`'s
  v1r write path (`pypowerwall/tedapi/pypowerwall_tedapi.py`) — same gateway schema,
  actively maintained.

## v1r local login (`src/aiopowerwall/transport.py`)

`PowerwallClient(gateway_password=...)` takes the **full** gateway/WiFi password;
`V1rTransport` derives the `/api/login/Basic` "customer" password by truncating to the
last 5 characters (`_customer_password`). This is a real, undocumented Tesla gateway
convention (also auto-derived by `jasonacox/pypowerwall`). Never require callers to
pass the pre-truncated value.

## TEDAPI protobuf schema: `tesla-protocol` + local `tedapi.proto`

The TEG / FileStore / Authorization / signing schema comes from the `tesla-protocol`
PyPI package (`tesla_protocol.energy_device`), range-pinned in `pyproject.toml` (kept
wide so this package can coexist with `tesla-fleet-api`'s own `tesla-protocol` floor);
this repo carries no copy. `src/aiopowerwall/proto/tedapi.proto` / `tedapi_pb2.py` is
the one locally checked-in schema (older `tedapi` package: GraphQL send/recv, firmware
request/response) that `tesla-protocol` has no equivalent for.

- **Field names are snake_case in `tesla-protocol`** (`delivery_channel`,
  `authorized_client`, `read_file_request`), not the camelCase of other
  reverse-engineered Tesla protos. Verify name and number against the installed
  package before wiring a new message:
  `python -c "from tesla_protocol.energy_device import X_pb2; print(X_pb2.Y.DESCRIPTOR.fields_by_name.keys())"`.
- **`teg_api_pb2.BackupEvent` field 3 is published as `sheduling_info`** (sic) — an
  upstream typo, unchanged through 2.0.0. `PowerwallClient.get_backup_events` reads
  `evt.sheduling_info` on purpose. `tests/test_tesla_protocol_compat.py` is the canary:
  it asserts every `tesla_protocol` field/enum this package depends on (this typo
  included) by descriptor lookup, so a future release that renames or drops one fails
  there instead of at a gateway call. Before widening the pin's upper bound, install the
  new ceiling version and rerun that test — if the typo is fixed, switch
  `evt.sheduling_info` to a descriptor-based lookup that tries both spellings instead of
  pinning back.
- `PowerwallClient._send_command_request(category=..., message_cls=..., ...)` is the
  shared helper for any `MessageEnvelope` oneof category; reuse it instead of adding
  another `_send_*_request` copy.
- Regenerate `tedapi_pb2.py` with `protoc --python_out=. tedapi.proto` from
  `src/aiopowerwall/proto/`, using a `protoc`/`grpcio-tools` whose emitted gencode
  version (the `Protobuf Python Version` header) is `<=` the installed `protobuf`
  runtime and the `pyproject.toml` floor. Newer gencode calls
  `ValidateProtobufRuntimeVersion` and hard-fails at import on an older runtime.

## Release workflow (`.github/workflows/release.yml`)

`release.yml` is the single top-level, tag-triggered (`v*.*.*`) publish workflow. The
PyPI trusted publisher is configured as workflow `release.yml` + environment `pypi`,
and PEP 740 attestation signing requires the *directly run* workflow's identity to
match. Do not split it into a caller + `workflow_call` reusable workflow: the reusable
half signs under a different identity and forces `attestations: false`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
