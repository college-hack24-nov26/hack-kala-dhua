"""Error taxonomy and handlers.

Errors are returned as RFC 9457 problem details, so a client can branch on
`type` rather than parsing prose. Unexpected exceptions never leak a stack
trace to the caller; they are logged with the request id instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse

from app.core.logging import get_logger, request_id_ctx

log = get_logger(__name__)

PROBLEM_BASE = "https://indradhanu.app/problems"


class AppError(Exception):
    """Base for every error this application raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    problem: str = "internal-error"
    title: str = "Something went wrong"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    problem = "not-found"
    title = "Resource not found"


class BadRequest(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    problem = "bad-request"
    title = "That request cannot be processed as sent"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    problem = "conflict"
    title = "Conflicting state"


class Unauthorised(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    problem = "unauthorised"
    title = "Authentication required"


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    problem = "forbidden"
    title = "Not permitted"


class OutsideDelegation(Forbidden):
    """Raised when an officer acts beyond the clause that authorises them.

    Distinct from a plain 403 because the UI shows the clause that blocked it.
    """

    problem = "outside-delegation"
    title = "Action exceeds delegated authority"


class UpstreamUnavailable(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    problem = "upstream-unavailable"
    title = "An upstream data source did not answer"


def _problem(
    status_code: int, problem: str, title: str, detail: str, **extra: Any
) -> ORJSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}/{problem}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "request_id": request_id_ctx.get(),
    }
    body.update(extra)
    return ORJSONResponse(
        status_code=status_code, content=body, media_type="application/problem+json"
    )


def problem_response(exc: Exception) -> ORJSONResponse:
    """Turn any exception into a problem document.

    Used by the middleware error boundary. Deliberate application errors keep
    their own status and message; anything else becomes a 500 that says the
    failure was logged against this request id, rather than leaking an internal
    message to the browser.
    """
    if isinstance(exc, AppError):
        return _problem(exc.status_code, exc.problem, exc.title, exc.detail, **exc.extra)
    return _problem(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal-error",
        "Something went wrong",
        "The failure has been logged against this request id.",
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> ORJSONResponse:
        return _problem(exc.status_code, exc.problem, exc.title, exc.detail, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        return _problem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid-request",
            "Request could not be validated",
            "One or more fields were rejected.",
            errors=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> ORJSONResponse:
        log.exception("unhandled_error", path=request.url.path, error=str(exc))
        return _problem(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal-error",
            "Something went wrong",
            "The failure has been logged against this request id.",
        )
