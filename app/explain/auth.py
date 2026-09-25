"""HTTP Basic Authentication for the ETP Explainer application.

Passwords in ``AUTH_PASSWORD`` may be either a bcrypt hash (``$2a$``/``$2b$``/``$2y$``)
or a plaintext string for local development. Uses the ``bcrypt`` package directly;
``passlib`` is avoided because 1.7.4 is incompatible with ``bcrypt>=4.1``.
"""

from __future__ import annotations

import secrets
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.explain.config import ExplainSettings, get_settings

# HTTP Basic auth security scheme
security = HTTPBasic()

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
_BCRYPT_MAX_BYTES = 72


def is_bcrypt_hash(value: str) -> bool:
    """Return True when ``value`` looks like a bcrypt hash."""
    return value.startswith(_BCRYPT_PREFIXES)


def verify_password(plain_password: str, stored: str) -> bool:
    """Verify a password against a stored bcrypt hash or plaintext value.

    Args:
        plain_password: The plain text password to verify
        stored: A bcrypt hash, or a plaintext password (development only)

    Returns:
        True if the password matches, False otherwise
    """
    if not stored:
        return False
    if not is_bcrypt_hash(stored):
        # Plaintext comparison (development only), constant-time.
        return secrets.compare_digest(plain_password.encode("utf-8"), stored.encode("utf-8"))
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES], stored.encode("utf-8"))
    except ValueError:
        # Malformed hash in config — never let this surface as a 500.
        return False


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost 12). Returns the ``$2b$`` hash string."""
    return bcrypt.hashpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_credentials(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    settings: Annotated[ExplainSettings, Depends(get_settings)],
) -> str:
    """Verify HTTP Basic Auth credentials.

    Args:
        credentials: The HTTP Basic credentials from the request
        settings: Application settings containing auth config

    Returns:
        The username if authentication succeeds

    Raises:
        HTTPException: 401 if authentication fails
    """
    # Check if auth is disabled
    if not settings.auth_enabled:
        return credentials.username

    # Verify username matches (using constant-time comparison)
    username_correct = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.auth_username.encode("utf-8"),
    )

    # Verify password
    password_correct = verify_password(credentials.password, settings.auth_password)

    # Both must be correct
    if not (username_correct and password_correct):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


# Type alias for auth dependency
CurrentUser = Annotated[str, Depends(verify_credentials)]
