"""Provider-neutral model adapter and environment-driven provider factory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI


ModelTransport = Literal["openai-compatible", "google", "anthropic"]


@runtime_checkable
class LLMProtocol(Protocol):
    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        ...


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    """Resolved model configuration without provider-specific client objects."""

    provider: str
    model_name: str
    transport: ModelTransport
    api_key: str = field(repr=False)
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class _ProviderSpec:
    transport: ModelTransport
    api_key_names: tuple[str, ...]
    base_url_names: tuple[str, ...] = ()
    default_base_url: str | None = None


_PROVIDER_SPECS: dict[str, _ProviderSpec] = {
    "openai": _ProviderSpec(
        transport="openai-compatible",
        api_key_names=("OPENAI_API_KEY", "LLM_API_KEY"),
        base_url_names=("OPENAI_BASE_URL", "LLM_BASE_URL"),
    ),
    "qwen": _ProviderSpec(
        transport="openai-compatible",
        api_key_names=("DASHSCOPE_API_KEY", "QWEN_API_KEY", "LLM_API_KEY"),
        base_url_names=("QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "LLM_BASE_URL"),
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "google": _ProviderSpec(
        transport="google",
        api_key_names=("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"),
        base_url_names=("GEMINI_BASE_URL", "LLM_BASE_URL"),
    ),
    "anthropic": _ProviderSpec(
        transport="anthropic",
        api_key_names=("ANTHROPIC_API_KEY", "LLM_API_KEY"),
        base_url_names=("ANTHROPIC_BASE_URL", "LLM_BASE_URL"),
    ),
    "glm": _ProviderSpec(
        transport="openai-compatible",
        api_key_names=(
            "ZAI_API_KEY",
            "GLM_API_KEY",
            "ZHIPUAI_API_KEY",
            "LLM_API_KEY",
        ),
        base_url_names=("GLM_BASE_URL", "ZAI_BASE_URL", "LLM_BASE_URL"),
        default_base_url="https://open.bigmodel.cn/api/paas/v4/",
    ),
    "deepseek": _ProviderSpec(
        transport="openai-compatible",
        api_key_names=("DEEPSEEK_API_KEY", "LLM_API_KEY"),
        base_url_names=("DEEPSEEK_BASE_URL", "LLM_BASE_URL"),
        default_base_url="https://api.deepseek.com",
    ),
    "openai-compatible": _ProviderSpec(
        transport="openai-compatible",
        api_key_names=("LLM_API_KEY",),
        base_url_names=("LLM_BASE_URL",),
    ),
}

_PROVIDER_ALIASES = {
    "openai": "openai",
    "gpt": "openai",
    "qwen": "qwen",
    "dashscope": "qwen",
    "google": "google",
    "gemini": "google",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "glm": "glm",
    "zhipu": "glm",
    "bigmodel": "glm",
    "deepseek": "deepseek",
    "openai-compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "custom": "openai-compatible",
}

SUPPORTED_LLM_PROVIDERS = tuple(_PROVIDER_SPECS)


class LangChainLLM:
    """Small compatibility adapter around a LangChain chat model."""

    def __init__(
        self,
        model,
        *,
        max_output_tokens_parameter: str | None = None,
    ) -> None:
        self.model = model
        self.max_output_tokens_parameter = max_output_tokens_parameter

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        invoke_kwargs = {}
        if self.max_output_tokens_parameter:
            invoke_kwargs[self.max_output_tokens_parameter] = max_output_tokens
        response = await self.model.ainvoke(messages, **invoke_kwargs)
        return _extract_text_content(getattr(response, "content", response))


def _extract_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content).strip()

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        else:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _first_configured(
    environment: Mapping[str, str],
    names: tuple[str, ...],
) -> str | None:
    for name in names:
        value = environment.get(name, "").strip()
        if value:
            return value
    return None


def _infer_provider(environment: Mapping[str, str]) -> str:
    configured = {
        provider
        for provider, spec in _PROVIDER_SPECS.items()
        if provider != "openai-compatible"
        and _first_configured(
            environment,
            tuple(name for name in spec.api_key_names if name != "LLM_API_KEY"),
        )
    }
    if len(configured) == 1:
        return configured.pop()
    if configured:
        names = ", ".join(sorted(configured))
        raise ValueError(
            "Missing LLM_PROVIDER; multiple provider credentials are configured: "
            f"{names}."
        )
    raise ValueError(
        "Missing LLM_PROVIDER. Supported values: "
        + ", ".join(SUPPORTED_LLM_PROVIDERS)
        + "."
    )


def resolve_llm_config(
    environment: Mapping[str, str] | None = None,
) -> ModelProviderConfig:
    """Resolve and validate one provider configuration from an environment."""

    env = os.environ if environment is None else environment
    raw_provider = env.get("LLM_PROVIDER", "").strip().lower()
    provider = _PROVIDER_ALIASES.get(raw_provider) if raw_provider else _infer_provider(env)
    if provider is None:
        raise ValueError(
            f"Unsupported LLM_PROVIDER: {raw_provider}. Supported values: "
            + ", ".join(SUPPORTED_LLM_PROVIDERS)
            + "."
        )

    model_name = env.get("DEFAULT_MODEL_NAME", "").strip()
    if not model_name:
        raise ValueError(
            f"Missing DEFAULT_MODEL_NAME for LLM_PROVIDER={provider}; "
            "configure an explicit model version."
        )

    spec = _PROVIDER_SPECS[provider]
    api_key = _first_configured(env, spec.api_key_names)
    if api_key is None:
        raise ValueError(
            f"Missing API key for LLM_PROVIDER={provider}; configure one of: "
            + ", ".join(spec.api_key_names)
            + "."
        )

    base_url = _first_configured(env, spec.base_url_names) or spec.default_base_url
    if provider == "openai-compatible" and not base_url:
        raise ValueError(
            "Missing LLM_BASE_URL for LLM_PROVIDER=openai-compatible."
        )

    return ModelProviderConfig(
        provider=provider,
        model_name=model_name,
        transport=spec.transport,
        api_key=api_key,
        base_url=base_url,
    )


def _create_anthropic_model(config: ModelProviderConfig):
    try:
        from langchain_anthropic import ChatAnthropic
    except ModuleNotFoundError as exc:  # pragma: no cover - installation failure
        raise RuntimeError(
            "Claude support requires the langchain-anthropic dependency."
        ) from exc

    kwargs = {
        "model": config.model_name,
        "api_key": config.api_key,
        "temperature": 0,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return ChatAnthropic(**kwargs)


def create_llm_from_config(config: ModelProviderConfig) -> LLMProtocol:
    """Create a provider client from an already validated configuration."""

    kwargs = {
        "model": config.model_name,
        "api_key": config.api_key,
        "temperature": 0,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url

    if config.transport == "openai-compatible":
        return LangChainLLM(
            ChatOpenAI(**kwargs),
            max_output_tokens_parameter="max_tokens",
        )
    if config.transport == "google":
        return LangChainLLM(
            ChatGoogleGenerativeAI(**kwargs),
            max_output_tokens_parameter="max_output_tokens",
        )
    if config.transport == "anthropic":
        return LangChainLLM(
            _create_anthropic_model(config),
            max_output_tokens_parameter="max_tokens",
        )
    raise AssertionError(f"Unsupported model transport: {config.transport}")


def create_llm(
    environment: Mapping[str, str] | None = None,
) -> LLMProtocol:
    """Create the configured chat model behind the Runtime protocol."""

    return create_llm_from_config(resolve_llm_config(environment))


__all__ = [
    "LLMProtocol",
    "LangChainLLM",
    "ModelProviderConfig",
    "SUPPORTED_LLM_PROVIDERS",
    "create_llm",
    "create_llm_from_config",
    "resolve_llm_config",
]
