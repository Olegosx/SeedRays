"""The Application API route group (ADR-0004, ADR-0011).

A thin adapter over the orchestrator operations: routes, input validation
and response shaping only. Amounts travel as strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.api.errors import ApiError, register_error_handlers
from seedrays.api.user_api import register_user_routes
from seedrays.mail.base import MailSender
from seedrays.orchestrator import operations as ops
from seedrays.storage.engine import create_sqlite_engine, registry_db_path


class AddressesRequest(BaseModel):
	"""Body of the create-addresses operation."""

	networks: list[str] | Literal["all"] = Field(
		description="Network codes, or 'all' for every configured network"
	)


@dataclass
class CallerContext:
	"""Per-request engines and the resolved application (internal)."""

	registry: AsyncEngine
	ctx: ops.AppContext


def create_app(
	data_dir: Path,
	frontend_dir: Path | None = None,
	mailer: MailSender | None = None,
) -> FastAPI:
	"""Build the FastAPI application: the Application and User API groups.

	Args:
		data_dir: The gateway data directory.
		frontend_dir: When given, the static frontend is served from here
			(ADR-0012: the backend process may serve the static files).
		mailer: Mail sender override for tests; by default the sender is
			built from the registry settings.

	Returns:
		The configured FastAPI application.
	"""
	app = FastAPI(title="SeedRays API", version="1")
	register_error_handlers(app)
	register_user_routes(app, data_dir, mailer=mailer)

	async def caller(
		x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
	) -> AsyncIterator[CallerContext]:
		"""Authenticate the application key and open the caller's engines."""
		if not x_api_key:
			raise ApiError(401, "unauthorized", "the X-API-Key header is required")
		registry = create_sqlite_engine(registry_db_path(data_dir))
		try:
			ctx = await ops.resolve_application(registry, data_dir, x_api_key)
			if ctx is None:
				raise ApiError(401, "unauthorized", "unknown API key")
			try:
				yield CallerContext(registry=registry, ctx=ctx)
			finally:
				await ctx.engine.dispose()
		finally:
			await registry.dispose()

	CallerDep = Depends(caller)

	@app.post("/v1/app/users/{external_id}/addresses")
	async def create_addresses(
		external_id: str, body: AddressesRequest, call: CallerContext = CallerDep
	) -> dict:
		"""Idempotently issue payment addresses for an application user."""
		return {"addresses": await ops.ensure_bindings(call.ctx, external_id, body.networks)}

	@app.get("/v1/app/users/{external_id}/addresses")
	async def get_addresses(
		external_id: str, call: CallerContext = CallerDep, network: str | None = None
	) -> dict:
		"""Read the user's existing addresses; creates nothing."""
		return {"addresses": await ops.list_addresses(call.ctx, external_id, network)}

	@app.get("/v1/app/users/{external_id}/balances")
	async def get_balances(
		external_id: str,
		call: CallerContext = CallerDep,
		network: str | None = None,
		asset: str | None = None,
	) -> dict:
		"""Total received and pending per network + asset."""
		return {
			"balances": await ops.get_balances(
				call.registry, call.ctx, external_id, network, asset
			)
		}

	@app.get("/v1/app/users/{external_id}/history")
	async def get_history(
		external_id: str,
		call: CallerContext = CallerDep,
		network: str | None = None,
		asset: str | None = None,
		status: str = "confirmed",
		limit: Annotated[int, Query(ge=0)] = ops.DEFAULT_PAGE_LIMIT,
	) -> dict:
		"""Incoming transaction history; limit 0 means everything."""
		return {
			"history": await ops.get_history(
				call.registry, call.ctx, external_id, network, asset, status, limit
			)
		}

	@app.get("/v1/app/users")
	async def get_users(
		call: CallerContext = CallerDep, limit: Annotated[int, Query(ge=0)] = ops.DEFAULT_PAGE_LIMIT
	) -> dict:
		"""The application's users; limit 0 means everything."""
		return {"users": await ops.list_app_users(call.ctx, limit)}

	if frontend_dir is not None:

		@app.get("/", include_in_schema=False)
		async def root() -> RedirectResponse:
			"""The cabinet entry point."""
			return RedirectResponse("/login.html")

		# Монтируется последним: маршруты API имеют приоритет над статикой.
		app.mount("/", StaticFiles(directory=frontend_dir), name="frontend")

	return app
