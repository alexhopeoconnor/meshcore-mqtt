"""Helpers for MeshCore MQTT auth token generation."""

from __future__ import annotations

import json
import subprocess
from typing import Any


def create_auth_token(
    public_key_hex: str,
    private_key_hex: str,
    expiry_seconds: int = 3600,
    **claims: Any,
) -> str:
    """Generate a MeshCore auth token using meshcore-decoder."""
    command = [
        "meshcore-decoder",
        "auth-token",
        public_key_hex,
        private_key_hex,
        "-e",
        str(expiry_seconds),
    ]

    if claims:
        command.extend(["-c", json.dumps(claims)])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "meshcore-decoder CLI not found. Install it with: npm install -g "
            "@michaelhart/meshcore-decoder"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("meshcore-decoder token generation timed out") from exc

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"meshcore-decoder failed to generate auth token: {error}")

    token = result.stdout.strip()
    if token.count(".") != 2:
        raise RuntimeError(f"meshcore-decoder returned invalid token: {token!r}")

    return token
