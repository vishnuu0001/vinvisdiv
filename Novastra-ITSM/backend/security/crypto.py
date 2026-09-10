# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Symmetric encryption for settings values (URLs/credentials) at rest.
# Date: 2026-07-24
# ---------------------------------------------------------------------------
"""Symmetric encryption for settings values (URLs/credentials) at rest."""
from __future__ import annotations

import os
import base64
import hashlib
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


CREDENTIAL_ENVELOPE_TTL_SECONDS = 3600


# Function: _get_fernet
@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = os.getenv("SETTINGS_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SETTINGS_ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
            "and add it to backend/.env before starting the service."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("SETTINGS_ENCRYPTION_KEY is not a valid Fernet key.") from exc


# Function: encrypt_value
def encrypt_value(plain: Optional[str]) -> Optional[str]:
    if not plain:
        return None
    return _get_fernet().encrypt(plain.encode()).decode()


# Function: decrypt_value
def decrypt_value(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt a stored settings value — SETTINGS_ENCRYPTION_KEY may be wrong or changed."
        ) from exc


# Function: _get_credential_envelope_fernet
@lru_cache(maxsize=1)
def _get_credential_envelope_fernet() -> Fernet:
    """Create a domain-separated Fernet key that never leaves the backend.

    An optional dedicated key can be supplied for key separation. JWT_SECRET is
    a safe fallback for existing deployments and is hashed with a purpose label,
    so the resulting Fernet key is not the JWT signing key itself.
    """
    secret = (
        os.getenv("SERVICENOW_CREDENTIAL_ENCRYPTION_KEY", "").strip()
        or os.getenv("JWT_SECRET", "").strip()
    )
    if not secret:
        raise RuntimeError("A backend credential-encryption secret is not configured.")
    derived = hashlib.sha256(f"novastra:servicenow-envelope:v1:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


# Function: encrypt_credential_envelope
def encrypt_credential_envelope(plain: Optional[str]) -> Optional[str]:
    if not plain:
        return None
    return _get_credential_envelope_fernet().encrypt(plain.encode()).decode()


# Function: decrypt_credential_envelope
def decrypt_credential_envelope(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _get_credential_envelope_fernet().decrypt(
            token.encode(),
            ttl=CREDENTIAL_ENVELOPE_TTL_SECONDS,
        ).decode()
    except InvalidToken as exc:
        raise RuntimeError("The encrypted ServiceNow credential is invalid or expired.") from exc
