"""Canary for the ``tesla-protocol`` dependency range in ``pyproject.toml``.

Imports every ``tesla_protocol.energy_device`` symbol this package relies on
and asserts the specific field/enum names it reads or writes still exist,
so a future ``tesla-protocol`` release that renames or removes one of them
fails here instead of as a runtime ``AttributeError`` against a gateway.
"""

from __future__ import annotations

from tesla_protocol.energy_device import (
    authorization_api_pb2,
    authorization_types_pb2,
    filestore_api_pb2,
    signed_message_pb2,
    teg_api_pb2,
    transport_pb2,
)


def _field_names(descriptor: object) -> set[str]:
    return {f.name for f in descriptor.fields}  # type: ignore[attr-defined]


def test_signed_message_pb2_symbols() -> None:
    assert signed_message_pb2.TAG_SIGNATURE_TYPE == 0
    assert signed_message_pb2.TAG_DOMAIN == 1
    assert signed_message_pb2.TAG_PERSONALIZATION == 2
    assert signed_message_pb2.TAG_EXPIRES_AT == 4
    assert signed_message_pb2.TAG_END == 255
    assert hasattr(signed_message_pb2, "SIGNATURE_TYPE_RSA")
    assert hasattr(signed_message_pb2, "DOMAIN_ENERGY_DEVICE")
    assert hasattr(signed_message_pb2, "MESSAGEFAULT_ERROR_NONE")
    assert hasattr(signed_message_pb2, "MESSAGEFAULT_ERROR_UNKNOWN_KEY_ID")
    assert signed_message_pb2.MessageFault_E.Name(
        signed_message_pb2.MESSAGEFAULT_ERROR_NONE
    ) == "MESSAGEFAULT_ERROR_NONE"

    routable_fields = _field_names(signed_message_pb2.RoutableMessage.DESCRIPTOR)
    assert {
        "to_destination",
        "from_destination",
        "protobuf_message_as_bytes",
        "signature_data",
        "signed_message_status",
        "request_uuid",
        "uuid",
        "flags",
    } <= routable_fields


def test_transport_pb2_symbols() -> None:
    assert hasattr(transport_pb2, "DELIVERY_CHANNEL_HERMES_COMMAND")
    envelope_fields = _field_names(transport_pb2.MessageEnvelope.DESCRIPTOR)
    assert {"delivery_channel", "teg", "authorization"} <= envelope_fields


def test_filestore_api_pb2_symbols() -> None:
    assert hasattr(filestore_api_pb2, "FILE_STORE_API_DOMAIN_CONFIG_JSON")


def test_authorization_types_pb2_enums() -> None:
    assert hasattr(authorization_types_pb2, "AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP")
    for enum_name in (
        "AuthorizedState",
        "AuthorizedClientType",
        "AuthorizedKeyType",
        "AuthorizationRole",
        "AuthorizedVerificationType",
    ):
        assert hasattr(authorization_types_pb2, enum_name)


def test_authorization_api_pb2_messages_request() -> None:
    msgs = authorization_api_pb2.AuthorizationMessages()
    request_response_fields = {
        "list_authorized_clients_request",
        "list_authorized_clients_response",
        "remove_authorized_client_request",
        "remove_authorized_client_response",
    }
    assert request_response_fields <= _field_names(msgs.DESCRIPTOR)

    record_fields = _field_names(
        msgs.list_authorized_clients_response.clients.add().DESCRIPTOR
    )
    assert {
        "public_key",
        "state",
        "type",
        "description",
        "key_type",
        "roles",
        "verification",
        "added_time",
        "identifier",
        "authorized_by_public_key",
    } <= record_fields


def test_teg_api_pb2_backup_event_scheduling_fields() -> None:
    """``PowerwallClient.get_backup_events`` depends on this exact (mis)spelling.

    See ``AGENTS.md`` — ``teg_api_pb2.BackupEvent`` field 3 is published as
    ``sheduling_info`` (sic); ``ManualBackupEvent`` field 1 is the correctly
    spelled ``scheduling_info``. If this test starts failing on
    ``sheduling_info``, check whether upstream fixed the typo before widening
    the ``tesla-protocol`` pin further.
    """
    msgs = teg_api_pb2.TEGMessages()
    assert {
        "get_backup_events_request",
        "get_backup_events_response",
        "schedule_manual_backup_event_request",
        "schedule_manual_backup_event_response",
        "cancel_manual_backup_event_request",
        "cancel_manual_backup_event_response",
    } <= _field_names(msgs.DESCRIPTOR)

    backup_events_response = msgs.get_backup_events_response
    assert {"manual_backup_event", "backup_events"} <= _field_names(
        backup_events_response.DESCRIPTOR
    )

    backup_event_fields = _field_names(teg_api_pb2.BackupEvent.DESCRIPTOR)
    assert {"id", "name", "sheduling_info"} <= backup_event_fields

    manual_backup_event_fields = _field_names(teg_api_pb2.ManualBackupEvent.DESCRIPTOR)
    assert {"scheduling_info"} <= manual_backup_event_fields

    scheduling_info_fields = _field_names(
        msgs.schedule_manual_backup_event_request.scheduling_info.DESCRIPTOR
    )
    assert {"start_time", "duration_seconds", "priority"} <= scheduling_info_fields
