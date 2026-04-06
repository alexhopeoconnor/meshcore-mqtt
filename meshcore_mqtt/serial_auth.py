"""Helpers for reading MeshCore keys via the MeshCore companion protocol."""

from __future__ import annotations

import time
from typing import Tuple

try:
    import serial
except ImportError:  # pragma: no cover - dependency comes from meshcore
    serial = None


FRAME_FROM_CLIENT = 0x3C
FRAME_FROM_DEVICE = 0x3E
PACKET_SELF_INFO = 5
PACKET_PRIVATE_KEY = 14
CMD_APP_START = b"\x01\x03      mccli"
CMD_EXPORT_PRIVATE_KEY = b"\x17"


def _read_exact(port: "serial.Serial", size: int, timeout: float) -> bytes:
    deadline = time.time() + timeout
    chunks = bytearray()
    while len(chunks) < size and time.time() < deadline:
        data = port.read(size - len(chunks))
        if data:
            chunks.extend(data)
        else:
            time.sleep(0.05)
    if len(chunks) != size:
        raise RuntimeError(f"Timed out reading {size} serial bytes")
    return bytes(chunks)


def _send_frame(port: "serial.Serial", payload: bytes) -> None:
    frame = bytes([FRAME_FROM_CLIENT]) + len(payload).to_bytes(2, "little") + payload
    port.write(frame)


def _read_frame(port: "serial.Serial", timeout: float) -> bytes:
    deadline = time.time() + timeout

    while time.time() < deadline:
        start = port.read(1)
        if not start:
            time.sleep(0.05)
            continue
        if start[0] != FRAME_FROM_DEVICE:
            continue

        remaining = max(deadline - time.time(), 0.1)
        header = _read_exact(port, 2, remaining)
        size = int.from_bytes(header, "little")
        remaining = max(deadline - time.time(), 0.1)
        return _read_exact(port, size, remaining)

    raise RuntimeError("Timed out waiting for MeshCore companion frame")


def _read_until_packet_type(
    port: "serial.Serial",
    packet_type: int,
    timeout: float,
) -> bytes:
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = _read_frame(port, max(deadline - time.time(), 0.1))
        if frame and frame[0] == packet_type:
            return frame
    raise RuntimeError(f"Timed out waiting for packet type {packet_type}")


def read_device_keys(
    port_path: str,
    baudrate: int = 115200,
    timeout: float = 1.0,
) -> Tuple[str, str]:
    """Read MeshCore public/private keys from a serial device."""
    if serial is None:  # pragma: no cover
        raise RuntimeError("pyserial is required for serial auth support")

    with serial.Serial(
        port_path,
        baudrate=baudrate,
        timeout=timeout,
        write_timeout=timeout,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        rtscts=False,
    ) as port:
        port.rts = False
        port.reset_input_buffer()
        port.reset_output_buffer()

        # Companion protocol handshake: app start yields SELF_INFO with public key.
        _send_frame(port, CMD_APP_START)
        self_info = _read_until_packet_type(port, PACKET_SELF_INFO, timeout=10.0)
        if len(self_info) < 36:
            raise RuntimeError(f"Invalid SELF_INFO frame length: {len(self_info)}")
        public_key = self_info[4:36].hex().upper()

        # Export private key using companion protocol command 23.
        _send_frame(port, CMD_EXPORT_PRIVATE_KEY)
        private_frame = _read_until_packet_type(port, PACKET_PRIVATE_KEY, timeout=10.0)
        if len(private_frame) < 65:
            raise RuntimeError(
                f"Invalid PRIVATE_KEY frame length: {len(private_frame)}"
            )
        private_key = private_frame[1:65].hex().upper()

        return public_key, private_key
