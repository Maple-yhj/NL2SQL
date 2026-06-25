from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from api.schemas import (
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMessageRequest,
    ConversationMessagesResponse,
    ConversationNl2SqlResponse,
    ConversationResponse,
    ConversationUpdateRequest,
    Nl2SqlRequest,
    Nl2SqlResponse,
)
from graph.memory_store import create_conversation_store
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
        return _internal_error_response(exc)


@router.post("/api/conversations", response_model=ConversationResponse)
async def create_conversation(request: ConversationCreateRequest):
    try:
        store = create_conversation_store()
        return await store.create_conversation(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            title=request.title,
        )
    except Exception as exc:
        return _internal_error_response(exc)


@router.get("/api/conversations", response_model=ConversationListResponse)
async def list_conversations(
    tenant_id: str = Query(default="demo", min_length=1),
    user_id: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    include_archived: bool = False,
):
    try:
        store = create_conversation_store()
        items = await store.list_conversations(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            limit=limit,
            include_archived=include_archived,
        )
        return {"items": items}
    except Exception as exc:
        return _internal_error_response(exc)


@router.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    tenant_id: str = Query(default="demo", min_length=1),
    user_id: str = Query(min_length=1),
):
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation
    except HTTPException:
        raise
    except Exception as exc:
        return _internal_error_response(exc)


@router.patch("/api/conversations/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
):
    try:
        store = create_conversation_store()
        conversation = await store.update_conversation(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=conversation_id.strip(),
            title=request.title,
            archived=request.archived,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation
    except HTTPException:
        raise
    except Exception as exc:
        return _internal_error_response(exc)


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
)
async def list_conversation_messages(
    conversation_id: str,
    tenant_id: str = Query(default="demo", min_length=1),
    user_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = await store.list_messages(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            conversation_id=conversation_id.strip(),
            limit=limit,
        )
        return {"items": messages}
    except HTTPException:
        raise
    except Exception as exc:
        return _internal_error_response(exc)


@router.post(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationNl2SqlResponse,
)
async def send_conversation_message(
    conversation_id: str,
    request: ConversationMessageRequest,
):
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await run_nl2sql(
            request.question,
            tenant_id=request.tenant_id,
            execute=request.execute,
            conversation_id=conversation_id.strip(),
            user_id=request.user_id,
            timeout_ms=request.timeout_ms,
            max_limit=request.max_limit,
            max_validation_attempts=request.max_validation_attempts,
            memory_history_limit=request.memory_history_limit,
            memory_store=store,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _internal_error_response(exc)


def _internal_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "Internal server error",
            "detail": str(exc),
        },
    )
