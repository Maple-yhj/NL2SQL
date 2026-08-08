"""Lifecycle composition roots for the user-selected dataset product."""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from data_agent.model_client import ModelClient

from .upload_runtime import UploadDatasetRuntime, UploadRuntimeComposition


ModelClientFactory = Callable[[], ModelClient | Awaitable[ModelClient]]


class _ConfiguredModelClient:
    def __init__(self, client: Any, *, model_id: str, version: str) -> None:
        self._client = client
        self.model_id = model_id
        self.version = version

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        return await self._client.complete(
            prompt=prompt,
            system=system,
            max_output_tokens=max_output_tokens,
        )

    async def close(self) -> None:
        await _close_resource(self._client)


def _default_model_client_factory(environment: Mapping[str, str]) -> ModelClient:
    from data_agent.adapters.llm import create_llm_from_config, resolve_llm_config

    config = resolve_llm_config(environment)
    return _ConfiguredModelClient(
        create_llm_from_config(config),
        model_id=f"{config.provider}.planner",
        version=config.model_name,
    )


async def build_upload_runtime(
    *,
    state_root: str | Path | None = None,
    model_client_factory: ModelClientFactory | None = None,
    environment: Mapping[str, str] | None = None,
) -> UploadRuntimeComposition:
    """Build the conversation shell without activating a bundled datasource."""

    env = dict(os.environ if environment is None else environment)
    configured_root = state_root or env.get("DATA_AGENT_STATE_DIR")
    control_root = Path(configured_root or "var/data-agent").expanduser()
    factory = model_client_factory or (lambda: _default_model_client_factory(env))
    model_client = factory()
    if inspect.isawaitable(model_client):
        model_client = await model_client
    if not isinstance(model_client.model_id, str) or not isinstance(
        model_client.version, str
    ):
        await _close_resource(model_client)
        raise TypeError("model client must expose stable id and version pins")
    return UploadRuntimeComposition(
        runtime=UploadDatasetRuntime(
            control_root / "control" / "conversations.sqlite3"
        ),
        model_client=model_client,
    )


async def build_analysis_agent_runtime(
    *,
    data_sources: Any,
    state_root: str | Path | None = None,
    model_client_factory: ModelClientFactory | None = None,
    environment: Mapping[str, str] | None = None,
    checkpointer_factory: Any | None = None,
    budget_limits: Any | None = None,
    conversation_summary_loader: Any | None = None,
    persist_turn: Any | None = None,
    response_builder: Any | None = None,
    run_id_factory: Any | None = None,
) -> Any:
    """Build the native Agent composition for an owned datasource service."""

    from data_agent.analysis_agent.composition import (
        build_analysis_agent_runtime as build_native_analysis_runtime,
    )

    env = dict(os.environ if environment is None else environment)
    configured_root = state_root or env.get("DATA_AGENT_STATE_DIR") or getattr(
        data_sources, "state_root", None
    )
    root = Path(configured_root or "var/data-agent").expanduser()
    factory = model_client_factory or (lambda: _default_model_client_factory(env))
    model_client = factory()
    if inspect.isawaitable(model_client):
        model_client = await model_client
    if not isinstance(model_client.model_id, str) or not isinstance(
        model_client.version, str
    ):
        await _close_resource(model_client)
        raise TypeError("model client must expose stable id and version pins")
    kwargs: dict[str, Any] = {
        "data_sources": data_sources,
        "model_client": model_client,
        "state_root": root,
        "checkpointer_factory": checkpointer_factory,
        "budget_limits": budget_limits,
        "persist_turn": persist_turn,
        "response_builder": response_builder,
        "run_id_factory": run_id_factory,
        "resources": (model_client,),
    }
    if conversation_summary_loader is not None:
        kwargs["conversation_summary_loader"] = conversation_summary_loader
    try:
        return await build_native_analysis_runtime(**kwargs)
    except BaseException:
        await _close_resource(model_client)
        raise


async def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if close is None:
        return
    value = close()
    if inspect.isawaitable(value):
        await value


__all__ = ["build_analysis_agent_runtime", "build_upload_runtime"]
