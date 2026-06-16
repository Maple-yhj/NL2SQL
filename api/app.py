from __future__ import annotations

from fastapi import FastAPI

from api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="NL2SQL LangGraph API")
    app.include_router(router)
    return app


app = create_app()
