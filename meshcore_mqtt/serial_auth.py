"""Helpers for reading MeshCore keys from a serial-connected node."""

from __future__ import annotations

import time
from typing import Tuple

try:
    import serial
except ImportError:  # pragma: no cover - dependency comes from meshcore
    serial = None


def _clean_hex(value: str) -> str:
    return "".join(value.split()).upper()


def _extract_arrow_value(response: str) -> str:
    if "-> >" not in response:
        raise RuntimeError(f"Unexpected serial response: {response!r}")
    value = response.split("-> >", 1)[1].strip()
    if "\n" in value:
        value = value.split("\n", 1)[0]
    return value.replace("\r", "").strip()


def _send_command(port: "serial.Serial", command: str, delay: float = 1.0) -> str:
    port.reset_input_buffer()
    port.reset_output_buffer()
    port.write(command.encode())
    time.sleep(delay)
    return port.read_all().decode(errors="replace")


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
    ) as port:
        public_response = _send_command(port, "get public.key\r\n", delay=1.0)
        public_key = _clean_hex(_extract_arrow_value(public_response))
        if len(public_key) != 64 or any(c not in "0123456789ABCDEF" for c in public_key):
            raise RuntimeError(f"Invalid MeshCore public key response: {public_key!r}")

        private_response = _send_command(port, "get prv.key\r\n", delay=1.0)
        private_key = _clean_hex(_extract_arrow_value(private_response))
        if len(private_key) != 128 or any(c not in "0123456789ABCDEF" for c in private_key):
            raise RuntimeError(
                f"Invalid MeshCore private key response length: {len(private_key)}"
            )

        return public_key, private_key
