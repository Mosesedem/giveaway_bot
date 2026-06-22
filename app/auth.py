"""Optional HTTP Basic Auth for the dashboard (disabled when env vars unset)."""

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


def dashboard_auth_enabled() -> bool:
    return bool(os.getenv("DASHBOARD_USER") and os.getenv("DASHBOARD_PASSWORD"))


def require_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    if not dashboard_auth_enabled():
        return

    expected_user = os.getenv("DASHBOARD_USER", "")
    expected_password = os.getenv("DASHBOARD_PASSWORD", "")
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard login required",
            headers={"WWW-Authenticate": "Basic"},
        )

    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials",
            headers={"WWW-Authenticate": "Basic"},
        )