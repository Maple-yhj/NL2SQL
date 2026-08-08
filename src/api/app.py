"""Inert FastAPI composition boundary for the Data Agent product."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from api.routes import router


RuntimeFactory = Callable[[], Awaitable[Any]]


class _DefaultRuntimeComposition:
    def __init__(
        self,
        *,
        conversation_composition: Any,
        analysis_composition: Any,
        data_source_service: Any,
        owns_data_source_service: bool,
    ) -> None:
        self.runtime = conversation_composition.runtime
        self.analysis_runtime = analysis_composition.runtime
        self.dependencies = conversation_composition.dependencies
        self.data_source_service = data_source_service
        self._conversation_composition = conversation_composition
        self._analysis_composition = analysis_composition
        self._owns_data_source_service = owns_data_source_service
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._analysis_composition.close()
        if self._owns_data_source_service:
            await self.data_source_service.close()
        await self._conversation_composition.close()


async def _default_runtime_factory(
    data_source_service: Any | None = None,
) -> Any:
    # Keep module import inert: model creation and local control-plane state begin
    # only after FastAPI enters its lifespan.
    from api.datasource_service import DataSourceService
    from data_agent.analysis_agent.composition import build_analysis_agent_runtime
    from data_agent.memory import MemoryBudget, MemoryQuery, MemoryScope
    from data_agent.runtime.models import AgentRequest, PrincipalContext
    from data_agent.runtime.composition_root import build_upload_runtime

    conversation = await build_upload_runtime()
    model_client = conversation.dependencies.model_client
    resolved_data_sources = data_source_service or DataSourceService(
        relationship_model_client=model_client
    )
    conversation_repository = conversation.runtime.repository
    memory = conversation.dependencies.memory

    async def load_agent_context(request: AgentRequest, principal: PrincipalContext):
        history = await conversation_repository.load_context_summary(
            request=request,
            principal=principal,
        )
        if request.conversation_id is None:
            scopes = (MemoryScope.USER,)
        else:
            scopes = (MemoryScope.CONVERSATION, MemoryScope.USER)
        approved = await memory.recall(
            MemoryQuery(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=request.domain_id,
                conversation_id=request.conversation_id,
                scopes=scopes,
                query=request.question,
            ),
            MemoryBudget(max_records=12, max_tokens=1024, max_characters=4096),
        )
        memory_lines = tuple(
            record.content.model_dump_json(exclude_none=True)
            for record in approved.records
        )
        sections = []
        if history:
            sections.append("Prior conversation:\n" + history)
        if memory_lines:
            sections.append("Approved memory:\n" + "\n".join(memory_lines))
        rendered = "\n\n".join(sections)
        return rendered[-8192:] if rendered else None

    async def persist_agent_turn(state, response):
        request = AgentRequest.model_validate(state["request"])
        if request.conversation_id is None:
            return
        authority = state["authority"]
        await conversation_repository.record_conversation_turn(
            run_id=str(state["run_id"]),
            request=request,
            principal=PrincipalContext(
                tenant_id=str(authority.tenant_id),
                user_id=str(authority.user_id),
            ),
            response=response,
        )

    try:
        analysis = await build_analysis_agent_runtime(
            data_sources=resolved_data_sources,
            model_client=model_client,
            state_root=resolved_data_sources.state_root,
            conversation_summary_loader=load_agent_context,
            persist_turn=persist_agent_turn,
        )
    except BaseException:
        await resolved_data_sources.close()
        await conversation.close()
        raise
    return _DefaultRuntimeComposition(
        conversation_composition=conversation,
        analysis_composition=analysis,
        data_source_service=resolved_data_sources,
        owns_data_source_service=True,
    )


def create_app(
    runtime_factory: RuntimeFactory | None = None,
    *,
    data_source_service: Any | None = None,
) -> FastAPI:
    factory = runtime_factory or (
        lambda: _default_runtime_factory(data_source_service)
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from api.datasource_service import DataSourceService
        from api.run_streams import RunCoordinator, RunEventStore

        composition = await factory()
        application.state.runtime_composition = composition
        application.state.runtime = composition.runtime
        application.state.analysis_runtime = getattr(
            composition,
            "analysis_runtime",
            composition.runtime,
        )
        application.state.memory_manager = getattr(
            getattr(composition, "dependencies", None),
            "memory",
            None,
        )
        model_client = getattr(
            getattr(composition, "dependencies", None),
            "model_client",
            None,
        )
        resolved_data_sources = (
            data_source_service
            or getattr(composition, "data_source_service", None)
            or DataSourceService(relationship_model_client=model_client)
        )
        application.state.data_source_service = resolved_data_sources
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
            if getattr(composition, "data_source_service", None) is not resolved_data_sources:
                await resolved_data_sources.close()
            await composition.close()

    application = FastAPI(title="Data Agent API", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app"]
