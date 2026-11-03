"""Authentication.

Supabase issues the JWT; this service verifies it and resolves the caller's
role from `profiles`. The role is what the API authorises against, and it is the
same role the database policies check, so a bug in the API layer cannot grant
access the database would refuse.

Verification handles both shapes Supabase issues. A token signed with an
asymmetric key (ES256/RS256, the current default) is verified against the
project's published JWKS; a legacy HS256 token is verified against the shared
JWT secret. Which path is taken is decided per token from its header, and the
algorithm is always chosen from our own allow-list rather than from the token,
because trusting the token's `alg` field is how algorithm-confusion forgeries
work. See `app/core/jwks.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import jwt
import hmac
import os

from fastapi import Depends, Header, Request

from app.core import jwks
from app.core.config import settings
from app.core.errors import Forbidden, Unauthorised
from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)


class Role(StrEnum):
    CITIZEN = "citizen"
    FIELD_OPERATOR = "field_operator"
    WARD_OFFICER = "ward_officer"
    COMMISSIONER = "commissioner"
    ADMIN = "admin"


STAFF = {Role.WARD_OFFICER, Role.COMMISSIONER, Role.ADMIN}
ESCALATION = {Role.COMMISSIONER, Role.ADMIN}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str | None
    role: Role
    ward_id: str | None = None
    operator: str | None = None
    full_name: str = ""

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF

    @property
    def can_escalate(self) -> bool:
        return self.role in ESCALATION


ANONYMOUS = Principal(user_id=None, role=Role.CITIZEN)


def _development_principal() -> Principal | None:
    """The unauthenticated caller, when DEV_AUTH_ROLE is set in development.

    Gated twice: the setting must be non-empty, and the environment must be
    development. `Settings` refuses to load at all if the second is false, so by
    the time this runs the check here is belt and braces rather than the only
    thing standing between an open API and the internet.
    """
    role = settings.dev_auth_role
    if not role or settings.indradhanu_env != "development":
        return None
    return Principal(
        user_id=None,
        role=Role(role),
        ward_id=None,
        operator=None,
        full_name=f"Development {role.replace('_', ' ')}",
    )


async def _decode(token: str) -> dict:
    """Verify and decode, by whichever method this token was signed with."""
    header = jwks.unverified_header(token)
    alg = header.get("alg")

    common = {
        "audience": "authenticated",
        "options": {"verify_exp": True, "require": ["exp", "sub"]},
    }

    try:
        if alg in jwks.ASYMMETRIC_ALGORITHMS:
            key = await jwks.signing_key_for(token)
            return jwt.decode(
                token, key.key, algorithms=list(jwks.ASYMMETRIC_ALGORITHMS), **common
            )

        if alg in jwks.SYMMETRIC_ALGORITHMS:
            if not settings.supabase_jwt_key:
                raise Unauthorised(
                    "This token is signed with the legacy shared secret, but "
                    "SUPABASE_JWT_KEY is not configured."
                )
            return jwt.decode(
                token,
                settings.supabase_jwt_key,
                algorithms=list(jwks.SYMMETRIC_ALGORITHMS),
                **common,
            )

        raise Unauthorised(f"Unsupported token algorithm {alg!r}.")

    except jwt.ExpiredSignatureError as exc:
        raise Unauthorised("Your session has expired. Sign in again.") from exc
    except jwt.InvalidAudienceError as exc:
        raise Unauthorised("That token was not issued for this application.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorised("That token could not be verified.") from exc
    except jwks.KeyUnavailable as exc:
        # Not the caller's fault, and not a reason to let them in either.
        log.error("jwks_unavailable", error=str(exc))
        raise Unauthorised(
            "The signing keys for this project could not be reached, so the "
            "token cannot be verified right now."
        ) from exc


async def _principal_from_token(token: str) -> Principal:
    claims = await _decode(token)
    user_id = claims.get("sub")
    if not user_id:
        raise Unauthorised("Token carries no subject.")

    row = await db.fetchrow(
        "select role, ward_id, operator, full_name from profiles where id = $1",
        user_id,
    )
    if row is None:
        # Signed in, but the profile trigger has not caught up yet.
        return Principal(user_id=user_id, role=Role.CITIZEN)
    return Principal(
        user_id=user_id,
        role=Role(row["role"]),
        ward_id=row["ward_id"],
        operator=row["operator"],
        full_name=row["full_name"] or "",
    )


#: Read-only access for checking a demo without a login (`X-Demo-Read-Key`).
#: Off unless DEMO_READ_KEY is set (>= 24 characters). Only GET/HEAD requests,
#: as a ward officer with no user id, so every write, approval, dispatch and
#: config change still needs a real staff login.
READ_ONLY_METHODS = {"GET", "HEAD"}
DEMO_READER = Principal(user_id=None, role=Role.WARD_OFFICER, full_name="Read-only demo reviewer")


def _demo_reader(request: Request | None, key: str | None) -> Principal | None:
    expected = os.environ.get("DEMO_READ_KEY", "")
    if not key or len(expected) < 24 or request is None or request.method not in READ_ONLY_METHODS:
        return None
    if not hmac.compare_digest(key.encode(), expected.encode()):
        return None
    log.info("demo_read_key_used", path=request.url.path)
    return DEMO_READER


async def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_demo_read_key: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolves the caller. Anonymous is a valid outcome: a resident can read
    the risk map and file a report without an account."""
    if not authorization and x_demo_read_key:
        reader = _demo_reader(request, x_demo_read_key)
        if reader is not None:
            return reader
    if not authorization:
        dev = _development_principal()
        if dev is not None:
            log.warning(
                "dev_auth_principal_used",
                role=str(dev.role),
                note="unauthenticated request granted a role by DEV_AUTH_ROLE",
            )
            return dev
        return ANONYMOUS
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorised("Expected an `Authorization: Bearer <token>` header.")
    return await _principal_from_token(token)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def require_staff(principal: CurrentPrincipal) -> Principal:
    if not principal.is_staff:
        raise Forbidden("This view is restricted to municipal staff.")
    return principal


async def require_commissioner(principal: CurrentPrincipal) -> Principal:
    if not principal.can_escalate:
        raise Forbidden(
            "This action is reserved to the Municipal Commissioner or an officer "
            "authorised in writing for the event."
        )
    return principal


StaffPrincipal = Annotated[Principal, Depends(require_staff)]
CommissionerPrincipal = Annotated[Principal, Depends(require_commissioner)]
