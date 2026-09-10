"""HTTP Basic Authentication for the ETP Explainer application."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from passlib.context import CryptContext

from app.explain.config import ExplainSettings, get_settings

# Password hashing context using bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# HTTP Basic auth security scheme
security = HTTPBasic()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash.

    Args:
        plain_password: The plain text password to verify
        hashed_password: The bcrypt hash to verify against

    Returns:
        True if the password matches, False otherwise
    """
    # If the hash doesn't look like a bcrypt hash, do a simple comparison
    # This allows using plain passwords in development
    if not hashed_password.startswith("$2b$"):
        return secrets.compare_digest(plain_password, hashed_password)

    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt.

    Args:
        password: The plain text password to hash

    Returns:
        The bcrypt hash
    """
    return pwd_context.hash(password)


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
