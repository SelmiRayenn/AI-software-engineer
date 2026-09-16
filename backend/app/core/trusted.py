from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.core.config import settings

TRUSTED_OPERATOR_HEADER = "X-Operator-Token"


def require_trusted_operator(
    operator_token: Annotated[str | None, Header(alias=TRUSTED_OPERATOR_HEADER)] = None,
) -> None:
    verify_trusted_operator_token(operator_token)


def verify_trusted_operator_token(operator_token: str | None) -> None:
    configured_token = settings.trusted_operator_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trusted operator endpoints are disabled; configure TRUSTED_OPERATOR_TOKEN.",
        )
    if operator_token is None or not secrets.compare_digest(
        operator_token.encode("utf-8"), configured_token.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Trusted operator access required."
        )
