"""Protobuf bindings for the legacy TEDAPI v1r GraphQL/firmware schema.

`tedapi_pb2` models the older ``tedapi`` package (GraphQL query send/recv and
firmware request/response) — Tesla's published `tesla-protocol` package does
not carry an equivalent for this schema, so it stays checked in here. The
TEG/FileStore/Authorization/signing schema lives in the `tesla-protocol`
PyPI package (`tesla_protocol.energy_device`) instead.

The generated `_pb2` module builds its classes dynamically via the protobuf
`_builder` API. mypy cannot see those classes statically without
mypy-protobuf-generated `.pyi` stubs, so we deliberately re-export it typed
as `Any` — attribute access is checked at runtime.

To regenerate the binding:

    protoc --python_out=. tedapi.proto
"""

from typing import Any

from . import tedapi_pb2 as _tedapi_pb2

# Re-export as Any so callers don't have to suppress attr-defined errors
# at every protobuf message construction site.
tedapi_pb2: Any = _tedapi_pb2

__all__ = ["tedapi_pb2"]
