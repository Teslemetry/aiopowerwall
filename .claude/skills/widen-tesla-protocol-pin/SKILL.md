---
name: widen-tesla-protocol-pin
description: Use when raising the upper bound of the tesla-protocol pin in pyproject.toml, for example so this package can coexist with a newer tesla-fleet-api.
---

# Widen the tesla-protocol pin

`tests/test_tesla_protocol_compat.py` is the canary. It asserts, by descriptor
lookup, every `tesla_protocol.energy_device` field and enum this package uses. A
release that renames or drops one fails there, not at a live gateway call.

1. List the published versions and pick the new ceiling:
   `uvx pip index versions tesla-protocol`
2. Run the canary and the full suite against the newest version the new range admits
   (`X.Y.Z`), without touching the lock file:
   `uv run --with 'tesla-protocol==X.Y.Z' pytest tests/test_tesla_protocol_compat.py`
   `uv run --with 'tesla-protocol==X.Y.Z' pytest`
   Also run the suite once against the floor (`tesla-protocol==4.0.0` today), so both
   ends of the range are tested.
3. If the canary fails on `sheduling_info` (sic, `teg_api_pb2.BackupEvent` field 3):
   upstream may have fixed the typo. Do not pin back. In
   `PowerwallClient.get_backup_events` (`src/aiopowerwall/client.py`), replace the
   direct `evt.sheduling_info` reads with a descriptor lookup that tries both
   `sheduling_info` and `scheduling_info`. Make the canary assert that one of the two
   names exists, and keep `tests/test_client.py` passing on both versions.
4. If any other assertion fails, look up the new name and number before you change code:
   `python -c "from tesla_protocol.energy_device import X_pb2; print(X_pb2.Y.DESCRIPTOR.fields_by_name.keys())"`
   Field names are snake_case.
5. Edit only the upper bound in `pyproject.toml` (`"tesla-protocol>=4.0.0,<N"`). Keep
   the range wide: `tesla-fleet-api` pins its own `tesla-protocol` floor, and both
   packages must install together in Home Assistant.
6. Refresh the lock: `uv lock --upgrade-package tesla-protocol`.
7. Run the gates: `uv run ruff check .`, `uv run mypy`, `uv run pytest`.
