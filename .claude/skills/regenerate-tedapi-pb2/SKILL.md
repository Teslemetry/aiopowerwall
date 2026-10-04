---
name: regenerate-tedapi-pb2
description: Use when src/aiopowerwall/proto/tedapi.proto changes and src/aiopowerwall/proto/tedapi_pb2.py must be regenerated.
---

# Regenerate tedapi_pb2.py

`tedapi.proto` is the only schema this repo carries; everything else comes from
`tesla-protocol`. The generated file must load on the oldest `protobuf` runtime that
`pyproject.toml` allows (`protobuf>=4.25`). Gencode newer than the runtime calls
`ValidateProtobufRuntimeVersion` and fails at import.

1. Do not use the system `protoc` if it is newer than the floor (for example
   `libprotoc 28.x` emits 5.28 gencode). Use `grpcio-tools` 1.62.3, which bundles
   protoc for protobuf 4.25.x. It has no wheels for new Pythons and imports
   `pkg_resources`, so run it on Python 3.11 with setuptools below 81:
   ```
   cd src/aiopowerwall/proto
   uvx --python 3.11 --with 'setuptools<81' --from 'grpcio-tools==1.62.3' \
     python -m grpc_tools.protoc -I. --python_out=. tedapi.proto
   ```
2. Check the gencode header (line 4). The version must be `<=` the `protobuf` floor:
   `sed -n 4p tedapi_pb2.py` → `# Protobuf Python Version: 4.25.1`
3. If you raise the `protobuf` floor in `pyproject.toml`, you may use a newer
   `grpcio-tools`, but its header version must still be `<=` the new floor.
4. Do not hand-edit or reformat `tedapi_pb2.py`. Ruff excludes `proto/*_pb2.py`, and
   mypy ignores errors in `aiopowerwall.proto.*`.
5. Import the module alone on the floor runtime, from `src/aiopowerwall/proto`. The
   whole package cannot load there, because `tesla-protocol` needs a newer `protobuf`:
   `uv run --isolated --no-project --python 3.11 --with 'protobuf==4.25.*' python -c "import tedapi_pb2"`
6. Run the gates: `uv run ruff check .`, `uv run mypy`, `uv run pytest`.
