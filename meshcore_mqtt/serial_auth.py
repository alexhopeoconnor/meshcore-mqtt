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


def _send_command(
    port: "serial.Serial",
    command: str,
    timeout: float = 10.0,
) -> str:
    if not command.endswith("\r\n"):
        command = f"{command}\r\n"

    port.reset_input_buffer()
    port.reset_output_buffer()
    port.write(command.encode("utf-8"))

    start_time = time.time()
    response_chunks: list[str] = []

    while (time.time() - start_time) < timeout:
        time.sleep(0.1)
        if port.in_waiting > 0:
            data = port.read_all().decode(errors="replace")
            response_chunks.append(data)
            full_response = "".join(response_chunks)
            if "-> " in full_response or full_response.rstrip().endswith(">"):
                break

    return "".join(response_chunks)


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
        # Wake the MeshCore serial CLI and clear any stale prompt data.
        port.write(b"\r\n\r\n")
        time.sleep(0.2)
        port.reset_input_buffer()
        port.reset_output_buffer()

        public_response = _send_command(port, "get public.key", timeout=10.0)
        public_key = _clean_hex(_extract_arrow_value(public_response))
        if len(public_key) != 64 or any(c not in "0123456789ABCDEF" for c in public_key):
            raise RuntimeError(f"Invalid MeshCore public key response: {public_key!r}")

        private_response = _send_command(port, "get prv.key", timeout=10.0)
        private_key = _clean_hex(_extract_arrow_value(private_response))
        if len(private_key) != 128 or any(c not in "0123456789ABCDEF" for c in private_key):
            raise RuntimeError(
                f"Invalid MeshCore private key response length: {len(private_key)}"
            )

        return public_key, private_key
