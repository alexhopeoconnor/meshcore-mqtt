"""Tests for MQTT transport and token authentication support."""

from unittest.mock import MagicMock, patch

import pytest

from meshcore_mqtt.config import (
    Config,
    ConnectionType,
    MQTTAuthMethod,
    MQTTConfig,
    MQTTTransport,
    MeshCoreConfig,
)
from meshcore_mqtt.mqtt_worker import MQTTWorker


def test_from_env_parses_websocket_token_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment variables should populate websocket/token auth config."""
    monkeypatch.setenv("MQTT_BROKER", "mqtt2.eastmesh.au")
    monkeypatch.setenv("MQTT_PORT", "443")
    monkeypatch.setenv("MQTT_TRANSPORT", "websockets")
    monkeypatch.setenv("MQTT_WS_PATH", "/")
    monkeypatch.setenv("MQTT_AUTH_METHOD", "token")
    monkeypatch.setenv("MQTT_TOKEN_AUDIENCE", "mqtt2.eastmesh.au")
    monkeypatch.setenv("MQTT_TOKEN_EMAIL", "mesh@example.com")
    monkeypatch.setenv("MESHCORE_CONNECTION", "serial")
    monkeypatch.setenv("MESHCORE_ADDRESS", "/dev/ttyUSB0")

    config = Config.from_env()

    assert config.mqtt.transport == MQTTTransport.WEBSOCKETS
    assert config.mqtt.auth_method == MQTTAuthMethod.TOKEN
    assert config.mqtt.token_audience == "mqtt2.eastmesh.au"
    assert config.mqtt.token_email == "mesh@example.com"


@patch("meshcore_mqtt.mqtt_worker.create_auth_token")
@patch("meshcore_mqtt.mqtt_worker.read_device_keys")
def test_token_auth_uses_serial_key_material(
    mock_read_device_keys: MagicMock,
    mock_create_auth_token: MagicMock,
) -> None:
    """Token auth should derive MQTT credentials from MeshCore serial keys."""
    mock_read_device_keys.return_value = ("AA" * 32, "BB" * 64)
    mock_create_auth_token.return_value = "header.payload.signature"

    config = Config(
        mqtt=MQTTConfig(
            broker="mqtt2.eastmesh.au",
            port=443,
            transport=MQTTTransport.WEBSOCKETS,
            auth_method=MQTTAuthMethod.TOKEN,
            token_audience="mqtt2.eastmesh.au",
            tls_enabled=True,
        ),
        meshcore=MeshCoreConfig(
            connection_type=ConnectionType.SERIAL,
            address="/dev/ttyUSB0",
        ),
    )
    worker = MQTTWorker(config)

    username, password = worker._resolve_auth_credentials()

    assert username == f"v1_{'AA' * 32}"
    assert password == "header.payload.signature"
    mock_read_device_keys.assert_called_once_with(
        "/dev/ttyUSB0", baudrate=115200, timeout=5.0
    )
    mock_create_auth_token.assert_called_once()


@patch("meshcore_mqtt.mqtt_worker.mqtt.Client")
def test_websocket_client_sets_transport_and_path(
    mock_client_ctor: MagicMock,
) -> None:
    """MQTT client should be created with websocket transport support."""
    mock_client = MagicMock()
    mock_client_ctor.return_value = mock_client

    config = Config(
        mqtt=MQTTConfig(
            broker="mqtt2.eastmesh.au",
            transport=MQTTTransport.WEBSOCKETS,
            ws_path="/",
        ),
        meshcore=MeshCoreConfig(
            connection_type=ConnectionType.TCP,
            address="127.0.0.1",
            port=5000,
        ),
    )
    worker = MQTTWorker(config)

    with patch.object(worker, "_resolve_auth_credentials", return_value=(None, None)):
        worker._create_client()

    mock_client_ctor.assert_called_once()
    assert mock_client_ctor.call_args.kwargs["transport"] == "websockets"
    mock_client.ws_set_options.assert_called_once_with(path="/")
