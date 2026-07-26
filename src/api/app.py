"""Inert FastAPI composition boundary for the Data Agent product."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from api.routes import router


RuntimeFactory = Callable[[], Awaitable[Any]]


async def _default_runtime_factory() -> Any:
    # Keep module import inert: bundle loading, model creation, and DB pools begin
    # only after FastAPI enters its lifespan.
    from data_agent.runtime.composition_root import build_runtime

    return await build_runtime()


def create_app(
    runtime_factory: RuntimeFactory | None = None,
    *,
    data_source_service: Any | None = None,
    data_source_query_service: Any | None = None,
) -> FastAPI:
    factory = runtime_factory or _default_runtime_factory

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from api.datasource_service import DataSourceService
        from api.dataset_query_service import DataSourceQueryService
        from api.run_streams import RunCoordinator, RunEventStore

        composition = await factory()
        application.state.runtime_composition = composition
        application.state.runtime = composition.runtime
        application.state.memory_manager = getattr(
            getattr(composition, "dependencies", None),
            "memory",
            None,
        )
        resolved_data_sources = (
            data_source_service or DataSourceService()
        )
        application.state.data_source_service = resolved_data_sources
        model_client = getattr(
            getattr(composition, "dependencies", None),
            "model_client",
            None,
        )
        application.state.data_source_query_service = (
            data_source_query_service
            or (
                DataSourceQueryService(resolved_data_sources, model_client)
                if model_client is not None
                else None
            )
        )
        application.state.run_coordinator = RunCoordinator(
            RunEventStore(
                resolved_data_sources.state_root
                / "control"
                / "run-events.sqlite3"
            )
        )
        try:
            yield
        finally:
            await resolved_data_sources.close()
            await composition.close()

    application = FastAPI(title="Data Agent API", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app"]
