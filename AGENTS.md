# Project agent memory

## Dev commands

- `uv sync --extra dev`, then the gates: `uv run ruff check .`, `uv run mypy`
  (strict, `src/aiopowerwall` only), `uv run pytest` (asyncio auto mode).
- Do not run `ruff format`: existing source is not format-clean.

## EnergySite compat adapter (`src/aiopowerwall/energysite.py`)

- **Never import `tesla_fleet_api`** anywhere in `src/`. Compatibility with its
  `EnergySite` and `AuthorizedClient` shapes is by duck typing only.
- The router (`tesla_fleet_api.tesla.router.Router`) skips a missing method and retries
  a raising one on the next backend. So: keep `site_info` absent (not a stub); a
  command with no faithful local mapping raises `NotImplementedError` with a `TODO`.
- Never guess a value: a `live_status` key with no local equivalent is `None`, and an
  unrecognized enum value (e.g. a newer firmware `state`) is returned as the raw int.
- **Do not route `operation`/`backup`/`grid_import_export` to different backends.** They
  all write the gateway `config.json`; a split lets one write stomp the other.
- If a `config.json` field name is not verified on hardware, cross-check it against
  `jasonacox/pypowerwall` (`pypowerwall/tedapi/pypowerwall_tedapi.py`).
- Keep `authorized_clients.AuthorizedClient` (dataclass) and `models.AuthorizedClient`
  (raw `TypedDict`) separate; do not merge or rename either.

## v1r local login

`PowerwallClient(gateway_password=...)` takes the **full** gateway password; the
transport truncates it to the last 5 characters. Never require callers to pass the
truncated value.

## Protobuf schema

- Every protobuf message (TEG / FileStore / Authorization / GraphQL / Common /
  signing) comes from the `tesla-protocol` package; do not vendor a copy. Keep its
  `pyproject.toml` pin range wide so this package can coexist with `tesla-fleet-api`.
- `tesla-protocol` field names are snake_case. Verify name and number before wiring a
  new message:
  `python -c "from tesla_protocol.energy_device import X_pb2; print(X_pb2.Y.DESCRIPTOR.fields_by_name.keys())"`.
- `teg_api_pb2.BackupEvent` field 3 is `sheduling_info` (sic, upstream typo).
  `tests/test_tesla_protocol_compat.py` asserts every depended-on field. Before
  widening the pin's upper bound, install the new ceiling and rerun it; if the typo is
  fixed, look the field up under both spellings rather than pinning back.
- Reuse `PowerwallClient._send_command_request` for any `MessageEnvelope` category;
  do not add another `_send_*_request`. The GraphQL and system-info reads are the
  exception: they keep the local HTTPS / installer header from `_local_envelope`.

## Release

Keep `.github/workflows/release.yml` a single tag-triggered workflow. The PyPI trusted
publisher (workflow `release.yml`, environment `pypi`) and PEP 740 attestations need
the directly run workflow's identity; a `workflow_call` split breaks signing.
