# aiopowerwall

An async Tesla Powerwall 3 client built on `aiohttp`, written for Home
Assistant and any other asyncio code.

## Status

**Beta (0.x).** The wire protocol and the public Python API may change
between minor versions until 1.0. Pin a tight version range if you depend
on this library in production.

This library speaks the Powerwall's **TEDAPI v1r** protocol — RSA-signed
protobuf messages directly to your Powerwall. It is intentionally
scoped to:

- Powerwall 3, and updated Powerwall 2 (untested)
- Local LAN access only (no cloud telemetry)
- Read + control commands (status, config, firmware, components,
  max-backup, islanding, curtailment)

The RSA key pair used for v1r authentication must be **registered with the
gateway out-of-band**, typically via the Tesla Fleet API. This library
consumes an already-paired private key — it does not implement registration.

### Multi-Powerwall systems

This client maintains a single v1r connection to the **leader** gateway and
signs every request with the leader's DIN — which is correct, since the RSA
key is only registered on the leader. On a multi-unit system the leader
returns whole-site aggregate data, but **per-follower** vitals are not
available over v1r: the leader ignores the `recipient.din` of a per-device
query and echoes its own data, so iterating over followers would yield
duplicates rather than per-unit readings. Reading individual follower units
requires a separate WiFi-side TEDAPI connection to `192.168.91.1`, which this
library does not implement.

## Install

```bash
pip install aiopowerwall
```

## Quick start

```python
import asyncio
from pathlib import Path
from aiopowerwall import PowerwallClient, current_power

async def main() -> None:
    pem = Path("tedapi_rsa_private.pem").read_bytes()
    async with PowerwallClient(
        host="192.168.91.1",
        gateway_password="<gateway-password>",
        rsa_private_key_pem=pem,
    ) as pw:
        await pw.connect()
        print("DIN:", pw.din)
        print("Battery SoC:", await pw.get_battery_soe(), "%")
        print("Grid:", await pw.get_grid_status())
        status = await pw.get_status()
        print("Power:", current_power(status))

asyncio.run(main())
```

## No caching, no coalescing

**Every method on `PowerwallClient` issues a fresh request to the gateway.**
The library does not cache responses, deduplicate concurrent calls, or
batch reads — the only state it holds across calls is the v1r session
(login + DIN, established once by `connect()`).

This keeps the library predictable but means callers are responsible for
freshness control:

- If you need several values from the same payload, fetch the payload
  once and pass it to the pure helper functions (see below) rather than
  calling multiple `get_*` methods.
- If you poll on a fixed interval, do the polling in your own code; the
  client will not throttle you.
- If two coroutines call the same `get_*` method concurrently, the
  gateway sees two requests.

`connect()` is the single exception: it is idempotent and lock-protected,
so concurrent callers share one login.

## API surface

| Method | Returns |
| --- | --- |
| `connect()` | DIN string (idempotent; required before other calls) |
| `get_din()` | DIN string (calls `connect()` if needed) |
| `get_config()` | `config.json` (dict) |
| `get_status()` | DeviceController query (narrow) |
| `get_device_controller()` | DeviceController query (extended) |
| `get_components()` | Powerwall 3 component data |
| `get_firmware_details()` | Firmware details dict |
| `get_meters_aggregates()` | `/api/meters/aggregates` |
| `get_battery_soe()` | Battery SoC percentage (**raw** physical scale) |
| `get_battery_soe_scaled()` | Battery SoC on the **user-facing** scale (Tesla app / Fleet API) |
| `get_grid_status()` | Grid status string |
| `get_backup_events()` | Active and scheduled backup events |

### Writes and commands

| Method | Effect |
| --- | --- |
| `write_config(updates)` | Patch `config.json` (dotted-path mapping) |
| `set_backup_reserve(percent)` | Set backup reserve on the **user-facing** scale (Tesla app / Fleet API) |
| `set_backup_reserve_raw(percent)` | Set backup reserve as the **raw** `config.json` value |
| `schedule_max_backup(seconds)` | Schedule a manual max-backup event |
| `cancel_max_backup()` | Cancel the active manual backup event |
| `set_island_mode(off_grid=, force=, …)` | Send `setIslandModeRequest` |
| `go_off_grid(force=True)` | Convenience wrapper around `set_island_mode` |
| `reconnect_grid()` | Convenience wrapper around `set_island_mode` |
| `trigger_islanding()` | Send `triggerIslandingBlackStartRequest` |
| `curtail(reserve_percent=100)` | Stop export via `backup` mode + reserve |
| `restore_from_curtailment()` | Restore mode + reserve captured by `curtail` |
| `curtailment_active` (property) | True between `curtail` and `restore_from_curtailment` |

> **Islanding caveat.** `set_island_mode` / `go_off_grid` post a local v1r
> command that the gateway acknowledges but, on some firmwares, does not
> actually act on — only the Fleet-API cloud relay path is known to
> operate the contactor. Verify with `get_status()`
> (`islanding.contactorClosed`) before relying on it. `trigger_islanding`
> issues the explicit black-start command if the mode-only request is a
> no-op on your gateway.

> **Backup-reserve scaling.** The gateway stores the reserve on a *raw*
> scale that differs from what the Tesla app and Fleet API show: the bottom
> 5% is an inaccessible buffer, so `raw = scaled * 0.95 + 5` (e.g. app-20%
> is raw-24%, app-0% is raw-5%). `set_backup_reserve` takes the
> **user-facing** value and applies that conversion for you;
> `set_backup_reserve_raw` writes the raw value verbatim. Use the
> `scaled_to_raw_reserve` / `raw_to_scaled_reserve` helpers to convert
> explicitly.

> **SoC scaling.** `battery_level(status)` returns the **user-facing** SoC
> the Tesla app and Fleet API (`live_status.percentage_charged`) show. The
> gateway reports SoC locally on a *raw* physical scale that includes the
> bottom-5% buffer and so reads higher; `battery_level_raw(status)` and the
> `/api/system_status/soe` reader `get_battery_soe()` expose that raw value.
> The transform is identical to reserve: `scaled = (raw - 5) / 0.95`
> (verified on PW3: local raw 52.78% == Fleet 50.29%). Use
> `get_battery_soe_scaled()` for the user-facing value off the soe endpoint,
> or the `scaled_to_raw_soc` / `raw_to_scaled_soc` helpers to convert
> explicitly.

### Pure helpers

These operate on an already-fetched status payload — fetch once with
`get_status()`, then call as many helpers as you need.

| Function | Returns |
| --- | --- |
| `battery_level(status)` | SoC from status on the **user-facing** scale (Tesla app / Fleet API) |
| `battery_level_raw(status)` | SoC from status on the **raw** physical scale |
| `current_power(status)` | `{location: realPowerW}` map |
| `backup_time_remaining(status)` | Hours of backup at current load |
| `scaled_to_raw_reserve(percent)` | User-facing reserve % → raw config value |
| `raw_to_scaled_reserve(percent)` | Raw config value → user-facing reserve % |
| `scaled_to_raw_soc(percent)` | User-facing SoC % → raw value |
| `raw_to_scaled_soc(percent)` | Raw SoC value → user-facing SoC % |

## Exceptions

All errors are subclasses of `PowerwallError`:

- `PowerwallConnectionError` — transport failure / timeout
- `PowerwallAuthenticationError` — bad password or unregistered RSA key
- `PowerwallRateLimitError` — gateway returned 429/503
- `PowerwallFaultError` — signed-message fault (key inactive, expired, etc.)
- `PowerwallProtocolError` — malformed response

## Acknowledgements

This project builds on the protocol research and reference implementation in
[`pypowerwall`](https://github.com/jasonacox/pypowerwall) by
[Jason Cox](https://github.com/jasonacox), distributed under the
[MIT License](https://github.com/jasonacox/pypowerwall/blob/main/LICENSE).
Huge thanks to Jason and the pypowerwall contributors for reverse-engineering
and documenting the TEDAPI protocol.

## License

MIT (see `LICENSE`). Original pypowerwall copyright and license notice are
retained in `LICENSE`.
