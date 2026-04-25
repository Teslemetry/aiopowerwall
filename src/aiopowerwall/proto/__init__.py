"""Protobuf bindings for the Tesla TEDAPI v1r protocol.

The generated `_pb2` modules build their classes dynamically via the
protobuf `_builder` API. mypy cannot see those classes statically without
mypy-protobuf-generated `.pyi` stubs, so we deliberately re-export the
modules typed as `Any` — attribute access is checked at runtime.

To regenerate the bindings:

    protoc --python_out=. tedapi.proto tedapi_combined.proto
"""

from typing import Any

from . import tedapi_combined_pb2 as _tedapi_combined_pb2
from . import tedapi_pb2 as _tedapi_pb2

# Re-export as Any so callers don't have to suppress attr-defined errors
# at every protobuf message construction site.
tedapi_pb2: Any = _tedapi_pb2
combined_pb2: Any = _tedapi_combined_pb2

__all__ = ["combined_pb2", "tedapi_pb2"]
