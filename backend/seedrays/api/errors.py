"""Unified API error format: {"error": {"code", "message"}} (ADR-0011)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from seedrays.orchestrator.operations import OperationError


class ApiError(Exception):
	"""An HTTP-level error carrying the unified body."""

	def __init__(self, status: int, code: str, message: str) -> None:
		super().__init__(message)
		self.status = status
		self.code = code
		self.message = message


# Какой HTTP-статус несёт каждый машинный код бизнес-ошибки.
_OPERATION_STATUS = {
	"unknown_app_user": 404,
	"network_not_configured": 400,
	"wallet_missing": 500,
	"invalid_status": 400,
	"invalid_limit": 400,
	"invalid_username": 400,
	"invalid_email": 400,
	"weak_password": 400,
	"username_taken": 409,
	"email_taken": 409,
	"invalid_credentials": 401,
	"email_not_confirmed": 403,
	"mail_failed": 502,
	"invalid_family": 400,
	"invalid_xpub": 400,
	"invalid_words": 400,
	"no_families": 400,
	"invalid_name": 400,
	"unknown_application": 404,
}


def _body(code: str, message: str) -> dict:
	return {"error": {"code": code, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
	"""Attach the unified error format to a FastAPI application."""

	@app.exception_handler(ApiError)
	async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
		return JSONResponse(status_code=exc.status, content=_body(exc.code, exc.message))

	@app.exception_handler(OperationError)
	async def _operation_error(_request: Request, exc: OperationError) -> JSONResponse:
		status = _OPERATION_STATUS.get(exc.code, 400)
		return JSONResponse(status_code=status, content=_body(exc.code, exc.message))

	@app.exception_handler(RequestValidationError)
	async def _validation_error(
		_request: Request, exc: RequestValidationError
	) -> JSONResponse:
		return JSONResponse(status_code=400, content=_body("validation", str(exc)))
