from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from api.auth import (
    AuthPrincipal,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_bearer_principal,
    load_auth_settings,
    verify_password,
)
from api.auth_store import create_auth_store, hash_refresh_token
from api.schemas import (
    AuthUserResponse,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMessageRequest,
    ConversationMessagesResponse,
    ConversationNl2SqlResponse,
    ConversationResponse,
    ConversationUpdateRequest,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    Nl2SqlRequest,
    Nl2SqlResponse,
    RefreshRequest,
    TokenResponse,
)
from graph.memory_store import create_conversation_store
from graph.pipeline import run_nl2sql


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "nl2sql-api"}


@router.post("/api/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    store = create_auth_store()
    settings = load_auth_settings()
    user = await store.find_user_by_login(
        tenant_id=request.tenant_id,
        username=request.username,
    )
    if (
        user is None
        or user.get("disabled")
        or not verify_password(request.password, user["password_hash"])
    ):
        raise _unauthorized()

    principal = _principal_from_user(user, token_id=str(uuid4()))
    access_token = create_access_token(principal, settings)
    refresh_token_id, refresh_token, refresh_expires_at = create_refresh_token(
        principal, settings
    )
    await store.store_refresh_token(
        token_id=refresh_token_id,
        token_hash=hash_refresh_token(refresh_token),
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        expires_at=refresh_expires_at,
    )
    await store.record_login(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": settings.access_token_expire_minutes * 60,
        "user": _user_response_from_principal(principal),
    }


@router.post("/api/auth/refresh", response_model=TokenResponse)
async def refresh(request: RefreshRequest):
    settings = load_auth_settings()
    old_principal = decode_token(request.refresh_token, "refresh", settings)
    old_token_hash = hash_refresh_token(request.refresh_token)
    store = create_auth_store()
    active_token = await store.get_active_refresh_token(
        token_id=old_principal.token_id,
        token_hash=old_token_hash,
    )
    if active_token is None:
        raise _unauthorized()

    user = await store.get_user(
        tenant_id=old_principal.tenant_id,
        user_id=old_principal.user_id,
    )
    if (
        user is None
        or user.get("disabled")
        or int(user["token_version"]) != old_principal.token_version
    ):
        raise _unauthorized()

    principal = _principal_from_user(user, token_id=str(uuid4()))
    access_token = create_access_token(principal, settings)
    new_token_id, new_refresh_token, new_refresh_expires_at = create_refresh_token(
        principal, settings
    )
    new_token_hash = hash_refresh_token(new_refresh_token)

    try:
        await store.rotate_refresh_token(
            old_token_id=old_principal.token_id,
            old_token_hash=old_token_hash,
            new_token_id=new_token_id,
            new_token_hash=new_token_hash,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            expires_at=new_refresh_expires_at,
        )
    except ValueError as exc:
        raise _unauthorized() from exc

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "expires_in": settings.access_token_expire_minutes * 60,
        "user": _user_response_from_principal(principal),
    }


@router.get("/api/auth/me", response_model=AuthUserResponse)
async def me(principal: AuthPrincipal = Depends(get_bearer_principal)):
    return _user_response_from_principal(principal)


@router.post("/api/auth/logout", response_model=LogoutResponse)
async def logout(request: LogoutRequest):
    if not request.refresh_token:
        return LogoutResponse()

    settings = load_auth_settings()
    principal = decode_token(request.refresh_token, "refresh", settings)
    store = create_auth_store()
    await store.revoke_refresh_token(
        token_id=principal.token_id,
        token_hash=hash_refresh_token(request.refresh_token),
    )
    return LogoutResponse()


@router.post("/api/nl2sql", response_model=Nl2SqlResponse, response_model_exclude_none=True)
async def nl2sql(
    request: Nl2SqlRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, request.tenant_id)
    try:
        graph_kwargs = {
            "timeout_ms": request.timeout_ms,
            "max_limit": request.max_limit,
            "max_validation_attempts": request.max_validation_attempts,
        }
        if request.agent_mode != "dynamic":
            graph_kwargs["agent_mode"] = request.agent_mode
        if request.include_tool_trace:
            graph_kwargs["include_tool_trace"] = True
        result = await run_nl2sql(
            request.question,
            tenant_id=tenant_id,
            execute=request.execute,
            **graph_kwargs,
        )
        return _filter_tool_trace(result, include=request.include_tool_trace)
    except Exception as exc:
        return _internal_error_response(exc)


@router.post("/api/conversations", response_model=ConversationResponse)
async def create_conversation(
    request: ConversationCreateRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, request.tenant_id)
    user_id = _resolve_user(principal, request.user_id)
    try:
        store = create_conversation_store()
        return await store.create_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            title=request.title,
        )
    except Exception as exc:
        return _internal_error_response(exc)


@router.get("/api/conversations", response_model=ConversationListResponse)
async def list_conversations(
    tenant_id: str | None = Query(default=None, min_length=1),
    user_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    include_archived: bool = False,
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, tenant_id)
    user_id = _resolve_user(principal, user_id)
    try:
        store = create_conversation_store()
        items = await store.list_conversations(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            include_archived=include_archived,
        )
        return {"items": items}
    except Exception as exc:
        return _internal_error_response(exc)


@router.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    tenant_id: str | None = Query(default=None, min_length=1),
    user_id: str | None = Query(default=None, min_length=1),
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, tenant_id)
    user_id = _resolve_user(principal, user_id)
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
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
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, request.tenant_id)
    user_id = _resolve_user(principal, request.user_id)
    try:
        store = create_conversation_store()
        conversation = await store.update_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
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
    tenant_id: str | None = Query(default=None, min_length=1),
    user_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, tenant_id)
    user_id = _resolve_user(principal, user_id)
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = await store.list_messages(
            tenant_id=tenant_id,
            user_id=user_id,
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
    response_model_exclude_none=True,
)
async def send_conversation_message(
    conversation_id: str,
    request: ConversationMessageRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
):
    tenant_id = _resolve_tenant(principal, request.tenant_id)
    user_id = _resolve_user(principal, request.user_id)
    try:
        store = create_conversation_store()
        conversation = await store.get_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        graph_kwargs = {
            "timeout_ms": request.timeout_ms,
            "max_limit": request.max_limit,
            "max_validation_attempts": request.max_validation_attempts,
            "memory_history_limit": request.memory_history_limit,
            "memory_store": store,
        }
        if request.agent_mode != "dynamic":
            graph_kwargs["agent_mode"] = request.agent_mode
        if request.include_tool_trace:
            graph_kwargs["include_tool_trace"] = True
        result = await run_nl2sql(
            request.question,
            tenant_id=tenant_id,
            execute=request.execute,
            conversation_id=conversation_id.strip(),
            user_id=user_id,
            **graph_kwargs,
        )
        return _filter_tool_trace(result, include=request.include_tool_trace)
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


def _filter_tool_trace(result: dict, *, include: bool) -> dict:
    output = dict(result)
    if not include:
        output.pop("tool_trace", None)
    return output


def _resolve_tenant(principal: AuthPrincipal, requested_tenant_id: str | None) -> str:
    requested = requested_tenant_id.strip() if requested_tenant_id is not None else None
    if requested is not None and requested != principal.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant does not match token")
    return principal.tenant_id


def _resolve_user(principal: AuthPrincipal, requested_user_id: str | None) -> str:
    requested = requested_user_id.strip() if requested_user_id is not None else None
    if requested is not None and requested != principal.user_id:
        raise HTTPException(status_code=403, detail="User does not match token")
    return principal.user_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_user(user: dict, token_id: str) -> AuthPrincipal:
    return AuthPrincipal(
        tenant_id=user["tenant_id"],
        user_id=user["user_id"],
        username=user["username"],
        roles=list(user["roles"]),
        token_version=int(user["token_version"]),
        token_id=token_id,
    )


def _user_response_from_principal(principal: AuthPrincipal) -> AuthUserResponse:
    return AuthUserResponse(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        username=principal.username,
        roles=principal.roles,
    )
