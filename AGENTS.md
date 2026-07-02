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
  Data reads (`get_backup_events`, `live_status`) wrap their payload under `response`.
- **`site_info` is intentionally absent** (not a stub) so the router falls through to the
  cloud for it. Do not add it.
- **Placeholders vs omission**: everything with no faithful local mapping yet raises
  `NotImplementedError` with a `TODO` (so the full command surface is scaffolded and a
  per-command-failover router falls back to cloud). Only `site_info` is omitted entirely.
- `schedule_backup_event` maps to `schedule_max_backup`: `start_time`/`priority` are
  accepted for signature parity but **not honoured** (event starts now at max priority).
- `live_status` is best-effort from meters aggregates + user-facing SoE + grid status;
  keys the local v1r reads can't supply (`energy_left`, `total_pack_energy`,
  `backup_capable`, `grid_services_*`, `storm_mode_active`, `timestamp`, `wall_connectors`)
  are returned as `None` rather than guessed. Grid-status→`(grid_status, island_status)`
  translation is a best-effort map (`_GRID_STATUS_MAP`).
- `connect_if_needed` is not part of the cloud `EnergySite`; it's the router's health
  signal (delegates to `PowerwallClient.connect`).
