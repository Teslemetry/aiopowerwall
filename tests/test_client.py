"""Unit tests for PowerwallClient.list_authorized_clients.

Exercises the protobuf parsing (enum-name stripping, base64 encoding,
optional-field presence) against a hand-built ``AuthorizationMessages``
response — no real hardware, LAN, or signing.
"""

from __future__ import annotations

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
