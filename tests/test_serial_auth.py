"""Tests for companion-protocol based serial auth helpers."""

from __future__ import annotations

from meshcore_mqtt.serial_auth import (
    CMD_APP_START,
    CMD_EXPORT_PRIVATE_KEY,
    FRAME_FROM_CLIENT,
    FRAME_FROM_DEVICE,
    PACKET_PRIVATE_KEY,
    PACKET_SELF_INFO,
    read_device_keys,
)


class FakeSerial:
    """Minimal serial port test double for framed companion protocol."""

    def __init__(self, *args, **kwargs):
        self.buffer = bytearray()
        self.timeout = kwargs.get("timeout", 1.0)
        self.write_timeout = kwargs.get("write_timeout", 1.0)
        self.rts = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def in_waiting(self) -> int:
        return len(self.buffer)

    def reset_input_buffer(self) -> None:
        self.buffer.clear()

    def reset_output_buffer(self) -> None:
        return None

    def write(self, data: bytes) -> int:
        if data == bytes([FRAME_FROM_CLIENT]) + len(CMD_APP_START).to_bytes(2, "little") + CMD_APP_START:
            payload = bytearray(58)
            payload[0] = PACKET_SELF_INFO
            payload[4:36] = bytes.fromhex("AA" * 32)
            self._queue_frame(bytes(payload))
        elif data == bytes([FRAME_FROM_CLIENT]) + len(CMD_EXPORT_PRIVATE_KEY).to_bytes(2, "little") + CMD_EXPORT_PRIVATE_KEY:
            payload = bytes([PACKET_PRIVATE_KEY]) + bytes.fromhex("BB" * 64)
            self._queue_frame(payload)
        return len(data)

    def _queue_frame(self, payload: bytes) -> None:
        self.buffer.extend(
            bytes([FRAME_FROM_DEVICE]) + len(payload).to_bytes(2, "little") + payload
        )

    def read(self, size: int = 1) -> bytes:
        if not self.buffer:
            return b""
        chunk = self.buffer[:size]
        del self.buffer[:size]
        return bytes(chunk)

    def close(self) -> None:
        return None


def test_read_device_keys_uses_companion_protocol(monkeypatch) -> None:
    """Companion protocol appstart + export private key should return both keys."""
    import meshcore_mqtt.serial_auth as serial_auth

    monkeypatch.setattr(serial_auth.serial, "Serial", FakeSerial)

    public_key, private_key = read_device_keys("/dev/ttyUSB0", baudrate=115200, timeout=1.0)

    assert public_key == "AA" * 32
    assert private_key == "BB" * 64
