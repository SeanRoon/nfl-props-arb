"""Loading the Polymarket US API credentials.

Never from `data/`, and never from anywhere inside the repo. The key is read from
the environment, or from a file path given by the environment, so that the only
way it ends up committed is if the operator deliberately puts it somewhere it was
told not to go.

    POLYMARKET_US_KEY_ID    -- the API key id the venue issued
    POLYMARKET_US_KEY_FILE  -- path to a file holding the Ed25519 private key
    POLYMARKET_US_KEY       -- the key itself (hex or base64), for the cases
                               where a file is more trouble than it is worth

`repr` and `str` are overridden so the key cannot be printed by accident -- a
traceback through this module is the most likely way a secret reaches a log file.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

ENV_KEY_ID = "POLYMARKET_US_KEY_ID"
ENV_KEY_FILE = "POLYMARKET_US_KEY_FILE"
ENV_KEY = "POLYMARKET_US_KEY"


class MissingCredentialsError(RuntimeError):
    """Raised when a live run is attempted with nothing to sign with."""


@dataclass(frozen=True)
class Credentials:
    """An API key id and the Ed25519 private key seed that signs for it."""

    key_id: str
    private_key: bytes

    def __repr__(self) -> str:
        return f"Credentials(key_id={self.key_id!r}, private_key=<redacted>)"

    __str__ = __repr__


def _decode(raw: str) -> bytes:
    """Accept hex or base64, since which one the venue hands out is unknown."""
    text = raw.strip()
    try:
        return bytes.fromhex(text)
    except ValueError:
        pass
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MissingCredentialsError(
            "private key is neither valid hex nor valid base64"
        ) from exc


def load(env: dict[str, str] | None = None) -> Credentials:
    """Read credentials from the environment, or raise saying what is missing."""
    src = os.environ if env is None else env
    key_id = (src.get(ENV_KEY_ID) or "").strip()
    key_file = (src.get(ENV_KEY_FILE) or "").strip()
    inline = (src.get(ENV_KEY) or "").strip()

    missing = []
    if not key_id:
        missing.append(ENV_KEY_ID)
    if not key_file and not inline:
        missing.append(f"{ENV_KEY_FILE} or {ENV_KEY}")
    if missing:
        raise MissingCredentialsError(
            "cannot place live orders: set " + ", ".join(missing)
        )

    if key_file:
        path = Path(key_file)
        if not path.exists():
            raise MissingCredentialsError(f"{ENV_KEY_FILE} points at a missing file: {path}")
        raw = path.read_text(encoding="utf-8")
    else:
        raw = inline

    return Credentials(key_id=key_id, private_key=_seed(_decode(raw)))


def _seed(key: bytes) -> bytes:
    """Reduce a key to the 32-byte Ed25519 seed `from_private_bytes` accepts.

    Polymarket US issues the 64-byte NaCl form, seed followed by public key.
    """
    if len(key) == 32:
        return key
    if len(key) == 64:
        return key[:32]
    raise MissingCredentialsError(
        f"private key is {len(key)} bytes; expected a 32-byte seed or 64-byte seed+public key"
    )
