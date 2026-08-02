"""Unit tests for PowerwallClient's authorized-client commands.

Exercises the protobuf parsing (enum-name stripping, base64 encoding,
optional-field presence) and the removal write path against hand-built
``AuthorizationMessages`` messages — no real hardware, LAN, or signing.
"""

from __future__ import annotations

import pytest

from aiopowerwall import PowerwallClient
from aiopowerwall.proto import combined_pb2


class _FakeTransport:
    def __init__(self, response: combined_pb2.MessageEnvelope) -> None:
        self._response = response
        self.sent: list[bytes] = []

    async def post_v1r(self, envelope_bytes: bytes, din: str) -> bytes:
        self.sent.append(envelope_bytes)
        result: bytes = self._response.SerializeToString()
        return result


def _client_for(response: combined_pb2.MessageEnvelope) -> PowerwallClient:
    """A bare client (no real transport/session) that returns ``response``."""
    pw = PowerwallClient.__new__(PowerwallClient)

    async def fake_connect() -> str:
        return "1234567-00-A--CJ2000000000"

    pw.connect = fake_connect  # type: ignore[method-assign]
    pw._transport = _FakeTransport(response)  # type: ignore[attr-defined]
    return pw


def _envelope_with_clients() -> combined_pb2.MessageEnvelope:
    envelope = combined_pb2.MessageEnvelope()
    resp = envelope.authorization.list_authorized_clients_response
    entry = resp.clients.add()
    entry.type = combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
    entry.description = "Test Client"
    entry.key_type = combined_pb2.AUTHORIZED_KEY_TYPE_RSA
    entry.public_key = b"\x01\x02\x03"
    entry.roles.append(combined_pb2.AUTHORIZATION_ROLE_CUSTOMER)
    entry.state = combined_pb2.AUTHORIZED_STATE_VERIFIED
    entry.verification = combined_pb2.AUTHORIZED_VERIFICATION_TYPE_SIGNED
    entry.added_time.seconds = 1_700_000_000
    entry.identifier = "abc-123"
    resp.enable_line_switch_off = True
    return envelope


async def test_list_authorized_clients_parses_response() -> None:
    pw = _client_for(_envelope_with_clients())
    result = await pw.list_authorized_clients()
    assert result == {
        "clients": [
            {
                "public_key": "AQID",
                "state": "VERIFIED",
                "type": "CUSTOMER_MOBILE_APP",
                "description": "Test Client",
                "key_type": "RSA",
                "roles": ["CUSTOMER"],
                "verification": "SIGNED",
                "added_time": 1_700_000_000,
                "identifier": "abc-123",
                "authorized_by_public_key": None,
            }
        ],
        "enable_line_switch_off": True,
    }


async def test_list_authorized_clients_sends_request_over_v1r() -> None:
    pw = _client_for(_envelope_with_clients())
    transport = pw._transport  # type: ignore[attr-defined]
    await pw.list_authorized_clients()
    assert len(transport.sent) == 1
    sent_envelope = combined_pb2.MessageEnvelope()
    sent_envelope.ParseFromString(transport.sent[0])
    assert sent_envelope.HasField("authorization")
    assert sent_envelope.authorization.HasField("list_authorized_clients_request")


async def test_list_authorized_clients_omits_optional_fields_when_absent() -> None:
    envelope = combined_pb2.MessageEnvelope()
    resp = envelope.authorization.list_authorized_clients_response
    entry = resp.clients.add()
    entry.type = combined_pb2.AUTHORIZED_CLIENT_TYPE_VEHICLE
    entry.key_type = combined_pb2.AUTHORIZED_KEY_TYPE_ECC
    entry.public_key = b""
    entry.state = combined_pb2.AUTHORIZED_STATE_PENDING_VERIFICATION
    entry.verification = combined_pb2.AUTHORIZED_VERIFICATION_TYPE_PRESENCE_PROOF
    # No roles, added_time, identifier, or authorized_by_public_key set.

    pw = _client_for(envelope)
    result = await pw.list_authorized_clients()
    client = result["clients"][0]
    assert client["roles"] == []
    assert client["added_time"] is None
    assert client["identifier"] is None
    assert client["authorized_by_public_key"] is None


async def test_list_authorized_clients_empty_list() -> None:
    envelope = combined_pb2.MessageEnvelope()
    envelope.authorization.list_authorized_clients_response.SetInParent()

    pw = _client_for(envelope)
    result = await pw.list_authorized_clients()
    assert result == {"clients": [], "enable_line_switch_off": False}


# ── remove_authorized_client ────────────────────────────────────────────────


def _remove_ack() -> combined_pb2.MessageEnvelope:
    envelope = combined_pb2.MessageEnvelope()
    envelope.authorization.remove_authorized_client_response.SetInParent()
    return envelope


async def test_remove_authorized_client_sends_raw_der_key() -> None:
    pw = _client_for(_remove_ack())
    transport = pw._transport  # type: ignore[attr-defined]

    await pw.remove_authorized_client(b"\x01\x02\x03")

    assert len(transport.sent) == 1
    sent = combined_pb2.MessageEnvelope()
    sent.ParseFromString(transport.sent[0])
    assert sent.authorization.HasField("remove_authorized_client_request")
    assert sent.authorization.remove_authorized_client_request.public_key == b"\x01\x02\x03"


async def test_remove_authorized_client_accepts_base64_from_listing() -> None:
    """A key can be round-tripped straight out of list_authorized_clients."""
    pw = _client_for(_remove_ack())
    transport = pw._transport  # type: ignore[attr-defined]

    # "AQID" is exactly what the listing reports for b"\x01\x02\x03".
    await pw.remove_authorized_client("AQID")

    sent = combined_pb2.MessageEnvelope()
    sent.ParseFromString(transport.sent[0])
    assert sent.authorization.remove_authorized_client_request.public_key == b"\x01\x02\x03"


async def test_remove_authorized_client_returns_none_on_empty_ack() -> None:
    """Firmware may omit the (fieldless) response oneof entirely."""
    pw = _client_for(combined_pb2.MessageEnvelope())
    assert await pw.remove_authorized_client(b"\x01\x02\x03") is None


@pytest.mark.parametrize("empty", [b"", ""])
async def test_remove_authorized_client_rejects_empty_key(empty: bytes | str) -> None:
    """An empty key would otherwise be a request to remove nothing."""
    pw = _client_for(_remove_ack())
    transport = pw._transport  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="must not be empty"):
        await pw.remove_authorized_client(empty)

    assert transport.sent == []
