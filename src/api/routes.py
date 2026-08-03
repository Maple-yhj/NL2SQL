"""HTTP routes that adapt authentication to the public Runtime boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from fastapi.responses import JSONResponse, StreamingResponse

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
    DataSourceDeleteResponse,
    DataSourceListResponse,
    DataSourceResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    MemoryProposalDecisionRequest,
    MemoryProposalDecisionResponse,
    MemoryProposalListResponse,
    Nl2SqlRequest,
    PostgresDataSourceRequest,
    RefreshRequest,
    RunCancelResponse,
    RunEventListResponse,
    SemanticBindingCreateRequest,
    SemanticBindingListResponse,
    TokenResponse,
)
from api.datasource_service import DataSourceService
from api.dataset_query_service import (
    DataSourceQueryService,
    DatasetConversationContext,
    DatasetPlanStatus,
    DatasetQueryPlan,
)
from api.run_streams import RunCoordinator
from data_agent.memory import (
    ApprovalContext,
    ApprovalDecision,
    MemoryApprovalError,
    MemoryConflictError,
    MemoryManager,
    MemoryStateError,
    ProposalStatus,
)
from data_agent.datasources import (
    DataSourceDefinition,
    DataSourceRegistryError,
    DataSourceRegistryErrorCode,
    FileSnapshotError,
    FileSnapshotErrorCode,
    SemanticBindingRecord,
)
from data_agent.runtime import (
    AgentEvent,
    AgentRequest,
    AgentResponse,
    DataAgentRuntime,
    PrincipalContext,
    ProductRuntime,
    USER_DATASET_DOMAIN_ID,
)
from data_agent.runtime.errors import AgentError, ErrorCode
from data_agent.runtime.events import (
    AgentEventType,
    RunFailedPayload,
    RunStartedPayload,
)


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


def get_run_coordinator(request: Request) -> RunCoordinator:
    coordinator = getattr(request.app.state, "run_coordinator", None)
    if coordinator is None:
        raise RuntimeError("Run coordinator is unavailable outside app lifespan")
    return coordinator


def get_memory_manager(request: Request) -> MemoryManager:
    manager = getattr(request.app.state, "memory_manager", None)
    if manager is None:
        raise RuntimeError("Memory manager is unavailable outside app lifespan")
    return manager


@router.get("/api/data-sources", response_model=DataSourceListResponse)
async def list_data_sources(
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    sources = await service.list_sources(tenant_id=principal.tenant_id)
    return DataSourceListResponse(
        items=[_data_source_response(source) for source in sources]
    )


@router.delete(
    "/api/data-sources/{source_id}",
    response_model=DataSourceDeleteResponse,
)
async def delete_data_source(
    source_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    service: DataSourceService = Depends(get_data_source_service),
):
    try:
        deleted = await service.delete_source(
            tenant_id=principal.tenant_id,
            source_id=source_id,
        )
    except (DataSourceRegistryError, FileSnapshotError, ValueError) as exc:
        raise _data_source_error(exc) from exc
    return DataSourceDeleteResponse(source_id=deleted.source_id)


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
            primary_relation=request.primary_relation,
            relationships=request.relationships,
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
            await _record_conversation_turn(
                runtime,
                request,
                runtime_principal,
                response,
            )
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


@router.post(
    "/api/nl2sql/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Typed runtime event stream",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}}
            },
        }
    },
)
async def stream_nl2sql(
    request: Nl2SqlRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
    data_query: DataSourceQueryService | None = Depends(
        get_data_source_query_service
    ),
    coordinator: RunCoordinator = Depends(get_run_coordinator),
):
    runtime_principal = _runtime_principal(principal)
    events = _request_event_stream(
        request,
        runtime_principal,
        runtime=runtime,
        data_query=data_query,
    )
    return _sse_response(coordinator.observe(runtime_principal, events))


@router.post(
    "/api/runs/{run_id}/cancel",
    response_model=RunCancelResponse,
)
async def cancel_run(
    run_id: str,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    coordinator: RunCoordinator = Depends(get_run_coordinator),
):
    resolved_run_id = run_id.strip()
    cancelled = await coordinator.cancel(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        run_id=resolved_run_id,
    )
    if not cancelled:
        raise HTTPException(status_code=404, detail="Active run not found")
    return RunCancelResponse(run_id=resolved_run_id, cancelled=True)


@router.get(
    "/api/runs/{run_id}/events",
    response_model=RunEventListResponse,
)
async def replay_run_events(
    run_id: str,
    after_sequence: int = Query(default=-1, ge=-1),
    limit: int = Query(default=200, ge=1, le=1000),
    principal: AuthPrincipal = Depends(get_bearer_principal),
    coordinator: RunCoordinator = Depends(get_run_coordinator),
):
    items = await coordinator.replay(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        run_id=run_id.strip(),
        after_sequence=after_sequence,
        limit=limit,
    )
    if not items and not await coordinator.contains(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        run_id=run_id.strip(),
    ):
        raise HTTPException(status_code=404, detail="Run events not found")
    return RunEventListResponse(items=list(items))


@router.get(
    "/api/memory/proposals",
    response_model=MemoryProposalListResponse,
)
async def list_memory_proposals(
    proposal_status: ProposalStatus = Query(
        default=ProposalStatus.PENDING_APPROVAL,
        alias="status",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AuthPrincipal = Depends(get_bearer_principal),
    memory: MemoryManager = Depends(get_memory_manager),
):
    items = await memory.list_proposals(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        roles=tuple(principal.roles),
        statuses=(proposal_status,),
        limit=limit,
    )
    return MemoryProposalListResponse(items=list(items))


@router.post(
    "/api/memory/proposals/{proposal_id}/decision",
    response_model=MemoryProposalDecisionResponse,
)
async def decide_memory_proposal(
    proposal_id: str,
    request: MemoryProposalDecisionRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    memory: MemoryManager = Depends(get_memory_manager),
):
    resolved_proposal_id = proposal_id.strip()
    if not resolved_proposal_id:
        raise HTTPException(status_code=422, detail="proposal_id must not be blank")
    approval = ApprovalContext(
        tenant_id=principal.tenant_id,
        approver_user_id=principal.user_id,
        roles=tuple(principal.roles),
        decision=request.decision,
        decided_at=datetime.now(UTC),
        reason=request.reason,
    )
    try:
        await memory.commit(resolved_proposal_id, approval)
    except MemoryApprovalError as exc:
        raise HTTPException(
            status_code=404,
            detail="Memory proposal not found",
        ) from exc
    except (MemoryConflictError, MemoryStateError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Memory proposal cannot accept this decision",
        ) from exc
    resulting_status = (
        ProposalStatus.COMMITTED
        if request.decision == ApprovalDecision.APPROVE
        else ProposalStatus.REJECTED
    )
    return MemoryProposalDecisionResponse(
        proposal_id=resolved_proposal_id,
        status=resulting_status,
    )


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
    domain_id: str = Query(default=USER_DATASET_DOMAIN_ID, min_length=1),
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
    domain_id: str = Query(default=USER_DATASET_DOMAIN_ID, min_length=1),
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
    domain_id: str = Query(default=USER_DATASET_DOMAIN_ID, min_length=1),
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
    domain_id: str = Query(default=USER_DATASET_DOMAIN_ID, min_length=1),
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
                domain_id=USER_DATASET_DOMAIN_ID,
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
                conversation_context = await _dataset_conversation_context(
                    runtime,
                    runtime_principal,
                    path_conversation_id,
                )
                response = await data_query.run(
                    agent_request,
                    runtime_principal,
                    conversation_context=conversation_context,
                )
            await _record_conversation_turn(
                runtime,
                agent_request,
                runtime_principal,
                response,
            )
        else:
            conversation = await runtime.get_conversation(
                principal=runtime_principal,
                domain_id=USER_DATASET_DOMAIN_ID,
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


@router.post(
    "/api/conversations/{conversation_id}/messages/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Typed runtime event stream",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}}
            },
        }
    },
)
async def stream_conversation_message(
    conversation_id: str,
    request: ConversationMessageRequest,
    principal: AuthPrincipal = Depends(get_bearer_principal),
    runtime: ProductRuntime = Depends(get_runtime),
    data_query: DataSourceQueryService | None = Depends(
        get_data_source_query_service
    ),
    coordinator: RunCoordinator = Depends(get_run_coordinator),
):
    path_conversation_id = conversation_id.strip()
    if request.conversation_id not in (None, path_conversation_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="conversation_id must match the route",
        )
    runtime_principal = _runtime_principal(principal)
    payload = request.model_dump(mode="python")
    payload["conversation_id"] = path_conversation_id
    agent_request = AgentRequest.model_validate(payload)
    lookup_domain = USER_DATASET_DOMAIN_ID
    conversation = await runtime.get_conversation(
        principal=runtime_principal,
        domain_id=lookup_domain,
        conversation_id=path_conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation_context = (
        await _dataset_conversation_context(
            runtime,
            runtime_principal,
            path_conversation_id,
        )
        if agent_request.source_id is not None
        else None
    )
    events = _request_event_stream(
        agent_request,
        runtime_principal,
        runtime=runtime,
        data_query=data_query,
        conversation_context=conversation_context,
    )
    return _sse_response(coordinator.observe(runtime_principal, events))


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


def _request_event_stream(
    request: AgentRequest,
    principal: PrincipalContext,
    *,
    runtime: ProductRuntime,
    data_query: DataSourceQueryService | None,
    conversation_context: DatasetConversationContext | None = None,
) -> AsyncIterator[AgentEvent]:
    if request.source_id is None:
        return runtime.run(request, principal)
    events = (
        data_query.stream(
            request,
            principal,
            conversation_context=conversation_context,
        )
        if data_query is not None
        else _single_response_events(
            request,
            _unavailable_dataset_response(request, principal),
        )
    )
    return _record_terminal_stream(
        runtime,
        request,
        principal,
        events,
    )


async def _dataset_conversation_context(
    runtime: ProductRuntime,
    principal: PrincipalContext,
    conversation_id: str,
) -> DatasetConversationContext | None:
    list_messages = getattr(runtime, "list_conversation_messages", None)
    if not callable(list_messages):
        return None
    messages = await list_messages(
        principal=principal,
        domain_id=USER_DATASET_DOMAIN_ID,
        conversation_id=conversation_id,
        limit=100,
    )
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "assistant":
            continue
        document = message.metadata.dataset_query_plan
        if document is None:
            continue
        try:
            prior_plan = DatasetQueryPlan.model_validate(document)
        except ValueError:
            continue
        if prior_plan.status != DatasetPlanStatus.READY:
            continue
        prior_question = message.metadata.contextualized_question
        if prior_question is None:
            prior_question = next(
                (
                    item.content
                    for item in reversed(messages[:index])
                    if item.role == "user"
                ),
                "",
            )
        if not prior_question.strip():
            continue
        return DatasetConversationContext(
            prior_question=prior_question,
            prior_plan=prior_plan,
        )
    return None


async def _record_terminal_stream(
    runtime: ProductRuntime,
    request: AgentRequest,
    principal: PrincipalContext,
    events: AsyncIterator[AgentEvent],
) -> AsyncIterator[AgentEvent]:
    async for event in events:
        if event.response is not None:
            await _record_conversation_turn(
                runtime,
                request,
                principal,
                event.response,
            )
        yield event


async def _record_conversation_turn(
    runtime: ProductRuntime,
    request: AgentRequest,
    principal: PrincipalContext,
    response: AgentResponse,
) -> None:
    if request.conversation_id is None:
        return
    recorder = getattr(runtime, "record_conversation_turn", None)
    if not callable(recorder):
        return
    await recorder(
        request=request,
        principal=principal,
        response=response,
    )


async def _single_response_events(
    request: AgentRequest,
    response: AgentResponse,
) -> AsyncIterator[AgentEvent]:
    run_id = "api-run-" + uuid4().hex
    yield AgentEvent(
        type=AgentEventType.RUN_STARTED,
        run_id=run_id,
        sequence=0,
        data=RunStartedPayload(
            mode=request.mode,
            enterprise_id=request.enterprise_id,
            domain_id=request.domain_id,
        ),
    )
    error_code = (
        response.error.code
        if response.error is not None
        else ErrorCode.INTERNAL_ERROR
    )
    yield AgentEvent(
        type=AgentEventType.RUN_FAILED,
        run_id=run_id,
        sequence=1,
        data=RunFailedPayload(error_code=error_code),
        response=response,
    )


def _sse_response(
    events: AsyncIterator[AgentEvent],
) -> StreamingResponse:
    async def encode() -> AsyncIterator[str]:
        async for event in events:
            yield (
                f"id: {event.sequence}\n"
                f"event: {event.type.value}\n"
                f"data: {event.model_dump_json()}\n\n"
            )

    return StreamingResponse(
        encode(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
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
