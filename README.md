# aiopowerwall

An async Tesla Powerwall 3 client built on `aiohttp`, written for Home
Assistant and any other asyncio code.

This library speaks the Powerwall's **TEDAPI v1r** protocol — RSA-signed
protobuf messages over the wired LAN to `192.168.91.1`. It is intentionally
scoped to:

- Powerwall 3 only
- Local LAN access only (no cloud telemetry)
- Read + control commands (status, config, firmware, components, max-backup)

The RSA key pair used for v1r authentication must be **registered with the
gateway out-of-band**, typically via the Tesla Fleet API. This library
consumes an already-paired private key — it does not implement registration.

## Install

```bash
pip install aiopowerwall
```

## Quick start

```python
import asyncio
from pathlib import Path
from aiopowerwall import PowerwallClient

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
        print("Power:", await pw.current_power())

asyncio.run(main())
```

## API surface

| Method | Returns |
| --- | --- |
| `connect()` | DIN string |
| `get_din()` | DIN string |
| `get_config()` | `config.json` (dict) |
| `get_status()` | DeviceController query (narrow) |
| `get_device_controller()` | DeviceController query (extended) |
| `get_components()` | Powerwall 3 component data |
| `get_firmware_version(details=...)` | Version string or details dict |
| `get_meters_aggregates()` | `/api/meters/aggregates` |
| `get_battery_soe()` | Battery SoC percentage |
| `get_grid_status()` | Grid status string |
| `battery_level()` | SoC computed from status payload |
| `current_power(location=...)` | Real power per meter aggregate |
| `backup_time_remaining()` | Hours of backup at current load |
| `write_config(updates)` | Patch `config.json` (dotted paths) |
| `schedule_max_backup(seconds)` | Schedule manual backup event |
| `cancel_max_backup()` | Cancel manual backup event |
| `get_backup_events()` | Active and scheduled backup events |

All read methods cache responses with a configurable TTL
(`cache_status_ttl`, `cache_config_ttl`); pass `force=True` to refresh.

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
