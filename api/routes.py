from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.schemas import Nl2SqlRequest, Nl2SqlResponse
from graph.pipeline import run_nl2sql


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "nl2sql-api"}


@router.post("/api/nl2sql", response_model=Nl2SqlResponse)
async def nl2sql(request: Nl2SqlRequest):
    try:
        return await run_nl2sql(
            request.question,
            tenant_id=request.tenant_id,
            execute=request.execute,
            timeout_ms=request.timeout_ms,
            max_limit=request.max_limit,
            max_validation_attempts=request.max_validation_attempts,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "Internal server error",
                "detail": str(exc),
            },
        )
