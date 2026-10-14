from __future__ import annotations

import hmac
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from title_mcp.domain.models import AuditActor


class PrincipalKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class ReviewPermission(StrEnum):
    REVIEW_WORKFLOW = "workflow:review"


class AuthenticatedPrincipal(BaseModel):
    """Identity supplied by a trusted host after authenticating its human channel.

    This object is never accepted as an MCP tool argument. Constructing an object
    from client-supplied identity claims is not authentication.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    subject: str = Field(min_length=1)
    kind: PrincipalKind
    permissions: frozenset[ReviewPermission] = Field(default_factory=frozenset)


def require_human_reviewer(principal: AuthenticatedPrincipal | None) -> AuditActor:
    if (
        principal is None
        or principal.kind is not PrincipalKind.HUMAN
        or ReviewPermission.REVIEW_WORKFLOW not in principal.permissions
    ):
        raise PermissionError("An authenticated human with review permission is required.")
    return AuditActor(
        actor_id=principal.subject,
        actor_type="user",
        display_name=principal.subject,
    )


class InstanceBearerAuth:
    """Protect every route of a single-tenant HTTP instance.

    One instance owns one state repository and connector-credential set. This is
    a private bearer-credential mode, not an OAuth authorization server. The
    credential grants agent access and never authorizes a human review.
    """

    def __init__(self, app: ASGIApp, credential: SecretStr | None) -> None:
        self.app = app
        self._credential = (
            credential.get_secret_value().encode("utf-8") if credential is not None else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = [
            value for key, value in scope.get("headers", []) if key.lower() == b"authorization"
        ]
        supplied = headers[0].split() if len(headers) == 1 else []
        valid = (
            self._credential is not None
            and len(supplied) == 2
            and supplied[0].lower() == b"bearer"
            and hmac.compare_digest(supplied[1], self._credential)
        )
        if not valid:
            response = JSONResponse(
                {"error": "Authentication required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
