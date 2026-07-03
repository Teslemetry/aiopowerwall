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
