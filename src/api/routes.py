"""HTTP routes that adapt authentication to the public Runtime boundary."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
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
    ConversationDataSourceBindingResponse,
    ConversationListResponse,
    ConversationMessageRequest,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationUpdateRequest,
    DataSourceCatalogResponse,
    DataSourceListResponse,
    DataSourceResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    Nl2SqlRequest,
    PostgresDataSourceRequest,
    RefreshRequest,
    SemanticBindingCreateRequest,
    SemanticBindingListResponse,
    TokenResponse,
)
from api.datasource_service import DataSourceService
from api.dataset_query_service import DataSourceQueryService
from data_agent.datasources import (
    DataSourceDefinition,
    DataSourceRegistryError,
    DataSourceRegistryErrorCode,
    FileSnapshotError,
    FileSnapshotErrorCode,
    SemanticBindingRecord,
)
from data_agent.runtime import (
    AgentRequest,
    AgentResponse,
    DataAgentRuntime,
    PrincipalContext,
    ProductRuntime,
)
from data_agent.runtime.errors import AgentError, ErrorCode


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "data-agent-api"}


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
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response_from_principal(principal),
    )


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
    try:
        await store.rotate_refresh_token(
            old_token_id=old_principal.token_id,
            old_token_hash=old_token_hash,
            new_token_id=new_token_id,
            new_token_hash=hash_refresh_token(new_refresh_token),
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            expires_at=new_refresh_expires_at,
        )
    except ValueError as exc:
        raise _unauthorized() from exc

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response_from_principal(principal),
    )


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


def get_runtime(request: Request) -> ProductRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError("Data Agent runtime is unavailable outside app lifespan")
    return runtime


def get_data_source_service(request: Request) -> DataSourceService:
    service = getattr(request.app.state, "data_source_service", None)
    if service is None:
        raise RuntimeError("Datasource service is unavailable outside app lifespan")
    return service


def get_data_source_query_service(
    request: Request,
) -> DataSourceQueryService | None:
    return getattr(request.app.state, "data_source_query_service", None)


@router.get("/api/data-sources", response_model=DataSourceListResponse)
async def list_data_sources(
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    sources = await service.list_sources(tenant_id=principal.tenant_id)
    return DataSourceListResponse(
        items=[_data_source_response(source) for source in sources]
    )


@router.post(
    "/api/data-sources/postgres",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_postgres_data_source(
    request: PostgresDataSourceRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        source = await service.register_postgres(
            tenant_id=principal.tenant_id,
            source_id=request.source_id,
            name=request.name,
            credential_ref=request.credential_ref,
            options={
                "host": request.host,
                "port": request.port,
                "database": request.database,
                "ssl_mode": request.ssl_mode,
            },
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc
    return _data_source_response(source)


@router.post(
    "/api/data-sources/files",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file_data_source(
    files: Annotated[list[UploadFile], File()],
    name: Annotated[str, Form(min_length=1)],
    source_id: Annotated[str | None, Form()] = None,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        source = await service.import_file_source(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            name=name.strip(),
            uploads=files,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc
    return _data_source_response(source)


@router.post(
    "/api/data-sources/sqlite",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_sqlite_data_source(
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=1)],
    source_id: Annotated[str | None, Form()] = None,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        source = await service.import_sqlite_source(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            name=name.strip(),
            upload=file,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc
    return _data_source_response(source)


@router.get(
    "/api/data-sources/{source_id}/catalog",
    response_model=DataSourceCatalogResponse,
)
async def get_data_source_catalog(
    source_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        snapshot = await service.get_snapshot(
            tenant_id=principal.tenant_id,
            source_id=source_id,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc
    return DataSourceCatalogResponse(
        source_id=snapshot.source_id,
        version=snapshot.version,
        fingerprint=snapshot.fingerprint,
        catalog=snapshot.catalog,
    )


@router.get(
    "/api/data-sources/{source_id}/bindings",
    response_model=SemanticBindingListResponse,
)
async def list_data_source_bindings(
    source_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    items = await service.list_bindings(
        tenant_id=principal.tenant_id,
        source_id=source_id,
    )
    return SemanticBindingListResponse(items=list(items))


@router.post(
    "/api/data-sources/{source_id}/bindings",
    response_model=SemanticBindingRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_source_binding(
    source_id: str,
    request: SemanticBindingCreateRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        return await service.create_binding(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            binding_id=request.binding_id,
            domain_id=request.domain_id,
            mappings=request.mappings,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc


@router.post(
    "/api/data-sources/{source_id}/bindings/{binding_id}/activate",
    response_model=SemanticBindingRecord,
)
async def activate_data_source_binding(
    source_id: str,
    binding_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        return await service.activate_binding(
            tenant_id=principal.tenant_id,
            source_id=source_id,
            binding_id=binding_id,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc


@router.post("/api/nl2sql", response_model=AgentResponse)
async def nl2sql(
    request: Nl2SqlRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
    data_query: DataSourceQueryService | None = Depends(
        get_data_source_query_service
    ),
):
    runtime_principal = _runtime_principal(principal)
    try:
        if request.source_id is not None:
            if data_query is None:
                response = _unavailable_dataset_response(
                    request,
                    runtime_principal,
                )
            else:
                response = await data_query.run(request, runtime_principal)
        else:
            response = await _collect_terminal_response(
                runtime,
                request,
                runtime_principal,
            )
    except Exception:
        response = _safe_internal_response(request, runtime_principal)
    status_code = _status_for_response(response)
    if status_code != status.HTTP_200_OK:
        return JSONResponse(
            status_code=status_code,
            content=response.model_dump(mode="json"),
        )
    return response


@router.post("/api/conversations", response_model=ConversationResponse)
async def create_conversation(
    request: ConversationCreateRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
):
    try:
        return await runtime.create_conversation(
            principal=_runtime_principal(principal),
            domain_id=request.domain_id,
            title=request.title,
        )
    except Exception as exc:
        raise _safe_server_error() from exc


@router.get("/api/conversations", response_model=ConversationListResponse)
async def list_conversations(
    domain_id: str = Query(default="commerce", min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    include_archived: bool = False,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
):
    try:
        items = await runtime.list_conversations(
            principal=_runtime_principal(principal),
            domain_id=domain_id,
            limit=limit,
            include_archived=include_archived,
        )
        return ConversationListResponse(items=list(items))
    except Exception as exc:
        raise _safe_server_error() from exc


@router.get(
    "/api/conversations/{conversation_id}/data-source-binding",
    response_model=ConversationDataSourceBindingResponse,
)
async def get_conversation_data_source_binding(
    conversation_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    binding = await service.get_conversation_binding(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id.strip(),
    )
    return ConversationDataSourceBindingResponse(binding=binding)


@router.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def get_conversation(
    conversation_id: str,
    domain_id: str = Query(default="commerce", min_length=1),
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
):
    try:
        conversation = await runtime.get_conversation(
            principal=_runtime_principal(principal),
            domain_id=domain_id,
            conversation_id=conversation_id.strip(),
        )
    except Exception as exc:
        raise _safe_server_error() from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.patch(
    "/api/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def update_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
    domain_id: str = Query(default="commerce", min_length=1),
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
):
    try:
        conversation = await runtime.update_conversation(
            principal=_runtime_principal(principal),
            domain_id=domain_id,
            conversation_id=conversation_id.strip(),
            title=request.title,
            archived=request.archived,
        )
    except Exception as exc:
        raise _safe_server_error() from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
)
async def list_conversation_messages(
    conversation_id: str,
    domain_id: str = Query(default="commerce", min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
):
    runtime_principal = _runtime_principal(principal)
    try:
        conversation = await runtime.get_conversation(
            principal=runtime_principal,
            domain_id=domain_id,
            conversation_id=conversation_id.strip(),
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        items = await runtime.list_conversation_messages(
            principal=runtime_principal,
            domain_id=domain_id,
            conversation_id=conversation_id.strip(),
            limit=limit,
        )
        return ConversationMessagesResponse(items=list(items))
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_server_error() from exc


@router.post(
    "/api/conversations/{conversation_id}/messages",
    response_model=AgentResponse,
)
async def send_conversation_message(
    conversation_id: str,
    request: ConversationMessageRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
    data_query: DataSourceQueryService | None = Depends(
        get_data_source_query_service
    ),
):
    path_conversation_id = conversation_id.strip()
    if request.conversation_id not in (None, path_conversation_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="conversation_id must match the route",
        )
    runtime_principal = _runtime_principal(principal)
    try:
        payload = request.model_dump(mode="python")
        payload["conversation_id"] = path_conversation_id
        agent_request = AgentRequest.model_validate(payload)
        if agent_request.source_id is not None:
            conversation = await runtime.get_conversation(
                principal=runtime_principal,
                domain_id="commerce",
                conversation_id=path_conversation_id,
            )
            if conversation is None:
                raise HTTPException(
                    status_code=404,
                    detail="Conversation not found",
                )
            if data_query is None:
                response = _unavailable_dataset_response(
                    agent_request,
                    runtime_principal,
                )
            else:
                response = await data_query.run(
                    agent_request,
                    runtime_principal,
                )
        else:
            conversation = await runtime.get_conversation(
                principal=runtime_principal,
                domain_id=request.domain_id,
                conversation_id=path_conversation_id,
            )
            if conversation is None:
                raise HTTPException(
                    status_code=404,
                    detail="Conversation not found",
                )
            response = await _collect_terminal_response(
                runtime,
                agent_request,
                runtime_principal,
            )
    except HTTPException:
        raise
    except Exception:
        response = _safe_internal_response(request, runtime_principal)
    status_code = _status_for_response(response)
    if status_code != status.HTTP_200_OK:
        return JSONResponse(
            status_code=status_code,
            content=response.model_dump(mode="json"),
        )
    return response


def _unavailable_dataset_response(
    request: AgentRequest,
    principal: PrincipalContext,
) -> AgentResponse:
    return AgentResponse(
        ok=False,
        question=request.question,
        contextualized_question=request.question,
        conversation_id=request.conversation_id,
        tenant_id=principal.tenant_id,
        answer="用户数据源查询服务尚未配置模型。",
        error=AgentError(
            code=ErrorCode.CONFIG_INVALID,
            message="用户数据源查询服务尚未配置模型。",
        ),
    )


async def _collect_terminal_response(
    runtime: DataAgentRuntime,
    request: AgentRequest,
    principal: PrincipalContext,
) -> AgentResponse:
    terminal: AgentResponse | None = None
    async for event in runtime.run(request, principal):
        if event.response is None:
            continue
        if terminal is not None:
            raise RuntimeError("runtime emitted more than one terminal response")
        terminal = event.response
    if terminal is None:
        raise RuntimeError("runtime stream ended without a terminal response")
    return terminal


def _runtime_principal(principal: AuthPrincipal) -> PrincipalContext:
    return PrincipalContext(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        roles=tuple(principal.roles),
    )


def _safe_internal_response(
    request: AgentRequest,
    principal: PrincipalContext,
) -> AgentResponse:
    return AgentResponse(
        ok=False,
        question=request.question,
        conversation_id=request.conversation_id,
        tenant_id=principal.tenant_id,
        error=AgentError(
            code=ErrorCode.INTERNAL_ERROR,
            message="The governed run failed safely.",
            retryable=False,
        ),
    )


def _status_for_response(response: AgentResponse) -> int:
    if response.ok or response.error is None:
        return status.HTTP_200_OK
    return {
        ErrorCode.INVALID_REQUEST: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.ACCESS_DENIED: status.HTTP_403_FORBIDDEN,
        ErrorCode.BUNDLE_NOT_FOUND: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.DEADLINE_EXCEEDED: status.HTTP_504_GATEWAY_TIMEOUT,
        ErrorCode.CANCELLED: status.HTTP_409_CONFLICT,
        ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }.get(response.error.code, status.HTTP_422_UNPROCESSABLE_CONTENT)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _safe_server_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error",
    )


def _data_source_response(
    source: DataSourceDefinition,
) -> DataSourceResponse:
    return DataSourceResponse(
        source_id=source.source_id,
        name=source.name,
        kind=source.kind,
        status=source.status,
        active_snapshot_version=source.active_snapshot_version,
        options=source.options,
        created_at=source.created_at.isoformat(),
        updated_at=source.updated_at.isoformat(),
    )


def _data_source_error(
    error: DataSourceRegistryError | FileSnapshotError | ValueError,
) -> HTTPException:
    if isinstance(error, DataSourceRegistryError):
        status_code = {
            DataSourceRegistryErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
            DataSourceRegistryErrorCode.ALREADY_EXISTS: status.HTTP_409_CONFLICT,
            DataSourceRegistryErrorCode.VERSION_CONFLICT: status.HTTP_409_CONFLICT,
        }.get(error.code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    elif (
        isinstance(error, FileSnapshotError)
        and error.code == FileSnapshotErrorCode.SIZE_LIMIT_EXCEEDED
    ):
        status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(status_code=status_code, detail=str(error))


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


__all__ = ["get_runtime", "router"]
