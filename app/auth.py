from __future__ import annotations

from fastapi import Cookie, Header, HTTPException, Request, status

from app.config import Settings


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def require_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
    transcript_token: str | None = Cookie(default=None),
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.normalized_api_token
    provided = (_extract_bearer_token(authorization) or transcript_token or "").strip()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth_not_configured",
                "message": "API_TOKEN must be configured before using this service.",
            },
        )

    if provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unauthorized",
                "message": "Provide a valid bearer token or web session token.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
