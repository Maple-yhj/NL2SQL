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
    from data_agent.runtime.composition_root import build_olist_runtime

    return await build_olist_runtime()


def create_app(runtime_factory: RuntimeFactory | None = None) -> FastAPI:
    factory = runtime_factory or _default_runtime_factory

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        composition = await factory()
        application.state.runtime_composition = composition
        application.state.runtime = composition.runtime
        try:
            yield
        finally:
            await composition.close()

    application = FastAPI(title="Data Agent API", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app"]
